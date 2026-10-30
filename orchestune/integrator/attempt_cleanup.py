"""Close invalidated Layer-2 integration attempts and clean up their temp refs (#1440).

An attempt the runner saved as ``INVALIDATED`` is never reused, yet its PR stayed
open and its ``integration/temp-parent-issue-<N>-<id>`` branch stayed on the
remote. This cleans both up, one resumable step at a time:

1. a one-time notice on the old PR (reason, old/current base, old head),
2. closing the PR (idempotent; a merged PR stops all destructive cleanup),
3. a one-time announcement of the successor attempt, once exactly one exists,
4. deleting the remote branch with a lease on the head recorded in the trusted
   ``INVALIDATED`` state.

No progress is stored. Every step is derived again each cycle from what is
observable (notice marker comment, PR state, remote ref), so an interruption
anywhere resumes at the first step still missing. Writes happen under the #1370
semantic claim, which is re-proved before each of them.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from orchestune.forge import Forge, ForgeAuthError
from orchestune.infra.git_cli import (
    ConditionalBranchDeletionResult as Deletion,
)
from orchestune.infra.git_cli import (
    delete_remote_branch_if_matches,
    run_git,
)
from orchestune.integrator.attempt import (
    OpenAttempt,
    restore_attempt,
    trusted_state_authors,
)
from orchestune.integrator.pr import list_open_attempt_prs
from orchestune.integrator.semantic_claim import (
    ClaimStatus,
    PushStatus,
    SemanticClaim,
    acquire_semantic_claim,
    release_semantic_claim,
    renew_semantic_claim,
)
from orchestune.issue_notice import latest_notice_body, render_notice
from orchestune.models import PrRecord
from orchestune.pr_link_notice_trust import TrustContext
from orchestune.review.semantic_gate import (
    IN_FLIGHT_OPERATION_STATUSES,
    SemanticOperationStatus,
    SemanticReviewPhase,
    SemanticReviewStateV1,
)

_CLAIM_TTL_SECONDS = 300
UNSETTLED_OPERATION_STATUSES = IN_FLIGHT_OPERATION_STATUSES | {
    SemanticOperationStatus.INDETERMINATE
}
_SHA = re.compile(r"[0-9a-fA-F]{40}")

STEP_NOTIFY = "notify"
STEP_CLOSE = "close"
STEP_ANNOUNCE = "announce"
STEP_DELETE = "delete_ref"


@dataclass(frozen=True)
class AttemptCleanupReport:
    """Outcome for one invalidated attempt.

    ``cleaned``: nothing left to do. ``planned``: dry run. ``pending``: held or
    unconfirmed, retried next cycle. ``blocked``: unsafe to continue, kept for a
    human. ``failed``: a step errored, retried next cycle.
    """

    status: str
    pr_number: int
    attempt_id: str
    branch: str
    done: tuple[str, ...] = ()
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"done": list(self.done)}


def _remote_refs(root: Path, pattern: str) -> dict[str, str] | None:
    try:
        result = run_git(
            ["ls-remote", "--heads", "origin", f"refs/heads/{pattern}"],
            cwd=root,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    refs: dict[str, str] = {}
    for line in result.stdout.splitlines():
        sha, _, ref = line.strip().partition("\t")
        if ref.startswith("refs/heads/") and _SHA.fullmatch(sha):
            refs[ref.removeprefix("refs/heads/")] = sha.lower()
    return refs


def list_remote_attempt_refs(
    root: Path, parent_issue_number: int
) -> dict[str, str] | None:
    """Remote ``integration/temp-parent-issue-<N>-<12 hex>`` branches and their tips.

    ``None`` means the remote could not be read, which is not "no branches".
    """
    prefix = f"integration/temp-parent-issue-{parent_issue_number}-"
    found = _remote_refs(root, f"{prefix}*")
    if found is None:
        return None
    shape = re.compile(rf"{re.escape(prefix)}[0-9a-f]{{12}}")
    return {name: sha for name, sha in found.items() if shape.fullmatch(name)}


def _remote_parent_tip(root: Path, parent_issue_number: int) -> str | None:
    found = _remote_refs(root, f"parent/issue-{parent_issue_number}")
    return next(iter(found.values()), None) if found else None


@dataclass(frozen=True)
class RemoteRefPorts:
    """Remote git access (replaceable for tests)."""

    list_refs: Callable[[Path, int], dict[str, str] | None] = list_remote_attempt_refs
    parent_tip: Callable[[Path, int], str | None] = _remote_parent_tip
    delete_ref: Callable[[Path, str, str], Deletion] = delete_remote_branch_if_matches


@dataclass(frozen=True)
class CleanupClaims:
    """The #1370 claim API (replaceable for tests)."""

    acquire: Callable[..., Any] = acquire_semantic_claim
    renew: Callable[..., Any] = renew_semantic_claim
    release: Callable[..., Any] = release_semantic_claim


def _operation_settled(state: SemanticReviewStateV1, _pr_number: int) -> bool:
    return (
        state.operation_id is None
        or state.operation_status not in UNSETTLED_OPERATION_STATUSES
    )


class _Hold(Exception):
    """Stop one attempt with a report status and a detail."""

    def __init__(self, status: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class _Target:
    pr: PrRecord
    state: SemanticReviewStateV1
    comments: list[dict[str, Any]] = field(default_factory=list)
    claim: SemanticClaim | None = None
    done: list[str] = field(default_factory=list)


def _invalidated_kind(attempt_id: str) -> str:
    return f"semantic-review-invalidated:{attempt_id}"


def _superseded_kind(attempt_id: str, successor_pr: int) -> str:
    return f"semantic-review-superseded:{attempt_id}:{successor_pr}"


class AttemptCleaner:
    """Close and clean up every invalidated attempt of one parent."""

    def __init__(
        self,
        forge: Forge,
        repository_root: Path,
        parent_issue_number: int,
        *,
        apply: bool,
        holder: str,
        clock: Callable[[], datetime],
        refs: RemoteRefPorts | None = None,
        claims: CleanupClaims | None = None,
        operation_settled: Callable[[SemanticReviewStateV1, int], bool] | None = None,
    ) -> None:
        self._forge = forge
        self._root = repository_root
        self._parent = parent_issue_number
        self._apply = apply
        self._holder = holder
        self._clock = clock
        self._refs = refs or RemoteRefPorts()
        self._claims = claims or CleanupClaims()
        self._settled = operation_settled or _operation_settled
        self._parent_branch = f"parent/issue-{parent_issue_number}"
        self._prefix = f"integration/temp-parent-issue-{parent_issue_number}-"
        self._trust = TrustContext(forge)
        self._authors = trusted_state_authors(forge, self._trust)

    # -- discovery ----------------------------------------------------------------

    def run(self) -> list[AttemptCleanupReport]:
        open_attempts = self._open_attempts()
        refs = self._refs.list_refs(self._root, self._parent)
        successor = self._successor(open_attempts)
        reports = []
        for target in self._targets(open_attempts, refs, successor is not None):
            report = self._clean(target, refs, successor)
            if report is not None:
                reports.append(report)
        return reports

    def _managed(self, pr: PrRecord) -> bool:
        return (
            pr.head_ref.startswith(self._prefix)
            and pr.base_ref == self._parent_branch
            and pr.is_cross_repository is False
        )

    def _restore(self, pr: PrRecord) -> OpenAttempt | None:
        try:
            return restore_attempt(self._forge, self._trust, self._parent, pr)
        except ForgeAuthError:
            raise
        except Exception:  # noqa: BLE001 - an unreadable attempt is not cleaned
            return None

    def _open_attempts(self) -> list[tuple[PrRecord, OpenAttempt]]:
        pairs = []
        for pr in list_open_attempt_prs(self._forge, self._parent):
            attempt = self._restore(pr)
            if attempt is not None:
                pairs.append((pr, attempt))
        return pairs

    def _successor(
        self, open_attempts: list[tuple[PrRecord, OpenAttempt]]
    ) -> OpenAttempt | None:
        """The one live attempt that replaced the invalidated ones (never guessed)."""
        live = [
            a
            for _, a in open_attempts
            if a.state is not None and a.state.phase != SemanticReviewPhase.INVALIDATED
        ]
        return live[0] if len(live) == 1 else None

    def _targets(
        self,
        open_attempts: list[tuple[PrRecord, OpenAttempt]],
        refs: dict[str, str] | None,
        has_successor: bool,
    ) -> list[_Target]:
        seen: set[int] = set()
        targets: list[_Target] = []

        def add(pr: PrRecord, attempt: OpenAttempt | None) -> None:
            state = attempt.state if attempt is not None else None
            if state is None or state.phase != SemanticReviewPhase.INVALIDATED:
                return
            if pr.number not in seen:
                seen.add(pr.number)
                targets.append(_Target(pr, state))

        for pr, attempt in open_attempts:
            add(pr, attempt)
        for branch in sorted(refs or {}):
            matches = [
                pr
                for pr in self._forge.list_prs(state="all", head=branch)
                if pr.head_ref == branch and self._managed(pr)
            ]
            if len(matches) == 1 and matches[0].number not in seen:
                add(matches[0], self._closed_attempt(matches[0]))
        if has_successor:
            for pr in self._forge.list_prs(state="closed"):
                if self._managed(pr) and pr.number not in seen:
                    add(pr, self._closed_attempt(pr))
        return targets

    def _closed_attempt(self, pr: PrRecord) -> OpenAttempt | None:
        if pr.state.upper() == "OPEN":
            return None  # an open PR is judged from the open listing only
        return self._restore(pr)

    # -- one attempt ----------------------------------------------------------------

    def _noticed(self, comments: list[dict[str, Any]], kind: str) -> bool:
        trusted = [
            c
            for c in comments
            if (self._trust.extract_author(c) or "").casefold() in self._authors
        ]
        return latest_notice_body(trusted, kind) is not None

    def _plan(
        self,
        target: _Target,
        refs: dict[str, str] | None,
        successor: OpenAttempt | None,
    ) -> tuple[list[str], str]:
        """Steps still missing, and why a ref must be kept (empty when it need not)."""
        pr, state = target.pr, target.state
        is_open = pr.state.upper() == "OPEN"
        steps = []
        if is_open and not self._noticed(
            target.comments, _invalidated_kind(state.attempt_id)
        ):
            steps.append(STEP_NOTIFY)
        if is_open:
            steps.append(STEP_CLOSE)
        if successor is not None and not self._noticed(
            target.comments, _superseded_kind(state.attempt_id, successor.pr_number)
        ):
            steps.append(STEP_ANNOUNCE)
        kept = ""
        tip = (refs or {}).get(pr.head_ref)
        if tip is not None and tip.lower() != state.head_sha.lower():
            kept = "the remote branch tip moved off the recorded head; kept"
        elif tip is not None:
            steps.append(STEP_DELETE)
        return steps, kept

    def _clean(
        self,
        target: _Target,
        refs: dict[str, str] | None,
        successor: OpenAttempt | None,
    ) -> AttemptCleanupReport | None:
        pr, state = target.pr, target.state
        if pr.state.upper() == "MERGED" or pr.merged_at:
            return self._report(
                target, "blocked", "the PR was merged; no destructive cleanup"
            )
        target.comments = self._forge.list_all_issue_comments(pr.number)
        steps, kept = self._plan(target, refs, successor)
        if not steps and not kept:
            return None
        if not self._apply:
            return self._report(target, "planned", kept, done=tuple(steps))
        try:
            if steps:
                if not self._settled(state, pr.number):
                    raise _Hold("pending", "the old fix operation may still be running")
                self._acquire(target)
            self._carry_out(target, steps, refs, successor)
        except _Hold as hold:
            return self._report(target, hold.status, hold.detail)
        except ForgeAuthError:
            raise
        except Exception as error:  # noqa: BLE001 - reported, retried next cycle
            return self._report(target, "failed", f"{type(error).__name__}: {error}")
        finally:
            self._release(target)
        if kept:
            return self._report(target, "blocked", kept)
        if refs is None and pr.state.upper() == "OPEN":
            return self._report(
                target,
                "pending",
                "remote refs unavailable; the branch is not cleaned yet",
            )
        return self._report(target, "cleaned", "")

    def _report(
        self,
        target: _Target,
        status: str,
        detail: str,
        *,
        done: tuple[str, ...] | None = None,
    ) -> AttemptCleanupReport:
        return AttemptCleanupReport(
            status,
            target.pr.number,
            target.state.attempt_id,
            target.pr.head_ref,
            tuple(target.done) if done is None else done,
            detail,
        )

    # -- claim ------------------------------------------------------------------------

    def _acquire(self, target: _Target) -> None:
        result = self._claims.acquire(
            self._root,
            parent_issue_number=self._parent,
            attempt_id=target.state.attempt_id,
            holder=self._holder,
            now=self._clock(),
            ttl_seconds=_CLAIM_TTL_SECONDS,
        )
        if result.status is not ClaimStatus.ACQUIRED or result.claim is None:
            raise _Hold(
                "pending", f"claim unavailable: {result.detail or result.status.value}"
            )
        target.claim = result.claim

    def _renew(self, target: _Target) -> None:
        assert target.claim is not None
        result = self._claims.renew(
            self._root, target.claim, now=self._clock(), ttl_seconds=_CLAIM_TTL_SECONDS
        )
        if result.status is not PushStatus.PUSHED or result.claim is None:
            target.claim = None
            raise _Hold("pending", f"claim lost: {result.detail}")
        target.claim = result.claim

    def _release(self, target: _Target) -> None:
        if target.claim is None:
            return
        try:
            self._claims.release(self._root, target.claim, now=self._clock())
        except Exception:  # an unreleased claim only expires later  # noqa: BLE001
            pass
        target.claim = None

    # -- steps -------------------------------------------------------------------------

    def _carry_out(
        self,
        target: _Target,
        steps: list[str],
        refs: dict[str, str] | None,
        successor: OpenAttempt | None,
    ) -> None:
        state = target.state
        if STEP_NOTIFY in steps:
            self._renew(target)
            self._post(
                target,
                _invalidated_kind(state.attempt_id),
                self._notice(state, successor),
            )
            target.done.append(STEP_NOTIFY)
        if STEP_CLOSE in steps:
            self._renew(target)
            self._close(target)
            target.done.append(STEP_CLOSE)
        if STEP_ANNOUNCE in steps and successor is not None:
            self._renew(target)
            self._post(
                target,
                _superseded_kind(state.attempt_id, successor.pr_number),
                self._announcement(state, successor),
            )
            target.done.append(STEP_ANNOUNCE)
        if STEP_DELETE in steps:
            self._renew(target)
            self._delete(target)
            target.done.append(STEP_DELETE)

    def _post(self, target: _Target, kind: str, body: str) -> None:
        number = target.pr.number
        try:
            self._forge.add_comment(number, render_notice(kind, body))
        except ForgeAuthError:
            raise
        except Exception:
            # The answer may have been lost after the comment landed: look again
            # before deciding, and never send it twice.
            target.comments = self._forge.list_all_issue_comments(number)
            if not self._noticed(target.comments, kind):
                raise

    def _close(self, target: _Target) -> None:
        number = target.pr.number
        self._forge.close_pull_request(number)
        after = self._forge.get_pull_request(number)
        if after.state.upper() == "MERGED" or after.merged_at:
            raise _Hold("blocked", "the PR was merged; no destructive cleanup")
        if after.state.upper() != "CLOSED":
            raise _Hold("pending", "close not confirmed by the read-back")
        target.pr = after if after.head_ref else target.pr

    def _delete(self, target: _Target) -> None:
        pr, state = target.pr, target.state
        current = self._forge.get_pull_request(pr.number)
        if current.state.upper() == "MERGED" or current.merged_at:
            raise _Hold("blocked", "the PR was merged; no destructive cleanup")
        if current.state.upper() != "CLOSED":
            raise _Hold("pending", "the PR is not closed yet")
        others = [
            p
            for p in self._forge.list_prs(state="open", head=pr.head_ref)
            if p.number != pr.number
        ]
        if others:
            raise _Hold("blocked", "the branch is used by another open PR; kept")
        try:
            result = self._refs.delete_ref(self._root, pr.head_ref, state.head_sha)
        except ValueError as error:
            raise _Hold(
                "blocked", f"the recorded head is not a commit: {error}"
            ) from error
        if result in {Deletion.DELETED, Deletion.ALREADY_ABSENT}:
            return
        if result is Deletion.TIP_MISMATCH:
            raise _Hold("blocked", "lease rejected: the branch tip moved; kept")
        if result is Deletion.DENIED:
            raise _Hold("failed", "deletion denied by the remote")
        raise _Hold("failed", "deletion failed")

    # -- notice texts -------------------------------------------------------------------

    def _notice(
        self, state: SemanticReviewStateV1, successor: OpenAttempt | None
    ) -> str:
        current = self._refs.parent_tip(self._root, self._parent)
        moved = current is not None and current != state.base_sha
        reason = (
            "the parent branch moved after this attempt was built"
            if moved
            else "the child set or the head of the attempt changed"
        )
        follow = (
            "announced in the next comment"
            if successor is not None
            else "not created yet; a separate comment names it once it exists"
        )
        return (
            "## Layer-2 semantic review attempt invalidated\n\n"
            "This integration attempt no longer matches its parent branch, so it is "
            "out of scope for review and merge. The PR is closed and the attempt "
            "branch is removed; the work continues in a newer attempt.\n\n"
            f"- Attempt: `{state.attempt_id}`\n"
            f"- Reason: {reason}\n"
            f"- Base when the attempt was built: `{state.base_sha}`\n"
            f"- Parent branch `{self._parent_branch}` tip now: `{current or 'unavailable'}`\n"
            f"- Head of this attempt: `{state.head_sha}`\n"
            f"- Successor: {follow}"
        )

    def _announcement(
        self, state: SemanticReviewStateV1, successor: OpenAttempt
    ) -> str:
        replacement = successor.state
        assert replacement is not None
        return (
            "## Layer-2 semantic review attempt superseded\n\n"
            f"Review and merge continue in PR #{successor.pr_number} "
            f"(attempt `{replacement.attempt_id}`, base `{replacement.base_sha}`). "
            f"This PR (attempt `{state.attempt_id}`) is closed and must not be merged."
        )


__all__ = [
    "AttemptCleaner",
    "AttemptCleanupReport",
    "CleanupClaims",
    "RemoteRefPorts",
    "UNSETTLED_OPERATION_STATUSES",
    "list_remote_attempt_refs",
]

"""Close invalidated Layer-2 integration attempts and clean up their temp refs (#1440, #1597).

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
from dataclasses import asdict, dataclass
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
    trusted_state_authors,
)
from orchestune.integrator.attempt_cleanup_effects import AttemptCleanupEffects
from orchestune.integrator.attempt_cleanup_plan import (
    STEP_ANNOUNCE,
    STEP_CLOSE,
    STEP_DELETE,
    STEP_NOTIFY,
    AttemptCleanupPlan,
    _Hold,
    _invalidated_kind,
    _superseded_kind,
    _Target,
)
from orchestune.integrator.semantic_claim import (
    ClaimStatus,
    PushStatus,
    acquire_semantic_claim,
    release_semantic_claim,
    renew_semantic_claim,
)
from orchestune.pr_link_notice_trust import TrustContext
from orchestune.review.semantic_gate import (
    IN_FLIGHT_OPERATION_STATUSES,
    SemanticOperationStatus,
    SemanticReviewStateV1,
)

_CLAIM_TTL_SECONDS = 300
UNSETTLED_OPERATION_STATUSES = IN_FLIGHT_OPERATION_STATUSES | {
    SemanticOperationStatus.INDETERMINATE
}
_SHA = re.compile(r"[0-9a-fA-F]{40}")


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


class AttemptCleaner:
    """Close and clean up every invalidated attempt of one parent."""

    def __init__(  # noqa: PLR0913
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
        self._trust = TrustContext(forge)
        self._authors = trusted_state_authors(forge, self._trust)
        self._plan = AttemptCleanupPlan(
            forge, parent_issue_number, self._trust, self._authors
        )
        self._effects = AttemptCleanupEffects(
            forge,
            repository_root,
            parent_issue_number,
            self._plan,
            self._refs.parent_tip,
            self._refs.delete_ref,
        )

    # -- discovery ----------------------------------------------------------------

    def run(self) -> list[AttemptCleanupReport]:
        open_attempts = self._plan.open_attempts()
        refs = self._refs.list_refs(self._root, self._parent)
        successor = self._plan.successor(open_attempts)
        reports = []
        for target in self._plan.targets(open_attempts, refs, successor is not None):
            report = self._clean(target, refs, successor)
            if report is not None:
                reports.append(report)
        return reports

    # -- one attempt ----------------------------------------------------------------

    def _apply_cleanup_steps(
        self,
        target: _Target,
        steps: list[str],
        refs: dict[str, str] | None,
        successor: OpenAttempt | None,
        kept: str,
    ) -> tuple[bool, AttemptCleanupReport | None, str, dict[str, str] | None, bool]:
        state = target.state
        was_open = target.pr.state.upper() == "OPEN"
        try:
            if steps:
                if not self._settled(state, target.pr.number):
                    raise _Hold("pending", "the old fix operation may still be running")
                self._acquire(target)
                steps, kept, refs = self._replan(target, successor)
                was_open = target.pr.state.upper() == "OPEN"
                if not steps and not kept:
                    return True, None, kept, refs, was_open
            self._carry_out(target, steps, refs, successor)
            return False, None, kept, refs, was_open
        except _Hold as hold:
            return (
                True,
                self._report(target, hold.status, hold.detail),
                kept,
                refs,
                was_open,
            )
        except ForgeAuthError:
            raise
        except Exception as error:  # noqa: BLE001 - reported, retried next cycle
            return (
                True,
                self._report(target, "failed", f"{type(error).__name__}: {error}"),
                kept,
                refs,
                was_open,
            )
        finally:
            self._release(target)

    def _clean(
        self,
        target: _Target,
        refs: dict[str, str] | None,
        successor: OpenAttempt | None,
    ) -> AttemptCleanupReport | None:
        pr = target.pr
        if pr.state.upper() == "MERGED" or pr.merged_at:
            return self._report(
                target, "blocked", "the PR was merged; no destructive cleanup"
            )
        target.comments = self._forge.list_all_issue_comments(pr.number)
        steps, kept = self._plan.plan(target, refs, successor)
        if not steps and not kept:
            return None
        if not self._apply:
            return self._report(target, "planned", kept, done=tuple(steps))
        finished, report, kept, refs, was_open = self._apply_cleanup_steps(
            target, steps, refs, successor, kept
        )
        if finished:
            return report
        if kept:
            return self._report(target, "blocked", kept)
        if refs is None and was_open:
            return self._report(
                target,
                "pending",
                "remote refs unavailable; the branch is not cleaned yet",
            )
        return self._report(target, "cleaned", "")

    def _replan(
        self, target: _Target, successor: OpenAttempt | None
    ) -> tuple[list[str], str, dict[str, str] | None]:
        """Plan again under the claim: another cycle may have finished meanwhile,
        and a stale plan would post its one-time comments a second time."""
        current = self._forge.get_pull_request(target.pr.number)
        if current.head_ref:
            target.pr = current
        if target.pr.state.upper() == "MERGED" or target.pr.merged_at:
            raise _Hold("blocked", "the PR was merged; no destructive cleanup")
        target.comments = self._forge.list_all_issue_comments(target.pr.number)
        refs = self._refs.list_refs(self._root, self._parent)
        return (*self._plan.plan(target, refs, successor), refs)

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
            self._effects.post(
                target,
                _invalidated_kind(state.attempt_id),
                self._effects.notice(state, successor),
            )
            target.done.append(STEP_NOTIFY)
        if STEP_CLOSE in steps:
            self._renew(target)
            self._effects.close(target)
            target.done.append(STEP_CLOSE)
        if STEP_ANNOUNCE in steps and successor is not None:
            self._renew(target)
            self._effects.post(
                target,
                _superseded_kind(state.attempt_id, successor.pr_number),
                self._effects.announcement(state, successor),
            )
            target.done.append(STEP_ANNOUNCE)
        if STEP_DELETE in steps:
            self._renew(target)
            self._effects.delete(target)
            target.done.append(STEP_DELETE)


__all__ = [
    "AttemptCleaner",
    "AttemptCleanupReport",
    "CleanupClaims",
    "RemoteRefPorts",
    "UNSETTLED_OPERATION_STATUSES",
    "list_remote_attempt_refs",
]

"""Discover and plan Layer-2 integration attempt cleanups (#1440, #1597)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orchestune.forge import Forge, ForgeAuthError
from orchestune.integrator.attempt import (
    OpenAttempt,
    restore_attempt,
)
from orchestune.integrator.pr import list_open_attempt_prs
from orchestune.integrator.semantic_claim import SemanticClaim
from orchestune.issue_notice import latest_notice_body
from orchestune.models import PrRecord
from orchestune.pr_link_notice_trust import TrustContext
from orchestune.review.semantic_gate import (
    SemanticReviewPhase,
    SemanticReviewStateV1,
)

STEP_NOTIFY = "notify"
STEP_CLOSE = "close"
STEP_ANNOUNCE = "announce"
STEP_DELETE = "delete_ref"


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


class AttemptCleanupPlan:
    """Plan cleanup steps and target discovery for invalidated attempts."""

    def __init__(
        self,
        forge: Forge,
        parent_issue_number: int,
        trust: TrustContext,
        authors: frozenset[str] | set[str],
    ) -> None:
        self._forge = forge
        self._parent = parent_issue_number
        self._trust = trust
        self._authors = authors
        self.parent_branch = f"parent/issue-{parent_issue_number}"
        self.attempt_prefix = f"integration/temp-parent-issue-{parent_issue_number}-"

    def managed(self, pr: PrRecord) -> bool:
        return (
            pr.head_ref.startswith(self.attempt_prefix)
            and pr.base_ref == self.parent_branch
            and pr.is_cross_repository is False
        )

    def restore(self, pr: PrRecord) -> OpenAttempt | None:
        try:
            return restore_attempt(self._forge, self._trust, self._parent, pr)
        except ForgeAuthError:
            raise
        except Exception:  # noqa: BLE001 - an unreadable attempt is not cleaned
            return None

    def open_attempts(self) -> list[tuple[PrRecord, OpenAttempt]]:
        pairs = []
        for pr in list_open_attempt_prs(self._forge, self._parent):
            attempt = self.restore(pr)
            if attempt is not None:
                pairs.append((pr, attempt))
        return pairs

    def successor(
        self, open_attempts: list[tuple[PrRecord, OpenAttempt]]
    ) -> OpenAttempt | None:
        """The one live attempt that replaced the invalidated ones (never guessed)."""
        live = [
            a
            for _, a in open_attempts
            if a.state is not None and a.state.phase != SemanticReviewPhase.INVALIDATED
        ]
        return live[0] if len(live) == 1 else None

    def closed_attempt(self, pr: PrRecord) -> OpenAttempt | None:
        if pr.state.upper() == "OPEN":
            return None  # an open PR is judged from the open listing only
        return self.restore(pr)

    def targets(
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
                if pr.head_ref == branch and self.managed(pr)
            ]
            if len(matches) == 1 and matches[0].number not in seen:
                add(matches[0], self.closed_attempt(matches[0]))
        if has_successor:
            for pr in self._forge.list_prs(state="closed"):
                if self.managed(pr) and pr.number not in seen:
                    add(pr, self.closed_attempt(pr))
        return targets

    def noticed(self, comments: list[dict[str, Any]], kind: str) -> bool:
        trusted = [
            c
            for c in comments
            if (self._trust.extract_author(c) or "").casefold() in self._authors
        ]
        return latest_notice_body(trusted, kind) is not None

    def plan(
        self,
        target: _Target,
        refs: dict[str, str] | None,
        successor: OpenAttempt | None,
    ) -> tuple[list[str], str]:
        """Steps still missing, and why a ref must be kept (empty when it need not)."""
        pr, state = target.pr, target.state
        is_open = pr.state.upper() == "OPEN"
        steps = []
        if is_open and not self.noticed(
            target.comments, _invalidated_kind(state.attempt_id)
        ):
            steps.append(STEP_NOTIFY)
        if is_open:
            steps.append(STEP_CLOSE)
        if successor is not None and not self.noticed(
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


__all__ = [
    "AttemptCleanupPlan",
    "STEP_ANNOUNCE",
    "STEP_CLOSE",
    "STEP_DELETE",
    "STEP_NOTIFY",
    "_Hold",
    "_Target",
    "_invalidated_kind",
    "_superseded_kind",
]

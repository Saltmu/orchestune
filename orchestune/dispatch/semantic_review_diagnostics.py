"""Operator diagnostics of a Layer-2 semantic review attempt (#1436).

One description serves the read-only ``orchestune review status`` output and the
escalation notice, so a stopped attempt can be understood from the notice alone.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal, cast

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.reviewer import resolve_reviewer_bot
from orchestune.dispatch.semantic_review_ack import AckAssessment, assess_review_ack
from orchestune.review.semantic_gate import SemanticReviewStateV1

_ACK_REASON = "review_not_acknowledged"


@dataclass(frozen=True)
class ReviewerResolution:
    reviewer: str | None
    #: How the reviewer was chosen, for the operator.
    origin: str


def describe_reviewer(provider: str, target_name: str) -> ReviewerResolution:
    """The reviewer Layer-2 asks and why (explicit setting or auto + target)."""
    if provider in {"claude", "codex"}:
        return ReviewerResolution(
            provider, f'explicit `semantic_review_provider = "{provider}"`'
        )
    if provider != "auto":
        return ReviewerResolution(None, f"unsupported provider `{provider}`")
    reviewer = resolve_reviewer_bot(cast(Literal["auto"], provider), target_name)
    if reviewer is None:
        return ReviewerResolution(
            None, f"`auto` cannot be resolved for dispatch target `{target_name}`"
        )
    return ReviewerResolution(
        reviewer,
        f"`auto` with dispatch target `{target_name}` (cross-vendor default)",
    )


@dataclass(frozen=True)
class ReviewDiagnostics:
    parent_issue_number: int
    pr_number: int
    attempt_id: str
    head_sha: str
    phase: str
    round: int
    reviewer: str | None
    reviewer_origin: str
    trigger_comment_id: str | None
    requested_at: str | None
    requests_on_record: int
    acknowledged: bool
    elapsed_seconds: int | None
    ack_timeout_seconds: int
    review_timeout_seconds: int
    auto_retry: bool
    ack_action: str
    escalation_reason: str | None
    escalated_from: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def resume_commands(self) -> tuple[str, ...]:
        pr = self.pr_number
        return (
            f'orchestune review retry --pr {pr} --reason "<why>" --apply',
            f'orchestune review override --pr {pr} --reason "<why>" --apply',
        )

    def lines(self) -> list[str]:
        """The facts, one ``key: value`` per line."""
        ack = "yes" if self.acknowledged else "no"
        elapsed = "n/a" if self.elapsed_seconds is None else f"{self.elapsed_seconds}s"
        return [
            f"parent issue: #{self.parent_issue_number}",
            f"pull request: #{self.pr_number}",
            f"attempt: {self.attempt_id}",
            f"head: {self.head_sha}",
            f"phase: {self.phase} (round {self.round})",
            f"reviewer: {self.reviewer or 'unresolved'} - {self.reviewer_origin}",
            f"request comment: {self.trigger_comment_id or 'none'}"
            f" (at {self.requested_at or 'n/a'}, {self.requests_on_record} on record)",
            f"reviewer activity: {ack}; elapsed {elapsed}"
            f" (ack deadline {self.ack_timeout_seconds}s,"
            f" review timeout {self.review_timeout_seconds}s,"
            f" auto retry {'on' if self.auto_retry else 'off'})",
            f"stop reason: {self.escalation_reason or 'none'}"
            + (f" (from {self.escalated_from})" if self.escalated_from else ""),
        ]

    def guidance(self) -> str:
        """What to check and how to resume, for the stop reason at hand."""
        lines = self.lines()
        if self.escalation_reason == _ACK_REASON and self.reviewer:
            lines += [
                f"The reviewer `{self.reviewer}` showed no activity after the request."
                " Check that its trigger path (workflow or App) exists on the default"
                " branch: `issue_comment` workflows run from the default branch,"
                " not the PR base.",
                "To ask another reviewer, set `semantic_review_provider` in"
                " orchestune.toml, then resume.",
            ]
        lines += [f"Resume: `{cmd}`" for cmd in self.resume_commands]
        return "\n".join(f"- {line}" for line in lines)


def build_review_diagnostics(
    config: DispatcherConfig,
    state: SemanticReviewStateV1,
    *,
    pr_number: int,
    requests: Sequence[tuple[str, datetime]],
    acknowledged: bool,
    now: datetime,
) -> tuple[ReviewDiagnostics, AckAssessment]:
    """Diagnostics of ``state`` plus the ack assessment they were derived with.

    ``requests`` are the trusted review requests of the state's round and head
    (comment id, creation time), oldest first.
    """
    target = getattr(config.dispatch_target, "target_name", None)
    resolution = describe_reviewer(
        config.semantic_review_provider, target if isinstance(target, str) else "local"
    )
    ack = assess_review_ack(
        trigger_times=[at for _, at in requests],
        now=now,
        acknowledged=acknowledged,
        ack_seconds=config.semantic_review_ack_deadline_seconds,
        auto_retry=config.semantic_review_auto_retry,
        backoff_seconds=config.semantic_review_retry_backoff_seconds,
    )
    last = requests[-1] if requests else None
    return (
        ReviewDiagnostics(
            parent_issue_number=state.parent_issue_number,
            pr_number=pr_number,
            attempt_id=state.attempt_id,
            head_sha=state.head_sha,
            phase=state.phase.value,
            round=state.round,
            reviewer=resolution.reviewer,
            reviewer_origin=resolution.origin,
            trigger_comment_id=str(last[0]) if last else None,
            requested_at=last[1].isoformat() if last else None,
            requests_on_record=len(requests),
            acknowledged=acknowledged,
            elapsed_seconds=ack.elapsed_seconds if last else None,
            ack_timeout_seconds=config.semantic_review_ack_deadline_seconds,
            review_timeout_seconds=config.semantic_review_timeout_seconds,
            auto_retry=config.semantic_review_auto_retry,
            ack_action=ack.action.value,
            escalation_reason=state.escalation_reason,
            escalated_from=state.escalated_from.value if state.escalated_from else None,
        ),
        ack,
    )

"""External Forge and operation side-effects collaborator for semantic review (#1595)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from orchestune.dispatch.config.model import DispatcherConfig
from orchestune.dispatch.gc.git import evaluate_worktree_removal
from orchestune.dispatch.local_semantic_fix import cleanup_semantic_fix_worktree
from orchestune.dispatch.semantic_review.adapters import (
    SemanticFixLaunchContext,
    SemanticOperationResult,
    dispatch_semantic_fix_agent,
    escalate_semantic_review_to_human,
    poll_semantic_operation,
    trigger_semantic_review_round,
    validate_semantic_review_preflight,
)
from orchestune.dispatch.semantic_review.evidence import (
    SemanticReviewEvidence,
    _Attempt,
    _Stop,
)
from orchestune.dispatch.semantic_review.lease import SemanticReviewLease
from orchestune.dispatch.semantic_review.read_context import ReadContextBuilder
from orchestune.dispatch.semantic_review.triggers import (
    round_trigger,
    round_triggers,
)
from orchestune.forge import Forge
from orchestune.issue_notice import latest_notice_body, post_notice_if_changed
from orchestune.labels import StatusLabel
from orchestune.review.markers import build_trigger_body
from orchestune.review.rounds import ReviewTrigger
from orchestune.review.semantic_gate import (
    SemanticOperationStatus,
    SemanticReviewPhase,
    SemanticReviewStateV1,
    format_semantic_review_state_marker,
)
from orchestune.review.snapshot import normalize_timestamp

_P = SemanticReviewPhase


@dataclass(frozen=True)
class SemanticReviewOperations:
    """The #1361 operation adapters (replaceable for tests)."""

    preflight: Callable[..., Any] = validate_semantic_review_preflight
    trigger: Callable[..., SemanticOperationResult] = trigger_semantic_review_round
    dispatch_fix: Callable[..., SemanticOperationResult] = dispatch_semantic_fix_agent
    poll: Callable[..., SemanticOperationResult] = poll_semantic_operation
    escalate: Callable[..., None] = escalate_semantic_review_to_human
    cleanup_fix_worktree: Callable[..., bool] = cleanup_semantic_fix_worktree
    read_context_builder: ReadContextBuilder | None = None


def _escalation_notice_kind(attempt_id: str, reason: str) -> str:
    """The notice kind `escalate_semantic_review_to_human` posts for one reason."""
    key = hashlib.sha256(f"{attempt_id}:{reason}".encode()).hexdigest()[:16]
    return f"semantic-review:{attempt_id}:{key}"


class SemanticReviewEffects:
    """Perform Forge and adapter writes under a held semantic-review claim."""

    def __init__(
        self,
        forge: Forge,
        repository_root: Path,
        config: DispatcherConfig,
        operations: SemanticReviewOperations,
        evidence: SemanticReviewEvidence,
        lease: SemanticReviewLease,
        reviewer: Callable[[], str],
        stamp: Callable[[], str],
    ) -> None:
        self._forge = forge
        self._root = repository_root
        self._config = config
        self._ops = operations
        self._evidence = evidence
        self._lease = lease
        self._reviewer = reviewer
        self._stamp = stamp

    def trigger_body(self, ctx: _Attempt, nxt: SemanticReviewStateV1) -> str:
        reviewer = self._reviewer()
        reply = self._evidence.safe_reply(ctx, nxt, reviewer)
        return build_trigger_body(reply, reviewer, nxt.round, nxt.head_sha)

    def resumed_trigger(
        self, ctx: _Attempt, nxt: SemanticReviewStateV1, triggers: list[ReviewTrigger]
    ) -> bool:
        """An explicit retry of the escalated round asks the reviewer again.

        Requests from before the escalation are no answer to it; one posted
        after the escalation was saved is this resume's own (interrupted run).
        """
        since = normalize_timestamp(ctx.state.updated_at)
        if ctx.state.phase != _P.ESCALATED or nxt.round != ctx.state.round or not since:
            return False
        resumed = [
            t
            for t in round_triggers(triggers, nxt, fresh=True)
            if t.created_at >= since
        ]
        if any(
            ctx.read_context and not ctx.read_context.matches_trigger(t.body, nxt)
            for t in resumed
        ):
            raise _Stop("read_context_invalid", error="resumed trigger report mismatch")
        if not resumed:
            self._lease.revalidate(ctx)
            self._forge.create_issue_comment(
                ctx.pr.pr_number, self.trigger_body(ctx, nxt)
            )
        return True

    def post_trigger(self, ctx: _Attempt, nxt: SemanticReviewStateV1) -> None:
        reviewer = self._reviewer()
        reply = self._evidence.safe_reply(ctx, nxt, reviewer)
        triggers = self._evidence.trusted_triggers(ctx)
        if self.resumed_trigger(ctx, nxt, triggers):
            return
        existing = round_trigger(triggers, nxt, fresh=True)
        if existing is not None:
            if ctx.read_context and not ctx.read_context.matches_trigger(
                existing.body, nxt
            ):
                raise _Stop(
                    "read_context_invalid", error="existing trigger report mismatch"
                )
            return  # posted by an earlier, interrupted run of this transition
        self._lease.revalidate(ctx)
        if round_trigger(triggers, nxt, fresh=False) is not None:
            # A reset round repeats an old (round, head) request, which the
            # adapter would take as already posted: request a new review.
            self._forge.create_issue_comment(
                ctx.pr.pr_number,
                build_trigger_body(reply, reviewer, nxt.round, nxt.head_sha),
            )
            return
        result = self._ops.trigger(
            self._forge,
            pr_number=ctx.pr.pr_number,
            attempt_id=nxt.attempt_id,
            round_number=nxt.round,
            head_sha=nxt.head_sha,
            reviewer=reviewer,
            reply_body=reply,
        )
        if result.status is SemanticOperationStatus.INDETERMINATE:
            raise _Stop("trigger_indeterminate", error=result.error_code)

    def dispatch_fix(
        self, ctx: _Attempt, reserved: SemanticReviewStateV1
    ) -> SemanticReviewStateV1:
        """Launch the reserved job once; an unknown launch is never relaunched."""
        assert reserved.operation_id is not None
        self._lease.revalidate(ctx)
        result = self._ops.dispatch_fix(
            self._config.dispatch_target,
            pr_number=ctx.pr.pr_number,
            attempt_id=reserved.attempt_id,
            operation_id=reserved.operation_id,
            temp_branch=ctx.pr.temp_branch,
            expected_head_sha=reserved.head_sha,
            round_number=reserved.round,
            launch_context=SemanticFixLaunchContext(
                worktree_root=(self._root / self._config.worktree_root).resolve(),
                repo_root=self._root,
            ),
            read_context=ctx.read_context.fix_instructions(reserved)
            if ctx.read_context
            else "",
        )
        launched = replace(
            reserved,
            operation_status=result.status,
            provider_handle=result.provider_handle,
            state_revision=reserved.state_revision + 1,
            updated_at=self._stamp(),
        )
        return self.save(ctx, launched)

    def save(
        self, ctx: _Attempt, state: SemanticReviewStateV1
    ) -> SemanticReviewStateV1:
        self._lease.revalidate(ctx)
        assert ctx.claim is not None
        stored = replace(
            state,
            claim_holder=ctx.claim.holder,
            claim_generation=ctx.claim.generation,
            claim_expires_at=ctx.claim.expires_at.isoformat(),
        )
        self._forge.add_comment(
            ctx.pr.pr_number, format_semantic_review_state_marker(stored)
        )
        return stored

    def cleanup_fix_worktree(self, op: str | None, handle: str | None = None) -> None:
        if not op:
            return
        wt = (
            Path(self._root) / self._config.worktree_root / f"semantic-fix-{op}"
        ).resolve()
        if wt.exists():
            pid = int(handle) if handle and handle.isdigit() else None
            self._ops.cleanup_fix_worktree(
                wt, repo_root=self._root, evaluate_fn=evaluate_worktree_removal, pid=pid
            )

    def escalation_recorded(self, state: SemanticReviewStateV1) -> bool:
        """Notice posted and the parent still blocked, else the (idempotent)
        escalation is redone; an unreadable answer counts as recorded, since a
        repeated escalation comments again."""
        reason = state.escalation_reason or "escalated"
        kind = _escalation_notice_kind(state.attempt_id, reason)
        parent = state.parent_issue_number
        try:
            if latest_notice_body(self._forge.list_comments(parent), kind) is None:
                return False
            labels = self._forge.get_issue_labels(parent)
        except Exception:  # noqa: BLE001 - boundary: an unreadable escalation record counts as already recorded
            return True
        return StatusLabel.BLOCKED_HUMAN_REVIEW.value in labels

    def escalate(
        self, ctx: _Attempt, state: SemanticReviewStateV1, guidance: str
    ) -> None:
        reason = state.escalation_reason or "escalated"
        source = state.escalated_from.value if state.escalated_from else "unknown"
        self._ops.escalate(
            self._forge,
            parent_issue_number=state.parent_issue_number,
            pr_number=ctx.pr.pr_number,
            attempt_id=state.attempt_id,
            reason=reason,
            details=(
                f"stopped at `{source}` (round {state.round}, head `{state.head_sha}`); "
                "the head is kept. Resume with a semantic-review-retry marker or skip "
                "with a semantic-review-override marker bound to this head."
                f"{guidance}"
            ),
        )

    def note_skip(
        self, ctx: _Attempt, state: SemanticReviewStateV1, reason: str
    ) -> None:
        override = ctx.override if reason == "human_override" else None
        by = f" by @{override.actor}: {override.reason}" if override else ""
        body = (
            "## Layer-2 semantic review skipped\n\n"
            f"- Attempt: `{state.attempt_id}`\n- Head: `{state.head_sha}`\n"
            f"- Reason: `{reason}`{by}"
        )
        post_notice_if_changed(
            self._forge,
            ctx.pr.pr_number,
            f"semantic-review-skip:{state.attempt_id}",
            body,
        )


__all__ = [
    "SemanticReviewEffects",
    "SemanticReviewOperations",
]

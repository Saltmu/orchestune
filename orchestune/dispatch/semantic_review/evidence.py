"""PR-comment evidence for the integration semantic-review cycle.

The cycle runner imports these names and calls them through
``SemanticReviewEvidence``. This module does not import the runner.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, cast

from orchestune.dispatch.semantic_review.ack import AckAssessment
from orchestune.dispatch.semantic_review.read_context import ReadReviewContext
from orchestune.dispatch.semantic_review.triggers import (
    restore_round_triggers,
)
from orchestune.forge import Forge
from orchestune.integrator.attempt import (
    OpenAttempt,
)
from orchestune.integrator.semantic_claim import (
    SemanticClaim,
)
from orchestune.pr_link_notice_trust import NoticePresence, TrustContext
from orchestune.read_dependencies.contracts import READ_REPORT_CHUNK_BODY_BYTES
from orchestune.review.rounds import (
    ReviewTrigger,
    build_restorable_trigger_body,
)
from orchestune.review.semantic_gate import (
    SemanticJudgmentDecision,
    SemanticReviewHumanOverrideV1,
    SemanticReviewRetryV1,
    SemanticReviewStateV1,
    SemanticTransitionObservation,
    evaluate_semantic_judgments,
    restore_latest_fix_result,
    restore_latest_human_override,
    restore_latest_retry_request,
    restore_latest_semantic_review_state,
)
from orchestune.review.snapshot import EvidenceContractError

#: The head check of `find_open_attempts`; a promoted fix output trips it until
#: the runner records the new head, so that one error is re-restored here.
_HEAD_MISMATCH = "semantic state head does not match the PR head"


_WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})


_JUDGMENT_BLOCK = re.compile(
    r"^```orchestune-review-judgments[^\n]*\n.*?^```[^\S\n]*$", re.M | re.S
)


@dataclass(frozen=True)
class _Review:
    complete: bool = False
    acknowledged: bool = False
    result: dict[str, Any] | None = None
    #: Trusted requests of this round and head: (comment id, creation time).
    requests: tuple[tuple[str, datetime], ...] = ()


@dataclass
class _Attempt:
    """One attempt being advanced; ``claim`` is set once the claim is held."""

    pr: OpenAttempt
    state: SemanticReviewStateV1
    override: SemanticReviewHumanOverrideV1 | None
    comments: list[dict[str, Any]]
    trust: TrustContext
    review: _Review = _Review()
    obs: SemanticTransitionObservation | None = None
    claim: SemanticClaim | None = None
    ack: AckAssessment | None = None
    read_context: ReadReviewContext | None = None


class _Stop(Exception):
    """Stop advancing one attempt with a report status (nothing more is written)."""

    def __init__(self, status: str, **details: Any) -> None:
        super().__init__(status)
        self.status = status
        self.details = details


def _state_authors(trust: TrustContext) -> frozenset[str]:
    """Orchestune itself and configured bot collaborators (the #1360 state rule)."""
    trusted: set[str] = set()
    identity = trust.get_self_identity()
    if identity:
        trusted.add(identity.casefold())
    trusted.update(
        author.casefold()
        for author in trust.get_trusted_collaborators() or ()
        if author.casefold().endswith("[bot]")
    )
    return frozenset(trusted)


def _authored(
    comments: list[dict[str, Any]],
    trust: TrustContext,
    accept: Callable[[str], bool],
) -> list[tuple[str, str]]:
    """``(body, author)`` of the comments whose author ``accept`` admits."""
    selected = []
    for comment in comments:
        author, body = trust.extract_author(comment), comment.get("body")
        if author and isinstance(body, str) and accept(author.casefold()):
            selected.append((body, author))
    return selected


class SemanticReviewEvidence:
    """Read semantic-review evidence from the PR comments of one attempt."""

    def __init__(self, forge: Forge) -> None:
        self._forge = forge

    def restore(self, pr: OpenAttempt) -> _Attempt:
        comments = self._forge.list_all_issue_comments(pr.pr_number)
        trust = TrustContext(self._forge)
        if pr.state is not None:
            return _Attempt(pr, pr.state, pr.override, comments, trust)
        if pr.state_error != _HEAD_MISMATCH:
            raise _Stop("state_unreadable", error=pr.state_error)
        try:
            trusted = _state_authors(trust)
            state = restore_latest_semantic_review_state(
                [body for body, _ in _authored(comments, trust, trusted.__contains__)]
            )
        except ValueError as error:
            raise _Stop("state_unreadable", error=str(error)) from error
        if state is None or state.pr_number != pr.pr_number:
            raise _Stop("state_unreadable", error=pr.state_error)
        override = None
        for body, actor in reversed(self.human_bodies(comments, trust)):
            found = restore_latest_human_override(
                [body], attempt_id=state.attempt_id, head_sha=state.head_sha
            )
            if found is not None:
                override = replace(found, actor=actor)
                break
        return _Attempt(pr, state, override, comments, trust)

    def human_bodies(
        self, comments: list[dict[str, Any]], trust: TrustContext
    ) -> list[tuple[str, str]]:
        """Bodies of humans with write access (the #1360 override rule)."""
        permissions: dict[str, str] = {}

        def allowed(author: str) -> bool:
            if author.casefold().endswith("[bot]"):
                return False
            if author not in permissions:
                permissions[author] = self._forge.get_actor_permission(author)
            return permissions[author].casefold() in _WRITE_PERMISSIONS

        return _authored(comments, trust, allowed)

    def trusted(self, ctx: _Attempt, comment: dict[str, Any]) -> bool:
        presence, _ = ctx.trust.evaluate_candidate_markers([comment])
        if presence is NoticePresence.UNKNOWN:
            raise _Stop("inputs_unknown", error="comment author trust is unknown")
        return presence is NoticePresence.PRESENT

    def trusted_triggers(self, ctx: _Attempt) -> list[ReviewTrigger]:
        """Only trusted requests open or bound a round's review window."""
        return restore_round_triggers([c for c in ctx.comments if self.trusted(ctx, c)])

    def judgment_comment(self, ctx: _Attempt) -> dict[str, Any] | None:
        """The trusted table the round's job named in its trusted result marker."""
        state = ctx.state
        bodies = [
            str(c["body"])
            for c in ctx.comments
            if isinstance(c.get("body"), str) and self.trusted(ctx, c)
        ]
        try:
            result = restore_latest_fix_result(
                bodies,
                attempt_id=state.attempt_id,
                operation_id=str(state.operation_id),
            )
        except ValueError:
            return None
        if (
            result is None
            or result.judgment_comment_id is None
            or result.status != "completed"
            or result.input_sha != (state.fix_input_sha or state.head_sha)
        ):
            return None
        for comment in ctx.comments:
            if comment.get("id") == result.judgment_comment_id:
                return comment if self.trusted(ctx, comment) else None
        return None

    def judgments(self, ctx: _Attempt) -> SemanticJudgmentDecision | None:
        comment = self.judgment_comment(ctx)
        if comment is None:
            return None
        return evaluate_semantic_judgments(
            str(comment.get("body") or ""),
            ctx.review.result if ctx.review.complete else None,
            expected_round=ctx.state.round,
        )

    def retry_request(self, ctx: _Attempt) -> SemanticReviewRetryV1 | None:
        """The latest unconsumed retry/restart a collaborator posted for this head."""
        state = ctx.state
        bodies = [body for body, _ in self.human_bodies(ctx.comments, ctx.trust)]
        consumed = {state.last_retry_request_id} - {None}
        try:
            return restore_latest_retry_request(
                bodies,
                attempt_id=state.attempt_id,
                head_sha=state.head_sha,
                consumed_request_ids=cast(set[str], consumed),
            )
        except ValueError:
            return None

    def trigger_reply(self, ctx: _Attempt, nxt: SemanticReviewStateV1) -> str:
        """Round 1 asks for a review; later rounds carry the previous table and SHA."""
        intro = (
            f"Layer-2 semantic review of integration attempt `{nxt.attempt_id[:12]}` "
            f"for parent #{nxt.parent_issue_number}: review the combined diff "
            "for cross-task defects."
        )
        comment = self.judgment_comment(ctx) if ctx.state.operation_id else None
        block = (
            _JUDGMENT_BLOCK.search(str(comment.get("body") or "")) if comment else None
        )
        reply = f"{intro}\n\nReview head: `{nxt.head_sha}`."
        if nxt.round > 1 and block is not None:
            reply = (
                f"{intro}\n\nRound {ctx.state.round} judgments, addressed at "
                f"`{nxt.head_sha}`:\n\n{block.group(0)}\n"
            )
        context = ctx.read_context.render(nxt) if ctx.read_context else ""
        return f"{reply}\n\n{context}" if context else reply

    def safe_reply(
        self, ctx: _Attempt, nxt: SemanticReviewStateV1, reviewer: str
    ) -> str:
        reply = self.trigger_reply(ctx, nxt)
        try:
            body = build_restorable_trigger_body(
                reply, reviewer, nxt.round, nxt.head_sha
            )
            if len(body.encode("utf-8")) > READ_REPORT_CHUNK_BODY_BYTES:
                raise EvidenceContractError(
                    "review reply exceeds the comment byte limit"
                )
        except EvidenceContractError:
            reply = f"Review head: `{nxt.head_sha}`."
            if ctx.read_context is not None:
                reply += "\n\n" + ctx.read_context.render(nxt, compact=True)
            build_restorable_trigger_body(reply, reviewer, nxt.round, nxt.head_sha)
        if ctx.read_context and not ctx.read_context.matches_trigger(reply, nxt):
            raise _Stop("read_context_invalid", error="required context was lost")
        return reply

"""Claim lease collaborator for the Layer-2 semantic-review runner (#1595)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from orchestune.dispatch.semantic_review.evidence import (
    _Attempt,
    _authored,
    _state_authors,
    _Stop,
)
from orchestune.forge import Forge
from orchestune.integrator.semantic_claim import (
    ClaimResult,
    ClaimStatus,
    PushResult,
    PushStatus,
    acquire_semantic_claim,
    delete_attempt_fix_branches,
    promote_fix_output,
    release_semantic_claim,
    renew_semantic_claim,
)
from orchestune.review.semantic_gate import restore_latest_semantic_review_state

CLAIM_TTL_SECONDS = 300

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SemanticClaimClient:
    """The #1370 claim API (replaceable for tests)."""

    acquire: Callable[..., ClaimResult] = acquire_semantic_claim
    renew: Callable[..., PushResult] = renew_semantic_claim
    release: Callable[..., PushResult] = release_semantic_claim
    promote: Callable[..., PushResult] = promote_fix_output
    delete_fix_branches: Callable[[str | Path, str], None] = delete_attempt_fix_branches


class SemanticReviewLease:
    """Own the semantic-review claim lifecycle for one runner instance."""

    def __init__(
        self,
        forge: Forge,
        repository_root: Path,
        claims: SemanticClaimClient,
        worker_id: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._forge = forge
        self._root = repository_root
        self._claims = claims
        self._worker = worker_id
        self._clock = clock

    def acquire(self, ctx: _Attempt) -> None:
        result = self._claims.acquire(
            self._root,
            parent_issue_number=ctx.state.parent_issue_number,
            attempt_id=ctx.state.attempt_id,
            holder=self._worker,
            now=self._clock(),
            ttl_seconds=CLAIM_TTL_SECONDS,
        )
        if result.status is ClaimStatus.HELD_BY_OTHER:
            holder = result.claim.holder if result.claim else None
            raise _Stop("claimed_by_other", holder=holder)
        if result.status is not ClaimStatus.ACQUIRED or result.claim is None:
            raise _Stop("claim_unknown", detail=result.detail)
        ctx.claim = result.claim

    def require_unchanged(self, ctx: _Attempt) -> None:
        """Another worker may have saved between our read and our claim: then
        this decision is stale and saving it would fork the state revision."""
        comments = self._forge.list_all_issue_comments(ctx.pr.pr_number)
        trusted = _state_authors(ctx.trust)
        try:
            latest = restore_latest_semantic_review_state(
                [
                    body
                    for body, _ in _authored(comments, ctx.trust, trusted.__contains__)
                ]
            )
        except ValueError as error:
            raise _Stop("state_unreadable", error=str(error)) from error
        if latest != ctx.state:
            raise _Stop("state_changed")

    def revalidate(self, ctx: _Attempt, ttl: float = CLAIM_TTL_SECONDS) -> None:
        """Renew (and so re-prove) ownership right before an external write."""
        assert ctx.claim is not None
        result = self._claims.renew(
            self._root, ctx.claim, now=self._clock(), ttl_seconds=ttl
        )
        if result.status is not PushStatus.PUSHED or result.claim is None:
            ctx.claim = None
            raise _Stop("claim_lost", detail=result.detail)
        ctx.claim = result.claim

    def release(self, ctx: _Attempt) -> None:
        if ctx.claim is None:
            return
        try:
            self._claims.release(self._root, ctx.claim, now=self._clock())
        except Exception as error:  # noqa: BLE001 - unreleased claim only expires later
            logger.warning("swallowed broad exception: %s", error)
            pass
        ctx.claim = None

    def promote_fix_output(self, ctx: _Attempt, *, fix_branch: str) -> PushResult:
        state = ctx.state
        assert ctx.claim is not None
        result = self._claims.promote(
            self._root,
            ctx.claim,
            temp_branch=ctx.pr.temp_branch,
            expected_head_sha=str(state.fix_input_sha),
            fix_branch=fix_branch,
            now=self._clock(),
        )
        if result.status is PushStatus.PUSHED and result.claim is not None:
            ctx.claim = result.claim
        return result


__all__ = [
    "CLAIM_TTL_SECONDS",
    "SemanticClaimClient",
    "SemanticReviewLease",
]

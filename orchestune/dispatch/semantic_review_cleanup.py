"""Post-cycle wiring of the invalidated-attempt cleanup (#1440).

Kept out of :mod:`orchestune.dispatch.postcycle_semantic_review`: the cleanup is
its own diagnostic, so a failure here is reported next to the review pass and
never changes how an attempt's review is judged.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.semantic_review_adapters import poll_semantic_operation
from orchestune.forge import Forge, ForgeAuthError
from orchestune.integrator.attempt_cleanup import (
    UNSETTLED_OPERATION_STATUSES,
    AttemptCleaner,
    CleanupClaims,
    RemoteRefPorts,
)
from orchestune.review.semantic_gate import SemanticReviewStateV1


def _settler(
    forge: Forge, repository_root: Path, poll: Callable[..., Any]
) -> Callable[[SemanticReviewStateV1, int], bool]:
    def settled(state: SemanticReviewStateV1, pr_number: int) -> bool:
        """The old job has stopped writing (polled; unknown is not settled)."""
        if state.operation_id is None or (
            state.operation_status not in UNSETTLED_OPERATION_STATUSES
        ):
            return True
        try:
            result = poll(
                forge,
                repository_root,
                pr_number=pr_number,
                attempt_id=state.attempt_id,
                operation_id=state.operation_id,
            )
        except ForgeAuthError:
            raise
        except Exception:  # noqa: BLE001
            return False
        return bool(result.status not in UNSETTLED_OPERATION_STATUSES)

    return settled


def run_attempt_cleanup(
    config: DispatcherConfig,
    *,
    forge: Forge,
    repository_root: Path,
    worker_id: str,
    claims: Any | None = None,
    poll: Callable[..., Any] | None = None,
    refs: RemoteRefPorts | None = None,
    clock: Callable[[], datetime] | None = None,
) -> list[dict[str, Any]]:
    """Close invalidated attempts of ``config``'s parent; never raises (but auth).

    ``claims`` (acquire/renew/release) and ``poll`` default to the real ones.
    """
    cleanup_claims = (
        CleanupClaims()
        if claims is None
        else CleanupClaims(claims.acquire, claims.renew, claims.release)
    )
    try:
        reports = AttemptCleaner(
            forge,
            repository_root,
            config.parent_issue_number,
            apply=config.apply,
            holder=worker_id,
            clock=clock or (lambda: datetime.now(UTC)),
            refs=refs,
            claims=cleanup_claims,
            operation_settled=_settler(
                forge, repository_root, poll or poll_semantic_operation
            ),
        ).run()
    except ForgeAuthError:
        raise
    except Exception as error:  # noqa: BLE001 - a separate diagnostic
        return [{"status": "failed", "detail": f"{type(error).__name__}: {error}"}]
    return [report.to_dict() for report in reports]

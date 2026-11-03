"""Shared semantic-review state fixtures.

Both the record tests and the state-machine tests build states through
``make_state`` and ``retry_req``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from orchestune.review.semantic_gate import (
    IntegrationAttemptIdentity,
    SemanticOperationKind,
    SemanticOperationStatus,
    SemanticRetryKind,
    SemanticReviewPhase,
    SemanticReviewRetryV1,
    SemanticReviewStateV1,
)

P = SemanticReviewPhase
OK = SemanticOperationStatus
KIND = SemanticOperationKind
BASE = "b" * 40
HEAD = "a" * 40
FIXED = "c" * 40
OTHER = "d" * 40
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
EARLIER = "2026-10-18T11:59:00Z"
CHILDREN = [(12, "beta", "2" * 40), (11, "alpha", "1" * 40)]
IDENTITY = IntegrationAttemptIdentity.create(1033, BASE, CHILDREN)


def make_state(**changes: Any) -> SemanticReviewStateV1:
    base = SemanticReviewStateV1(
        attempt_id=IDENTITY.attempt_id,
        parent_issue_number=1033,
        pr_number=77,
        phase=P.NOT_STARTED,
        round=0,
        head_sha=HEAD,
        base_sha=BASE,
        child_sources=IDENTITY.child_sources,
    )
    return replace(base, **changes)


def retry_req(
    kind: SemanticRetryKind = "retry", request_id: str = "req-1", **c: Any
) -> SemanticReviewRetryV1:
    return SemanticReviewRetryV1(
        IDENTITY.attempt_id, HEAD, request_id, kind, "config fixed", **c
    )

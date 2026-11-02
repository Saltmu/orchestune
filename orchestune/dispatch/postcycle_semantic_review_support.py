"""Support helpers for advance_pending_semantic_reviews to avoid module bloat and cycles."""

from __future__ import annotations

import os
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.semantic_review_ci import (
    RemoteHeadFn,
    SemanticCiObservation,
    verify_semantic_ci,
)
from orchestune.dispatch.semantic_review_cleanup import run_attempt_cleanup
from orchestune.integrator.attempt_cleanup import RemoteRefPorts

VerifyCiFn = Callable[[str, str], SemanticCiObservation | None]


def default_worker_id() -> str:
    return f"semantic-runner@{socket.gethostname()}:{os.getpid()}"


def semantic_ci_verifier(
    config: DispatcherConfig,
    repository_root: Path,
    remote_head_fn: RemoteHeadFn | None = None,
) -> VerifyCiFn:
    def verify(
        temp_branch: str, expected_head_sha: str
    ) -> SemanticCiObservation | None:
        return verify_semantic_ci(
            temp_branch,
            expected_head_sha,
            config=config,
            repository_root=repository_root,
            remote_head_fn=remote_head_fn,
        )

    return verify


def run_cycle_cleanup(
    config: DispatcherConfig,
    *,
    repository_root: Path,
    worker_id: str | None = None,
    claims: Any = None,
    poll: Any = None,
    refs: RemoteRefPorts | None = None,
) -> list[dict[str, Any]]:
    return run_attempt_cleanup(
        config,
        forge=config.resolved_forge,
        repository_root=repository_root,
        worker_id=worker_id or default_worker_id(),
        claims=claims,
        poll=poll,
        refs=refs,
    )

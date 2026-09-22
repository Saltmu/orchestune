"""Fail-closed extension points for local-CI evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CiEvidence:
    """Placeholder for versioned evidence bound to a completion request."""

    head_sha: str
    base_sha: str
    succeeded: bool


def validate_ci_evidence(request: Any) -> CiEvidence:
    """Validate saved local-CI evidence for a completion request."""
    raise NotImplementedError("complete CI evidence validation is not implemented")


def run_local_ci_if_needed(request: Any) -> CiEvidence:
    """Run local CI when valid evidence is unavailable."""
    raise NotImplementedError("complete local-CI execution is not implemented")

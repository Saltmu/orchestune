"""Fail-closed extension point for canonical Issue outcome posting."""

from __future__ import annotations

from typing import Any


def post_issue_outcome(request: Any) -> str:
    """Post an idempotent completion Outcome Record to the Issue."""
    raise NotImplementedError("complete outcome posting is not implemented")

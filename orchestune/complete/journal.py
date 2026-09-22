"""Fail-closed extension points for durable completion journaling."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CompletionJournal:
    """Placeholder identity for an in-progress completion."""

    completion_id: str
    issue_number: int


def reserve_completion(request: Any) -> CompletionJournal:
    """Reserve a completion under the claim's state lock."""
    raise NotImplementedError("completion reservation is not implemented")


def mark_handoff_ready(journal: Any) -> CompletionJournal:
    """Mark a posted completion ready for GC handoff."""
    raise NotImplementedError("completion handoff is not implemented")

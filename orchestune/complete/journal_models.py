"""Compatibility exports for persisted completion records.

The completion journal DTO remains here because it describes an input/output
operation. Values persisted in RunState are owned by the ledger package.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orchestune.complete.contracts import (
    CompleteStage,
    CompletionLabelStatus,
    DownstreamPolicyRecord,
)
from orchestune.ledger.completion_records import (
    CompletionJournalRecord,
    CompletionReplayReceipt,
    CompletionReservation,
    InvalidLedgerRecord,
)


@dataclass(frozen=True)
class CompletionJournal:
    """Durable identity and evidence for one completion attempt."""

    issue_number: int
    claim_id: str
    completion_id: str
    result: str
    stage: str
    payload: dict[str, Any] | None = None
    comment_id: str | None = None
    comment_url: str | None = None
    handoff_ready: bool = False


__all__ = [
    "CompleteStage",
    "CompletionJournal",
    "CompletionJournalRecord",
    "CompletionLabelStatus",
    "CompletionReplayReceipt",
    "CompletionReservation",
    "DownstreamPolicyRecord",
    "InvalidLedgerRecord",
]

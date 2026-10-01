"""Requests and safe diagnostics for local operator recovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RecoveryRequest:
    issue_number: int
    claim_id: str | None = None
    reason: str | None = None
    apply: bool = False
    restore_marker: bool = False
    state_path: Path | None = None
    timeout_seconds: float = 0.0

    def validate(self) -> None:
        if self.issue_number <= 0 or self.timeout_seconds < 0:
            raise ValueError("Issue must be positive and timeout nonnegative")
        if self.apply and (
            not self.claim_id or not self.reason or not self.reason.strip()
        ):
            raise ValueError("Apply requires --claim-id and --reason")


@dataclass(frozen=True)
class RecoveryResult:
    success: bool
    issue_number: int
    action: str
    reason: str
    diagnostics: dict[str, Any] = field(default_factory=dict)

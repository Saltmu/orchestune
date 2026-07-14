from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PhaseStatus(str, Enum):
    SUCCESS = "success"
    WARNING = "warning"
    RETRYABLE_FAILURE = "retryable_failure"
    FATAL_FAILURE = "fatal_failure"


@dataclass
class PhaseResult:
    phase_name: str
    status: PhaseStatus
    report: dict | None = None
    error_message: str | None = None
    retryable: bool = False

    def to_dict(self) -> dict:
        return {
            "phase_name": self.phase_name,
            "status": self.status.value,
            "report": self.report,
            "error_message": self.error_message,
            "retryable": self.retryable,
        }

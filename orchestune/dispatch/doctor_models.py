"""Pure result types and constants for ``orchestune doctor``."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

DiagnosticStatus = Literal["ok", "warning", "error", "not_checked"]
ExecutionMode = Literal["local", "actions"]

# Heavier statuses win when aggregating.
STATUS_SEVERITY: Mapping[str, int] = {
    "ok": 0,
    "not_checked": 1,
    "warning": 2,
    "error": 3,
}


def worst_status(statuses: Iterable[DiagnosticStatus]) -> DiagnosticStatus:
    """Return the heaviest status, or ``"ok"`` for an empty input."""
    worst: DiagnosticStatus = "ok"
    for status in statuses:
        if STATUS_SEVERITY[status] > STATUS_SEVERITY[worst]:
            worst = status
    return worst


CODE_CONFIG_READABLE = "dispatch.config.readable"
CODE_WORKFLOW_READABLE = "dispatch.workflow.readable"
CODE_ACTIONS_GROUP = "dispatch.actions.group"
CODE_ACTIONS_CANCEL = "dispatch.actions.cancel"
CODE_ACTIONS_PARALLELISM = "dispatch.actions.parallelism"
CODE_ACTIONS_ENTRYPOINT = "dispatch.actions.entrypoint"
CODE_ACTIONS_TARGET = "dispatch.actions.target"
CODE_ACTIONS_CREDENTIALS = "dispatch.actions.credentials"
CODE_LOCAL_SERIALIZATION = "dispatch.local.serialization"
CODE_OTHER_ENTRYPOINTS = "dispatch.repository.other_entrypoints"
CODE_OTHER_CONTROL_ENTRYPOINTS = "dispatch.repository.other_control_entrypoints"
CODE_EXTERNAL_OWNERSHIP = "dispatch.external_ownership"
CODE_STATE_CONTINUITY = "dispatch.state.continuity"

ALL_CODES: tuple[str, ...] = (
    CODE_CONFIG_READABLE,
    CODE_WORKFLOW_READABLE,
    CODE_ACTIONS_GROUP,
    CODE_ACTIONS_CANCEL,
    CODE_ACTIONS_PARALLELISM,
    CODE_ACTIONS_ENTRYPOINT,
    CODE_ACTIONS_TARGET,
    CODE_ACTIONS_CREDENTIALS,
    CODE_LOCAL_SERIALIZATION,
    CODE_OTHER_ENTRYPOINTS,
    CODE_OTHER_CONTROL_ENTRYPOINTS,
    CODE_EXTERNAL_OWNERSHIP,
    CODE_STATE_CONTINUITY,
)

# Ownership/state items never decide ``configuration_status``.
OWNERSHIP_CODES = frozenset(
    {CODE_LOCAL_SERIALIZATION, CODE_EXTERNAL_OWNERSHIP, CODE_STATE_CONTINUITY}
)

OWNERSHIP_NOTICE = (
    "設定診断は運用所有権の保証ではありません。"
    " / Configuration diagnostics do not guarantee operational ownership."
)


@dataclass(frozen=True)
class Diagnostic:
    code: str
    status: DiagnosticStatus
    message: str
    evidence: tuple[str, ...] = ()
    remediation: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "status": self.status,
            "message": self.message,
            "evidence": list(self.evidence),
            "remediation": self.remediation,
        }


@dataclass(frozen=True)
class WorkflowFile:
    path: str  # repository-root-relative POSIX path
    document: Mapping[str, Any] | None
    error: str | None


@dataclass(frozen=True)
class DoctorContext:
    mode: ExecutionMode
    specified: tuple[WorkflowFile, ...]
    discovered: tuple[WorkflowFile, ...]
    config: Mapping[str, Any]
    config_valid: bool


@dataclass(frozen=True)
class DoctorReport:
    mode: ExecutionMode
    diagnostics: tuple[Diagnostic, ...]

    @property
    def has_error(self) -> bool:
        return any(d.status == "error" for d in self.diagnostics)

    @property
    def configuration_status(self) -> Literal["valid", "invalid", "unverified"]:
        if self.has_error:
            return "invalid"
        if any(
            d.status == "not_checked" and d.code not in OWNERSHIP_CODES
            for d in self.diagnostics
        ):
            return "unverified"
        return "valid"

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "mode": self.mode,
            "configuration_status": self.configuration_status,
            "ownership_status": "unverified",
            "notice": OWNERSHIP_NOTICE,
            "diagnostics": [d.to_json() for d in self.diagnostics],
        }

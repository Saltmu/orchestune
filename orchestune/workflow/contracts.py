"""L0 data contracts for advisory workflow observations; never execution state."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any

GATES = ("pre-claim", "pre-edit", "pre-pr", "pre-complete")


class WorkflowExitCode(IntEnum):
    """Command-local results; task lifecycle failure codes remain unchanged."""

    READY = 0
    UNMET = 2
    WAITING_HUMAN = 3
    TERMINAL_BLOCKED = 4


@dataclass(frozen=True)
class WorkflowCheck:
    """A structural/mechanical condition, with its observed source and gaps."""

    id: str
    status: str
    evidence: tuple[dict[str, Any], ...] = ()
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkflowResult:
    """Version-one public output shared by workflow check and future next."""

    issue_number: int
    claim_id: str | None
    stage_id: str
    gate_id: str | None
    status: str
    reason_codes: tuple[str, ...]
    checks: tuple[WorkflowCheck, ...]
    actions: tuple[dict[str, Any], ...]
    references: tuple[str, ...]
    artifacts: dict[str, Any]
    observed_head_sha: str | None
    schema_version: int = 1

    @property
    def exit_code(self) -> WorkflowExitCode:
        return WorkflowExitCode[self.status.upper()]

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-compatible data, retaining explicit nullable fields."""
        return asdict(self)


@dataclass
class WorkflowContext:
    """One invocation's read-only evidence; Any DTOs avoid L0 upward imports.

    The reader supplies established Issue/PrRecord, ActiveWorktree and RunState
    objects plus a get/list-only Forge view. No field is persisted by checks.
    """

    issue_number: int
    session_dir: Path
    root: Path
    repository: str = ""
    repository_identity: str = ""
    state: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    issue: Any = None
    active: Any = None
    ledger: Any = None
    state_path: Path | None = None
    head_sha: str | None = None
    branch: str | None = None
    worktree_status: str = "unknown"
    dependencies_ready: bool | None = None
    forge: Any = None
    pr_number: int | None = None
    pr: Any = None
    pr_body: str | None = None
    pr_body_path: Path | None = None
    review_reply: Path | None = None

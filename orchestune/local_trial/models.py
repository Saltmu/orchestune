"""DTOs, enums, and constants for local trial execution (#1734)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

SCHEMA_VERSION: int = 1
MAX_CYCLES: int = 3
MAX_REVIEWS: int = 3
AGENT_TIMEOUT_SECONDS: float = 1800.0
CI_TIMEOUT_SECONDS: float = 900.0
MAX_ARTIFACT_BYTES: int = 262144
PROCESS_TAIL_BYTES: int = 1048576
MAX_CONTEXT_BYTES: int = 1048576

type JsonValue = None | bool | int | float | str | list[Any] | dict[str, Any]


class AttemptKind(StrEnum):
    PLAN = "plan"
    IMPLEMENT = "implement"
    FIX = "fix"
    CI = "ci"
    REVIEW = "review"


class TrialPhase(StrEnum):
    PREPARING = "preparing"
    PLANNING = "planning"
    PLANNED = "planned"
    IMPLEMENTING = "implementing"
    FIXING = "fixing"
    VALIDATING = "validating"
    REVIEWING = "reviewing"
    READY = "ready"
    NEEDS_ATTENTION = "needs_attention"
    CLOSED = "closed"


class Checkpoint(StrEnum):
    PREPARE = "prepare"
    PLAN = "plan"
    AWAIT_APPROVAL = "await_approval"
    IMPLEMENT = "implement"
    COMMIT = "commit"
    CI = "ci"
    REVIEW_PREPARE = "review_prepare"
    REVIEW = "review"
    ASSESS = "assess"
    DECISION = "decision"
    CLOSED = "closed"


class ProcessStatus(StrEnum):
    SUCCESS = "success"
    NONZERO_EXIT = "nonzero_exit"
    TIMED_OUT = "timed_out"
    START_FAILED = "start_failed"
    STOP_UNCONFIRMED = "stop_unconfirmed"
    INTERRUPTED = "interrupted"


class DisciplineStatus(StrEnum):
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"
    UNVERIFIED = "unverified"


class FindingSeverity(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FindingAction(StrEnum):
    FIXED = "fixed"
    DECLINED = "declined"


class ReviewVerdict(StrEnum):
    PASS = "pass"
    CHANGES_REQUESTED = "changes_requested"


class GitOperation(StrEnum):
    PREPARE = "prepare"
    PREPARE_REVIEW = "prepare_review"
    COMMIT = "commit"


class DecisionValue(StrEnum):
    KEEP = "keep"
    ADOPTED = "adopted"


@dataclass(frozen=True, kw_only=True)
class RepositoryIdentity:
    source_root: str
    primary_root: str
    common_dir: str
    base_ref: str
    base_sha: str


@dataclass(frozen=True, kw_only=True)
class WorkspaceIdentity:
    implementation_path: str
    review_path: str
    branch: str


@dataclass(frozen=True, kw_only=True)
class RequestSnapshot:
    text: str
    digest: str
    ci_argv_override: tuple[str, ...] | None = None


@dataclass(frozen=True, kw_only=True)
class AgentInfo:
    name: Literal["claude", "codex"] | str
    executable: str
    version: str


@dataclass(frozen=True, kw_only=True)
class TrialPlan:
    goal: str
    acceptance_criteria: tuple[str, ...]
    write_paths: tuple[str, ...]
    discipline_paths: tuple[str, ...]
    discipline_notes: tuple[str, ...]
    ci_argv: tuple[str, ...] | None
    discipline_status: (
        DisciplineStatus | Literal["compatible", "incompatible", "unverified"]
    )


@dataclass(frozen=True, kw_only=True)
class Finding:
    id: str
    severity: FindingSeverity | Literal["high", "medium", "low"]
    path: str
    line: int | None
    reason: str
    requested_change: str


@dataclass(frozen=True, kw_only=True)
class FindingResponse:
    finding_id: str
    action: FindingAction | Literal["fixed", "declined"]
    explanation: str


@dataclass(frozen=True, kw_only=True)
class PlanPayload:
    plan: TrialPlan


@dataclass(frozen=True, kw_only=True)
class ImplementationPayload:
    summary: str
    responses: tuple[FindingResponse, ...]


@dataclass(frozen=True, kw_only=True)
class ReviewPayload:
    verdict: ReviewVerdict | Literal["pass", "changes_requested"]
    findings: tuple[Finding, ...]


@dataclass(frozen=True, kw_only=True)
class AgentPayload:
    schema_version: int
    trial_id: str
    attempt_id: str
    kind: AttemptKind
    input_head_sha: str
    plan_digest: str | None
    payload: PlanPayload | ImplementationPayload | ReviewPayload


@dataclass(frozen=True, kw_only=True)
class AttemptInput:
    trial_id: str
    attempt_id: str
    kind: AttemptKind
    input_head_sha: str
    base_sha: str
    plan_digest: str | None
    plan_revision: int
    cwd: str
    artifact_dir: str
    started_at: str


@dataclass(frozen=True, kw_only=True)
class ProcessRecord:
    outcome: ProcessStatus
    returncode: int | None
    stop_confirmed: bool | None
    elapsed_seconds: float
    timeout_seconds: float
    stdout_tail: str
    stderr_tail: str
    detail: str


@dataclass(frozen=True, kw_only=True)
class AttemptResult:
    schema_version: int
    input: AttemptInput
    process: ProcessRecord
    agent_payload: AgentPayload | None
    ended_at: str


@dataclass(frozen=True, kw_only=True)
class Evidence:
    attempt_id: str
    base_sha: str
    head_sha: str
    plan_digest: str
    ci_argv_digest: str
    passed: bool


@dataclass(frozen=True, kw_only=True)
class Budget:
    plan_revision: int
    cycles_started: int
    reviews_started: int


@dataclass(frozen=True, kw_only=True)
class ControllerInfo:
    pid: int
    started_at: str
    token: str


@dataclass(frozen=True, kw_only=True)
class Change:
    status: str
    path: str
    old_path: str | None = None


@dataclass(frozen=True, kw_only=True)
class WorkspaceSnapshot:
    head_sha: str
    base_tip_sha: str
    index_digest: str
    content_digest: str
    branch: str | None
    changes: tuple[Change, ...]


@dataclass(frozen=True, kw_only=True)
class GitIntent:
    operation: GitOperation | Literal["prepare", "prepare_review", "commit"]
    pre_head_sha: str
    expected_tree_sha: str | None
    target_sha: str
    allowed_paths: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class Observation:
    started_at: str
    updated_at: str
    elapsed_seconds: float
    setup_steps: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class StopConfirmation:
    attempt_id: str
    recorded_at: str


@dataclass(frozen=True, kw_only=True)
class Decision:
    value: DecisionValue | Literal["keep", "adopted"]
    recorded_at: str


@dataclass(frozen=True, kw_only=True)
class TrialState:
    schema_version: int
    trial_id: str
    repository: RepositoryIdentity
    workspace: WorkspaceIdentity
    request: RequestSnapshot
    agent: AgentInfo
    plan: TrialPlan | None
    approved_plan_digest: str | None
    approved_worktree_digest: str | None
    approved_index_digest: str | None
    phase: TrialPhase
    checkpoint: Checkpoint
    reason: str | None
    revision: int
    budget: Budget
    head_sha: str
    pending_attempt: AttemptInput | None
    pending_git: GitIntent | None
    attempt_ids: tuple[str, ...]
    ci_evidence: Evidence | None
    review_evidence: Evidence | None
    findings: tuple[Finding, ...]
    responses: tuple[FindingResponse, ...]
    stop_confirmations: tuple[StopConfirmation, ...]
    controller: ControllerInfo | None
    observation: Observation
    decision: Decision | None


@dataclass(frozen=True, kw_only=True)
class AgentRequest:
    input: AttemptInput
    prompt: str
    schema: dict[str, JsonValue]
    context: dict[str, JsonValue]


@dataclass(frozen=True, kw_only=True)
class TrialView:
    state: TrialState | None
    effective_phase: TrialPhase
    reason: str | None
    diff: str
    attempts: tuple[AttemptResult, ...]
    next_actions: tuple[str, ...]
    worktree_digest: str | None
    ci_candidates: tuple[tuple[str, ...], ...]
    ready_is_current: bool

"""Active worktree flat DTO and immutable migration views.

The flat ``ActiveWorktree`` remains the in-memory compatibility DTO for this
migration step. Its ``core``, ``launch``, ``claim``, and ``completion`` views
are derived snapshots; they are never stored alongside the flat fields.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from types import MappingProxyType
from typing import Any


def _freeze_json(value: Any) -> Any:
    """Copy JSON-shaped values into deeply immutable equivalents."""
    if isinstance(value, Mapping):
        frozen = {
            key: _freeze_json(item)
            for key, item in value.items()
            if isinstance(key, str)
        }
        if len(frozen) != len(value):
            raise TypeError("JSON object keys must be strings")
        return MappingProxyType(frozen)
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, str | bool | int | float):
        return value
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _thaw_json(value: Any) -> Any:
    """Copy immutable view values back into JSON dict/list containers."""
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True)
class ActiveWorktreeCore:
    """Fields shared by every ActiveWorktree owner."""

    issue_number: int
    branch: str
    worktree_path: str
    declared_footprint: tuple[str, ...]
    base_branch: str = "origin/main"


@dataclass(frozen=True)
class LaunchInfo:
    """Dispatch process, launch, and execution-selection fields."""

    pid: int | None = None
    started_at: float | None = None
    recompute_count: int = 0
    forced_serial: bool = False
    external_id: str | None = None
    external_url: str | None = None
    estimated_tokens: int | None = None
    token_estimate_recorded: bool = False
    profile: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    selection_reason: str | None = None
    launch_attempt_id: str | None = None
    launch_phase: str | None = None


@dataclass(frozen=True)
class ClaimInfo:
    """Claim identity and reservation ownership fields."""

    owner_kind: str = "dispatch"
    claim_id: str | None = None
    claim_stage: str | None = None
    base_ref: str | None = None
    base_sha: str | None = None
    reservation_kind: str = "footprint"
    repository_id: str | None = None
    claimed_at: float | None = None
    owner_token_digest: str | None = None


@dataclass(frozen=True)
class ActiveCompletionJournal:
    """Completion fields on an active worktree (distinct from durable records)."""

    completion_id: str | None = None
    completion_result: str | None = None
    completion_stage: str | None = None
    completion_payload: Mapping[str, Any] | None = None
    completion_comment_id: str | None = None
    completion_comment_url: str | None = None
    completion_handoff_ready: bool = False
    completion_policy_config: Any | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "completion_payload", _freeze_json(self.completion_payload)
        )
        object.__setattr__(
            self,
            "completion_policy_config",
            _freeze_json(self.completion_policy_config),
        )


def _validate_record_groups(
    core: object, launch: object, claim: object, completion: object
) -> None:
    for name, value, expected in (
        ("core", core, ActiveWorktreeCore),
        ("launch", launch, LaunchInfo),
        ("claim", claim, ClaimInfo),
        ("completion", completion, ActiveCompletionJournal),
    ):
        if not isinstance(value, expected):
            raise TypeError(f"{name} must be {expected.__name__}")


def _validate_core(core: ActiveWorktreeCore) -> None:
    if (
        isinstance(core.issue_number, bool)
        or not isinstance(core.issue_number, int)
        or core.issue_number <= 0
    ):
        raise ValueError("issue_number must be a positive integer")
    for name, value in (
        ("branch", core.branch),
        ("worktree_path", core.worktree_path),
        ("base_branch", core.base_branch),
    ):
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
    if not isinstance(core.declared_footprint, tuple) or not all(
        isinstance(path, str) for path in core.declared_footprint
    ):
        raise ValueError("declared_footprint must be a tuple of strings")


def _validate_completion(completion: ActiveCompletionJournal) -> None:
    for name, value in (
        ("completion_payload", completion.completion_payload),
        ("completion_policy_config", completion.completion_policy_config),
    ):
        if value is not None and not isinstance(value, Mapping):
            raise ValueError(f"{name} must be an object or null")


def _canonical_field_values(
    core: ActiveWorktreeCore,
    launch: LaunchInfo,
    claim: ClaimInfo,
    completion: ActiveCompletionJournal,
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for record in (core, launch, claim, completion):
        for record_field in fields(record):
            value = getattr(record, record_field.name)
            if isinstance(record, ActiveCompletionJournal):
                value = _thaw_json(value)
            values[record_field.name] = value
    return values


@dataclass
class ActiveWorktree:
    """The legacy flat ActiveWorktree DTO with immutable nested views.

    Keep this declaration order and its flat constructor until the final cutover.
    ``from_records`` is the canonical new construction path. ``decode_active_worktree``
    remains the compatibility path for already validated flat ledger input.
    """

    issue_number: int
    branch: str
    worktree_path: str
    pid: int | None
    started_at: float | None
    declared_footprint: tuple[str, ...]
    recompute_count: int = 0
    forced_serial: bool = False
    external_id: str | None = None
    external_url: str | None = None
    base_branch: str = "origin/main"
    estimated_tokens: int | None = None
    token_estimate_recorded: bool = False
    profile: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    selection_reason: str | None = None
    launch_attempt_id: str | None = None
    launch_phase: str | None = None
    owner_kind: str = "dispatch"
    claim_id: str | None = None
    claim_stage: str | None = None
    base_ref: str | None = None
    base_sha: str | None = None
    reservation_kind: str = "footprint"
    repository_id: str | None = None
    claimed_at: float | None = None
    owner_token_digest: str | None = None
    completion_id: str | None = None
    completion_result: str | None = None
    completion_stage: str | None = None
    completion_payload: dict[str, Any] | None = None
    completion_comment_id: str | None = None
    completion_comment_url: str | None = None
    completion_handoff_ready: bool = False
    completion_policy_config: dict[str, Any] | None = None

    @property
    def core(self) -> ActiveWorktreeCore:
        return ActiveWorktreeCore(
            issue_number=self.issue_number,
            branch=self.branch,
            worktree_path=self.worktree_path,
            declared_footprint=tuple(self.declared_footprint),
            base_branch=self.base_branch,
        )

    @property
    def launch(self) -> LaunchInfo:
        return LaunchInfo(
            pid=self.pid,
            started_at=self.started_at,
            recompute_count=self.recompute_count,
            forced_serial=self.forced_serial,
            external_id=self.external_id,
            external_url=self.external_url,
            estimated_tokens=self.estimated_tokens,
            token_estimate_recorded=self.token_estimate_recorded,
            profile=self.profile,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            selection_reason=self.selection_reason,
            launch_attempt_id=self.launch_attempt_id,
            launch_phase=self.launch_phase,
        )

    @property
    def claim(self) -> ClaimInfo:
        return ClaimInfo(
            owner_kind=self.owner_kind,
            claim_id=self.claim_id,
            claim_stage=self.claim_stage,
            base_ref=self.base_ref,
            base_sha=self.base_sha,
            reservation_kind=self.reservation_kind,
            repository_id=self.repository_id,
            claimed_at=self.claimed_at,
            owner_token_digest=self.owner_token_digest,
        )

    @property
    def completion(self) -> ActiveCompletionJournal:
        return ActiveCompletionJournal(
            completion_id=self.completion_id,
            completion_result=self.completion_result,
            completion_stage=self.completion_stage,
            completion_payload=self.completion_payload,
            completion_comment_id=self.completion_comment_id,
            completion_comment_url=self.completion_comment_url,
            completion_handoff_ready=self.completion_handoff_ready,
            completion_policy_config=self.completion_policy_config,
        )

    @classmethod
    def from_records(
        cls,
        *,
        core: ActiveWorktreeCore,
        launch: LaunchInfo,
        claim: ClaimInfo,
        completion: ActiveCompletionJournal,
    ) -> ActiveWorktree:
        """Build the compatibility DTO from typed records with canonical core values."""
        _validate_record_groups(core, launch, claim, completion)
        _validate_core(core)
        _validate_completion(completion)
        return cls(**_canonical_field_values(core, launch, claim, completion))

    def with_core(self, core: ActiveWorktreeCore) -> ActiveWorktree:
        """Return a copy with only common fields replaced through their boundary."""
        if not isinstance(core, ActiveWorktreeCore):
            raise TypeError("core must be ActiveWorktreeCore")
        _validate_core(core)
        return replace(
            self,
            issue_number=core.issue_number,
            branch=core.branch,
            worktree_path=core.worktree_path,
            declared_footprint=tuple(core.declared_footprint),
            base_branch=core.base_branch,
        )

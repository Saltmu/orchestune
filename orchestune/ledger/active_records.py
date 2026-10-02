"""Active worktree core and frozen owner subrecords.

``ActiveWorktree`` stores ``core`` and the ``launch``, ``claim``, and
``completion`` subrecords as its only in-memory representation. The flat
persisted JSON shape is handled by ``active_codec``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
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


@dataclass(slots=True)
class ActiveWorktree:
    """The in-memory ActiveWorktree: ``core`` plus frozen owner subrecords.

    The four subrecords are the only in-memory source of truth; there are no
    flat attribute aliases. The persisted flat JSON shape is owned by
    ``active_codec``. Subrecords are immutable: change one by assigning a
    ``dataclasses.replace`` copy to the matching attribute (or use the
    ``with_*`` copy helpers). ``slots=True`` makes a stale write to a removed
    flat attribute fail loudly instead of silently adding an ignored attribute.
    """

    core: ActiveWorktreeCore
    launch: LaunchInfo = field(default_factory=LaunchInfo)
    claim: ClaimInfo = field(default_factory=ClaimInfo)
    completion: ActiveCompletionJournal = field(default_factory=ActiveCompletionJournal)

    def __post_init__(self) -> None:
        _validate_record_groups(self.core, self.launch, self.claim, self.completion)

    @classmethod
    def from_records(
        cls,
        *,
        core: ActiveWorktreeCore,
        launch: LaunchInfo,
        claim: ClaimInfo,
        completion: ActiveCompletionJournal,
    ) -> ActiveWorktree:
        """Build an ActiveWorktree from typed records with validated core values."""
        _validate_record_groups(core, launch, claim, completion)
        _validate_core(core)
        _validate_completion(completion)
        return cls(core=core, launch=launch, claim=claim, completion=completion)

    def with_core(self, core: ActiveWorktreeCore) -> ActiveWorktree:
        """Return a copy with only common fields replaced through their boundary."""
        if not isinstance(core, ActiveWorktreeCore):
            raise TypeError("core must be ActiveWorktreeCore")
        _validate_core(core)
        return replace(self, core=core)

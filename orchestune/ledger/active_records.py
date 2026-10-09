"""Active worktree core and frozen owner subrecords.

``ActiveWorktree`` stores ``core`` and the ``launch``, ``claim``, and
``completion`` subrecords as its only in-memory representation. The flat
persisted JSON shape is handled by ``active_codec``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import InitVar, dataclass, field, replace
from hashlib import sha256
from math import isfinite
from types import MappingProxyType
from typing import Any

from orchestune.ownership_contracts import ClaimStage, OwnerKind, ReservationKind

LAUNCH_PHASES = frozenset({"prepared", "unknown", "launching", "launched", "failed"})
# Keep ledger independent of complete; these are persisted strings, not proof.
COMPLETION_STAGES = frozenset(
    {
        "initializing",
        "preflight_validating",
        "evidence_verifying",
        "journaling",
        "posting",
        "handed_off_to_gc",
        "reserved",
        "outcome_posted",
        "label_confirmed",
        "handed_off",
    }
)


def _strings(record: Any, names: tuple[str, ...]) -> None:
    for name in names:
        value = getattr(record, name)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{name} must be a string or null")


def _choice(name: str, value: Any, choices: set[str] | frozenset[str]) -> None:
    if value is not None and (not isinstance(value, str) or value not in choices):
        raise ValueError(f"{name} must be a known value")


def _finite(name: str, value: Any) -> None:
    if value is None:
        return
    try:
        valid = (
            not isinstance(value, bool)
            and isinstance(value, int | float)
            and isfinite(value)
        )
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be a finite number or null")


def validate_launch(record: Any) -> None:
    _strings(
        record,
        (
            "external_id",
            "external_url",
            "profile",
            "model",
            "reasoning_effort",
            "selection_reason",
            "launch_attempt_id",
            "launch_target",
            "launch_log_path",
        ),
    )
    _choice("launch_phase", record.launch_phase, LAUNCH_PHASES)
    _finite("started_at", record.started_at)
    for name in ("pid", "recompute_count", "estimated_tokens", "launch_log_offset"):
        value = getattr(record, name)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int)
        ):
            raise ValueError(f"{name} must be an integer or null")
        if name != "pid" and value is not None and value < 0:
            raise ValueError(f"{name} must be non-negative")
    if record.recompute_count is None:
        raise ValueError("recompute_count must be an integer")
    for name in ("forced_serial", "token_estimate_recorded"):
        if not isinstance(getattr(record, name), bool):
            raise ValueError(f"{name} must be a boolean")


def validate_claim(record: Any) -> None:
    _strings(
        record,
        ("claim_id", "base_ref", "base_sha", "repository_id", "owner_token_digest"),
    )
    for name, choices in (
        ("owner_kind", {item.value for item in OwnerKind}),
        ("claim_stage", {item.value for item in ClaimStage}),
        ("reservation_kind", {item.value for item in ReservationKind}),
    ):
        value = getattr(record, name)
        _choice(name, value, choices)
        if name != "claim_stage" and value is None:
            raise ValueError(f"{name} must be a known value")
    _finite("claimed_at", record.claimed_at)


def validate_completion(record: Any) -> None:
    _strings(
        record, ("completion_id", "completion_comment_id", "completion_comment_url")
    )
    _choice("completion_stage", record.completion_stage, COMPLETION_STAGES)
    _choice(
        "completion_result", record.completion_result, {"done", "blocked", "not-needed"}
    )
    if not isinstance(record.completion_handoff_ready, bool):
        raise ValueError("completion_handoff_ready must be a boolean")
    for name in ("completion_payload", "completion_policy_config"):
        value = getattr(record, name)
        if value is not None and not isinstance(value, Mapping):
            raise ValueError(f"{name} must be an object or null")
    if record.completion_id is None and (
        any(
            getattr(record, name) is not None
            for name in (
                "completion_stage",
                "completion_result",
                "completion_payload",
                "completion_comment_id",
                "completion_comment_url",
            )
        )
        or record.completion_handoff_ready
    ):
        raise ValueError("completion progress requires completion_id")


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
    # #1270: the target that actually ran and the append-log range of this run.
    # Legacy ledgers have none of them; such a run is "attribution unknown".
    launch_target: str | None = None
    launch_log_path: str | None = None
    launch_log_offset: int | None = None
    _legacy: InitVar[bool] = False

    def __post_init__(self, _legacy: bool) -> None:
        # InitVar is absent from fields/asdict/codec but replace copies its
        # instance value, keeping decoded legacy records compatible on update.
        object.__setattr__(self, "_legacy", _legacy)
        if not _legacy:
            self.validate()

    def validate(self) -> None:
        validate_launch(self)


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
    _legacy: InitVar[bool] = False

    def __post_init__(self, _legacy: bool) -> None:
        object.__setattr__(self, "_legacy", _legacy)
        if not _legacy:
            self.validate()

    def validate(self) -> None:
        validate_claim(self)


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
    _legacy: InitVar[bool] = False

    def __post_init__(self, _legacy: bool) -> None:
        object.__setattr__(self, "_legacy", _legacy)
        object.__setattr__(
            self, "completion_payload", _freeze_json(self.completion_payload)
        )
        object.__setattr__(
            self,
            "completion_policy_config",
            _freeze_json(self.completion_policy_config),
        )
        if not _legacy:
            self.validate()

    def validate(self) -> None:
        validate_completion(self)


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
        launch.validate()
        claim.validate()
        completion.validate()
        # A canonical factory must not carry the codec's legacy opt-out into
        # subsequent replacements of otherwise valid decoded records.
        if getattr(launch, "_legacy", False):
            launch = replace(launch, _legacy=False)
        if getattr(claim, "_legacy", False):
            claim = replace(claim, _legacy=False)
        if getattr(completion, "_legacy", False):
            completion = replace(completion, _legacy=False)
        return cls(core=core, launch=launch, claim=claim, completion=completion)

    def with_core(self, core: ActiveWorktreeCore) -> ActiveWorktree:
        """Return a copy with only common fields replaced through their boundary."""
        if not isinstance(core, ActiveWorktreeCore):
            raise TypeError("core must be ActiveWorktreeCore")
        _validate_core(core)
        return replace(self, core=core)

    def update_core(self, core: ActiveWorktreeCore) -> None:
        """Apply a core update in place when callers retain the active identity."""
        if not isinstance(core, ActiveWorktreeCore):
            raise TypeError("core must be ActiveWorktreeCore")
        _validate_core(core)
        self.core = core

    def materialize_claim_for_persistence(self) -> None:
        """Make recovery identity explicit, preserving existing active identity."""
        claim = self.claim
        if claim.owner_kind not in {kind.value for kind in OwnerKind}:
            raise ValueError("active worktree owner_kind must be a known value")
        if claim.reservation_kind not in {kind.value for kind in ReservationKind}:
            raise ValueError("active worktree reservation_kind must be a known value")
        claim_id = claim.claim_id
        if claim_id is None:
            claim_id = f"recovered-{self.core.issue_number}"
        started_at = self.launch.started_at
        self.claim = replace(
            claim,
            claim_id=claim_id,
            claim_stage=(
                ClaimStage.COMPLETED.value
                if claim.claim_stage is None
                else claim.claim_stage
            ),
            base_ref=self.core.base_branch
            if claim.base_ref is None
            else claim.base_ref,
            repository_id=(
                "unverified-recovery"
                if claim.repository_id is None
                else claim.repository_id
            ),
            claimed_at=(
                (started_at if started_at is not None else 0.0)
                if claim.claimed_at is None
                else claim.claimed_at
            ),
            owner_token_digest=(
                sha256(f"recovered-unverifiable:{claim_id}".encode()).hexdigest()
                if claim.owner_token_digest is None
                else claim.owner_token_digest
            ),
        )

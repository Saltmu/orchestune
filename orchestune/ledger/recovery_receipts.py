"""Typed, versioned recovery receipts persisted in ``run_state.json``."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import isfinite
from typing import Any, Literal, cast

from orchestune.ledger.active_codec import (
    decode_active_worktree,
    encode_active_worktree,
)
from orchestune.ledger.active_records import ActiveWorktree
from orchestune.ledger.completion_records import InvalidLedgerRecord

RELEASE_OPERATION = "release"
GC_RELEASE_OPERATION = "gc-release"
EXTERNAL_STOP_OPERATION = "external-stop-confirmation"
RESTART_BLOCKED_OPERATION = "restart-blocked"
ORPHANED_PR_OPERATION = "orphaned-pr"
RESTORE_MARKER_OPERATION = "restore-marker"
RECLAIM_PHASES = frozenset({"prepared", "active_saved", "completed"})
RESTART_PHASES = frozenset({"prepared", "restarted"})

_MISSING = object()


def recovery_receipt_key(
    repository_id: str, issue_number: int, claim_id: str | None, operation: str
) -> str:
    return f"{repository_id}::{issue_number}::{claim_id}::{operation}"


def gc_release_receipt_key(
    repository_id: str, issue_number: int, claim_id: str | None
) -> str:
    return recovery_receipt_key(
        repository_id, issue_number, claim_id, GC_RELEASE_OPERATION
    )


def legacy_active_snapshot(active: ActiveWorktree) -> dict[str, Any]:
    """Flat receipt snapshot; unlike the ledger, it keeps a null policy config."""
    snapshot = encode_active_worktree(active)
    snapshot.setdefault("completion_policy_config", None)
    return snapshot


def _preserved_json(
    source: dict[str, Any] | None,
    original: dict[str, Any] | None,
    current: dict[str, Any],
) -> dict[str, Any]:
    if source is None or original is None:
        return current
    result = deepcopy(source)
    for key, value in current.items():
        if key in result or original.get(key, _MISSING) != value:
            result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class RecoveryExecutionIdentity:
    repository_id: str
    issue_number: int
    claim_id: str
    owner_token_digest: str | None
    claimed_at: float | None
    branch: str
    launch_attempt_id: str | None
    external_id: str
    started_at: float | None

    @classmethod
    def from_json(cls, raw: Any) -> RecoveryExecutionIdentity:
        if not isinstance(raw, dict):
            raise ValueError("execution_identity must be an object")
        names = (
            "repository_id",
            "issue_number",
            "claim_id",
            "owner_token_digest",
            "claimed_at",
            "branch",
            "launch_attempt_id",
            "external_id",
            "started_at",
        )
        if set(raw) != set(names):
            raise ValueError("execution_identity fields are invalid")
        for name in ("repository_id", "claim_id", "branch", "external_id"):
            if not isinstance(raw[name], str) or not raw[name].strip():
                raise ValueError("execution identity text is invalid")
        number = raw["issue_number"]
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            raise ValueError("execution identity issue_number is invalid")
        for name in ("owner_token_digest", "launch_attempt_id"):
            if raw[name] is not None and (
                not isinstance(raw[name], str) or not raw[name].strip()
            ):
                raise ValueError(f"execution identity {name} is invalid")
        times: dict[str, float | None] = {}
        for name in ("claimed_at", "started_at"):
            value = raw[name]
            if value is not None and (
                type(value) not in (int, float) or not isfinite(value)
            ):
                raise ValueError(f"execution identity {name} is invalid")
            times[name] = float(value) if value is not None else None
        return cls(
            repository_id=raw["repository_id"],
            issue_number=number,
            claim_id=raw["claim_id"],
            owner_token_digest=raw["owner_token_digest"],
            claimed_at=times["claimed_at"],
            branch=raw["branch"],
            launch_attempt_id=raw["launch_attempt_id"],
            external_id=raw["external_id"],
            started_at=times["started_at"],
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "repository_id": self.repository_id,
            "issue_number": self.issue_number,
            "claim_id": self.claim_id,
            "owner_token_digest": self.owner_token_digest,
            "claimed_at": self.claimed_at,
            "branch": self.branch,
            "launch_attempt_id": self.launch_attempt_id,
            "external_id": self.external_id,
            "started_at": self.started_at,
        }


@dataclass(frozen=True, slots=True)
class RecoveryActiveSnapshot:
    """Typed active-worktree snapshot with codec-only source JSON preservation."""

    value: ActiveWorktree
    _source_json: dict[str, Any] = field(repr=False, compare=False)
    _original_value: ActiveWorktree = field(repr=False, compare=False)

    @classmethod
    def from_json(cls, raw: Any) -> RecoveryActiveSnapshot:
        if not isinstance(raw, dict):
            raise ValueError("active snapshot must be an object")
        value = decode_active_worktree(raw)
        return cls(value, deepcopy(raw), value)

    def to_json(self) -> dict[str, Any]:
        if self.value == self._original_value:
            return deepcopy(self._source_json)
        return _preserved_json(
            self._source_json,
            self._source_json,
            legacy_active_snapshot(self.value),
        )


@dataclass(frozen=True, slots=True)
class RecoveryReceiptBase(ABC):
    schema_version: int
    repository_id: str
    issue_number: int
    claim_id: str
    operation: str
    _codec_source_json: dict[str, Any] | None = field(
        default=None, repr=False, compare=False, kw_only=True
    )
    _codec_original_fields: dict[str, Any] | None = field(
        default=None, repr=False, compare=False, kw_only=True
    )

    def _base_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "repository_id": self.repository_id,
            "issue_number": self.issue_number,
            "claim_id": self.claim_id,
            "operation": self.operation,
        }

    @abstractmethod
    def to_dict(self) -> dict[str, Any]:
        """Encode the typed receipt at the run_state.json boundary."""


@dataclass(frozen=True, slots=True)
class ExternalStopConfirmation(RecoveryReceiptBase):
    source: str
    confirmed_external_stopped: bool
    reason: str
    recorded_at: str
    launch_attempt_id: str | None
    external_id: str
    execution_identity: RecoveryExecutionIdentity
    active: RecoveryActiveSnapshot
    worktree_action: str

    def to_dict(self) -> dict[str, Any]:
        current = {
            **self._base_json(),
            "source": self.source,
            "confirmed_external_stopped": self.confirmed_external_stopped,
            "reason": self.reason,
            "recorded_at": self.recorded_at,
            "launch_attempt_id": self.launch_attempt_id,
            "external_id": self.external_id,
            "execution_identity": self.execution_identity.to_json(),
            "active": self.active.to_json(),
            "worktree_action": self.worktree_action,
        }
        return _preserved_json(
            self._codec_source_json, self._codec_original_fields, current
        )


@dataclass(frozen=True, slots=True)
class ReleaseReceipt(RecoveryReceiptBase):
    reason: str | None
    recorded_at: str
    worktree_action: str
    active: RecoveryActiveSnapshot
    confirmation_receipt_key: str | None = None
    source: str | None = None
    confirmed_external_stopped: bool | None = None
    launch_attempt_id: str | None = None
    external_id: str | None = None
    execution_identity: RecoveryExecutionIdentity | None = None

    def to_dict(self) -> dict[str, Any]:
        current: dict[str, Any] = {
            **self._base_json(),
            "reason": self.reason,
            "recorded_at": self.recorded_at,
            "worktree_action": self.worktree_action,
            "active": self.active.to_json(),
        }
        optional = {
            "confirmation_receipt_key": self.confirmation_receipt_key,
            "source": self.source,
            "confirmed_external_stopped": self.confirmed_external_stopped,
            "launch_attempt_id": self.launch_attempt_id,
            "external_id": self.external_id,
            "execution_identity": self.execution_identity.to_json()
            if self.execution_identity
            else None,
        }
        for key, value in optional.items():
            if value is not None:
                current[key] = value
        return _preserved_json(
            self._codec_source_json, self._codec_original_fields, current
        )


@dataclass(frozen=True, slots=True)
class RestoreMarkerReceipt(RecoveryReceiptBase):
    reason: str | None
    recorded_at: str
    worktree_action: str
    active: RecoveryActiveSnapshot

    def to_dict(self) -> dict[str, Any]:
        current = {
            **self._base_json(),
            "reason": self.reason,
            "recorded_at": self.recorded_at,
            "worktree_action": self.worktree_action,
            "active": self.active.to_json(),
        }
        return _preserved_json(
            self._codec_source_json, self._codec_original_fields, current
        )


@dataclass(frozen=True, slots=True)
class ReclaimReceipt:
    new_claim_id: str
    phase: Literal["prepared", "active_saved", "completed"]
    recorded_at: str | None
    superseded_claim_ids: tuple[str, ...] = ()
    _codec_source_json: dict[str, Any] | None = field(
        default=None, repr=False, compare=False, kw_only=True
    )
    _codec_original_fields: dict[str, Any] | None = field(
        default=None, repr=False, compare=False, kw_only=True
    )

    def to_dict(self) -> dict[str, Any]:
        current: dict[str, Any] = {
            "new_claim_id": self.new_claim_id,
            "phase": self.phase,
            "superseded_claim_ids": list(self.superseded_claim_ids),
        }
        if self.recorded_at is not None:
            current["recorded_at"] = self.recorded_at
        return _preserved_json(
            self._codec_source_json, self._codec_original_fields, current
        )


@dataclass(frozen=True, slots=True)
class GcReleaseReceipt(RecoveryReceiptBase):
    completion_id: str | None
    completion_result: str | None
    branch: str
    worktree_path: str
    base_sha: str | None
    worktree_action: str
    stop_confirmed: bool
    recorded_at: str
    active: RecoveryActiveSnapshot
    reclaim: ReclaimReceipt | None = None

    def to_dict(self) -> dict[str, Any]:
        current: dict[str, Any] = {
            **self._base_json(),
            "branch": self.branch,
            "worktree_path": self.worktree_path,
            "worktree_action": self.worktree_action,
            "stop_confirmed": self.stop_confirmed,
            "recorded_at": self.recorded_at,
            "active": self.active.to_json(),
        }
        for key, value in (
            ("completion_id", self.completion_id),
            ("completion_result", self.completion_result),
            ("base_sha", self.base_sha),
        ):
            if value is not None:
                current[key] = value
        if self.reclaim is not None:
            current["reclaim"] = self.reclaim.to_dict()
        return _preserved_json(
            self._codec_source_json, self._codec_original_fields, current
        )


@dataclass(frozen=True, slots=True)
class RestartBlockedReceipt(RecoveryReceiptBase):
    phase: Literal["prepared", "restarted"]
    reason: str
    new_claim_id: str
    recorded_at: str
    worktree_action: str
    active: RecoveryActiveSnapshot
    replacement: RecoveryActiveSnapshot

    def to_dict(self) -> dict[str, Any]:
        current = {
            **self._base_json(),
            "phase": self.phase,
            "reason": self.reason,
            "new_claim_id": self.new_claim_id,
            "recorded_at": self.recorded_at,
            "worktree_action": self.worktree_action,
            "active": self.active.to_json(),
            "replacement": self.replacement.to_json(),
        }
        return _preserved_json(
            self._codec_source_json, self._codec_original_fields, current
        )


@dataclass(frozen=True, slots=True)
class OrphanedPrReceipt(RecoveryReceiptBase):
    pr_number: int
    head_sha: str
    active: RecoveryActiveSnapshot

    def to_dict(self) -> dict[str, Any]:
        current = {
            **self._base_json(),
            "pr_number": self.pr_number,
            "head_sha": self.head_sha,
            "active": self.active.to_json(),
        }
        return _preserved_json(
            self._codec_source_json, self._codec_original_fields, current
        )


type RecoveryReceipt = (
    ExternalStopConfirmation
    | RestartBlockedReceipt
    | ReleaseReceipt
    | RestoreMarkerReceipt
    | GcReleaseReceipt
    | OrphanedPrReceipt
)
type RecoveryReceiptValue = RecoveryReceipt | InvalidLedgerRecord


def _required(raw: dict[str, Any], key: str, expected: type[Any]) -> Any:
    value = raw[key]
    valid = type(value) is expected if expected is bool else isinstance(value, expected)
    if not valid or (expected is int and isinstance(value, bool)):
        raise ValueError(f"{key} must be {expected.__name__}")
    return value


def _optional(raw: dict[str, Any], key: str, expected: type[Any]) -> Any | None:
    if key not in raw or raw[key] is None:
        return None
    return _required(raw, key, expected)


def _positive_issue(raw: dict[str, Any]) -> int:
    value = _required(raw, "issue_number", int)
    if value <= 0:
        raise ValueError("issue_number must be positive")
    return cast(int, value)


def _validated_recorded_at(raw: dict[str, Any]) -> str:
    value = _required(raw, "recorded_at", str)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("recorded_at must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("recorded_at must include a timezone")
    return cast(str, value)


def _validated_utc_recorded_at(raw: dict[str, Any]) -> str:
    value = _validated_recorded_at(raw)
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("external-stop recorded_at must be UTC")
    return value


def _decode_active(raw: dict[str, Any], key: str) -> RecoveryActiveSnapshot:
    return RecoveryActiveSnapshot.from_json(raw[key])


def _base(raw: dict[str, Any], operation: str) -> dict[str, Any]:
    version = _required(raw, "schema_version", int)
    if version != 1:
        raise ValueError("unsupported recovery receipt schema_version")
    repository_id = _required(raw, "repository_id", str)
    claim_id = _required(raw, "claim_id", str)
    if not repository_id.strip() or not claim_id.strip():
        raise ValueError("recovery receipt identity text is empty")
    return {
        "schema_version": version,
        "repository_id": repository_id,
        "issue_number": _positive_issue(raw),
        "claim_id": claim_id,
        "operation": operation,
    }


def _decode_external(raw: dict[str, Any]) -> ExternalStopConfirmation:
    base = _base(raw, EXTERNAL_STOP_OPERATION)
    identity = RecoveryExecutionIdentity.from_json(raw["execution_identity"])
    if (
        identity.repository_id != base["repository_id"]
        or identity.issue_number != base["issue_number"]
        or identity.claim_id != base["claim_id"]
    ):
        raise ValueError("external receipt identity differs")
    active = _decode_active(raw, "active")
    active_fields = encode_active_worktree(active.value)
    active_identity = RecoveryExecutionIdentity.from_json(
        {name: active_fields[name] for name in identity.to_json()}
    )
    if active_identity != identity:
        raise ValueError("external active snapshot identity differs")
    source = _required(raw, "source", str)
    stopped = _required(raw, "confirmed_external_stopped", bool)
    reason = _required(raw, "reason", str)
    external_id = _required(raw, "external_id", str)
    worktree_action = _required(raw, "worktree_action", str)
    if (
        source != "operator"
        or stopped is not True
        or not reason.strip()
        or not external_id.strip()
        or worktree_action != "retain"
    ):
        raise ValueError("external stop confirmation evidence is invalid")
    launch_attempt_id = _optional(raw, "launch_attempt_id", str)
    if (
        identity.launch_attempt_id != launch_attempt_id
        or identity.external_id != external_id
    ):
        raise ValueError("external receipt execution identity differs")
    return ExternalStopConfirmation(
        **base,
        source=source,
        confirmed_external_stopped=stopped,
        reason=reason,
        recorded_at=_validated_utc_recorded_at(raw),
        launch_attempt_id=launch_attempt_id,
        external_id=external_id,
        execution_identity=identity,
        active=active,
        worktree_action=worktree_action,
        _codec_source_json=deepcopy(raw),
        _codec_original_fields=deepcopy(raw),
    )


def _decode_release(raw: dict[str, Any]) -> ReleaseReceipt:
    base = _base(raw, RELEASE_OPERATION)
    identity_raw = raw.get("execution_identity")
    identity = (
        RecoveryExecutionIdentity.from_json(identity_raw)
        if identity_raw is not None
        else None
    )
    return ReleaseReceipt(
        **base,
        reason=_optional(raw, "reason", str),
        recorded_at=_validated_recorded_at(raw),
        worktree_action=_required(raw, "worktree_action", str),
        active=_decode_active(raw, "active"),
        confirmation_receipt_key=_optional(raw, "confirmation_receipt_key", str),
        source=_optional(raw, "source", str),
        confirmed_external_stopped=_optional(raw, "confirmed_external_stopped", bool),
        launch_attempt_id=_optional(raw, "launch_attempt_id", str),
        external_id=_optional(raw, "external_id", str),
        execution_identity=identity,
        _codec_source_json=deepcopy(raw),
        _codec_original_fields=deepcopy(raw),
    )


def _decode_restore_marker(raw: dict[str, Any]) -> RestoreMarkerReceipt:
    base = _base(raw, RESTORE_MARKER_OPERATION)
    return RestoreMarkerReceipt(
        **base,
        reason=_optional(raw, "reason", str),
        recorded_at=_validated_recorded_at(raw),
        worktree_action=_required(raw, "worktree_action", str),
        active=_decode_active(raw, "active"),
        _codec_source_json=deepcopy(raw),
        _codec_original_fields=deepcopy(raw),
    )


def _decode_reclaim(raw: Any) -> ReclaimReceipt | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("reclaim must be an object")
    phase = _required(raw, "phase", str)
    if phase not in RECLAIM_PHASES:
        raise ValueError("unknown reclaim phase")
    ids = raw.get("superseded_claim_ids", [])
    if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
        raise ValueError("superseded_claim_ids must be an array of strings")
    recorded_at = raw.get("recorded_at")
    if recorded_at is not None:
        recorded_at = _validated_recorded_at(raw)
    new_claim_id = _required(raw, "new_claim_id", str)
    if not new_claim_id.strip():
        raise ValueError("reclaim new_claim_id must be non-empty")
    return ReclaimReceipt(
        new_claim_id=new_claim_id,
        phase=phase,
        recorded_at=recorded_at,
        superseded_claim_ids=tuple(ids),
        _codec_source_json=deepcopy(raw),
        _codec_original_fields=deepcopy(raw),
    )


def _decode_gc(raw: dict[str, Any]) -> GcReleaseReceipt:
    base = _base(raw, GC_RELEASE_OPERATION)
    completion_id = raw.get("completion_id")
    completion_result = raw.get("completion_result")
    base_sha = raw.get("base_sha")
    for name, value in (
        ("completion_id", completion_id),
        ("completion_result", completion_result),
        ("base_sha", base_sha),
    ):
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{name} must be text or null")
    return GcReleaseReceipt(
        **base,
        completion_id=completion_id,
        completion_result=completion_result,
        branch=_required(raw, "branch", str),
        worktree_path=_required(raw, "worktree_path", str),
        base_sha=base_sha,
        worktree_action=_required(raw, "worktree_action", str),
        stop_confirmed=_required(raw, "stop_confirmed", bool),
        recorded_at=_validated_recorded_at(raw),
        active=_decode_active(raw, "active"),
        reclaim=_decode_reclaim(raw.get("reclaim")),
        _codec_source_json=deepcopy(raw),
        _codec_original_fields=deepcopy(raw),
    )


def _decode_restart(raw: dict[str, Any]) -> RestartBlockedReceipt:
    base = _base(raw, RESTART_BLOCKED_OPERATION)
    phase = _required(raw, "phase", str)
    if phase not in RESTART_PHASES:
        raise ValueError("unknown restart-blocked phase")
    reason = _required(raw, "reason", str)
    new_claim_id = _required(raw, "new_claim_id", str)
    if not reason.strip() or not new_claim_id.strip():
        raise ValueError("restart-blocked receipt text is empty")
    return RestartBlockedReceipt(
        **base,
        phase=phase,
        reason=reason,
        new_claim_id=new_claim_id,
        recorded_at=_validated_recorded_at(raw),
        worktree_action=_required(raw, "worktree_action", str),
        active=_decode_active(raw, "active"),
        replacement=_decode_active(raw, "replacement"),
        _codec_source_json=deepcopy(raw),
        _codec_original_fields=deepcopy(raw),
    )


def _decode_orphan(raw: dict[str, Any]) -> OrphanedPrReceipt:
    base = _base(raw, ORPHANED_PR_OPERATION)
    pr_number = _required(raw, "pr_number", int)
    if pr_number <= 0:
        raise ValueError("pr_number must be positive")
    return OrphanedPrReceipt(
        **base,
        pr_number=pr_number,
        head_sha=_required(raw, "head_sha", str),
        active=_decode_active(raw, "active"),
        _codec_source_json=deepcopy(raw),
        _codec_original_fields=deepcopy(raw),
    )


_DECODERS: dict[str, Callable[[dict[str, Any]], RecoveryReceipt]] = {
    EXTERNAL_STOP_OPERATION: _decode_external,
    RELEASE_OPERATION: _decode_release,
    RESTORE_MARKER_OPERATION: _decode_restore_marker,
    GC_RELEASE_OPERATION: _decode_gc,
    RESTART_BLOCKED_OPERATION: _decode_restart,
    ORPHANED_PR_OPERATION: _decode_orphan,
}


def decode_recovery_receipt(raw: Any) -> RecoveryReceiptValue:
    """Decode one record; semantic invalidity stays local to that entry."""
    if not isinstance(raw, dict):
        raise TypeError("recovery receipt entries must be objects")
    operation = raw.get("operation")
    decoder = _DECODERS.get(operation) if isinstance(operation, str) else None
    if decoder is None:
        return InvalidLedgerRecord("recovery_receipts", "unknown operation", raw)
    try:
        return decoder(raw)
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        return InvalidLedgerRecord("recovery_receipts", str(error), raw)


def encode_recovery_receipt(value: RecoveryReceiptValue) -> dict[str, Any]:
    if isinstance(value, InvalidLedgerRecord):
        return deepcopy(value.raw_json)
    if isinstance(value, RecoveryReceiptBase):
        return value.to_dict()
    raise TypeError(f"unsupported recovery receipt value: {type(value).__name__}")


def invalid_recovery_receipt_identity(
    value: RecoveryReceiptValue,
    *,
    operation: str,
    repository_id: str | None,
    issue_number: int,
    claim_id: str | None = None,
    attempt_id: str | None = None,
) -> bool:
    """Safely identify malformed evidence that must hold a matching operation."""
    if not isinstance(value, InvalidLedgerRecord):
        return False
    raw = value.raw_json
    return (
        raw.get("operation") == operation
        and (repository_id is None or raw.get("repository_id") == repository_id)
        and raw.get("issue_number") == issue_number
        and (claim_id is None or raw.get("claim_id") == claim_id)
        and (
            attempt_id is None
            or (
                isinstance(raw.get("active"), dict)
                and raw["active"].get("launch_attempt_id") == attempt_id
            )
        )
    )


def build_recovery_receipt(
    *,
    repository_id: str,
    issue_number: int,
    active: ActiveWorktree,
    operation: str,
    reason: str | None,
) -> tuple[str, ReleaseReceipt | RestoreMarkerReceipt]:
    """Build the typed operator/GC release value without leaving ledger types."""
    if operation not in {RELEASE_OPERATION, RESTORE_MARKER_OPERATION}:
        raise ValueError(f"unsupported recovery receipt operation: {operation!r}")
    claim_id = _required_active_claim_id(active)
    key = recovery_receipt_key(repository_id, issue_number, claim_id, operation)
    receipt_type = (
        ReleaseReceipt if operation == RELEASE_OPERATION else RestoreMarkerReceipt
    )
    receipt = receipt_type(
        schema_version=1,
        repository_id=repository_id,
        issue_number=issue_number,
        claim_id=claim_id,
        operation=operation,
        reason=reason,
        recorded_at=datetime.now(UTC).isoformat(),
        worktree_action="retain",
        active=RecoveryActiveSnapshot.from_json(legacy_active_snapshot(active)),
    )
    return key, receipt


def _required_active_claim_id(active: ActiveWorktree) -> str:
    claim_id = active.claim.claim_id
    if not isinstance(claim_id, str) or not claim_id.strip():
        raise ValueError("recovery receipt requires an active claim_id")
    return claim_id


def record_recovery_in_raw(
    raw: dict[str, Any],
    key: str,
    active: ActiveWorktree,
    *,
    repository_id: str,
    issue_number: int,
    operation: str,
    reason: str | None,
) -> None:
    """Codec-boundary compatibility helper for legacy raw state transactions."""
    if operation == RELEASE_OPERATION:
        del raw["active_worktrees"][key]
    receipt_key, receipt = build_recovery_receipt(
        repository_id=repository_id,
        issue_number=issue_number,
        active=active,
        operation=operation,
        reason=reason,
    )
    raw.setdefault("recovery_receipts", {})[receipt_key] = receipt.to_dict()


__all__ = [
    "EXTERNAL_STOP_OPERATION",
    "GC_RELEASE_OPERATION",
    "ORPHANED_PR_OPERATION",
    "RECLAIM_PHASES",
    "RELEASE_OPERATION",
    "RESTORE_MARKER_OPERATION",
    "RESTART_BLOCKED_OPERATION",
    "ExternalStopConfirmation",
    "GcReleaseReceipt",
    "OrphanedPrReceipt",
    "ReclaimReceipt",
    "RecoveryActiveSnapshot",
    "RecoveryExecutionIdentity",
    "RecoveryReceipt",
    "RecoveryReceiptBase",
    "RecoveryReceiptValue",
    "ReleaseReceipt",
    "RestoreMarkerReceipt",
    "RestartBlockedReceipt",
    "build_recovery_receipt",
    "decode_recovery_receipt",
    "encode_recovery_receipt",
    "gc_release_receipt_key",
    "invalid_recovery_receipt_identity",
    "legacy_active_snapshot",
    "record_recovery_in_raw",
    "recovery_receipt_key",
]

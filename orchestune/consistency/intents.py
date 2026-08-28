"""Atomic, restart-safe persistence for explicit transition intents."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from orchestune.consistency.models import (
    ConsistencyScope,
    DesiredFact,
    FactValue,
    IntentStatus,
    TransitionIntent,
)
from orchestune.infra.json_state import read_json_with_recovery, write_json_atomic

_SCHEMA_VERSION = 1
_LIVE_STATUSES = frozenset({IntentStatus.PLANNED, IntentStatus.APPLIED})
_ALLOWED_TRANSITIONS = {
    IntentStatus.PLANNED: frozenset(
        {IntentStatus.APPLIED, IntentStatus.FAILED, IntentStatus.EXPIRED}
    ),
    IntentStatus.APPLIED: frozenset(
        {IntentStatus.VERIFIED, IntentStatus.FAILED, IntentStatus.EXPIRED}
    ),
    IntentStatus.VERIFIED: frozenset(),
    IntentStatus.FAILED: frozenset(),
    IntentStatus.EXPIRED: frozenset(),
}


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _validate_fact_value(value: FactValue, field: str = "value") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{field} must contain only finite floats")
    if isinstance(value, tuple):
        for index, item in enumerate(value):
            _validate_fact_value(item, f"{field}[{index}]")
        return
    if value is not None and not isinstance(value, str | int | float | bool):
        raise ValueError(f"{field} contains an unsupported value")


def _validate_intent(intent: TransitionIntent) -> None:
    if not intent.intent_id:
        raise ValueError("intent_id must not be empty")
    if not intent.operation:
        raise ValueError("operation must not be empty")
    _require_aware(intent.created_at, "created_at")
    if intent.expires_at is not None:
        _require_aware(intent.expires_at, "expires_at")
        if intent.expires_at <= intent.created_at:
            raise ValueError("expires_at must be later than created_at")
    if any(not isinstance(item, str) for item in intent.diagnostics):
        raise ValueError("diagnostics must contain only strings")
    for change in intent.expected_changes:
        _validate_fact_value(change.value, f"expected change {change.name!r}")


def _fact_value_to_json(value: FactValue) -> Any:
    if isinstance(value, tuple):
        return [_fact_value_to_json(item) for item in value]
    return value


def _fact_value_from_json(value: Any, field: str) -> FactValue:
    if isinstance(value, list):
        return tuple(
            _fact_value_from_json(item, f"{field}[{index}]")
            for index, item in enumerate(value)
        )
    if value is None or isinstance(value, str | int | float | bool):
        parsed: FactValue = value
        _validate_fact_value(parsed, field)
        return parsed
    raise ValueError(f"{field} contains an unsupported value")


def _datetime_from_json(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 string") from exc
    _require_aware(parsed, field)
    return parsed


def _optional_datetime_from_json(value: Any, field: str) -> datetime | None:
    if value is None:
        return None
    return _datetime_from_json(value, field)


def _scope_from_json(value: Any, field: str) -> ConsistencyScope:
    try:
        return ConsistencyScope(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} is not a valid consistency scope") from exc


def _fact_to_json(fact: DesiredFact) -> dict[str, Any]:
    return {
        "name": fact.name,
        "value": _fact_value_to_json(fact.value),
        "scope": fact.scope.value,
        "subject_id": fact.subject_id,
        "reason": fact.reason,
    }


def _fact_from_json(raw: Any, index: int) -> DesiredFact:
    if not isinstance(raw, dict):
        raise ValueError(f"expected_changes[{index}] must be an object")
    prefix = f"expected_changes[{index}]"
    name = raw.get("name")
    subject_id = raw.get("subject_id")
    reason = raw.get("reason")
    if not isinstance(name, str) or not name:
        raise ValueError(f"{prefix}.name must be a non-empty string")
    if subject_id is not None and not isinstance(subject_id, str):
        raise ValueError(f"{prefix}.subject_id must be a string or null")
    if not isinstance(reason, str):
        raise ValueError(f"{prefix}.reason must be a string")
    if "value" not in raw:
        raise ValueError(f"{prefix}.value is required")
    return DesiredFact(
        name=name,
        value=_fact_value_from_json(raw["value"], f"{prefix}.value"),
        scope=_scope_from_json(raw.get("scope"), f"{prefix}.scope"),
        subject_id=subject_id,
        reason=reason,
    )


def _intent_to_json(intent: TransitionIntent) -> dict[str, Any]:
    _validate_intent(intent)
    return {
        "intent_id": intent.intent_id,
        "scope": intent.scope.value,
        "operation": intent.operation,
        "created_at": intent.created_at.isoformat(),
        "subject_id": intent.subject_id,
        "status": intent.status.value,
        "expires_at": (
            intent.expires_at.isoformat() if intent.expires_at is not None else None
        ),
        "expected_changes": [
            _fact_to_json(change) for change in intent.expected_changes
        ],
        "diagnostics": list(intent.diagnostics),
    }


def _strings_from_json(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")
    return tuple(value)


def _intent_from_json(raw: Any, index: int) -> TransitionIntent:
    if not isinstance(raw, dict):
        raise ValueError(f"intents[{index}] must be an object")
    prefix = f"intents[{index}]"
    try:
        intent_id = raw["intent_id"]
        operation = raw["operation"]
        subject_id = raw["subject_id"]
        changes = raw["expected_changes"]
    except KeyError as exc:
        raise ValueError(f"{prefix}.{exc.args[0]} is required") from exc
    if not isinstance(intent_id, str) or not intent_id:
        raise ValueError(f"{prefix}.intent_id must be a non-empty string")
    if not isinstance(operation, str) or not operation:
        raise ValueError(f"{prefix}.operation must be a non-empty string")
    if subject_id is not None and not isinstance(subject_id, str):
        raise ValueError(f"{prefix}.subject_id must be a string or null")
    if not isinstance(changes, list):
        raise ValueError(f"{prefix}.expected_changes must be a list")
    status_value = raw.get("status")
    if not isinstance(status_value, str):
        raise ValueError(f"{prefix}.status is invalid")
    try:
        status = IntentStatus(status_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{prefix}.status is invalid") from exc
    intent = TransitionIntent(
        intent_id=intent_id,
        scope=_scope_from_json(raw.get("scope"), f"{prefix}.scope"),
        operation=operation,
        created_at=_datetime_from_json(raw.get("created_at"), f"{prefix}.created_at"),
        subject_id=subject_id,
        status=status,
        expires_at=_optional_datetime_from_json(
            raw.get("expires_at"), f"{prefix}.expires_at"
        ),
        expected_changes=tuple(
            _fact_from_json(change, change_index)
            for change_index, change in enumerate(changes)
        ),
        diagnostics=_strings_from_json(raw.get("diagnostics"), f"{prefix}.diagnostics"),
    )
    _validate_intent(intent)
    return intent


def _parse_journal(raw: Any) -> tuple[TransitionIntent, ...]:
    if not isinstance(raw, dict):
        raise ValueError("invalid intent journal: expected an object")
    if raw.get("version") != _SCHEMA_VERSION:
        raise ValueError("invalid intent journal: unsupported schema version")
    entries = raw.get("intents")
    if not isinstance(entries, list):
        raise ValueError("invalid intent journal: intents must be a list")
    try:
        intents = tuple(
            _intent_from_json(entry, index) for index, entry in enumerate(entries)
        )
    except ValueError as exc:
        raise ValueError(f"invalid intent journal: {exc}") from exc
    ids = [intent.intent_id for intent in intents]
    if len(ids) != len(set(ids)):
        raise ValueError("invalid intent journal: duplicate intent_id")
    return tuple(sorted(intents, key=lambda intent: intent.intent_id))


def _same_identity(left: TransitionIntent, right: TransitionIntent) -> bool:
    return (
        left.intent_id,
        left.scope,
        left.operation,
        left.created_at,
        left.subject_id,
        left.expires_at,
        left.expected_changes,
    ) == (
        right.intent_id,
        right.scope,
        right.operation,
        right.created_at,
        right.subject_id,
        right.expires_at,
        right.expected_changes,
    )


class IntentJournal:
    """Versioned atomic journal for restart-safe non-atomic transitions."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> tuple[TransitionIntent, ...]:
        raw = read_json_with_recovery(self.path, label="intent journal")
        if raw is None:
            return ()
        return _parse_journal(raw)

    def _save(self, intents: Iterable[TransitionIntent]) -> None:
        ordered = tuple(sorted(intents, key=lambda intent: intent.intent_id))
        write_json_atomic(
            self.path,
            {
                "version": _SCHEMA_VERSION,
                "intents": [_intent_to_json(intent) for intent in ordered],
            },
        )

    def plan(self, intent: TransitionIntent) -> TransitionIntent:
        """Persist a complete plan before the caller starts external mutation."""

        _validate_intent(intent)
        if intent.status is not IntentStatus.PLANNED:
            raise ValueError("a new intent must have planned status")
        current = list(self.load())
        for existing in current:
            if existing.intent_id != intent.intent_id:
                continue
            if _same_identity(existing, intent):
                return existing
            raise ValueError(f"conflicting intent_id: {intent.intent_id}")
        current.append(intent)
        self._save(current)
        return intent

    def _transition(
        self,
        intent_id: str,
        target: IntentStatus,
        diagnostics: tuple[str, ...] = (),
    ) -> TransitionIntent:
        current = list(self.load())
        for index, existing in enumerate(current):
            if existing.intent_id != intent_id:
                continue
            if existing.status is target:
                return existing
            if target not in _ALLOWED_TRANSITIONS[existing.status]:
                raise ValueError(
                    f"invalid intent transition: {existing.status.value} -> {target.value}"
                )
            if target is IntentStatus.FAILED and not diagnostics:
                raise ValueError("failed intent transition requires diagnostics")
            updated = replace(
                existing,
                status=target,
                diagnostics=(*existing.diagnostics, *diagnostics),
            )
            current[index] = updated
            self._save(current)
            return updated
        raise KeyError(f"intent {intent_id!r} not found")

    def mark_applied(self, intent_id: str) -> TransitionIntent:
        return self._transition(intent_id, IntentStatus.APPLIED)

    def mark_verified(self, intent_id: str) -> TransitionIntent:
        return self._transition(intent_id, IntentStatus.VERIFIED)

    def mark_failed(
        self, intent_id: str, *, diagnostics: tuple[str, ...]
    ) -> TransitionIntent:
        return self._transition(intent_id, IntentStatus.FAILED, diagnostics)

    def pending(self, *, now: datetime) -> tuple[TransitionIntent, ...]:
        _require_aware(now, "now")
        return tuple(
            intent
            for intent in self.load()
            if intent.status in _LIVE_STATUSES
            and (intent.expires_at is None or intent.expires_at > now)
        )

    def expire_overdue(self, *, now: datetime) -> tuple[TransitionIntent, ...]:
        _require_aware(now, "now")
        current = list(self.load())
        expired: list[TransitionIntent] = []
        for index, intent in enumerate(current):
            if (
                intent.status not in _LIVE_STATUSES
                or intent.expires_at is None
                or intent.expires_at > now
            ):
                continue
            diagnostic = (
                f"expired at {now.isoformat()} "
                f"(deadline {intent.expires_at.isoformat()})"
            )
            updated = replace(
                intent,
                status=IntentStatus.EXPIRED,
                diagnostics=(*intent.diagnostics, diagnostic),
            )
            current[index] = updated
            expired.append(updated)
        if expired:
            self._save(current)
        return tuple(sorted(expired, key=lambda intent: intent.intent_id))


__all__ = ["IntentJournal"]

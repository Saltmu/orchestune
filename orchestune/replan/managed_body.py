"""Fail-closed reconciliation of generated SubIssue body regions."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import yaml

from orchestune.models import normalize_newlines

_FOOTPRINT_BLOCK_PATTERN = re.compile(r"```yaml\s*\n(.*?)```", re.DOTALL)

GENERATED_SUBTASK_START = "<!-- orchestune:generated-subtask:start -->"
GENERATED_SUBTASK_END = "<!-- orchestune:generated-subtask:end -->"

PLAN_OWNED_FOOTPRINT_KEYS = frozenset(
    {
        "subtask_id",
        "footprint",
        "symbols",
        "depends_on",
        "shared_contract",
        "writes_shared_contract",
        "parent_issue_number",
        "execution_profile",
        "model_tier",
    }
)
RUNTIME_OWNED_FOOTPRINT_KEYS = frozenset({"recompute_count", "forced_serial"})
KNOWN_FOOTPRINT_KEYS = PLAN_OWNED_FOOTPRINT_KEYS | RUNTIME_OWNED_FOOTPRINT_KEYS


class ManagedBodyConflict(ValueError):
    """Raised when ownership cannot be proven without overwriting human work."""


@dataclass(frozen=True)
class _ManagedRegion:
    prefix: str
    block: str
    suffix: str


def _managed_region(body: str, *, allow_missing: bool) -> _ManagedRegion | None:
    start_count = body.count(GENERATED_SUBTASK_START)
    end_count = body.count(GENERATED_SUBTASK_END)
    if start_count == 0 and end_count == 0 and allow_missing:
        return None
    if start_count != 1 or end_count != 1:
        raise ManagedBodyConflict("managed body markers must occur exactly once")
    start = body.index(GENERATED_SUBTASK_START)
    end_start = body.index(GENERATED_SUBTASK_END)
    if start >= end_start:
        raise ManagedBodyConflict("managed body markers are reversed or nested")
    end = end_start + len(GENERATED_SUBTASK_END)
    return _ManagedRegion(body[:start], body[start:end], body[end:])


def _footprint_parts(body: str) -> tuple[str, dict[str, object], str]:
    matches = list(_FOOTPRINT_BLOCK_PATTERN.finditer(body))
    if len(matches) != 1:
        raise ManagedBodyConflict(
            "managed body must contain exactly one Footprint YAML"
        )
    match = matches[0]
    try:
        loaded = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ManagedBodyConflict(
            "managed body contains invalid Footprint YAML"
        ) from exc
    if not isinstance(loaded, dict):
        raise ManagedBodyConflict("managed body Footprint must be a mapping")
    return body[: match.start()], loaded, body[match.end() :]


def _validate_known_footprint(data: Mapping[Any, Any]) -> None:
    string_keys = {key for key in data if isinstance(key, str)}
    unknown = set(data) - string_keys | (string_keys - KNOWN_FOOTPRINT_KEYS)
    if unknown:
        raise ManagedBodyConflict(
            f"unknown Footprint keys: {sorted(map(str, unknown))}"
        )


def _validate_runtime_metadata(metadata: Mapping[str, object]) -> None:
    unknown = set(metadata) - RUNTIME_OWNED_FOOTPRINT_KEYS
    if unknown:
        raise ValueError(f"unknown runtime Footprint metadata: {sorted(unknown)}")
    count = metadata.get("recompute_count")
    if count is not None and (
        isinstance(count, bool) or not isinstance(count, int) or count < 0
    ):
        raise ValueError("recompute_count must be a non-negative integer")
    forced = metadata.get("forced_serial")
    if forced is not None and not isinstance(forced, bool):
        raise ValueError("forced_serial must be a boolean")


def _replace_footprint(body: str, *, runtime_metadata: Mapping[str, object]) -> str:
    before, data, after = _footprint_parts(body)
    _validate_known_footprint(data)
    _validate_runtime_metadata(runtime_metadata)
    updated = {
        key: value
        for key, value in data.items()
        if key not in RUNTIME_OWNED_FOOTPRINT_KEYS
    }
    updated.update(runtime_metadata)
    rendered = yaml.dump(
        updated,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    return f"{before}```yaml\n{rendered}```{after}"


def _runtime_metadata(body: str) -> dict[str, object]:
    _, data, _ = _footprint_parts(body)
    _validate_known_footprint(data)
    metadata = {key: data[key] for key in RUNTIME_OWNED_FOOTPRINT_KEYS if key in data}
    try:
        # Replanning is an explicit, approval-bound transaction. Unlike runtime
        # recovery, it must not silently coerce corrupted persisted ownership
        # state and then overwrite the Issue body.
        _validate_runtime_metadata(metadata)
    except ValueError as exc:
        raise ManagedBodyConflict(str(exc)) from exc
    return metadata


def _with_region_runtime(
    region: _ManagedRegion, runtime_metadata: Mapping[str, object]
) -> str:
    block = (
        _replace_footprint(region.block, runtime_metadata=runtime_metadata)
        if runtime_metadata
        else region.block
    )
    return f"{region.prefix}{block}{region.suffix}"


def ensure_managed_body(body: str) -> str:
    """Wrap generated content and validate an existing marker pair."""

    normalized = normalize_newlines(body)
    region = _managed_region(normalized, allow_missing=True)
    if region is None:
        return (
            f"{GENERATED_SUBTASK_START}\n{normalized.rstrip()}\n"
            f"{GENERATED_SUBTASK_END}\n\n## Human Notes\n"
        )
    if "## Human Notes" not in region.suffix:
        return f"{normalized.rstrip()}\n\n## Human Notes\n"
    return normalized


def with_runtime_metadata(
    body: str, runtime_metadata: Mapping[str, object] | None
) -> str:
    """Render validated runtime-owned Footprint fields into a generated body."""

    if not runtime_metadata:
        return body
    region = _managed_region(body, allow_missing=True)
    if region is None:
        return _replace_footprint(body, runtime_metadata=runtime_metadata)
    return _with_region_runtime(region, runtime_metadata)


def _legacy_matches(current: str, legacy: str) -> dict[str, object]:
    current_before, current_data, current_after = _footprint_parts(current)
    legacy_before, legacy_data, legacy_after = _footprint_parts(legacy)
    _validate_known_footprint(current_data)
    _validate_known_footprint(legacy_data)
    if (current_before, current_after) != (legacy_before, legacy_after):
        raise ManagedBodyConflict(
            "legacy non-Footprint body differs from known renderer"
        )
    for key in PLAN_OWNED_FOOTPRINT_KEYS:
        if current_data.get(key) != legacy_data.get(key):
            raise ManagedBodyConflict(f"legacy plan-owned Footprint key differs: {key}")
    return {
        key: current_data[key]
        for key in RUNTIME_OWNED_FOOTPRINT_KEYS
        if key in current_data
    }


def reconcile_managed_body(
    current_body: str,
    expected_body: str,
    *,
    legacy_body: str | None = None,
) -> str:
    """Replace only Orchestune-owned content, preserving everything outside it."""

    current = normalize_newlines(current_body)
    expected = normalize_newlines(expected_body)
    expected_region = _managed_region(expected, allow_missing=False)
    assert expected_region is not None
    current_region = _managed_region(current, allow_missing=True)

    if current_region is None:
        if legacy_body is None:
            raise ManagedBodyConflict("markerless legacy body has no known renderer")
        metadata = _legacy_matches(current, normalize_newlines(legacy_body))
        return _with_region_runtime(expected_region, metadata)

    metadata = _runtime_metadata(current_region.block)
    expected_block = with_runtime_metadata(expected_region.block, metadata)
    return f"{current_region.prefix}{expected_block}{current_region.suffix}"


__all__ = [
    "GENERATED_SUBTASK_END",
    "GENERATED_SUBTASK_START",
    "KNOWN_FOOTPRINT_KEYS",
    "ManagedBodyConflict",
    "PLAN_OWNED_FOOTPRINT_KEYS",
    "RUNTIME_OWNED_FOOTPRINT_KEYS",
    "ensure_managed_body",
    "reconcile_managed_body",
    "with_runtime_metadata",
]

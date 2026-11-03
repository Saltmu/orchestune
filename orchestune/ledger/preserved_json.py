"""Preserve unknown JSON fields across record updates."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

_MISSING = object()


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


__all__ = [
    "_MISSING",
    "_preserved_json",
]

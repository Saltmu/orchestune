"""Measure a context payload in UTF-8 bytes.

Callers pass the byte cap they want. This module does not estimate tokenizer
tokens and does not fetch or store records.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_TIER2_LIMIT = 3
DEFAULT_TIER2_MIN_SCORE = 1.0
DEFAULT_TIER2_MIN_SCORE_RATIO = 0.5
DEFAULT_LOCAL_CONTEXT_MAX_BYTES = 32768
DEFAULT_CLOUD_PROMPT_MAX_BYTES = 8192


@dataclass(frozen=True, slots=True)
class BudgetFit:
    """Byte size of one payload and whether it is within the requested cap."""

    size_bytes: int
    fits: bool


def fit_context_budget(
    payload: object, *, max_bytes: int = DEFAULT_LOCAL_CONTEXT_MAX_BYTES
) -> BudgetFit:
    """Return the UTF-8 size of text, bytes, or one JSON object."""
    size_bytes = _utf8_size(payload)
    return BudgetFit(size_bytes, size_bytes <= max_bytes)


def _utf8_size(payload: object) -> int:
    if isinstance(payload, str):
        return len(payload.encode("utf-8"))
    if isinstance(payload, bytes):
        return len(payload)
    if isinstance(payload, Mapping):
        rendered = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return len(rendered.encode("utf-8"))
    raise TypeError("context budget accepts text, bytes, or a JSON object")

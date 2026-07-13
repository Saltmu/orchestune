"""Parsing and risk classification for decomposition plans."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from orchestune.dag_models import SubTask

_RISK_PATH_PATTERNS = (
    re.compile(r"(^|/)data/sources/"),
    re.compile(r"(^|/)credentials/"),
    re.compile(r"(^|/)auth", re.IGNORECASE),
)
_RISK_KEYWORDS = ("subprocess", "auth", "credential")
_FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_VALID_PRIORITIES = frozenset(("high", "medium", "low"))


def detect_risk_from_values(
    footprint: Iterable[str],
    symbols: Iterable[str],
    description: str,
    explicit: bool = False,
) -> tuple[bool, tuple[str, ...]]:
    footprint = tuple(footprint)
    symbols = tuple(symbols)
    reasons: list[str] = []

    for path in footprint:
        if any(pattern.search(path) for pattern in _RISK_PATH_PATTERNS):
            reasons.append(f"footprint:{path}")

    haystack = " ".join([*footprint, *symbols, description]).lower()
    reasons.extend(
        f"keyword:{keyword}" for keyword in _RISK_KEYWORDS if keyword in haystack
    )
    if explicit:
        reasons.append("explicit")

    unique_reasons = tuple(dict.fromkeys(reasons))
    return bool(unique_reasons), unique_reasons


def extract_frontmatter(text: str) -> dict[str, Any]:
    match = _FRONTMATTER_PATTERN.match(text)
    if not match:
        raise ValueError(
            "decomposition_plan.md にYAMLフロントマター（--- ... ---）が見つかりません"
        )
    data = yaml.safe_load(match.group(1))
    if not isinstance(data, dict):
        raise ValueError("フロントマターの内容がマッピング形式ではありません")
    return data


def _parse_subtask(raw: dict[str, Any]) -> SubTask:
    footprint = tuple(str(item) for item in raw.get("footprint", []) or [])
    symbols = tuple(str(item) for item in raw.get("symbols", []) or [])
    depends_on = tuple(str(item) for item in raw.get("depends_on", []) or [])
    description = str(raw.get("description", ""))
    priority = str(raw.get("priority", "medium")).lower()
    if priority not in _VALID_PRIORITIES:
        priority = "medium"
    overview = str(raw.get("overview", ""))
    acceptance_criteria = tuple(
        str(item) for item in raw.get("acceptance_criteria", []) or []
    )
    proposed_changes = tuple(
        str(item) for item in raw.get("proposed_changes", []) or []
    )
    verification_plan = tuple(
        str(item) for item in raw.get("verification_plan", []) or []
    )

    risk, risk_reasons = detect_risk_from_values(
        footprint,
        symbols,
        description,
        explicit=bool(raw.get("risk", False)),
    )
    return SubTask(
        id=str(raw["id"]),
        description=description,
        footprint=footprint,
        symbols=symbols,
        depends_on=depends_on,
        risk=risk,
        risk_reasons=risk_reasons,
        priority=priority,
        overview=overview,
        acceptance_criteria=acceptance_criteria,
        proposed_changes=proposed_changes,
        verification_plan=verification_plan,
    )


def parse_decomposition_plan(path: str | Path) -> list[SubTask]:
    """Parse decomposition plan YAML frontmatter into validated subtasks."""
    data = extract_frontmatter(Path(path).read_text(encoding="utf-8"))
    raw_subtasks = data.get("subtasks")
    if not isinstance(raw_subtasks, list) or not raw_subtasks:
        raise ValueError("subtasks が定義されていないか、空です")

    subtasks: list[SubTask] = []
    seen_ids: set[str] = set()
    for raw in raw_subtasks:
        subtask = _parse_subtask(raw)
        if subtask.id in seen_ids:
            raise ValueError(f"サブタスクIDが重複しています: {subtask.id}")
        seen_ids.add(subtask.id)
        subtasks.append(subtask)

    for subtask in subtasks:
        unknown = [item for item in subtask.depends_on if item not in seen_ids]
        if unknown:
            raise ValueError(
                f"未知のdepends_onが指定されています: {subtask.id} -> {unknown}"
            )
    return subtasks

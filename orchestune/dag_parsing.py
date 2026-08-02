"""Parsing and risk classification for decomposition plans."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from orchestune.dag_models import SubTask, normalize_footprint_path

logger = logging.getLogger(__name__)

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
    footprint = tuple(normalize_footprint_path(path) for path in footprint)
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


def extract_frontmatter_and_body(text: str) -> tuple[dict[str, Any], str]:
    match = _FRONTMATTER_PATTERN.match(text)
    if not match:
        raise ValueError(
            "decomposition_plan.md にYAMLフロントマター（--- ... ---）が見つかりません"
        )
    data = yaml.safe_load(match.group(1))
    if not isinstance(data, dict):
        raise ValueError("フロントマターの内容がマッピング形式ではありません")
    body = text[match.end() :].strip()
    return data, body


def extract_frontmatter(text: str) -> dict[str, Any]:
    return extract_frontmatter_and_body(text)[0]


def _parse_subtask_id(raw: dict[str, Any]) -> str:
    """#272: `id`は分解計画で唯一の必須フィールド。欠落時は文脈の無いKeyErrorではなく、
    どのサブタスクが不正かを示すValueErrorで失敗させる。

    #279レビュー対応: `str()`による暗黙の強制変換は、YAMLのnull・数値・リスト
    （`id:` / `id: 123` / `id: []`）をそれぞれ`"None"`/`"123"`/`"[]"`という
    Issueタイトル・ブランチ名の識別子へ黙って昇格させてしまう。文書上の契約どおり
    文字列であることを検証し、非文字列は引用符での明示を促して拒否する。
    """
    if "id" not in raw:
        raise ValueError(f"サブタスクに必須フィールド 'id' がありません: {sorted(raw)}")
    raw_id = raw["id"]
    if not isinstance(raw_id, str):
        raise ValueError(
            f"サブタスクの 'id' は文字列である必要があります: {raw_id!r} "
            f"({type(raw_id).__name__})。引用符で囲んで文字列として指定してください"
        )
    subtask_id = raw_id.strip()
    if not subtask_id:
        raise ValueError("サブタスクの 'id' が空です")
    return subtask_id


def _warn_on_degraded_fields(
    subtask_id: str, description: str, footprint: tuple[str, ...]
) -> None:
    """#272: `description`/`footprint`はパーサー上は任意（既定値へフォールバック）だが、
    欠落するとリスク検知・コンフリクト検知の入力が失われる。サイレントに劣化させず警告する。"""
    missing = []
    if not description:
        missing.append("description")
    if not footprint:
        missing.append("footprint")
    if missing:
        logger.warning(
            "サブタスク '%s' に %s がありません。既定値で続行しますが、"
            "リスク検知・フットプリント競合検知の精度が低下します。",
            subtask_id,
            "/".join(missing),
        )


def _parse_subtask(raw: dict[str, Any]) -> SubTask:
    subtask_id = _parse_subtask_id(raw)
    footprint = tuple(
        normalize_footprint_path(str(item)) for item in raw.get("footprint", []) or []
    )
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
    shared_contract = (
        str(raw["shared_contract"]) if raw.get("shared_contract") else None
    )
    writes_shared_contract = bool(raw.get("writes_shared_contract", False))

    _warn_on_degraded_fields(subtask_id, description, footprint)

    risk, risk_reasons = detect_risk_from_values(
        footprint,
        symbols,
        description,
        explicit=bool(raw.get("risk", False)),
    )
    return SubTask(
        id=subtask_id,
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
        shared_contract=shared_contract,
        writes_shared_contract=writes_shared_contract,
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

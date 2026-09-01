"""Pure decomposition-plan loading shared by provisioning and replanning."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from orchestune.dag.models import SubTask
from orchestune.dag.parsing import (
    extract_frontmatter_and_body,
    parse_decomposition_plan,
)
from orchestune.validation import validate_issue_number

VALID_PARENT_ISSUE_SOURCES = frozenset(("adopted", "derived"))


@dataclass(frozen=True)
class PlanMetadata:
    title: str
    parent_issue_number: int | None
    parent_issue_source: str | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if self.parent_issue_source not in (None, *VALID_PARENT_ISSUE_SOURCES):
            raise ValueError(
                "decomposition_plan.md の 'parent_issue_source' は "
                f"'adopted' または 'derived' である必要があります: {self.parent_issue_source!r}"
            )
        if self.parent_issue_source == "adopted" and self.parent_issue_number is None:
            raise ValueError(
                "decomposition_plan.md に 'parent_issue_source: adopted' が指定されていますが、"
                "'parent_issue_number' が設定されていません"
            )


def load_plan(path: str | Path) -> tuple[list[SubTask], PlanMetadata]:
    """Parse a decomposition plan without Forge or process dependencies."""

    subtasks = parse_decomposition_plan(path)
    raw, description = extract_frontmatter_and_body(
        Path(path).read_text(encoding="utf-8")
    )
    issue_numbers = {
        str(entry["id"]).strip(): validate_issue_number(entry["issue_number"])
        for entry in raw.get("subtasks") or []
        if isinstance(entry, dict) and entry.get("issue_number") not in (None, "")
    }
    enriched = [
        dataclasses.replace(subtask, issue_number=issue_numbers.get(subtask.id))
        for subtask in subtasks
    ]
    raw_parent = raw.get("parent_issue_number")
    parent_issue_number = (
        None
        if raw_parent in (None, "")
        else validate_issue_number(cast(int | str, raw_parent))
    )
    raw_parent_source = raw.get("parent_issue_source")
    parent_issue_source = (
        str(raw_parent_source).strip() if raw_parent_source not in (None, "") else None
    )
    return enriched, PlanMetadata(
        title=str(raw.get("title") or "").strip(),
        parent_issue_number=parent_issue_number,
        parent_issue_source=parent_issue_source,
        description=description,
    )


__all__ = ["PlanMetadata", "load_plan"]

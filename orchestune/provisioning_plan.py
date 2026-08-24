"""Plan metadata and parent-plan persistence for Issue provisioning."""

from __future__ import annotations

import dataclasses
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from orchestune.dag.models import SubTask
from orchestune.dag.parsing import (
    extract_frontmatter_and_body,
    parse_decomposition_plan,
)
from orchestune.forge import IssueForge
from orchestune.issue_parsing import (
    PARENT_MARKER,
    embed_decomposition_plan_in_parent_body,
    restore_plan_markdown_from_parent_body,
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


def _load_plan(path: str | Path) -> tuple[list[SubTask], PlanMetadata]:
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


def _parent_body(
    title: str, description: str = "", plan_data: dict | str | None = None
) -> str:
    body = title
    if description:
        body += f"\n\n{description}"
    body += f"\n\n配下のサブタスクはこのIssueのSub-issueとして紐付けられます。\n\n{PARENT_MARKER}"
    return (
        embed_decomposition_plan_in_parent_body(body, plan_data)
        if plan_data is not None
        else body
    )


def restore_plan_file_from_parent(
    forge: IssueForge,
    parent_issue_number: int,
    output_path: str | Path = "decomposition_plan.md",
) -> Path:
    parent_issue = forge.get_issue(parent_issue_number)
    if parent_issue is None:
        raise ValueError(
            f"Parent issue #{parent_issue_number} was not found on the forge."
        )
    restored_markdown = restore_plan_markdown_from_parent_body(parent_issue.body)
    if not restored_markdown:
        raise ValueError(
            f"Parent issue #{parent_issue_number} does not contain a valid decomposition plan block."
        )
    out_file = Path(output_path)
    out_file.write_text(restored_markdown, encoding="utf-8")
    return out_file


def sync_parent_decomposition_plan(
    forge: IssueForge, parent_issue_number: int, plan_path: str | Path
) -> bool:
    """Embed the current plan frontmatter in its parent Issue body."""
    try:
        parent_issue = forge.get_issue(parent_issue_number)
        if parent_issue is None:
            print(
                f"Warning: could not sync decomposition plan into #{parent_issue_number}'s body: parent issue not found",
                file=sys.stderr,
            )
            return False
        raw_frontmatter, _ = extract_frontmatter_and_body(
            Path(plan_path).read_text(encoding="utf-8")
        )
        if not raw_frontmatter:
            print(
                f"Warning: could not sync decomposition plan into #{parent_issue_number}'s body: no frontmatter in {plan_path}",
                file=sys.stderr,
            )
            return False
        updated_body = embed_decomposition_plan_in_parent_body(
            parent_issue.body, raw_frontmatter
        )
        if updated_body != parent_issue.body:
            forge.update_issue_body(parent_issue_number, updated_body)
        return True
    except Exception as error:
        print(
            f"Warning: could not sync decomposition plan into #{parent_issue_number}'s body: {error}",
            file=sys.stderr,
        )
        return False

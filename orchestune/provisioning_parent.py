"""Resolve and normalize the EPIC parent used by provisioning."""

from __future__ import annotations

from pathlib import Path

from orchestune.dag.parsing import extract_frontmatter_and_body
from orchestune.forge import IssueForge
from orchestune.issue_parsing import PARENT_MARKER, ensure_parent_marker, is_epic_issue
from orchestune.plan_writer import write_issue_numbers
from orchestune.provisioning_plan import (
    PlanMetadata,
    _parent_body,
    sync_parent_decomposition_plan,
)


def _resolve_explicit_parent_issue(
    forge: IssueForge,
    parent_issue_number: int,
    plan_path: str | Path,
    metadata: PlanMetadata | None = None,
) -> tuple[int, bool]:
    issue = forge.get_issue(parent_issue_number)
    if issue is None:
        raise RuntimeError(
            f"Adopted parent issue #{parent_issue_number} does not exist; refusing to provision subtasks under it."
        )
    if issue.state.upper() == "CLOSED":
        raise RuntimeError(
            f"Adopted parent issue #{parent_issue_number} is closed; refusing to adopt a closed issue as EPIC parent."
        )
    if not is_epic_issue(issue):
        title = (
            issue.title
            if issue.title.startswith("[EPIC] ")
            else f"[EPIC] {issue.title}"
        )
        if title != issue.title:
            forge.update_issue_title(parent_issue_number, title)
        if PARENT_MARKER not in issue.body:
            forge.update_issue_body(
                parent_issue_number, ensure_parent_marker(issue.body)
            )
    if (
        metadata is None
        or metadata.parent_issue_number != parent_issue_number
        or metadata.parent_issue_source != "adopted"
    ):
        write_issue_numbers(
            plan_path,
            parent_issue_number=parent_issue_number,
            parent_issue_source="adopted",
        )
    return parent_issue_number, sync_parent_decomposition_plan(
        forge, parent_issue_number, plan_path
    )


def _resolve_adopted_parent_metadata(
    forge: IssueForge,
    metadata: PlanMetadata,
    plan_path: str | Path,
) -> tuple[int, bool]:
    if metadata.parent_issue_number is None:
        raise ValueError(
            "decomposition_plan.md に 'parent_issue_source: adopted' が指定されていますが、"
            "'parent_issue_number' が設定されていません"
        )
    candidate = forge.get_issue(metadata.parent_issue_number)
    if candidate is None:
        raise RuntimeError(
            f"Adopted parent issue #{metadata.parent_issue_number} does not exist; refusing to provision subtasks under it."
        )
    if candidate.state.upper() == "CLOSED":
        raise RuntimeError(
            f"Adopted parent issue #{metadata.parent_issue_number} is closed; refusing to adopt a closed issue as EPIC parent."
        )
    if not is_epic_issue(candidate):
        raise RuntimeError(
            f"Adopted parent issue #{metadata.parent_issue_number} is not an Orchestune EPIC issue "
            "(missing '[EPIC] ' prefix or parent marker); refusing to automatically mutate an unconfirmed issue. "
            f"If you intended to adopt this issue, pass '--parent-issue {metadata.parent_issue_number}' explicitly."
        )
    return _resolve_explicit_parent_issue(
        forge, metadata.parent_issue_number, plan_path, metadata
    )


def _resolve_derived_parent_issue(
    forge: IssueForge,
    metadata: PlanMetadata,
    plan_path: str | Path,
) -> tuple[int, bool]:
    parent_issue_number = metadata.parent_issue_number
    parent_title = f"[EPIC] {metadata.title}"
    if parent_issue_number is not None:
        candidate = forge.get_issue(parent_issue_number)
        if (
            candidate is None
            or candidate.title != parent_title
            or PARENT_MARKER not in candidate.body
        ):
            parent_issue_number = None
    if parent_issue_number is None:
        candidates = forge.find_open_issues_by_exact_title(parent_title)
        marked_candidate = next(
            (candidate for candidate in candidates if PARENT_MARKER in candidate.body),
            None,
        )
        if marked_candidate is not None:
            parent_issue_number = marked_candidate.number
        else:
            raw_frontmatter, _ = extract_frontmatter_and_body(
                Path(plan_path).read_text(encoding="utf-8")
            )
            parent_issue_number = forge.create_issue(
                parent_title,
                _parent_body(metadata.title, metadata.description, raw_frontmatter),
            )
        write_issue_numbers(
            plan_path,
            parent_issue_number=parent_issue_number,
            parent_issue_source="derived",
        )
    return parent_issue_number, sync_parent_decomposition_plan(
        forge, parent_issue_number, plan_path
    )


def _resolve_parent_issue(
    forge: IssueForge,
    metadata: PlanMetadata,
    plan_path: str | Path,
    *,
    explicit_parent_issue: int | None = None,
) -> tuple[int, bool]:
    if explicit_parent_issue is not None:
        return _resolve_explicit_parent_issue(
            forge, explicit_parent_issue, plan_path, metadata
        )
    if metadata.parent_issue_source == "adopted":
        return _resolve_adopted_parent_metadata(forge, metadata, plan_path)
    return _resolve_derived_parent_issue(forge, metadata, plan_path)

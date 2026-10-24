"""Resolve and normalize the EPIC parent used by provisioning."""

from __future__ import annotations

from pathlib import Path

from orchestune.forge import IssueForge
from orchestune.issue_parsing import PARENT_MARKER, ensure_parent_marker, is_epic_issue
from orchestune.models import IssueRecord
from orchestune.plan_writer import write_issue_numbers
from orchestune.provisioning.plan import (
    PlanMetadata,
    _parent_body,
    sync_parent_decomposition_plan,
)


def _inspect_explicit_parent(
    forge: IssueForge, validated_parent: int
) -> tuple[int, str, IssueRecord]:
    issue = forge.get_issue(validated_parent)
    if issue is None:
        raise RuntimeError(
            f"Adopted parent issue #{validated_parent} does not exist; refusing to provision subtasks under it."
        )
    if issue.state.upper() == "CLOSED":
        raise RuntimeError(
            f"Adopted parent issue #{validated_parent} is closed; refusing to adopt a closed issue as EPIC parent."
        )
    body = (
        ensure_parent_marker(issue.body)
        if PARENT_MARKER not in issue.body
        else issue.body
    )
    return validated_parent, body, issue


def _inspect_adopted_parent(
    forge: IssueForge, metadata: PlanMetadata
) -> tuple[int, str]:
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
    return metadata.parent_issue_number, candidate.body


def inspect_candidate_parent(
    forge: IssueForge,
    metadata: PlanMetadata,
    validated_parent: int | None,
) -> tuple[int | None, str]:
    if validated_parent is not None:
        num, body, _ = _inspect_explicit_parent(forge, validated_parent)
        return num, body
    if metadata.parent_issue_source == "adopted":
        return _inspect_adopted_parent(forge, metadata)

    parent_title = f"[EPIC] {metadata.title}"
    if metadata.parent_issue_number is not None:
        candidate = forge.get_issue(metadata.parent_issue_number)
        if (
            candidate is not None
            and candidate.title == parent_title
            and PARENT_MARKER in candidate.body
        ):
            return metadata.parent_issue_number, candidate.body

    candidates = forge.find_open_issues_by_exact_title(parent_title)
    marked = next((c for c in candidates if PARENT_MARKER in c.body), None)
    if marked is not None:
        return marked.number, marked.body

    return None, _parent_body(metadata.title, metadata.description, None)


def _resolve_explicit_parent_issue(
    forge: IssueForge,
    parent_issue_number: int,
    plan_path: str | Path,
    metadata: PlanMetadata | None = None,
    sync_plan: bool = True,
) -> tuple[int, bool]:
    _, body, issue = _inspect_explicit_parent(forge, parent_issue_number)
    if not is_epic_issue(issue):
        title = (
            issue.title
            if issue.title.startswith("[EPIC] ")
            else f"[EPIC] {issue.title}"
        )
        if title != issue.title:
            forge.update_issue_title(parent_issue_number, title)
        if PARENT_MARKER not in issue.body:
            forge.update_issue_body(parent_issue_number, body)
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
    if not sync_plan:
        return parent_issue_number, True
    return parent_issue_number, sync_parent_decomposition_plan(
        forge, parent_issue_number, plan_path
    )


def _resolve_adopted_parent_metadata(
    forge: IssueForge,
    metadata: PlanMetadata,
    plan_path: str | Path,
    sync_plan: bool = True,
) -> tuple[int, bool]:
    num, _ = _inspect_adopted_parent(forge, metadata)
    return _resolve_explicit_parent_issue(
        forge, num, plan_path, metadata, sync_plan=sync_plan
    )


def _resolve_derived_parent_issue(
    forge: IssueForge,
    metadata: PlanMetadata,
    plan_path: str | Path,
    sync_plan: bool = True,
) -> tuple[int, bool]:
    parent_issue_number, initial_body = inspect_candidate_parent(forge, metadata, None)
    if parent_issue_number is None:
        parent_title = f"[EPIC] {metadata.title}"
        parent_issue_number = forge.create_issue(parent_title, initial_body)
    if (
        metadata.parent_issue_number != parent_issue_number
        or metadata.parent_issue_source != "derived"
    ):
        write_issue_numbers(
            plan_path,
            parent_issue_number=parent_issue_number,
            parent_issue_source="derived",
        )
    if not sync_plan:
        return parent_issue_number, True
    return parent_issue_number, sync_parent_decomposition_plan(
        forge, parent_issue_number, plan_path
    )


def _resolve_parent_issue(
    forge: IssueForge,
    metadata: PlanMetadata,
    plan_path: str | Path,
    *,
    explicit_parent_issue: int | None = None,
    sync_plan: bool = True,
) -> tuple[int, bool]:
    if explicit_parent_issue is not None:
        return _resolve_explicit_parent_issue(
            forge, explicit_parent_issue, plan_path, metadata, sync_plan=sync_plan
        )
    if metadata.parent_issue_source == "adopted":
        return _resolve_adopted_parent_metadata(
            forge, metadata, plan_path, sync_plan=sync_plan
        )
    return _resolve_derived_parent_issue(
        forge, metadata, plan_path, sync_plan=sync_plan
    )

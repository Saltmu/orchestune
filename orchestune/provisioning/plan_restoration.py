"""Restore decomposition plan from parent issue and child issues."""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from orchestune.dag.graph import build_dag
from orchestune.dag.models import SubTask
from orchestune.dag.parsing import reject_retired_model_tier
from orchestune.forge import IssueForge
from orchestune.issue_parsing import (
    DECOMPOSITION_PLAN_MARKER,
    PARENT_MARKER,
    decomposition_plan_from_parent_body,
    normalize_newlines,
    remediation_record_from_body,
    restore_plan_dict_to_markdown,
)
from orchestune.models import IssueRecord
from orchestune.provisioning.body_parsing import parse_subtask_issue_body
from orchestune.provisioning.plan_manifest import is_manifest_plan
from orchestune.provisioning.rendering import validate_template_identity_marker
from orchestune.validation import validate_issue_number


def _load_template_content(template_path: str | Path | None) -> tuple[str, Path]:
    path = (
        Path(template_path)
        if template_path is not None
        else Path(".github/issue_template.md")
    )
    if not path.exists():
        raise FileNotFoundError(f"Template file not found: {path}")
    content = path.read_text(encoding="utf-8")
    validate_template_identity_marker(content, path)
    return content, path


def _validate_parent_plan_structure(plan_dict: dict[str, Any]) -> None:
    # Validate plan_format against SUPPORTED_PLAN_FORMATS (raises ValueError if unsupported)
    is_manifest_plan(plan_dict)
    subtasks = plan_dict.get("subtasks")
    if not isinstance(subtasks, list):
        raise ValueError("parent plan 'subtasks' must be a list")

    seen_ids: set[str] = set()
    seen_issues: set[int] = set()
    for entry in subtasks:
        if not isinstance(entry, dict) or "id" not in entry:
            raise ValueError("Each subtask in parent plan must be a dict with 'id'")
        reject_retired_model_tier(entry.get("model_tier"))
        sid = str(entry["id"]).strip()
        if sid in seen_ids:
            raise ValueError(f"duplicate subtask id in parent plan: {sid!r}")
        seen_ids.add(sid)
        num = entry.get("issue_number")
        if num is not None:
            validated_num = validate_issue_number(num)
            if validated_num in seen_issues:
                raise ValueError(
                    f"duplicate issue_number in parent plan: #{validated_num}"
                )
            seen_issues.add(validated_num)


def _validate_dependencies_match(
    parent_deps: tuple[str, ...],
    child_deps: tuple[str, ...],
    subtask_id: str,
    issue_num: int,
) -> None:
    if len(parent_deps) != len(set(parent_deps)):
        raise ValueError(f"duplicate dependencies in parent subtask {subtask_id!r}")
    if len(child_deps) != len(set(child_deps)):
        raise ValueError(f"duplicate dependencies in child #{issue_num}")
    if set(parent_deps) != set(child_deps):
        raise ValueError(
            f"depends_on mismatch for #{issue_num}: parent {parent_deps} != child {child_deps}"
        )


def _reconcile_shared_metadata(
    entry: dict[str, Any],
    child_data: dict[str, Any],
    issue_num: int,
    restored: dict[str, Any],
) -> None:
    if "writes_shared_contract" in entry:
        p_bool = bool(entry["writes_shared_contract"])
        c_bool = bool(child_data.get("writes_shared_contract", False))
        if p_bool != c_bool:
            raise ValueError(
                f"writes_shared_contract mismatch for #{issue_num}: parent {p_bool} != child {c_bool}"
            )
        restored["writes_shared_contract"] = c_bool
    else:
        restored["writes_shared_contract"] = bool(
            child_data.get("writes_shared_contract", False)
        )

    for key in (
        "shared_contract",
        "execution_profile",
        "parent_issue_number",
    ):
        p_val = entry.get(key)
        c_val = child_data.get(key)
        if p_val is not None and c_val is not None and p_val != c_val:
            raise ValueError(
                f"{key} mismatch for #{issue_num}: parent {p_val!r} != child {c_val!r}"
            )
        restored[key] = c_val if p_val is None else p_val


def _reconcile_subtask(
    entry: dict[str, Any],
    child_data: dict[str, Any],
    child_issue: IssueRecord,
) -> dict[str, Any]:
    issue_num = child_issue.number
    if child_data["subtask_id"] != str(entry["id"]).strip():
        raise ValueError(
            f"subtask_id mismatch for #{issue_num}: {child_data['subtask_id']!r} != {entry['id']!r}"
        )

    parent_desc = str(entry.get("description", "")).strip()
    child_desc = str(child_data.get("description", "")).strip()
    if parent_desc and child_desc and parent_desc != child_desc:
        raise ValueError(
            f"description mismatch for #{issue_num}: {child_desc!r} != {parent_desc!r}"
        )

    parent_deps = tuple(entry.get("depends_on") or ())
    child_deps = tuple(child_data.get("depends_on") or ())
    _validate_dependencies_match(parent_deps, child_deps, entry["id"], issue_num)

    restored = dict(entry)
    # The 6 keys are restored from child body
    restored["overview"] = child_data["overview"]
    restored["proposed_changes"] = list(child_data["proposed_changes"])
    restored["acceptance_criteria"] = list(child_data["acceptance_criteria"])
    restored["verification_plan"] = list(child_data["verification_plan"])
    restored["footprint"] = list(child_data["footprint"])
    restored["symbols"] = list(child_data["symbols"])
    restored["description"] = parent_desc or child_desc
    restored["depends_on"] = list(parent_deps)

    _reconcile_shared_metadata(entry, child_data, issue_num, restored)
    return restored


def _extract_prose_body(parent_body: str) -> str | None:
    body = normalize_newlines(parent_body)
    parts = re.split(re.escape(DECOMPOSITION_PLAN_MARKER), body, maxsplit=1)
    if not parts:
        return None
    raw_prose = parts[0]
    cleaned = re.sub(re.escape(PARENT_MARKER), "", raw_prose)
    cleaned = re.sub(
        r"配下のサブタスクはこのIssueのSub-issueとして紐付けられます。", "", cleaned
    ).strip()
    return cleaned or None


def _validate_concurrent_modifications(
    forge: IssueForge,
    parent_issue_number: int,
    parent_body: str,
    fetched_children: dict[int, IssueRecord],
) -> None:
    parent_again = forge.get_issue(parent_issue_number)
    if parent_again is None or parent_again.body != parent_body:
        raise ValueError(
            "Parent issue changed concurrently during restoration; please retry."
        )
    for issue_num, initial_record in fetched_children.items():
        child_again = forge.get_issue(issue_num)
        if child_again is None or child_again.body != initial_record.body:
            raise ValueError(
                f"Child issue #{issue_num} changed concurrently during restoration; please retry."
            )


def _restore_single_subtask(
    entry: dict[str, Any],
    forge: IssueForge,
    template_content: str,
    fetched_children: dict[int, IssueRecord],
) -> dict[str, Any]:
    entry = {k: v for k, v in entry.items() if k != "model_tier"}
    issue_num = entry.get("issue_number")
    if issue_num is None:
        return dict(entry)

    num = validate_issue_number(issue_num)
    child = forge.get_issue(num)
    if child is None:
        raise ValueError(f"Child issue #{num} was not found on the forge.")
    fetched_children[num] = child

    child_data = parse_subtask_issue_body(template_content, child.title, child.body)

    parent_rem = entry.get("remediation")
    child_rem = remediation_record_from_body(child.body)
    if child_rem is not None and parent_rem != child_rem:
        raise ValueError(
            f"Child #{num} remediation record differs from parent remediation entry"
        )
    if child_rem is None and parent_rem is not None:
        raise ValueError(
            f"Parent has remediation entry for {entry['id']!r} but child #{num} lacks remediation record"
        )

    return _reconcile_subtask(entry, child_data, child)


def _validate_restored_dag(subtasks: list[dict[str, Any]]) -> None:
    subtask_models = [
        SubTask(
            id=t["id"],
            description=t.get("description", ""),
            overview=t.get("overview", ""),
            proposed_changes=tuple(t.get("proposed_changes", ())),
            acceptance_criteria=tuple(t.get("acceptance_criteria", ())),
            verification_plan=tuple(t.get("verification_plan", ())),
            footprint=tuple(t.get("footprint", ())),
            symbols=tuple(t.get("symbols", ())),
            depends_on=tuple(t.get("depends_on", ())),
            risk=bool(t.get("risk", False)),
            risk_reasons=tuple(t.get("risk_reasons", ())),
            shared_contract=t.get("shared_contract"),
            writes_shared_contract=bool(t.get("writes_shared_contract", False)),
            issue_number=t.get("issue_number"),
            execution_profile=t.get("execution_profile"),
        )
        for t in subtasks
    ]
    build_dag(subtask_models)


def restore_plan_from_parent(
    forge: IssueForge,
    parent_issue_number: int,
    template_path: str | Path | None = None,
) -> tuple[dict[str, Any], IssueRecord, dict[int, IssueRecord]]:
    """Fetch parent and children, validate, and build the fully restored plan dictionary.

    Returns:
        tuple[dict[str, Any], IssueRecord, dict[int, IssueRecord]]:
            A tuple of (restored_plan, parent_issue, fetched_children).
            Note: Callers persisting restored plans to disk (such as restore_plan_file_from_parent)
            must validate concurrent modifications with _validate_concurrent_modifications
            using the returned fetched_children before atomic file write.
    """
    template_content, _ = _load_template_content(template_path)
    parent_issue = forge.get_issue(parent_issue_number)
    if parent_issue is None:
        raise ValueError(
            f"Parent issue #{parent_issue_number} was not found on the forge."
        )

    parent_plan = decomposition_plan_from_parent_body(parent_issue.body)
    if not parent_plan or not isinstance(parent_plan, dict):
        raise ValueError(
            f"Parent issue #{parent_issue_number} does not contain a valid decomposition plan block."
        )

    _validate_parent_plan_structure(parent_plan)

    fetched_children: dict[int, IssueRecord] = {}
    restored_subtasks = [
        _restore_single_subtask(entry, forge, template_content, fetched_children)
        for entry in parent_plan["subtasks"]
    ]

    _validate_restored_dag(restored_subtasks)

    restored_plan = dict(parent_plan)
    restored_plan.pop("plan_format", None)
    restored_plan["subtasks"] = restored_subtasks

    return restored_plan, parent_issue, fetched_children


def restore_plan_file_from_parent(
    forge: IssueForge,
    parent_issue_number: int,
    output_path: str | Path = "decomposition_plan.md",
    *,
    template_path: str | Path | None = None,
) -> Path:
    """Restore a decomposition plan from parent and child issues, saving atomically."""
    restored_plan, parent_issue, fetched_children = restore_plan_from_parent(
        forge, parent_issue_number, template_path=template_path
    )

    _validate_concurrent_modifications(
        forge, parent_issue_number, parent_issue.body, fetched_children
    )

    prose_body = _extract_prose_body(parent_issue.body)
    markdown_content = restore_plan_dict_to_markdown(
        restored_plan, prose_body=prose_body
    )

    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file = out_file.parent / f".tmp-{out_file.name}-{uuid.uuid4().hex}"

    try:
        temp_file.write_text(markdown_content, encoding="utf-8")
        temp_file.replace(out_file)
    finally:
        if temp_file.exists():
            temp_file.unlink(missing_ok=True)

    return out_file


__all__ = [
    "restore_plan_file_from_parent",
    "restore_plan_from_parent",
]

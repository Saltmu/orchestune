"""Render registration metadata without changing unrelated Issue text."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import yaml

from orchestune.issue_parsing import (
    DECOMPOSITION_PLAN_BLOCK_PATTERN,
    FOOTPRINT_BLOCK_PATTERN,
    decomposition_plan_from_parent_body,
    parent_issue_number_from_body,
    parse_footprint_block,
    parse_task_from_issue,
)
from orchestune.models import IssueRecord
from orchestune.provisioning.rendering import _subtask_id_from_body
from orchestune.remediation.models import RemediationTask
from orchestune.remediation.preview import _record, expected_footprint

MARKER = "<!-- orchestune:remediation-v1 -->"


def remediation_record(
    task: RemediationTask, parent: int, revision: str
) -> dict[str, Any]:
    return {
        "parent_issue_number": parent,
        "subtask_id": task.subtask.id,
        "spec_revision": revision,
        "kind": task.kind,
        "parent_acceptance": list(task.parent_acceptance),
        "origins": list(task.origins),
        "reason": task.reason,
    }


def registration_body(
    body: str, task: RemediationTask, parent: int, revision: str
) -> str:
    """Replace only the first Footprint; retain title and other body sections."""
    footprint = (
        "```yaml\n"
        + yaml.safe_dump(
            expected_footprint(task, parent), allow_unicode=True, sort_keys=False
        )
        + "```"
    )
    match = FOOTPRINT_BLOCK_PATTERN.search(body)
    if match:
        current = parse_footprint_block(body) or {}
        if MARKER in body and all(
            current.get(k) == v for k, v in expected_footprint(task, parent).items()
        ):
            pass  # Preserve claim metadata added by a concurrently running child.
        else:
            body = body[: match.start()] + footprint + body[match.end() :]
    else:
        body += "\n\n## Footprint\n\n" + footprint
    if MARKER not in body:
        record = remediation_record(task, parent, revision)
        body += (
            "\n\n## 補修タスクの根拠\n\n"
            + f"- Kind: {task.kind}\n- Parent acceptance: {', '.join(task.parent_acceptance)}\n"
            + f"- Origins: {', '.join(task.origins)}\n- Reason: {task.reason}\n\n"
            + MARKER
            + "\n```json\n"
            + json.dumps(record, ensure_ascii=False, sort_keys=True)
            + "\n```\n"
        )
    if _record(IssueRecord(0, "", body, (), "")) != remediation_record(
        task, parent, revision
    ):
        raise ValueError("remediation record does not round-trip")
    return body


def validate_registered_body(
    issue: IssueRecord, task: RemediationTask, parent: int
) -> None:
    parsed = parse_task_from_issue(issue)
    expected = task.subtask
    if (
        _subtask_id_from_body(issue.body) != expected.id
        or parent_issue_number_from_body(issue.body) != parent
        or parsed.yaml_error
        or parsed.footprint_error
        or parsed.subtask_id != expected.id
        or parsed.footprint != expected.footprint
        or parsed.symbols != expected.symbols
        or parsed.depends_on != expected.depends_on
        or parsed.execution_profile != expected.execution_profile
        or parsed.model_tier != expected.model_tier
        or parsed.shared_contract != expected.shared_contract
        or parsed.writes_shared_contract != expected.writes_shared_contract
    ):
        raise ValueError(
            f"Issue #{issue.number}: registration metadata readback failed"
        )


def plan_block(body: str) -> str:
    matches = list(DECOMPOSITION_PLAN_BLOCK_PATTERN.finditer(body))
    if len(matches) != 1:
        raise ValueError("parent must contain exactly one decomposition plan block")
    return matches[0][0]


def append_plan(body: str, entries: list[dict[str, Any]]) -> str:
    plan_block(body)
    plan = deepcopy(decomposition_plan_from_parent_body(body))
    if plan is None:
        raise ValueError("parent plan missing")
    current = {entry["id"]: entry for entry in plan["subtasks"]}
    for entry in entries:
        if entry["id"] in current:
            if current[entry["id"]] != entry:
                raise ValueError(f"conflicting plan entry: {entry['id']}")
        else:
            plan["subtasks"].append(entry)
    block = (
        "<!-- orchestune:decomposition-plan -->\n```yaml\n"
        + yaml.safe_dump(plan, allow_unicode=True, sort_keys=False)
        + "```"
    )
    match = DECOMPOSITION_PLAN_BLOCK_PATTERN.search(body)
    assert match is not None
    ending = (
        "\r\n" if match[0].endswith("\r\n") else "\n" if match[0].endswith("\n") else ""
    )
    return body[: match.start()] + block + ending + body[match.end() :]

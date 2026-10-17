"""Classify remediation scope and state, and bind confirmation to a snapshot."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from typing import Any, cast

from orchestune.issue_parsing import (
    DECOMPOSITION_PLAN_BLOCK_PATTERN,
    FOOTPRINT_BLOCK_PATTERN,
    decomposition_plan_from_parent_body,
    is_epic_issue,
    parent_issue_number_from_body,
    parse_footprint_block,
)
from orchestune.labels import StatusLabel
from orchestune.ledger.status_machine import lifecycle_labels
from orchestune.models import IssueRecord
from orchestune.remediation.models import (
    PreviewProblem,
    RemediationPreview,
    RemediationSnapshot,
    RemediationSpec,
    RemediationTask,
    TaskPreview,
)
from orchestune.remediation.spec import stable_hash

_MARKER = "<!-- orchestune:remediation-v1 -->"
_RECORD = re.compile(re.escape(_MARKER) + r"\s*```json\s*\n(.*?)```", re.DOTALL)
_SCOPE_GUIDANCE = (
    "scope-review-required: edit the parent Issue, replan, or create a separate EPIC"
)
_PRIORITY = {"input": (0, 2), "parent": (1, 6), "scope": (2, 7), "existing": (3, 6)}


def expected_footprint(task: RemediationTask, parent: int) -> dict[str, Any]:
    """The same metadata keys rendered by the provisioning issue template."""
    subtask = task.subtask
    return {
        "subtask_id": subtask.id,
        "footprint": list(subtask.footprint),
        "symbols": list(subtask.symbols),
        "depends_on": list(subtask.depends_on),
        "shared_contract": subtask.shared_contract,
        "writes_shared_contract": subtask.writes_shared_contract,
        "parent_issue_number": parent,
        "execution_profile": subtask.execution_profile,
        "model_tier": subtask.model_tier,
    }


def _parent_plan(
    snapshot: RemediationSnapshot, problems: list[PreviewProblem]
) -> list[dict[str, Any]]:
    parent = snapshot.parent
    plan = decomposition_plan_from_parent_body(parent.body)
    if (
        not is_epic_issue(parent)
        or plan is None
        or not isinstance(plan.get("subtasks"), list)
    ):
        problems.append(
            PreviewProblem(
                "input", "parent must be an EPIC containing a valid decomposition plan"
            )
        )
        return []
    if parent.state.upper() != "OPEN":
        problems.append(
            PreviewProblem(
                "parent", "parent Issue is closed; explicitly reopen it before retrying"
            )
        )
    if snapshot.parent_merged:
        problems.append(
            PreviewProblem(
                "parent",
                "current parent branch was merged into main; use a normal Issue or new EPIC",
            )
        )
    entries = plan["subtasks"]
    if any(
        not isinstance(e, dict) or not isinstance(e.get("id"), str) for e in entries
    ):
        problems.append(PreviewProblem("input", "invalid parent plan subtask"))
        return []
    if len({entry["id"] for entry in entries}) != len(entries):
        problems.append(PreviewProblem("input", "duplicate parent plan subtask id"))
    return cast(list[dict[str, Any]], entries)


def _acceptance(
    snapshot: RemediationSnapshot, entries: list[dict[str, Any]]
) -> dict[str, tuple[str, ...]]:
    sources: dict[str, list[str]] = {}
    body = DECOMPOSITION_PLAN_BLOCK_PATTERN.sub("", snapshot.parent.body)
    for line in body.splitlines():
        match = re.fullmatch(r"\s*[-*+]\s+\[[ xX]\]\s+(.+)", line)
        if match:
            sources.setdefault(" ".join(match[1].split()), []).append(
                "parent checklist: " + match[1]
            )
    for entry in entries:
        values = entry.get("acceptance_criteria", [])
        if isinstance(values, list):
            for value in values:
                if isinstance(value, str):
                    sources.setdefault(" ".join(value.split()), []).append(
                        f"subtask {entry['id']}: {value}"
                    )
    return {key: tuple(value) for key, value in sources.items()}


def _validate_graph(
    spec: RemediationSpec, entries: list[dict[str, Any]]
) -> list[PreviewProblem]:
    graph: dict[str, tuple[str, ...]] = {}
    problems: list[PreviewProblem] = []
    for entry in entries:
        deps = entry.get("depends_on", [])
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            problems.append(
                PreviewProblem("input", f"invalid dependencies for {entry['id']}")
            )
            deps = []
        graph[entry["id"]] = tuple(deps)
    for task in spec.tasks:
        graph[task.subtask.id] = task.subtask.depends_on
    unknown = {
        (name, dep) for name, deps in graph.items() for dep in deps if dep not in graph
    }
    problems.extend(
        PreviewProblem("input", f"unknown dependency: {name} -> {dep}")
        for name, dep in sorted(unknown)
    )
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> bool:
        if name in visiting:
            return True
        if name in visited:
            return False
        visiting.add(name)
        cycle = any(visit(dep) for dep in graph[name] if dep in graph)
        visiting.remove(name)
        visited.add(name)
        return cycle

    if any(visit(name) for name in graph):
        problems.append(PreviewProblem("input", "dependency graph contains a cycle"))
    return problems


def _record(issue: IssueRecord) -> dict[str, Any] | None:
    matches = list(_RECORD.finditer(issue.body))
    if _MARKER not in issue.body:
        return None
    if len(matches) != 1 or issue.body.count(_MARKER) != 1:
        raise ValueError("malformed or duplicate remediation record")
    try:
        value = json.loads(matches[0][1])
    except ValueError as exc:
        raise ValueError("malformed remediation JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("remediation record must be an object")
    return value


def _issue_checks(
    issue: IssueRecord,
    task: RemediationTask,
    snapshot: RemediationSnapshot,
    numbers: dict[str, int],
    own_record: bool,
) -> tuple[list[str], tuple[str, ...]]:
    reasons: list[str] = []
    current = parse_footprint_block(issue.body)
    expected = expected_footprint(task, snapshot.parent.number)
    diff = tuple(
        f"{key}: {((current or {}).get(key))!r} -> {expected.get(key)!r}"
        for key in sorted((current or {}).keys() | expected.keys(), key=str)
        if (current or {}).get(key) != expected.get(key)
    )
    primary = lifecycle_labels(issue.labels)
    if len(primary) > 1 or (
        not own_record
        and (
            issue.state.upper() != "OPEN"
            or not primary <= {StatusLabel.QUEUED, StatusLabel.BLOCKED}
        )
    ):
        reasons.append(
            "existing Issue must be open with no lifecycle or only queued/blocked"
        )
    parents = {
        p
        for p in (
            parent_issue_number_from_body(issue.body),
            issue.parent.get("number") if issue.parent else None,
        )
        if p is not None
    }
    if parents - {snapshot.parent.number}:
        reasons.append("existing Issue belongs to another parent")
    if FOOTPRINT_BLOCK_PATTERN.search(issue.body) and (
        current is None
        or not current.keys() & expected.keys()
        or not current.keys() <= expected.keys() | {"description", "risk", "priority"}
    ):
        reasons.append("first YAML block is not a compatible Footprint mapping")
    if current and current.get("subtask_id") not in (None, task.subtask.id):
        reasons.append("existing Footprint subtask_id differs")
    resolved = {numbers[d] for d in task.subtask.depends_on if d in numbers}
    if set(issue.blocked_by) - resolved:
        reasons.append("native blocked_by cannot be resolved by spec depends_on")
    return reasons, diff


def _registration_steps(
    issue: IssueRecord,
    task: RemediationTask,
    spec: RemediationSpec,
    snapshot: RemediationSnapshot,
    entries: list[dict[str, Any]],
    numbers: dict[str, int],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    current = parse_footprint_block(issue.body) or {}
    expected = expected_footprint(task, snapshot.parent.number)
    primary = lifecycle_labels(issue.labels)
    dependencies = {numbers[d] for d in task.subtask.depends_on if d in numbers}
    entry = next((e for e in entries if e["id"] == task.subtask.id), {})
    remediation = entry.get("remediation", {})
    plan_ready = (
        entry.get("issue_number") == issue.number
        and isinstance(remediation, dict)
        and remediation.get("spec_revision") == spec.spec_revision
    )
    final_ready = issue.state.upper() == "CLOSED" or all(
        re.search(rf"(?m)^⚠️[^\n]*#{issue.number}(?![0-9])", pr.body) is not None
        for pr in snapshot.final_prs
        if pr.state.upper() == "OPEN"
    )
    audit = f"<!-- orchestune:remediation-audit spec_revision={spec.spec_revision} -->"
    stages = {
        "footprint": all(current.get(k) == v for k, v in expected.items()),
        "labels": len(primary) == 1,
        "relationships": (issue.parent or {}).get("number") == snapshot.parent.number
        and dependencies <= set(issue.blocked_by),
        "plan": plan_ready,
        "final-pr": final_ready,
        "audit": any(audit in comment for comment in snapshot.parent_comments),
    }
    return tuple(k for k, v in stages.items() if v), tuple(
        k for k, v in stages.items() if not v
    )


def _scope_checks(
    task: RemediationTask,
    snapshot: RemediationSnapshot,
    sources: dict[str, tuple[str, ...]],
) -> tuple[list[str], list[PreviewProblem]]:
    problems: list[PreviewProblem] = []
    matches: list[str] = []
    for criterion in task.parent_acceptance:
        found = sources.get(" ".join(criterion.split()), ())
        matches.extend(found)
        if not found:
            problems.append(
                PreviewProblem(
                    "scope",
                    f"{task.subtask.id}: unmatched parent_acceptance {criterion!r}; {_SCOPE_GUIDANCE}",
                )
            )
    origins = {o.reference: o for o in snapshot.origins}
    if task.kind not in {"integration-bug", "review-followup"} or not any(
        origins.get(r) and origins[r].valid for r in task.origins
    ):
        problems.append(
            PreviewProblem(
                "scope",
                f"{task.subtask.id}: unsupported kind or no parent-scope origin; {_SCOPE_GUIDANCE}",
            )
        )
    for ref in task.origins:
        if ref not in origins or not origins[ref].valid:
            problems.append(
                PreviewProblem("scope", f"{task.subtask.id}: invalid origin {ref}")
            )
    return matches, problems


def _resolve_target(
    task: RemediationTask,
    spec: RemediationSpec,
    snapshot: RemediationSnapshot,
    entries: list[dict[str, Any]],
    problems: list[PreviewProblem],
) -> tuple[IssueRecord | None, int | None, bool, bool]:
    candidates = {i.number: i for i in (*snapshot.children, *snapshot.existing)}
    discovered = [
        i
        for i in snapshot.children
        if (parse_footprint_block(i.body) or {}).get("subtask_id") == task.subtask.id
    ]
    number = task.subtask.issue_number
    if len(discovered) > 1 or (
        number is not None and any(i.number != number for i in discovered)
    ):
        problems.append(
            PreviewProblem(
                "existing", f"{task.subtask.id}: conflicting child subtask_id"
            )
        )
    if number is None and len(discovered) == 1:
        number = discovered[0].number
    issue = candidates.get(number) if number is not None else None
    record = None
    if issue:
        try:
            record = _record(issue)
        except ValueError as exc:
            problems.append(PreviewProblem("existing", f"{task.subtask.id}: {exc}"))
    own = (
        record is not None
        and record.get("subtask_id") == task.subtask.id
        and record.get("spec_revision") == spec.spec_revision
        and record.get("parent_issue_number") == snapshot.parent.number
    )
    if record is not None and not own:
        problems.append(
            PreviewProblem(
                "existing",
                f"{task.subtask.id}: remediation identity/spec_revision mismatch; manual-review",
            )
        )
    return issue, number, own, bool(discovered)


def _task_preview(
    task: RemediationTask,
    spec: RemediationSpec,
    snapshot: RemediationSnapshot,
    entries: list[dict[str, Any]],
    sources: dict[str, tuple[str, ...]],
    numbers: dict[str, int],
) -> tuple[TaskPreview, list[PreviewProblem]]:
    matches, problems = _scope_checks(task, snapshot, sources)
    issue, number, own, discovered = _resolve_target(
        task, spec, snapshot, entries, problems
    )
    entry = next((e for e in entries if e["id"] == task.subtask.id), None)
    if entry is not None and (
        entry.get("issue_number") != number
        or (not own and (issue is None or _MARKER not in issue.body))
    ):
        problems.append(
            PreviewProblem(
                "input", f"{task.subtask.id}: subtask_id already exists in parent plan"
            )
        )
    diff: tuple[str, ...] = ()
    done: tuple[str, ...] = ()
    remaining: tuple[str, ...] = ()
    action = "new"
    if number is not None and issue is None:
        problems.append(PreviewProblem("existing", f"Issue #{number} not found"))
    if issue:
        reasons, diff = _issue_checks(issue, task, snapshot, numbers, own)
        problems.extend(
            PreviewProblem("existing", f"{task.subtask.id}: {reason}")
            for reason in reasons
        )
        if own:
            done, remaining = _registration_steps(
                issue, task, spec, snapshot, entries, numbers
            )
            action = "resume" if remaining else "registered"
        elif discovered:
            problems.append(
                PreviewProblem(
                    "existing",
                    f"{task.subtask.id}: existing child has no matching remediation record",
                )
            )
    action = "manual-review" if problems else action
    return TaskPreview(
        task, action, number, tuple(matches), diff, done, remaining
    ), problems


def _snapshot_state(snapshot: RemediationSnapshot) -> dict[str, Any]:
    state = asdict(snapshot)
    for key in ("children", "existing"):
        for record in state[key]:
            record["labels"] = sorted(record["labels"])
            record["blocked_by"] = sorted(record["blocked_by"])
    state["parent"]["labels"] = sorted(state["parent"]["labels"])
    state["parent_comments"] = sorted(snapshot.parent_comments)
    return state


def _issue_numbers(
    spec: RemediationSpec, snapshot: RemediationSnapshot, entries: list[dict[str, Any]]
) -> dict[str, int]:
    numbers = {
        e["id"]: e["issue_number"]
        for e in entries
        if isinstance(e.get("issue_number"), int)
    }
    numbers.update(
        {
            t.subtask.id: t.subtask.issue_number
            for t in spec.tasks
            if t.subtask.issue_number is not None
        }
    )
    for child in snapshot.children:
        sid = (parse_footprint_block(child.body) or {}).get("subtask_id")
        if isinstance(sid, str):
            numbers.setdefault(sid, child.number)
    return numbers


def build_remediation_preview(
    spec: RemediationSpec, snapshot: RemediationSnapshot
) -> RemediationPreview:
    """Return every problem and the first applicable exit category, without I/O."""
    problems: list[PreviewProblem] = []
    entries = _parent_plan(snapshot, problems)
    if any(t.subtask.issue_number == snapshot.parent.number for t in spec.tasks):
        problems.append(
            PreviewProblem(
                "input", "parent Issue must not alias a subtask issue_number"
            )
        )
    problems.extend(_validate_graph(spec, entries))
    numbers = _issue_numbers(spec, snapshot, entries)
    sources = _acceptance(snapshot, entries)
    tasks = []
    for task in spec.tasks:
        decision, errors = _task_preview(
            task, spec, snapshot, entries, sources, numbers
        )
        tasks.append(decision)
        problems.extend(errors)
    exit_code = (
        _PRIORITY[min(problems, key=lambda p: _PRIORITY[p.category][0]).category][1]
        if problems
        else (5 if all(t.action == "registered" for t in tasks) else 0)
    )
    state = _snapshot_state(snapshot)
    token = "remediate-preview-v1:sha256:" + stable_hash(
        {
            "spec_revision": spec.spec_revision,
            "targets": sorted(
                (t.subtask.id, t.subtask.issue_number) for t in spec.tasks
            ),
            "snapshot": state,
        }
    )
    return RemediationPreview(
        spec.spec_revision, token, tuple(tasks), tuple(problems), exit_code
    )

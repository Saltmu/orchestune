"""L3 confirmed remediation registration with recoverable commit points."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from orchestune.dag.graph import build_dag
from orchestune.dag.parsing import extract_frontmatter
from orchestune.forge import GitHubForge
from orchestune.integrator.pr import mark_parent_final_pr_pending
from orchestune.issue_parsing import (
    decomposition_plan_from_parent_body,
    parse_footprint_block,
)
from orchestune.labels import StatusLabel
from orchestune.plan_writer import validate_subtask_issue_number_targets
from orchestune.provisioning.plan import GITHUB_ISSUE_BODY_LIMIT
from orchestune.provisioning.plan_manifest import PROJECTED_ISSUE_NUMBER
from orchestune.provisioning.rendering import (
    build_subtask_issue_body,
    validate_template_identity_marker,
)
from orchestune.remediation.audit import audit_once
from orchestune.remediation.body import (
    append_plan,
    plan_block,
    registration_body,
    remediation_record,
    validate_registered_body,
)
from orchestune.remediation.models import (
    RemediationPreview,
    RemediationResult,
    RemediationSnapshot,
    RemediationSpec,
)
from orchestune.remediation.operations import link_child, register_child
from orchestune.remediation.plan_sources import build_dag_subtasks
from orchestune.remediation.preview import build_remediation_preview
from orchestune.remediation.snapshot import collect_remediation_snapshot
from orchestune.remediation.spec import load_remediation_spec, positive_number


def _entries(
    spec: RemediationSpec, raw: dict[str, Any], parent: int, numbers: dict[str, int]
) -> list[dict[str, Any]]:
    tasks = {t.subtask.id: t for t in spec.tasks}
    result = []
    for entry in raw["subtasks"]:
        entry = {
            k: v
            for k, v in entry.items()
            if k not in {"kind", "parent_acceptance", "origins", "reason"}
        }
        sid = entry["id"].strip()
        entry["id"] = sid
        entry["issue_number"] = numbers[sid]
        entry["remediation"] = remediation_record(
            tasks[sid], parent, spec.spec_revision
        )
        result.append(entry)
    return result


def _assert_size(body: str) -> None:
    if len(body) > GITHUB_ISSUE_BODY_LIMIT:
        raise ValueError("Issue body exceeds GitHub's size limit")


def _prepare(
    spec: RemediationSpec,
    snapshot: RemediationSnapshot,
    preview: RemediationPreview,
    path: Path,
    template_path: str | Path,
    root: Path,
) -> tuple[dict[str, str], dict[str, Any]]:
    validate_subtask_issue_number_targets(path, (t.subtask.id for t in spec.tasks))
    raw = extract_frontmatter(path.read_text(encoding="utf-8"))
    projected = {
        p.task.subtask.id: p.issue_number or PROJECTED_ISSUE_NUMBER
        for p in preview.tasks
    }
    updated = append_plan(
        snapshot.parent.body, _entries(spec, raw, snapshot.parent.number, projected)
    )
    _assert_size(updated)
    plan = decomposition_plan_from_parent_body(updated)
    assert plan is not None
    by_id = {e["id"]: e for e in plan["subtasks"]}
    known_numbers = {
        e["issue_number"]: e["id"]
        for e in plan["subtasks"]
        if e.get("issue_number") and e["id"] not in projected
    }
    for item in preview.tasks:
        if item.issue_number in known_numbers:
            raise ValueError("Issue number already belongs to another parent-plan task")
    for task in spec.tasks:
        for dependency in task.subtask.depends_on:
            positive_number(
                by_id[dependency].get("issue_number"),
                f"dependency {dependency} issue_number",
            )
    validation_subtasks = build_dag_subtasks(plan["subtasks"], snapshot, spec.tasks)
    build_dag(validation_subtasks, repo_root=root)
    return _bodies(spec, snapshot, preview, template_path, root), raw


def _bodies(
    spec: RemediationSpec,
    snapshot: RemediationSnapshot,
    preview: RemediationPreview,
    template_path: str | Path,
    root: Path,
) -> dict[str, str]:
    template = Path(template_path).read_text(encoding="utf-8")
    validate_template_identity_marker(template, template_path)
    existing = {i.number: i for i in (*snapshot.children, *snapshot.existing)}
    bodies = {}
    for item in preview.tasks:
        task = item.task
        original = existing.get(item.issue_number) if item.issue_number else None
        body = (
            original.body
            if original
            else build_subtask_issue_body(
                task.subtask, template, root, snapshot.parent.number
            )
        )
        body = registration_body(body, task, snapshot.parent.number, spec.spec_revision)
        _assert_size(body)
        validate_registered_body(
            replace(original or snapshot.parent, body=body),
            task,
            snapshot.parent.number,
        )
        bodies[task.subtask.id] = body
    return bodies


def _sync_plan(
    forge: Any, snapshot: RemediationSnapshot, entries: list[dict[str, Any]]
) -> None:
    current = forge.get_issue(snapshot.parent.number)
    if current is None or current.state.upper() != "OPEN":
        raise ValueError("parent no longer open")
    if plan_block(current.body) != plan_block(snapshot.parent.body):
        raise ValueError("parent plan fingerprint changed")
    updated = append_plan(current.body, entries)
    _assert_size(updated)
    if updated != current.body:
        forge.update_issue_body(current.number, updated)
    written = forge.get_issue(current.number)
    if written is None or written.body != updated:
        raise ValueError("parent readback differs; check concurrent updates")


def _finish(
    forge: Any,
    spec: RemediationSpec,
    snapshot: RemediationSnapshot,
    numbers: dict[str, int],
    raw: dict[str, Any],
    path: Path,
    template: str | None = None,
) -> None:
    plan = decomposition_plan_from_parent_body(snapshot.parent.body)
    assert plan is not None
    resolved = {
        e["id"]: e["issue_number"] for e in plan["subtasks"] if e.get("issue_number")
    }
    resolved.update(numbers)
    for task in spec.tasks:
        link_child(
            forge,
            snapshot.parent.number,
            numbers[task.subtask.id],
            tuple(resolved[d] for d in task.subtask.depends_on),
        )
    fresh = collect_remediation_snapshot(
        forge, snapshot.parent.number, load_remediation_spec(path)
    )
    if fresh.parent_merged or fresh.parent.state.upper() != "OPEN":
        raise ValueError("parent state changed; cannot finish registration")
    if set(numbers.values()) - {child.number for child in fresh.children}:
        raise ValueError(
            "registered child is not discoverable by native relationship or parent metadata"
        )
    _sync_plan(forge, snapshot, _entries(spec, raw, snapshot.parent.number, numbers))
    mark_parent_final_pr_pending(snapshot.parent.number, fresh.children, forge)
    if any(pr.state.upper() == "OPEN" for pr in fresh.final_prs):
        checked = collect_remediation_snapshot(forge, snapshot.parent.number, spec)
        decision = build_remediation_preview(spec, checked, template=template)
        if any("final-pr" in item.remaining_steps for item in decision.tasks):
            raise ValueError("final PR pending update is incomplete")
    audit_once(
        forge,
        snapshot.parent.number,
        spec.spec_revision,
        _entries(spec, raw, snapshot.parent.number, numbers),
    )


def _completed_ids(snapshot: RemediationSnapshot) -> set[str]:
    issues = {i.number: i for i in (*snapshot.children, *snapshot.existing)}
    done_numbers = {n for n, i in issues.items() if StatusLabel.DONE in i.labels}
    result = {
        (parse_footprint_block(i.body) or {}).get("subtask_id")
        for i in issues.values()
        if i.number in done_numbers
    }
    plan = decomposition_plan_from_parent_body(snapshot.parent.body) or {}
    result.update(
        e["id"]
        for e in plan.get("subtasks", [])
        if e.get("issue_number") in done_numbers
    )
    return {sid for sid in result if isinstance(sid, str)}


def _register_children(
    forge: Any,
    snapshot: RemediationSnapshot,
    preview: RemediationPreview,
    bodies: dict[str, str],
    path: Path,
    numbers: dict[str, int],
) -> None:
    for item in preview.tasks:
        sid = item.task.subtask.id
        numbers[sid] = register_child(
            forge,
            item.task,
            snapshot.parent.number,
            bodies[sid],
            item.issue_number,
            path,
            dependencies_done=set(item.task.subtask.depends_on)
            <= _completed_ids(snapshot),
        )


class _Rejected(ValueError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _validated_preview(
    forge: Any,
    path: Path,
    confirm_token: str,
    parent_issue_number: int | None,
    template: str | None = None,
) -> tuple[RemediationSpec, RemediationSnapshot, RemediationPreview]:
    spec = load_remediation_spec(path)
    if parent_issue_number is not None and spec.parent_issue_number not in (
        None,
        parent_issue_number,
    ):
        raise ValueError("--parent-issue conflicts with spec parent_issue_number")
    parent = positive_number(
        parent_issue_number
        if parent_issue_number is not None
        else spec.parent_issue_number,
        "parent_issue_number",
    )
    snapshot = collect_remediation_snapshot(forge, parent, spec)
    preview = build_remediation_preview(spec, snapshot, template=template)
    if confirm_token != preview.preview_token:
        raise _Rejected(3, "Confirmed preview token is stale; run preview again")
    if preview.exit_code:
        raise _Rejected(
            preview.exit_code,
            "; ".join(p.message for p in preview.problems) or "All tasks registered",
        )
    return spec, snapshot, preview


def _preflight_remediation(
    forge: Any,
    path: Path,
    confirm_token: str,
    parent_issue_number: int | None,
    template_path: str | Path,
    repo_root: str | Path | None,
) -> tuple[
    RemediationSpec,
    RemediationSnapshot,
    RemediationPreview,
    dict[str, str],
    dict[str, Any],
    str,
]:
    template = Path(template_path).read_text(encoding="utf-8")
    validate_template_identity_marker(template, template_path)
    spec, snapshot, preview = _validated_preview(
        forge, path, confirm_token, parent_issue_number, template=template
    )
    parent = snapshot.parent.number
    bodies, raw = _prepare(
        spec, snapshot, preview, path, template_path, Path(repo_root or Path.cwd())
    )
    # Recheck parent, children, and local spec immediately before the first write.
    fresh_spec = load_remediation_spec(path)
    fresh = collect_remediation_snapshot(forge, parent, fresh_spec)
    if (
        build_remediation_preview(fresh_spec, fresh, template=template).preview_token
        != confirm_token
    ):
        raise _Rejected(3, "State changed during preflight; run preview again")
    return spec, snapshot, preview, bodies, raw, template


def apply_remediation(
    spec_path: str | Path,
    confirm_token: str,
    *,
    forge: Any = None,
    template_path: str | Path = ".github/issue_template.md",
    repo_root: str | Path | None = None,
    parent_issue_number: int | None = None,
) -> RemediationResult:
    """Revalidate confirmation, preflight all tasks, then register without rollback."""
    forge = forge if forge is not None else GitHubForge()
    path = Path(spec_path)
    try:
        spec, snapshot, preview, bodies, raw, template = _preflight_remediation(
            forge, path, confirm_token, parent_issue_number, template_path, repo_root
        )
    except _Rejected as exc:
        return RemediationResult(exc.code, str(exc))
    except Exception as exc:
        return RemediationResult(2, str(exc))
    numbers: dict[str, int] = {}
    try:
        _register_children(forge, snapshot, preview, bodies, path, numbers)
        _finish(forge, spec, snapshot, numbers, raw, path, template=template)
    except Exception as exc:
        return RemediationResult(
            4,
            f"Partial registration: {exc}. Run preview again and resume with its new token",
            tuple(numbers.values()),
        )
    return RemediationResult(
        0,
        f"Registered. Next orchestune dispatch -p {snapshot.parent.number} cycle will process these tasks",
        tuple(numbers.values()),
    )

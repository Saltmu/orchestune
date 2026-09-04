"""L3 workflow for atomically ordered decomposition-generation replacement."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, cast

from orchestune.dag.parsing import extract_frontmatter_and_body
from orchestune.forge import GitHubForge
from orchestune.issue_parsing import embed_decomposition_plan_in_parent_body
from orchestune.provisioning.plan import GITHUB_ISSUE_BODY_LIMIT
from orchestune.provisioning.rendering import _validate_template_identity_marker
from orchestune.replan.audit import (
    find_existing_replan_audit,
    render_replan_audit,
)
from orchestune.replan.models import ReplacementResult
from orchestune.replan.operations import (
    ReplanOperationForge,
    create_replan_generation,
    link_replan_generation,
    retire_replan_generation,
)
from orchestune.replan.plan import load_replan_plan
from orchestune.replan.preview import ReplanPreview, build_replan_preview
from orchestune.replan.snapshot import (
    ReplanSnapshot,
    ReplanSnapshotForge,
    collect_replan_snapshot,
)
from orchestune.validation import validate_issue_number


class ReplanApplyForge(ReplanOperationForge, ReplanSnapshotForge, Protocol):
    def update_issue_body(self, issue_number: int | str, body: str) -> None: ...


def _parent_number(plan_parent: int | None, explicit_parent: int | None) -> int:
    if plan_parent is None and explicit_parent is None:
        raise ValueError("replan requires a parent Issue number")
    if (
        plan_parent is not None
        and explicit_parent is not None
        and plan_parent != explicit_parent
    ):
        raise ValueError("plan and explicit parent Issue numbers do not match")
    return validate_issue_number(
        explicit_parent if explicit_parent is not None else plan_parent  # type: ignore[arg-type]
    )


def _decision_maps(
    preview: ReplanPreview,
) -> dict[str, int | None]:
    generations = {generation.subtask_id for generation in preview.generations}
    return {
        decision.subject: decision.issue_number
        for decision in preview.decisions
        if decision.subject in generations and decision.action in {"create", "reuse"}
    }


def _already_active_generation(
    preview: ReplanPreview, snapshot: ReplanSnapshot
) -> bool:
    new = _decision_maps(preview)
    current = {
        candidate.subtask_id: candidate.issue_number
        for candidate in snapshot.retirement_candidates
    }
    return (
        bool(new)
        and current == new
        and all(number is not None for number in new.values())
    )


def _assert_safe(preview: ReplanPreview) -> None:
    unsafe = [
        decision
        for decision in preview.decisions
        if decision.action in {"manual-review", "conflict"}
    ]
    if unsafe:
        detail = "; ".join(
            f"{decision.subject}: {decision.reason}" for decision in unsafe
        )
        raise ValueError(f"replan cannot be applied automatically: {detail}")


def _switch_parent_plan(
    forge: ReplanApplyForge, parent_issue_number: int, plan_path: str | Path
) -> None:
    parent = forge.get_issue(parent_issue_number)
    if parent is None:
        raise ValueError(f"parent Issue #{parent_issue_number} was not found")
    plan_data, _ = extract_frontmatter_and_body(
        Path(plan_path).read_text(encoding="utf-8")
    )
    updated = embed_decomposition_plan_in_parent_body(parent.body, plan_data)
    if len(updated) > GITHUB_ISSUE_BODY_LIMIT:
        raise ValueError("updated parent Issue body exceeds GitHub's size limit")
    if updated != parent.body:
        forge.update_issue_body(parent_issue_number, updated)


def _audit_once(
    forge: ReplanApplyForge, parent_issue_number: int, result: ReplacementResult
) -> None:
    if not find_existing_replan_audit(forge, parent_issue_number, result.plan_revision):
        forge.add_comment(parent_issue_number, render_replan_audit(result))


def _completed_result(preview: ReplanPreview) -> ReplacementResult:
    new = _decision_maps(preview)
    return ReplacementResult(
        preview.plan_revision,
        reused_issue_numbers=tuple(
            number for number in new.values() if number is not None
        ),
    )


def _prepare_generations(
    forge: ReplanApplyForge,
    preview: ReplanPreview,
    plan_path: str | Path,
    template: str,
    repo_root: Path,
    parent_issue_number: int,
) -> tuple[dict[str, int], list[int], list[int]]:
    plan = load_replan_plan(plan_path)
    decisions = _decision_maps(preview)
    generations = {item.subtask_id: item for item in preview.generations}
    resolved: dict[str, int] = {}
    created: list[int] = []
    reused: list[int] = []
    for subtask in sorted(plan.subtasks, key=lambda item: item.id):
        operation = create_replan_generation(
            forge,
            subtask,
            generations[subtask.id],
            template=template,
            repo_root=repo_root,
            plan_path=plan_path,
            parent_issue_number=parent_issue_number,
            existing_issue_number=decisions[subtask.id],
        )
        resolved[subtask.id] = operation.issue_number
        (created if operation.created else reused).append(operation.issue_number)
    return resolved, created, reused


def _link_generations(
    forge: ReplanApplyForge,
    plan_path: str | Path,
    parent_issue_number: int,
    resolved: dict[str, int],
) -> bool:
    plan = load_replan_plan(plan_path)
    degraded = False
    for subtask in sorted(plan.subtasks, key=lambda item: item.id):
        degraded |= link_replan_generation(
            forge,
            parent_issue_number,
            resolved[subtask.id],
            tuple(resolved[dependency] for dependency in subtask.depends_on),
        )
    return degraded


def apply_replan(
    plan_path: str | Path,
    confirm_token: str,
    *,
    forge: ReplanApplyForge | None = None,
    template_path: str | Path = ".github/issue_template.md",
    repo_root: str | Path | None = None,
    parent_issue_number: int | None = None,
) -> ReplacementResult:
    """Apply a freshly confirmed preview in recoverable, fail-closed phases."""

    resolved_forge = cast(ReplanApplyForge, forge or GitHubForge())
    plan = load_replan_plan(plan_path)
    parent = _parent_number(plan.parent_issue_number, parent_issue_number)
    snapshot = collect_replan_snapshot(resolved_forge, parent)
    preview = build_replan_preview(plan, snapshot)
    if confirm_token != preview.preview_token:
        raise ValueError("confirmed preview token does not match the current snapshot")
    if _already_active_generation(preview, snapshot):
        result = _completed_result(preview)
        _audit_once(resolved_forge, parent, result)
        return result
    _assert_safe(preview)

    root = Path(repo_root) if repo_root is not None else Path.cwd()
    template = Path(template_path).read_text(encoding="utf-8")
    _validate_template_identity_marker(template, template_path)
    resolved, created, reused = _prepare_generations(
        resolved_forge, preview, plan_path, template, root, parent
    )
    degraded = _link_generations(resolved_forge, plan_path, parent, resolved)
    retired: list[int] = []
    for candidate in snapshot.retirement_candidates:
        retirement = retire_replan_generation(
            resolved_forge,
            parent,
            candidate,
            preview.plan_revision,
            replacement_issue_numbers=tuple(resolved.values()),
        )
        retired.append(retirement.issue_number)
        degraded |= retirement.degraded
    result = ReplacementResult(
        preview.plan_revision,
        tuple(created),
        tuple(reused),
        tuple(retired),
        degraded,
    )
    _audit_once(resolved_forge, parent, result)
    _switch_parent_plan(resolved_forge, parent, plan_path)
    return result


__all__ = ["apply_replan"]

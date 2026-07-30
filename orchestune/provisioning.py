"""Codifies `decomposition_plan.md` -> GitHub Issue provisioning (Stage A).

Turns the prose procedure in `skills/orchestune-dispatch/SKILL.md` into a
deterministic, idempotent, resumable transformation: an approved plan's
`SubTask` fields carry everything `.github/issue_template.md` needs, so
filing is pure code rather than an agent re-interpreting instructions each
run.
"""

from __future__ import annotations

import argparse
import dataclasses
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from orchestune.dag_graph import build_dag
from orchestune.dag_models import SubTask
from orchestune.dag_parsing import extract_frontmatter, parse_decomposition_plan
from orchestune.forge import GitHubForge, IssueForge
from orchestune.plan_writer import write_issue_numbers

_SUBTASK_ID_IN_BODY = re.compile(r"subtask_id:\s*(\S+)")

_PLACEHOLDERS = (
    "subtask_id",
    "description",
    "overview",
    "proposed_changes",
    "acceptance_criteria",
    "verification_plan",
    "footprint",
    "symbols",
    "depends_on",
)


@dataclass(frozen=True)
class PlanMetadata:
    title: str
    parent_issue_number: int | None


@dataclass(frozen=True)
class IssuePreview:
    subtask_id: str
    title: str
    body: str
    labels: tuple[str, ...]
    already_has_issue: bool


@dataclass(frozen=True)
class ProvisionResult:
    parent_issue_number: int | None
    applied: bool
    created: dict[str, int]
    reused: dict[str, int]
    previews: tuple[IssuePreview, ...] = ()


def _load_plan(path: str | Path) -> tuple[list[SubTask], PlanMetadata]:
    subtasks = parse_decomposition_plan(path)
    raw = extract_frontmatter(Path(path).read_text(encoding="utf-8"))

    issue_numbers: dict[str, int] = {}
    for entry in raw.get("subtasks") or []:
        if isinstance(entry, dict) and entry.get("issue_number") not in (None, ""):
            issue_numbers[str(entry["id"])] = int(entry["issue_number"])

    enriched = [
        dataclasses.replace(subtask, issue_number=issue_numbers.get(subtask.id))
        for subtask in subtasks
    ]
    raw_parent = raw.get("parent_issue_number")
    parent_issue_number = (
        None if raw_parent is None or raw_parent == "" else int(raw_parent)
    )
    metadata = PlanMetadata(
        title=str(raw.get("title") or "").strip(),
        parent_issue_number=parent_issue_number,
    )
    return enriched, metadata


def _derive_labels(subtask: SubTask, *, dependencies_done: bool) -> tuple[str, ...]:
    """Pure label derivation: status/priority/risk from the subtask's own fields."""
    labels = [
        "status:blocked"
        if subtask.depends_on and not dependencies_done
        else "status:queued"
    ]
    labels.append(f"priority:{subtask.priority}")
    if subtask.risk:
        labels.append("risk:flagged")
    return tuple(labels)


def _issue_title(subtask: SubTask) -> str:
    return f"[FEAT] {subtask.id}: {subtask.description}"


def _yaml_inline_list(items: Sequence[str]) -> str:
    return yaml.dump(list(items), default_flow_style=True, allow_unicode=True).strip()


def _bullet_list(items: Sequence[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "特になし"


def _render_issue_body(subtask: SubTask, template: str) -> str:
    values = {
        "subtask_id": subtask.id,
        "description": subtask.description,
        "overview": subtask.overview or "特になし",
        "proposed_changes": _bullet_list(subtask.proposed_changes),
        "acceptance_criteria": _bullet_list(subtask.acceptance_criteria),
        "verification_plan": _bullet_list(subtask.verification_plan),
        "footprint": _yaml_inline_list(subtask.footprint),
        "symbols": _yaml_inline_list(subtask.symbols),
        "depends_on": _yaml_inline_list(subtask.depends_on),
    }
    body = template
    for placeholder in _PLACEHOLDERS:
        body = body.replace(f"{{{{{placeholder}}}}}", values[placeholder])
    return body


def _index_sub_issues_by_subtask_id(
    forge: IssueForge, parent_issue_number: int
) -> dict[str, int]:
    index: dict[str, int] = {}
    for record in forge.list_sub_issues(parent_issue_number):
        match = _SUBTASK_ID_IN_BODY.search(record.body or "")
        if match:
            index.setdefault(match.group(1), record.number)
    return index


def _preview_only(
    subtasks: list[SubTask], dag_order: list[str], template: str
) -> ProvisionResult:
    by_id = {subtask.id: subtask for subtask in subtasks}
    previews = tuple(
        IssuePreview(
            subtask_id=subtask_id,
            title=_issue_title(by_id[subtask_id]),
            body=_render_issue_body(by_id[subtask_id], template),
            labels=_derive_labels(by_id[subtask_id], dependencies_done=False),
            already_has_issue=by_id[subtask_id].issue_number is not None,
        )
        for subtask_id in dag_order
    )
    return ProvisionResult(
        parent_issue_number=None,
        applied=False,
        created={},
        reused={},
        previews=previews,
    )


def provision_issues(
    plan_path: str | Path,
    forge: IssueForge | None = None,
    apply: bool = True,
    template_path: str | Path = ".github/issue_template.md",
) -> ProvisionResult:
    """Provision GitHub Issues for every subtask in an approved decomposition plan.

    Idempotent and resumable: each subtask's `issue_number` (or a matching
    `subtask_id` found in an existing sub-issue's body) short-circuits
    re-creation, and every resolved number is written back to `plan_path`
    immediately, before moving on to the next subtask.
    """
    subtasks, metadata = _load_plan(plan_path)
    if not metadata.title:
        raise ValueError(
            "decomposition_plan.md に必須の 'title' フィールドがありません"
        )

    dag = build_dag(subtasks)
    template = Path(template_path).read_text(encoding="utf-8")

    if not apply:
        return _preview_only(subtasks, dag.topological_order, template)

    resolved_forge = forge or GitHubForge()

    parent_issue_number = metadata.parent_issue_number
    if parent_issue_number is None:
        parent_body = f"{metadata.title}\n\n配下のサブタスクはこのIssueのSub-issueとして紐付けられます。"
        parent_issue_number = resolved_forge.create_issue(
            f"[EPIC] {metadata.title}", parent_body
        )
        write_issue_numbers(plan_path, parent_issue_number=parent_issue_number)

    existing_by_subtask_id = _index_sub_issues_by_subtask_id(
        resolved_forge, parent_issue_number
    )

    resolved_numbers: dict[str, int] = {}
    dependencies_done: dict[str, bool] = {}
    created: dict[str, int] = {}
    reused: dict[str, int] = {}

    for subtask_id in dag.topological_order:
        subtask = dag.subtasks[subtask_id]
        number = subtask.issue_number or existing_by_subtask_id.get(subtask_id)

        if number is not None:
            reused[subtask_id] = number
            labels = resolved_forge.get_issue_labels(number)
            dependencies_done[subtask_id] = "status:done" in labels
        else:
            all_deps_done = all(
                dependencies_done.get(dep, False) for dep in subtask.depends_on
            )
            labels = _derive_labels(subtask, dependencies_done=all_deps_done)
            body = _render_issue_body(subtask, template)
            number = resolved_forge.create_issue(
                _issue_title(subtask), body, labels=labels
            )
            resolved_forge.add_sub_issue(parent_issue_number, number)
            for dependency_id in subtask.depends_on:
                resolved_forge.set_blocked_by(number, resolved_numbers[dependency_id])
            created[subtask_id] = number
            dependencies_done[subtask_id] = False

        resolved_numbers[subtask_id] = number
        write_issue_numbers(plan_path, {subtask_id: number})

    return ProvisionResult(
        parent_issue_number=parent_issue_number,
        applied=True,
        created=created,
        reused=reused,
    )


def _print_result(result: ProvisionResult) -> None:
    if not result.applied:
        print("Dry run (--no-apply): no Issues were created.")
        for preview in result.previews:
            status = "reuse expected" if preview.already_has_issue else "would create"
            print(f"\n=== {preview.subtask_id} ({status}) ===")
            print(f"Title: {preview.title}")
            print(f"Labels: {', '.join(preview.labels)}")
            print(preview.body)
        return

    print(f"Parent issue: #{result.parent_issue_number}")
    print(f"Created: {len(result.created)}")
    for subtask_id, number in result.created.items():
        print(f"  + {subtask_id} -> #{number}")
    print(f"Reused: {len(result.reused)}")
    for subtask_id, number in result.reused.items():
        print(f"  = {subtask_id} -> #{number}")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="orchestune provision: decomposition_plan.mdからIssueを起票する"
    )
    parser.add_argument(
        "--plan",
        default="decomposition_plan.md",
        help="Path to the decomposition plan markdown file (default: decomposition_plan.md)",
    )
    parser.add_argument(
        "--template",
        default=".github/issue_template.md",
        help="Path to the issue body template (default: .github/issue_template.md)",
    )
    parser.add_argument("--apply", dest="apply", action="store_true", default=True)
    parser.add_argument("--no-apply", dest="apply", action="store_false")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _build_arg_parser().parse_args(argv)

    try:
        result = provision_issues(
            args.plan, apply=args.apply, template_path=args.template
        )
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    _print_result(result)
    raise SystemExit(0)

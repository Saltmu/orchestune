"""L3 workflow that turns a decomposition plan into GitHub Issues."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from orchestune.dag_graph import build_dag
from orchestune.dag_models import (
    DagResult,
    SubTask,
    compile_extra_ignore_patterns,
    extract_dag_ignore_patterns,
    extract_dag_similarity_threshold,
    load_orchestune_config,
)
from orchestune.dag_similarity import DEFAULT_SIMILARITY_THRESHOLD
from orchestune.forge import GitHubForge, IssueForge, RelationshipUnavailableError
from orchestune.plan_writer import write_issue_numbers
from orchestune.provisioning_parent import _resolve_parent_issue
from orchestune.provisioning_plan import _load_plan, sync_parent_decomposition_plan
from orchestune.provisioning_rendering import (
    _build_subtask_issue_body,
    _derive_labels,
    _issue_title,
    _validate_template_identity_marker,
)
from orchestune.provisioning_subtasks import (
    _index_sub_issues_by_subtask_id,
    _link_subtask_relationships,
    _provision_subtask,
)
from orchestune.validation import validate_issue_number


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
    degraded_subtask_ids: tuple[str, ...] = ()
    plan_synced: bool = True


def _build_provisioning_dag(subtasks: list[SubTask], repo_root: Path) -> DagResult:
    config = load_orchestune_config(repo_root)
    threshold = extract_dag_similarity_threshold(config)
    return build_dag(
        subtasks,
        ignore_patterns=compile_extra_ignore_patterns(
            extract_dag_ignore_patterns(config)
        ),
        threshold=DEFAULT_SIMILARITY_THRESHOLD if threshold is None else threshold,
    )


def _preview_only(
    subtasks: list[SubTask],
    dag_order: list[str],
    template: str,
    repo_root: Path,
    parent_issue_number: int | None = None,
) -> ProvisionResult:
    by_id = {subtask.id: subtask for subtask in subtasks}
    previews = tuple(
        IssuePreview(
            subtask_id,
            _issue_title(by_id[subtask_id]),
            _build_subtask_issue_body(
                by_id[subtask_id], template, repo_root, parent_issue_number
            ),
            _derive_labels(by_id[subtask_id], dependencies_done=False),
            by_id[subtask_id].issue_number is not None,
        )
        for subtask_id in dag_order
    )
    return ProvisionResult(parent_issue_number, False, {}, {}, previews=previews)


def provision_issues(
    plan_path: str | Path,
    forge: IssueForge | None = None,
    apply: bool = True,
    template_path: str | Path = ".github/issue_template.md",
    repo_root: str | Path | None = None,
    parent_issue: int | None = None,
) -> ProvisionResult:
    """Provision plan subtasks idempotently, optionally under an adopted EPIC.

    ``repo_root`` resolves footprints and configuration independently of the
    caller's working directory. ``parent_issue`` adopts and normalizes an
    existing Issue instead of deriving a new parent from the plan title.
    """
    resolved_repo_root = Path(repo_root) if repo_root is not None else Path.cwd()
    subtasks, metadata = _load_plan(plan_path)
    if not metadata.title:
        raise ValueError(
            "decomposition_plan.md に必須の 'title' フィールドがありません"
        )
    if parent_issue is not None:
        parent_issue = validate_issue_number(parent_issue)
    dag = _build_provisioning_dag(subtasks, resolved_repo_root)
    template = Path(template_path).read_text(encoding="utf-8")
    _validate_template_identity_marker(template, template_path)
    if not apply:
        return _preview_only(
            subtasks,
            dag.topological_order,
            template,
            resolved_repo_root,
            parent_issue if parent_issue is not None else metadata.parent_issue_number,
        )
    resolved_forge = forge or GitHubForge()
    parent_issue_number, plan_synced = _resolve_parent_issue(
        resolved_forge, metadata, plan_path, explicit_parent_issue=parent_issue
    )
    existing_by_subtask_id, metadata_search_supported = _index_sub_issues_by_subtask_id(
        resolved_forge, parent_issue_number
    )
    resolved_numbers: dict[str, int] = {}
    dependencies_done: dict[str, bool] = {}
    created: dict[str, int] = {}
    reused: dict[str, int] = {}
    degraded_subtask_ids: list[str] = []
    for subtask_id in dag.topological_order:
        subtask = dag.subtasks[subtask_id]
        number, is_reused, is_done, has_parent_metadata = _provision_subtask(
            resolved_forge,
            subtask,
            template,
            resolved_repo_root,
            plan_path,
            existing_by_subtask_id,
            dependencies_done,
            parent_issue_number,
        )
        (reused if is_reused else created)[subtask_id] = number
        dependencies_done[subtask_id] = is_done
        link_result = _link_subtask_relationships(
            resolved_forge,
            parent_issue_number,
            number,
            subtask.depends_on,
            resolved_numbers,
        )
        if (
            not (has_parent_metadata and metadata_search_supported)
            and not link_result.parent_linked
        ):
            raise RelationshipUnavailableError(
                f"#{number} ({subtask_id}) has neither a native parent link nor discoverable parent_issue_number body metadata; this forge cannot reliably link it to its parent Issue"
            )
        if link_result.degraded:
            degraded_subtask_ids.append(subtask_id)
        resolved_numbers[subtask_id] = number
        write_issue_numbers(plan_path, {subtask_id: number})
        if not sync_parent_decomposition_plan(
            resolved_forge, parent_issue_number, plan_path
        ):
            plan_synced = False
    return ProvisionResult(
        parent_issue_number,
        True,
        created,
        reused,
        degraded_subtask_ids=tuple(degraded_subtask_ids),
        plan_synced=plan_synced,
    )

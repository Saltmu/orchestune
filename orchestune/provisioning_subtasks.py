"""Create, reuse, and connect the subtask Issues of a provisioned plan."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from orchestune.dag.models import SubTask
from orchestune.forge import IssueForge, RelationshipUnavailableError
from orchestune.issue_parsing import (
    backfill_parent_issue_number,
    effective_parent_number,
    find_children_by_parent,
)
from orchestune.models import IssueRecord
from orchestune.plan_writer import write_issue_numbers
from orchestune.provisioning_rendering import (
    _build_subtask_issue_body,
    _derive_labels,
    _issue_title,
    _subtask_id_from_body,
)


def _index_sub_issues_by_subtask_id(
    forge: IssueForge, parent_issue_number: int
) -> tuple[dict[str, IssueRecord], bool]:
    result = find_children_by_parent(forge, parent_issue_number)
    index: dict[str, IssueRecord] = {}
    for record in result.issues:
        subtask_id = _subtask_id_from_body(record.body)
        if subtask_id:
            index.setdefault(subtask_id, record)
    return index, result.metadata_search_supported


def _ensure_reused_issue_is_discoverable(
    forge: IssueForge, candidate: IssueRecord, parent_issue_number: int
) -> bool:
    """Ensure a reused Issue remains findable by its provisioning parent.

    A native parent is authoritative over body metadata. When it names a
    different parent, backfilling the body cannot repair discovery, so only a
    later native re-link may correct that state.
    """
    if effective_parent_number(candidate) == parent_issue_number:
        return True
    if candidate.parent and candidate.parent.get("number") is not None:
        return False
    new_body = backfill_parent_issue_number(candidate.body, parent_issue_number)
    if new_body is None:
        return True
    try:
        forge.update_issue_body(candidate.number, new_body)
        return True
    except (RelationshipUnavailableError, AttributeError, NotImplementedError) as error:
        print(
            f"Warning: could not backfill parent_issue_number into #{candidate.number}'s body: {error}",
            file=sys.stderr,
        )
        return False


def _provision_subtask(
    forge: IssueForge,
    subtask: SubTask,
    template: str,
    repo_root: Path,
    plan_path: str | Path,
    existing_by_subtask_id: dict[str, IssueRecord],
    dependencies_done: dict[str, bool],
    parent_issue_number: int,
) -> tuple[int, bool, bool, bool]:
    number = None
    candidate: IssueRecord | None = None
    if subtask.issue_number is not None:
        fetched = forge.get_issue(subtask.issue_number)
        if fetched is not None and _subtask_id_from_body(fetched.body) == subtask.id:
            number, candidate = subtask.issue_number, fetched
    if (
        number is None
        and (existing := existing_by_subtask_id.get(subtask.id)) is not None
    ):
        number, candidate = existing.number, existing
    if number is not None:
        has_parent_metadata = (
            _ensure_reused_issue_is_discoverable(forge, candidate, parent_issue_number)
            if candidate is not None
            else True
        )
        return (
            number,
            True,
            "status:done" in forge.get_issue_labels(number),
            has_parent_metadata,
        )

    all_deps_done = all(dependencies_done.get(dep, False) for dep in subtask.depends_on)
    number = forge.create_issue(
        _issue_title(subtask),
        _build_subtask_issue_body(subtask, template, repo_root, parent_issue_number),
        labels=_derive_labels(subtask, dependencies_done=all_deps_done),
    )
    # Persist before relationship writes: a failed link must be retryable by
    # this stable issue number rather than creating an orphaned duplicate.
    write_issue_numbers(plan_path, {subtask.id: number})
    return number, False, False, True


@dataclass(frozen=True)
class RelationshipLinkResult:
    parent_linked: bool
    unresolved_dependencies: tuple[str, ...]

    @property
    def degraded(self) -> bool:
        return not self.parent_linked or bool(self.unresolved_dependencies)


def _link_subtask_relationships(
    forge: IssueForge,
    parent_issue_number: int,
    issue_number: int,
    depends_on: Sequence[str],
    resolved_numbers: dict[str, int],
) -> RelationshipLinkResult:
    unavailable_errors = (
        RelationshipUnavailableError,
        AttributeError,
        NotImplementedError,
    )
    parent_linked = True
    try:
        forge.add_sub_issue(parent_issue_number, issue_number)
    except unavailable_errors as error:
        parent_linked = False
        print(
            f"Warning: this forge does not support native sub-issue linking; #{issue_number} relies on body metadata for parent #{parent_issue_number} instead: {error}",
            file=sys.stderr,
        )
    unresolved: list[str] = []
    for dependency_id in depends_on:
        try:
            forge.set_blocked_by(issue_number, resolved_numbers[dependency_id])
        except unavailable_errors as error:
            unresolved.append(dependency_id)
            print(
                f"Warning: this forge does not support native blocked_by linking; #{issue_number} relies on body depends_on for {dependency_id} (#{resolved_numbers[dependency_id]}) instead: {error}",
                file=sys.stderr,
            )
    return RelationshipLinkResult(parent_linked, tuple(unresolved))

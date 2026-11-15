"""Collect one context snapshot from a Forge the caller already holds.

Page failures stay in the returned snapshot. This module does not publish the
database and does not read an index.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Protocol

import yaml

from orchestune.context.contracts import (
    ExtractionRequest,
    PlanMetadataSnapshot,
    PlanSubtaskMetadata,
)
from orchestune.context.extract import extract_shared_artifacts
from orchestune.context.storage import CollectionSnapshot, IndexedRecord
from orchestune.models import IssueRecord

_YAML_FENCE = re.compile(r"```yaml\n(.*?)```", re.DOTALL)
_READ_FAILURES = (OSError, ValueError, RuntimeError)


@dataclass(frozen=True, slots=True)
class FetchRequest:
    """Bounds for one collection. ``max_pages`` applies only to ``since``."""

    repository: str
    parent_issue: int | None = None
    issue_numbers: tuple[int, ...] = ()
    since: str | None = None
    max_pages: int | None = None


class _ContextForge(Protocol):
    def get_issue(self, issue_number: int | str) -> IssueRecord | None: ...

    def list_sub_issues(self, parent_issue_number: int | str) -> list[IssueRecord]: ...

    def list_issues_in_range(
        self,
        *,
        since: str | None = None,
        state: str = "open",
        max_pages: int | None = None,
    ) -> tuple[list[IssueRecord], bool]: ...

    def list_all_issue_comments(
        self, issue_number: int | str
    ) -> list[dict[str, Any]]: ...


class _SelectionError(Exception):
    """A selector named an issue the forge confirmed is absent."""

    def __init__(self, coverage: str, reason: str) -> None:
        super().__init__(reason)
        self.coverage = coverage


def fetch_context(forge: _ContextForge, request: FetchRequest) -> CollectionSnapshot:
    """Collect parent, explicit issues, and an optional updated-since range.

    A repository slug that disagrees with the request publishes nothing. An
    exception while listing or reading comments also publishes nothing. Stopping
    because ``max_pages`` is reached publishes the issues already listed.
    """
    blocked = _repository_blocker(forge, request.repository)
    if blocked is not None:
        return blocked
    try:
        selected, truncated = _selected_issues(forge, request)
    except _SelectionError as exc:
        return _failed(exc.coverage, str(exc), ())
    except _READ_FAILURES as exc:
        return _failed("unavailable", str(exc), ())
    return _collect(forge, request.repository, selected, truncated)


def _repository_blocker(forge: object, repository: str) -> CollectionSnapshot | None:
    getter = getattr(forge, "get_repository_slug", None)
    if not callable(getter):
        return None
    try:
        slug = getter()
    except _READ_FAILURES as exc:
        return _failed("unavailable", str(exc), ())
    if slug != repository:
        return _failed("unavailable", "repository mismatch", ())
    return None


def _selected_issues(
    forge: _ContextForge, request: FetchRequest
) -> tuple[dict[int, IssueRecord], bool]:
    selected: dict[int, IssueRecord] = {}
    if request.parent_issue is not None:
        _add_parent(forge, request.parent_issue, selected)
    for number in request.issue_numbers:
        _add_issue(forge, number, selected)
    truncated = False
    if request.since is not None:
        truncated = _add_range(forge, request, selected)
    return selected, truncated


def _add_parent(
    forge: _ContextForge, parent: int, selected: dict[int, IssueRecord]
) -> None:
    _add_issue(forge, parent, selected)
    for child in forge.list_sub_issues(parent):
        selected[child.number] = child


def _add_issue(
    forge: _ContextForge, number: int, selected: dict[int, IssueRecord]
) -> None:
    issue = forge.get_issue(number)
    if issue is None:
        raise _SelectionError("unavailable", f"issue {number} is missing")
    selected[issue.number] = issue


def _add_range(
    forge: _ContextForge, request: FetchRequest, selected: dict[int, IssueRecord]
) -> bool:
    issues, exhausted = forge.list_issues_in_range(
        since=request.since, state="all", max_pages=request.max_pages
    )
    for issue in issues:
        selected[issue.number] = issue
    return not exhausted


def _collect(
    forge: _ContextForge,
    repository: str,
    selected: dict[int, IssueRecord],
    truncated: bool,
) -> CollectionSnapshot:
    records: list[IndexedRecord] = []
    completed = 0
    ordered = [selected[number] for number in sorted(selected)]
    try:
        for issue in ordered:
            comments = tuple(forge.list_all_issue_comments(issue.number))
            records.extend(_issue_records(repository, issue, comments))
            completed += 1
    except _READ_FAILURES as exc:
        coverage = "partial" if completed else "unavailable"
        return _failed(coverage, str(exc), tuple(records))
    coverage = "partial" if truncated else "complete"
    reason = "stopped at max_pages" if truncated else None
    return _snapshot(True, coverage, reason, tuple(records), ordered)


def _snapshot(
    publish: bool,
    coverage: str,
    reason: str | None,
    records: tuple[IndexedRecord, ...],
    ordered: list[IssueRecord],
) -> CollectionSnapshot:
    manifest = {
        "coverage_status": coverage,
        "reason": reason,
        "issue_numbers": [issue.number for issue in ordered],
    }
    return CollectionSnapshot(
        publish=publish,
        coverage_status=coverage,
        reason=reason,
        records=tuple(sorted(records, key=_record_key)),
        manifest=manifest,
    )


def _failed(
    coverage: str, reason: str, records: tuple[IndexedRecord, ...]
) -> CollectionSnapshot:
    return _snapshot(False, coverage, reason, records, [])


def _issue_records(
    repository: str, issue: IssueRecord, comments: tuple[dict[str, Any], ...]
) -> tuple[IndexedRecord, ...]:
    extracted = extract_shared_artifacts(
        ExtractionRequest(
            repository=repository,
            issue_number=issue.number,
            fetch_complete=True,
            comments=comments,
            plan=_plan_snapshot(issue),
        )
    )
    return tuple(
        IndexedRecord(artifact=artifact, title=issue.title, description=issue.body)
        for artifact in extracted.artifacts
    )


def _plan_snapshot(issue: IssueRecord) -> PlanMetadataSnapshot | None:
    match = _YAML_FENCE.search(issue.body)
    if match is None:
        return None
    try:
        loaded = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(loaded, dict):
        return None
    subtask_id = loaded.get("subtask_id")
    if not isinstance(subtask_id, str):
        return None
    task_kind = loaded.get("task_kind")
    return PlanMetadataSnapshot(
        revision_digest=hashlib.sha256(issue.body.encode("utf-8")).hexdigest(),
        parent_issue=_parent_number(issue),
        subtasks=(
            PlanSubtaskMetadata(
                subtask_id=subtask_id,
                issue_number=issue.number,
                depends_on=_string_tuple(loaded.get("depends_on")),
                footprint=_string_tuple(loaded.get("footprint")),
                symbols=_string_tuple(loaded.get("symbols")),
                task_kind=task_kind if isinstance(task_kind, str) else None,
            ),
        ),
    )


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return ()


def _parent_number(issue: IssueRecord) -> int | None:
    parent = issue.parent
    if not isinstance(parent, dict):
        return None
    number = parent.get("number")
    if isinstance(number, bool) or not isinstance(number, int):
        return None
    return number


def _record_key(record: IndexedRecord) -> tuple[str, int, str, str]:
    artifact = record.artifact
    return (
        artifact.repository,
        artifact.issue_number,
        artifact.source_comment_id,
        artifact.source_revision_digest,
    )

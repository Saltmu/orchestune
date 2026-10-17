"""Immutable contracts for remediation specifications and previews."""

from __future__ import annotations

from dataclasses import dataclass

from orchestune.dag.models import SubTask
from orchestune.models import IssueRecord, PrRecord


@dataclass(frozen=True)
class RemediationTask:
    subtask: SubTask
    kind: str
    parent_acceptance: tuple[str, ...]
    origins: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class RemediationSpec:
    parent_issue_number: int | None
    tasks: tuple[RemediationTask, ...]
    spec_revision: str


@dataclass(frozen=True)
class OriginObservation:
    reference: str
    valid: bool
    reason: str


@dataclass(frozen=True)
class RemediationSnapshot:
    parent: IssueRecord
    children: tuple[IssueRecord, ...]
    existing: tuple[IssueRecord, ...]
    final_prs: tuple[PrRecord, ...]
    origins: tuple[OriginObservation, ...]
    parent_merged: bool
    parent_comments: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreviewProblem:
    category: str
    message: str


@dataclass(frozen=True)
class TaskPreview:
    task: RemediationTask
    action: str
    issue_number: int | None
    acceptance_sources: tuple[str, ...]
    footprint_diff: tuple[str, ...] = ()
    completed_steps: tuple[str, ...] = ()
    remaining_steps: tuple[str, ...] = ()


@dataclass(frozen=True)
class RemediationPreview:
    spec_revision: str
    preview_token: str
    tasks: tuple[TaskPreview, ...]
    problems: tuple[PreviewProblem, ...]
    exit_code: int

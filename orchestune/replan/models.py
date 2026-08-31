"""Immutable domain contracts shared by replan workflows."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum

from orchestune.dag.models import SubTask
from orchestune.labels import StatusLabel
from orchestune.models import normalize_newlines

_PLAN_REVISION_PATTERN = re.compile(r"replan-v1:sha256:[0-9a-f]{64}")


class PlanRevision(str):
    """Validated, immutable semantic revision of a decomposition plan."""

    def __new__(cls, value: str) -> PlanRevision:
        if not _PLAN_REVISION_PATTERN.fullmatch(value):
            raise ValueError(f"invalid plan revision: {value!r}")
        return str.__new__(cls, value)


class ChangeKind(StrEnum):
    CREATE = "create"
    UPDATE_TITLE = "update-title"
    UPDATE_BODY = "update-body"
    ADD_RELATION = "relation-add"
    REMOVE_RELATION = "relation-remove"
    STATUS_CHANGE = "status-change"
    SUPERSEDED_CANDIDATE = "superseded-candidate"


class Disposition(StrEnum):
    SAFE = "safe"
    MANUAL_REVIEW = "manual-review"
    FORBIDDEN = "forbidden"
    CONFLICT = "conflict"
    NO_OP = "no-op"


@dataclass(frozen=True)
class EndpointRef:
    """Exactly-one reference to an internal SubTask or an Issue."""

    subtask_id: str | None = None
    issue_number: int | None = None

    def __post_init__(self) -> None:
        has_subtask = self.subtask_id is not None
        has_issue = self.issue_number is not None
        if has_subtask == has_issue:
            raise ValueError(
                "EndpointRef requires exactly one of subtask_id or issue_number"
            )
        if has_subtask and (
            not isinstance(self.subtask_id, str) or not self.subtask_id.strip()
        ):
            raise ValueError("EndpointRef subtask_id must be a non-empty string")
        if has_subtask:
            assert self.subtask_id is not None
            if self.subtask_id != self.subtask_id.strip():
                raise ValueError(
                    "EndpointRef subtask_id must not contain outer whitespace"
                )
        if has_issue and (
            isinstance(self.issue_number, bool)
            or not isinstance(self.issue_number, int)
            or self.issue_number <= 0
        ):
            raise ValueError("EndpointRef issue_number must be a positive integer")

    @property
    def key(self) -> tuple[str, str | int]:
        if self.subtask_id is not None:
            return ("subtask_id", self.subtask_id)
        assert self.issue_number is not None
        return ("issue_number", self.issue_number)


@dataclass(frozen=True)
class ExternalDependency:
    """A directed edge where ``blocked`` depends on ``blocker``."""

    blocked: EndpointRef
    blocker: EndpointRef

    def __post_init__(self) -> None:
        internal_count = sum(
            endpoint.subtask_id is not None for endpoint in (self.blocked, self.blocker)
        )
        if internal_count != 1:
            raise ValueError(
                "ExternalDependency must connect one SubTask and one Issue"
            )

    @property
    def key(self) -> tuple[tuple[str, str | int], tuple[str, str | int]]:
        return (self.blocked.key, self.blocker.key)


@dataclass(frozen=True)
class ReplanPlan:
    title: str
    parent_issue_number: int | None
    parent_issue_source: str | None
    subtasks: tuple[SubTask, ...]
    external_dependencies: tuple[ExternalDependency, ...] = ()
    description: str = ""


def _stable_hash(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class IssueSnapshot:
    number: int
    title: str
    body: str
    labels: tuple[str, ...]
    state: str
    parent_issue_number: int | None = None
    blocked_by: tuple[int, ...] = ()
    merged_closing_prs: tuple[int, ...] = ()
    created_at: str = ""

    def __post_init__(self) -> None:
        if (
            isinstance(self.number, bool)
            or not isinstance(self.number, int)
            or self.number <= 0
        ):
            raise ValueError("IssueSnapshot number must be a positive integer")
        for field_name, values in (
            ("blocked_by", self.blocked_by),
            ("merged_closing_prs", self.merged_closing_prs),
        ):
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
                for value in values
            ):
                raise ValueError(
                    f"IssueSnapshot {field_name} values must be positive integers"
                )
        if self.parent_issue_number is not None and (
            isinstance(self.parent_issue_number, bool)
            or not isinstance(self.parent_issue_number, int)
            or self.parent_issue_number <= 0
        ):
            raise ValueError(
                "IssueSnapshot parent_issue_number must be a positive integer"
            )
        object.__setattr__(self, "body", normalize_newlines(self.body))
        object.__setattr__(self, "labels", tuple(sorted(set(self.labels))))
        object.__setattr__(self, "blocked_by", tuple(sorted(set(self.blocked_by))))
        object.__setattr__(
            self, "merged_closing_prs", tuple(sorted(set(self.merged_closing_prs)))
        )

    @property
    def fingerprint(self) -> str:
        payload = {
            "number": self.number,
            "title": self.title,
            "body": self.body,
            "labels": self.labels,
            "state": self.state,
            "parent_issue_number": self.parent_issue_number,
            "blocked_by": self.blocked_by,
            "merged_closing_prs": self.merged_closing_prs,
        }
        return f"sha256:{_stable_hash(payload)}"


@dataclass(frozen=True)
class ReplanChange:
    kind: ChangeKind
    disposition: Disposition
    reason: str
    subtask_id: str | None = None
    issue_number: int | None = None
    before: str | None = None
    after: str | None = None


@dataclass(frozen=True)
class ReplanPreview:
    plan_revision: PlanRevision
    preview_token: str
    snapshots: tuple[IssueSnapshot, ...]
    changes: tuple[ReplanChange, ...]


@dataclass(frozen=True)
class ApplyPolicy:
    """Status-label policy inputs; classification itself belongs to preview."""

    safe_statuses: tuple[StatusLabel, ...] = (
        StatusLabel.QUEUED,
        StatusLabel.BLOCKED,
    )
    manual_review_statuses: tuple[StatusLabel, ...] = (
        StatusLabel.IN_PROGRESS,
        StatusLabel.BLOCKED_RECOMPUTE,
        StatusLabel.BLOCKED_HUMAN_REVIEW,
        StatusLabel.EXTERNAL_LOCK,
        StatusLabel.FORCE_SERIAL,
        StatusLabel.MANUAL_MERGE_REQUIRED,
    )
    forbidden_statuses: tuple[StatusLabel, ...] = (
        StatusLabel.DONE,
        StatusLabel.NOT_NEEDED,
    )

    def __post_init__(self) -> None:
        for status in (
            *self.safe_statuses,
            *self.manual_review_statuses,
            *self.forbidden_statuses,
        ):
            if not isinstance(status, StatusLabel):
                raise TypeError("ApplyPolicy statuses must use StatusLabel values")


@dataclass(frozen=True)
class ApplyResult:
    plan_revision: PlanRevision
    applied: tuple[ReplanChange, ...] = ()
    pending: tuple[ReplanChange, ...] = ()
    failed: tuple[ReplanChange, ...] = ()
    degraded: bool = False


__all__ = [
    "ApplyPolicy",
    "ApplyResult",
    "ChangeKind",
    "Disposition",
    "EndpointRef",
    "ExternalDependency",
    "IssueSnapshot",
    "PlanRevision",
    "ReplanChange",
    "ReplanPlan",
    "ReplanPreview",
]

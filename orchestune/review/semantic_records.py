"""Semantic-review domain types and persistent v1 markers.

The fast path and the round state machine stay in ``semantic_gate``,
which re-exports every public name defined here.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import MISSING, dataclass, fields
from enum import StrEnum
from typing import Any, Literal, TypeVar, cast

from orchestune.review.rounds import effective_lines


class SemanticReviewMode(StrEnum):
    REQUIRED = "required"
    ADVISORY = "advisory"
    OFF = "off"


VALID_SEMANTIC_REVIEW_MODES = frozenset(m.value for m in SemanticReviewMode)


class SemanticReviewPhase(StrEnum):
    NOT_STARTED = "not_started"
    FAST_PATH_PASSED = "fast_path_passed"
    REVIEW_PENDING = "review_pending"
    FIX_IN_PROGRESS = "fix_in_progress"
    CI_VERIFICATION = "ci_verification"
    PASSED = "passed"
    SKIPPED_BY_HUMAN = "skipped_by_human"
    ESCALATED = "escalated"
    INVALIDATED = "invalidated"


TERMINAL_PASSED_PHASES = frozenset(
    {
        SemanticReviewPhase.FAST_PATH_PASSED,
        SemanticReviewPhase.PASSED,
        SemanticReviewPhase.SKIPPED_BY_HUMAN,
    }
)


ACTIVE_PENDING_PHASES = frozenset(
    {
        SemanticReviewPhase.NOT_STARTED,
        SemanticReviewPhase.REVIEW_PENDING,
        SemanticReviewPhase.FIX_IN_PROGRESS,
        SemanticReviewPhase.CI_VERIFICATION,
    }
)


class SemanticOperationKind(StrEnum):
    """One job both posts the round's judgment table and makes any needed fix."""

    FIX = "fix"


class SemanticOperationStatus(StrEnum):
    RESERVED = "reserved"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INDETERMINATE = "indeterminate"


IN_FLIGHT_OPERATION_STATUSES = frozenset(
    {
        SemanticOperationStatus.RESERVED,
        SemanticOperationStatus.DISPATCHING,
        SemanticOperationStatus.RUNNING,
    }
)


_STATE_MARKER = "orchestune:semantic-review-state:v1"


_OVERRIDE_MARKER = "orchestune:semantic-review-override:v1"


_RETRY_MARKER = "orchestune:semantic-review-retry:v1"


_FIX_RESULT_MARKER = "orchestune:semantic-fix-result:v1"


def _canonical_json(value: Any) -> str:
    """One-line JSON that can never close the surrounding HTML comment."""
    text = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace(">", "\\u003e")


@dataclass(frozen=True)
class ChildSourceRef:
    issue_number: int
    subtask_id: str
    source_sha: str

    def key(self) -> tuple[int, str, str]:
        return (self.issue_number, self.subtask_id, self.source_sha)

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_number": self.issue_number,
            "subtask_id": self.subtask_id,
            "source_sha": self.source_sha,
        }

    @classmethod
    def from_value(cls, value: object) -> ChildSourceRef:
        """Accept a ref, an `(issue, subtask, sha)` triple or its dict form."""
        if isinstance(value, ChildSourceRef):
            value = value.key()
        if isinstance(value, Mapping):
            if set(value) != {"issue_number", "subtask_id", "source_sha"}:
                raise ValueError("child source has unexpected keys")
            value = (value["issue_number"], value["subtask_id"], value["source_sha"])
        if not isinstance(value, tuple | list) or len(value) != 3:
            raise ValueError("child source must be (issue_number, subtask_id, sha)")
        issue, subtask, sha = value
        _require_int(issue, "child issue_number", minimum=1)
        for name, item in (("subtask_id", subtask), ("source_sha", sha)):
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"child {name} must be a nonempty string")
        return cls(issue, subtask.strip(), sha.strip())


def normalize_child_sources(values: Sequence[object]) -> tuple[ChildSourceRef, ...]:
    """Sorted, duplicate-free child sources (order of the input never matters)."""
    children = sorted((ChildSourceRef.from_value(v) for v in values), key=_child_key)
    if len({child.key() for child in children}) != len(children):
        raise ValueError("duplicate child source")
    return tuple(children)


def _child_key(child: ChildSourceRef) -> tuple[int, str, str]:
    return child.key()


@dataclass(frozen=True)
class IntegrationAttemptIdentity:
    """Deterministic identity of one (parent, base, child-set) integration."""

    parent_issue_number: int
    base_sha: str
    child_sources: tuple[ChildSourceRef, ...]
    attempt_id: str

    @classmethod
    def create(
        cls,
        parent_issue_number: int,
        base_sha: str,
        child_sources: Sequence[ChildSourceRef | tuple[int, str, str]],
    ) -> IntegrationAttemptIdentity:
        _require_int(parent_issue_number, "parent_issue_number", minimum=1)
        if not isinstance(base_sha, str) or not base_sha.strip():
            raise ValueError("base_sha must be a nonempty string")
        children = normalize_child_sources(child_sources)
        payload = {
            "parent_issue_number": parent_issue_number,
            "base_sha": base_sha.strip(),
            "child_sources": [list(child.key()) for child in children],
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return cls(parent_issue_number, base_sha.strip(), children, digest)

    @property
    def temp_branch(self) -> str:
        return (
            f"integration/temp-parent-issue-{self.parent_issue_number}"
            f"-{self.attempt_id[:12]}"
        )

    def matches_state(self, state: SemanticReviewStateV1) -> bool:
        return (
            state.attempt_id == self.attempt_id
            and state.parent_issue_number == self.parent_issue_number
            and state.base_sha == self.base_sha
        )


def _require_int(value: object, name: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _require_text(value: object, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _require_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


_E = TypeVar("_E", bound=StrEnum)


def _require_enum(
    value: object, enum: type[_E], name: str, *, optional: bool = False
) -> _E | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(f"invalid {name}: {value!r}")
    try:
        return enum(value)
    except ValueError:
        raise ValueError(f"invalid {name}: {value!r}") from None


def _require_keys(
    data: object, what: str, required: frozenset[str], optional: frozenset[str]
) -> Mapping[str, Any]:
    """A mapping holding every required key and nothing unknown."""
    if not isinstance(data, Mapping):
        raise ValueError(f"{what} must be a mapping")
    if set(data) - required - optional or not required <= set(data):
        raise ValueError(f"{what} has unexpected fields")
    return data


_INT_FIELDS = frozenset(
    {
        "restart_count",
        "round",
        "fix_retries",
        "state_revision",
        "claim_generation",
        "pr_number",
    }
)


_REQUIRED_TEXT_FIELDS = frozenset({"attempt_id", "head_sha", "base_sha"})


_ENUM_FIELDS: Mapping[str, type[StrEnum]] = {
    "escalated_from": SemanticReviewPhase,
    "operation_kind": SemanticOperationKind,
    "operation_status": SemanticOperationStatus,
}


@dataclass(frozen=True)
class SemanticReviewStateV1:
    """Durable state of one attempt, posted as a v1 marker comment."""

    attempt_id: str
    parent_issue_number: int
    pr_number: int
    phase: SemanticReviewPhase
    round: int
    head_sha: str
    base_sha: str
    child_sources: tuple[ChildSourceRef, ...]
    claim_holder: str | None = None
    claim_expires_at: str | None = None
    ci_verified_sha: str | None = None
    judgment_digest: str | None = None
    preflight_notice_key: str | None = None
    escalation_reason: str | None = None
    #: The step an explicit `retry` resumes; recorded on every escalation.
    escalated_from: SemanticReviewPhase | None = None
    fix_retries: int = 0
    updated_at: str | None = None
    state_revision: int = 0
    claim_generation: int = 0
    attempt_started_at: str | None = None
    phase_started_at: str | None = None
    round_started_at: str | None = None
    reviewed_sha: str | None = None
    review_source_digest: str | None = None
    operation_id: str | None = None
    operation_kind: SemanticOperationKind | None = None
    operation_status: SemanticOperationStatus | None = None
    provider_handle: str | None = None
    fix_input_sha: str | None = None
    fix_output_sha: str | None = None
    fix_completed: bool = False
    #: Generation of explicit retries/restarts; part of every operation id.
    restart_count: int = 0
    last_retry_request_id: str | None = None
    limits_reset_reason: str | None = None
    #: Report for the current head and the report actually judged this round.
    read_report_digest: str | None = None
    reviewed_read_report_digest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {f.name: getattr(self, f.name) for f in fields(self)}
        data["phase"] = self.phase.value
        data["child_sources"] = [child.to_dict() for child in self.child_sources]
        for name in _ENUM_FIELDS:
            data[name] = data[name].value if data[name] is not None else None
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SemanticReviewStateV1:
        """Strictly typed: unknown keys, wrong types or enum values are errors."""
        if not isinstance(data, Mapping):
            raise ValueError("semantic review state must be a mapping")
        known = {f.name: f for f in fields(cls)}
        unknown = set(data) - set(known)
        if unknown:
            raise ValueError(f"unknown state fields: {', '.join(sorted(unknown))}")
        values: dict[str, Any] = {}
        for name, field in known.items():
            if name not in data:
                if field.default is MISSING:
                    raise ValueError(f"missing state field: {name}")
                continue
            values[name] = _coerce_state_field(name, data[name])
        return cls(**values)


def _coerce_state_field(name: str, value: object) -> object:
    if name in {"read_report_digest", "reviewed_read_report_digest"}:
        if value is not None and (
            not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
        ):
            raise ValueError(f"{name} must be a SHA-256 digest or null")
        return value
    if name == "parent_issue_number":
        return _require_int(value, name, minimum=1)
    if name in _INT_FIELDS:
        return _require_int(value, name)
    if name == "phase":
        return _require_enum(value, SemanticReviewPhase, name)
    if name in _ENUM_FIELDS:
        return _require_enum(value, _ENUM_FIELDS[name], name, optional=True)
    if name == "child_sources":
        if not isinstance(value, list):
            raise ValueError("child_sources must be a list")
        return normalize_child_sources(value)
    if name == "fix_completed":
        return _require_bool(value, name)
    return _require_text(value, name, optional=name not in _REQUIRED_TEXT_FIELDS)


@dataclass(frozen=True)
class SemanticReviewHumanOverrideV1:
    """A human skip of the semantic gate, valid only for one attempt and head."""

    attempt_id: str
    head_sha: str
    reason: str
    actor: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SemanticReviewHumanOverrideV1:
        data = _require_keys(
            data,
            "semantic review override",
            frozenset({"attempt_id", "head_sha", "reason"}),
            frozenset({"actor"}),
        )
        return cls(
            attempt_id=str(_require_text(data["attempt_id"], "attempt_id")),
            head_sha=str(_require_text(data["head_sha"], "head_sha")),
            reason=str(_require_text(data["reason"], "reason")),
            actor=_require_text(data.get("actor"), "actor", optional=True),
        )


SemanticRetryKind = Literal["retry", "restart"]


_RETRY_KINDS: frozenset[str] = frozenset({"retry", "restart"})


@dataclass(frozen=True)
class SemanticReviewRetryV1:
    """An explicit human retry/restart of an escalated attempt at one head.

    `retry` resumes the escalated step and `restart` reviews the head again in
    a new round; both keep the consumed limits unless `reset_limits` is set.
    """

    attempt_id: str
    head_sha: str
    request_id: str
    kind: SemanticRetryKind
    reason: str
    reset_limits: bool = False
    actor: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SemanticReviewRetryV1:
        data = _require_keys(
            data,
            "semantic review retry",
            frozenset({"attempt_id", "head_sha", "request_id", "kind", "reason"}),
            frozenset({"reset_limits", "actor"}),
        )
        kind = data["kind"]
        if not isinstance(kind, str) or kind not in _RETRY_KINDS:
            raise ValueError(f"invalid retry kind: {kind!r}")
        return cls(
            attempt_id=str(_require_text(data["attempt_id"], "attempt_id")),
            head_sha=str(_require_text(data["head_sha"], "head_sha")),
            request_id=str(_require_text(data["request_id"], "request_id")),
            kind=cast(SemanticRetryKind, kind),
            reason=str(_require_text(data["reason"], "reason")),
            reset_limits=_require_bool(data.get("reset_limits", False), "reset_limits"),
            actor=_require_text(data.get("actor"), "actor", optional=True),
        )


SemanticFixStatus = Literal["completed", "failed"]


_FIX_STATUSES: frozenset[str] = frozenset({"completed", "failed"})


@dataclass(frozen=True)
class SemanticFixResultV1:
    """What one judgment/fix job reports; `output_sha` is None without changes."""

    attempt_id: str
    operation_id: str
    input_sha: str
    output_sha: str | None
    judgment_comment_id: int | None
    status: SemanticFixStatus

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SemanticFixResultV1:
        names = frozenset(f.name for f in fields(cls))
        data = _require_keys(data, "semantic fix result", names, frozenset())
        attempt_id = str(_require_text(data["attempt_id"], "attempt_id"))
        operation_id = str(_require_text(data["operation_id"], "operation_id"))
        if not operation_id.startswith(attempt_id[:12]):
            raise ValueError("fix result operation_id is not of its attempt")
        status = data["status"]
        if not isinstance(status, str) or status not in _FIX_STATUSES:
            raise ValueError(f"invalid fix result status: {status!r}")
        output_sha = _require_text(data["output_sha"], "output_sha", optional=True)
        if status == "failed" and output_sha is not None:
            raise ValueError("a failed fix result cannot report an output")
        comment_id = data["judgment_comment_id"]
        return cls(
            attempt_id=attempt_id,
            operation_id=operation_id,
            input_sha=str(_require_text(data["input_sha"], "input_sha")),
            output_sha=output_sha,
            judgment_comment_id=None
            if comment_id is None
            else _require_int(comment_id, "judgment_comment_id", minimum=1),
            status=cast(SemanticFixStatus, status),
        )


_BRANCH_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def semantic_fix_output_branch(operation_id: str) -> str:
    """Where a fix job pushes; Orchestune alone promotes it to the PR head.

    Outside the `integration/temp-*` GC prefix, so a pending output survives
    temp-branch collection.
    """
    if (
        not isinstance(operation_id, str)
        or not _BRANCH_COMPONENT.fullmatch(operation_id)
        or ".." in operation_id
        or operation_id.endswith(".lock")
    ):
        raise ValueError(f"invalid operation_id for a branch: {operation_id!r}")
    return f"integration/semantic-fix/{operation_id}"


def _format_marker(name: str, data: Mapping[str, Any]) -> str:
    return f"<!-- {name} {_canonical_json(data)} -->"


def _marker_payload(comment_body: str, name: str) -> dict[str, Any] | None:
    """The JSON of the single real marker line; quoted/fenced copies are ignored.

    A malformed or repeated marker line is an error, never silently skipped.
    """
    lines = [
        line
        for line in effective_lines(comment_body)
        if line.startswith("<!--") and name in line
    ]
    if not lines:
        return None
    if len(lines) != 1:
        raise ValueError(f"multiple {name} markers in one comment")
    prefix, suffix = f"<!-- {name} ", " -->"
    line = lines[0]
    if not (line.startswith(prefix) and line.endswith(suffix)):
        raise ValueError(f"malformed {name} marker line")
    try:
        data = json.loads(line[len(prefix) : -len(suffix)])
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid {name} marker JSON") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{name} marker must hold a JSON object")
    return data


def format_semantic_review_state_marker(state: SemanticReviewStateV1) -> str:
    return _format_marker(_STATE_MARKER, state.to_dict())


def parse_semantic_review_state_marker(
    comment_body: str,
) -> SemanticReviewStateV1 | None:
    """None without a marker; ValueError for a malformed or ill-typed one."""
    data = _marker_payload(comment_body, _STATE_MARKER)
    return None if data is None else SemanticReviewStateV1.from_dict(data)


def restore_latest_semantic_review_state(
    comment_bodies: Sequence[str], *, attempt_id: str | None = None
) -> SemanticReviewStateV1 | None:
    """Latest state (bodies in posting order), optionally of one attempt.

    A corrupt marker newer than the latest match raises ValueError: silently
    falling back to an older state (e.g. a stale PASSED) is never allowed. So
    does a revision conflict in that attempt's history: a later state whose
    `state_revision` does not exceed an earlier one, unless it repeats it.
    """
    latest: SemanticReviewStateV1 | None = None
    newer: SemanticReviewStateV1 | None = None
    for body in reversed(comment_bodies):
        try:
            state = parse_semantic_review_state_marker(body)
        except ValueError:
            if latest is None:
                raise
            continue
        if state is None:
            continue
        if latest is None:
            if attempt_id in (None, state.attempt_id):
                latest = newer = state
            continue
        if state.attempt_id != latest.attempt_id or newer is None:
            continue
        if state.state_revision > newer.state_revision or (
            state.state_revision == newer.state_revision and state != newer
        ):
            raise ValueError(
                f"semantic review state revision conflict at {newer.state_revision}"
            )
        newer = state
    return latest


def format_semantic_review_override_marker(
    override: SemanticReviewHumanOverrideV1,
) -> str:
    return _format_marker(_OVERRIDE_MARKER, override.to_dict())


def parse_semantic_review_override_marker(
    comment_body: str,
) -> SemanticReviewHumanOverrideV1 | None:
    data = _marker_payload(comment_body, _OVERRIDE_MARKER)
    return None if data is None else SemanticReviewHumanOverrideV1.from_dict(data)


def restore_latest_human_override(
    comment_bodies: Sequence[str], *, attempt_id: str, head_sha: str
) -> SemanticReviewHumanOverrideV1 | None:
    """Latest override bound to this exact attempt and head (authors pre-verified)."""
    for body in reversed(comment_bodies):
        override = parse_semantic_review_override_marker(body)
        if (
            override is not None
            and override.attempt_id == attempt_id
            and override.head_sha == head_sha
        ):
            return override
    return None


def format_semantic_review_retry_marker(request: SemanticReviewRetryV1) -> str:
    return _format_marker(_RETRY_MARKER, request.to_dict())


def parse_semantic_review_retry_marker(
    comment_body: str,
) -> SemanticReviewRetryV1 | None:
    data = _marker_payload(comment_body, _RETRY_MARKER)
    return None if data is None else SemanticReviewRetryV1.from_dict(data)


def restore_latest_retry_request(
    comment_bodies: Sequence[str],
    *,
    attempt_id: str,
    head_sha: str,
    consumed_request_ids: Collection[str],
) -> SemanticReviewRetryV1 | None:
    """Latest request bound to this attempt and head, unless already consumed.

    An older request behind the latest one is superseded, never revived.
    Authors are verified by the caller.
    """
    for body in reversed(comment_bodies):
        request = parse_semantic_review_retry_marker(body)
        if (
            request is not None
            and request.attempt_id == attempt_id
            and request.head_sha == head_sha
        ):
            return None if request.request_id in consumed_request_ids else request
    return None


def format_semantic_fix_result_marker(result: SemanticFixResultV1) -> str:
    return _format_marker(_FIX_RESULT_MARKER, result.to_dict())


def parse_semantic_fix_result_marker(comment_body: str) -> SemanticFixResultV1 | None:
    data = _marker_payload(comment_body, _FIX_RESULT_MARKER)
    return None if data is None else SemanticFixResultV1.from_dict(data)


def restore_latest_fix_result(
    comment_bodies: Sequence[str], *, attempt_id: str, operation_id: str
) -> SemanticFixResultV1 | None:
    """Latest result of exactly this operation (authors pre-verified)."""
    for body in reversed(comment_bodies):
        result = parse_semantic_fix_result_marker(body)
        if (
            result is not None
            and result.attempt_id == attempt_id
            and result.operation_id == operation_id
        ):
            return result
    return None

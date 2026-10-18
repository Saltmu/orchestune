"""Layer-2 (integration PR) semantic review contracts (no network or filesystem).

Pure domain types, persistent v1 markers, the single-task fast path and the
round state machine of the integration PR review/fix loop. Callers verify comment
authors and observe GitHub/CI/jobs; this module only decides from those verified
inputs. Prose is never judged here: review outcomes come from the structured
`orchestune-review-judgments` table, whose structure and coverage `judgment`
validates.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import MISSING, dataclass, fields, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, TypeVar, cast

from orchestune.review.judgment import (
    judgment_digest,
    parse_judgments,
    validate_coverage,
    validate_judgments,
)
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


@dataclass(frozen=True)
class FastPathDecision:
    eligible: bool
    reason: str
    phase: SemanticReviewPhase


def evaluate_single_task_fast_path(
    *,
    child_count: int,
    force_integration_review: bool,
    base_sha_matches: bool,
    tree_matches_without_conflict: bool,
    layer1_verdict: str | None,
    layer1_reviewed_sha: str | None,
    source_sha: str,
) -> FastPathDecision:
    """Skip layer-2 review only when the PR is exactly the layer-1-reviewed tree."""
    not_started = SemanticReviewPhase.NOT_STARTED
    if force_integration_review:
        return FastPathDecision(False, "forced_integration_review", not_started)
    if child_count != 1:
        return FastPathDecision(False, "multiple_children", not_started)
    if not base_sha_matches:
        return FastPathDecision(False, "base_sha_mismatch", not_started)
    if not tree_matches_without_conflict:
        return FastPathDecision(False, "tree_or_conflict_diff", not_started)
    if layer1_verdict != "pass" or not source_sha or layer1_reviewed_sha != source_sha:
        return FastPathDecision(False, "layer1_evidence_insufficient", not_started)
    return FastPathDecision(
        True, "single_task_fast_path", SemanticReviewPhase.FAST_PATH_PASSED
    )


#: The only accepted judgment/status pairs; anything else is a broken contract.
ALLOWED_JUDGMENT_STATUSES: Mapping[str, frozenset[str]] = {
    "adopt": frozenset({"unresolved", "resolved"}),
    "decline": frozenset({"declined"}),
    "already_addressed": frozenset({"resolved"}),
    "duplicate": frozenset({"resolved"}),
    "needs_information": frozenset({"deferred"}),
}


@dataclass(frozen=True)
class SemanticJudgmentDecision:
    """Structural verdict on one round's judgment table (never on its prose)."""

    valid: bool
    reason: str
    round: int | None = None
    digest: str | None = None
    rows: int = 0
    unresolved: int = 0
    deferred: int = 0

    @property
    def requires_fix(self) -> bool:
        return self.valid and self.unresolved > 0

    @property
    def passable(self) -> bool:
        """Every finding judged and settled; an empty table proves nothing."""
        return (
            self.valid and self.rows > 0 and self.unresolved == 0 and self.deferred == 0
        )


def evaluate_semantic_judgments(
    judgments: str | Mapping[str, Any] | None,
    review_result: Mapping[str, Any] | None,
    *,
    expected_round: int,
) -> SemanticJudgmentDecision:
    """Validate a table (comment body or parsed mapping) against one round.

    `review_result` is the acquired review of that round; without it coverage
    cannot be checked, so the table is not accepted. Adopt counts never decide:
    only unresolved/deferred statuses of valid pairs do.
    """
    if judgments is None:
        return SemanticJudgmentDecision(False, "judgments_missing")
    if review_result is None:
        return SemanticJudgmentDecision(False, "review_result_missing")
    try:
        table = (
            parse_judgments(judgments)
            if isinstance(judgments, str)
            else validate_judgments(dict(judgments))
        )
        validate_coverage(table, {**review_result, "round": expected_round})
    except ValueError as exc:
        return SemanticJudgmentDecision(False, f"judgments_invalid: {exc}")
    rows = table["findings"]
    if any(
        row["status"] not in ALLOWED_JUDGMENT_STATUSES[row["judgment"]] for row in rows
    ):
        return SemanticJudgmentDecision(False, "judgment_status_pair_invalid")
    digest, _ = judgment_digest(table)
    unresolved = sum(row["status"] == "unresolved" for row in rows)
    deferred = sum(row["status"] == "deferred" for row in rows)
    if deferred:
        reason = "deferred_judgments"
    elif unresolved:
        reason = "unresolved_adopt"
    else:
        reason = "judgments_settled" if rows else "empty_judgment_table"
    return SemanticJudgmentDecision(
        True, reason, expected_round, digest, len(rows), unresolved, deferred
    )


@dataclass(frozen=True)
class SemanticReviewLimits:
    max_rounds: int = 3
    max_fix_retries: int = 2
    review_timeout_seconds: int = 3600
    fix_timeout_seconds: int = 3600
    ci_timeout_seconds: int = 3600
    attempt_timeout_seconds: int = 6 * 3600

    def __post_init__(self) -> None:
        for f in fields(self):
            _require_int(getattr(self, f.name), f.name, minimum=1)


class SemanticCiConclusion(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    PENDING = "pending"


@dataclass(frozen=True)
class SemanticCiObservation:
    """CI of one commit; only a result for the current head is ever accepted."""

    sha: str
    conclusion: SemanticCiConclusion


@dataclass(frozen=True)
class SemanticOperationObservation:
    """Observed job state; ignored unless it names the reserved operation id.

    `output_sha` equal to the job's input means it changed nothing; `input_sha`,
    when known, must be the head the job was reserved for.
    """

    operation_id: str
    status: SemanticOperationStatus
    output_sha: str | None = None
    input_sha: str | None = None

    @classmethod
    def from_fix_result(
        cls, result: SemanticFixResultV1
    ) -> SemanticOperationObservation:
        if result.status == "failed":
            return cls(
                result.operation_id,
                SemanticOperationStatus.FAILED,
                input_sha=result.input_sha,
            )
        return cls(
            result.operation_id,
            SemanticOperationStatus.COMPLETED,
            result.output_sha or result.input_sha,
            result.input_sha,
        )


@dataclass(frozen=True)
class SemanticTransitionObservation:
    """Verified inputs of one evaluation (authors/bindings checked by the caller)."""

    identity: IntegrationAttemptIdentity
    observed_head_sha: str
    now: datetime
    mode: SemanticReviewMode = SemanticReviewMode.REQUIRED
    limits: SemanticReviewLimits = SemanticReviewLimits()
    override: SemanticReviewHumanOverrideV1 | None = None
    retry_request: SemanticReviewRetryV1 | None = None
    review_complete: bool = False
    reviewed_sha: str | None = None
    judgments: SemanticJudgmentDecision | None = None
    ci: SemanticCiObservation | None = None
    operation: SemanticOperationObservation | None = None


@dataclass(frozen=True)
class SemanticTransitionDecision:
    next_phase: SemanticReviewPhase
    next_round: int
    next_fix_retries: int
    reason: str
    next_state: SemanticReviewStateV1
    requires_fix_dispatch: bool = False
    #: The completed job's output awaits promotion to the PR head (state unchanged).
    requires_fix_promotion: bool = False
    requires_review_trigger: bool = False
    requires_escalation: bool = False


_P = SemanticReviewPhase
_EVIDENCE_CLEARED: Mapping[str, Any] = {
    "reviewed_sha": None,
    "review_source_digest": None,
    "judgment_digest": None,
    "ci_verified_sha": None,
}
_FRESH_REVIEW: Mapping[str, Any] = {
    **_EVIDENCE_CLEARED,
    "operation_id": None,
    "operation_kind": None,
    "operation_status": None,
    "provider_handle": None,
    "fix_input_sha": None,
    "fix_output_sha": None,
    "fix_completed": False,
}
_PHASE_TIMEOUTS: Mapping[SemanticReviewPhase, str] = {
    _P.REVIEW_PENDING: "review_timeout_seconds",
    _P.FIX_IN_PROGRESS: "fix_timeout_seconds",
    _P.CI_VERIFICATION: "ci_timeout_seconds",
}
_RETRYABLE_PHASES = frozenset(
    {_P.NOT_STARTED, _P.REVIEW_PENDING, _P.FIX_IN_PROGRESS, _P.CI_VERIFICATION}
)


def semantic_operation_id(
    attempt_id: str,
    round_num: int,
    kind: SemanticOperationKind,
    retry: int,
    restart: int = 0,
) -> str:
    """Deterministic, so re-evaluating one step reserves the same operation.

    The retry/restart generation keeps a late result of a job reserved before
    an explicit retry/restart from being taken for the job reserved after it.
    """
    return f"{attempt_id[:12]}-g{restart}-r{round_num}-{kind.value}-{retry}"


@dataclass(frozen=True)
class _Step:
    state: SemanticReviewStateV1
    obs: SemanticTransitionObservation

    @property
    def now(self) -> str:
        return self.obs.now.isoformat().replace("+00:00", "Z")

    def hold(
        self, reason: str, *, promotion: bool = False
    ) -> SemanticTransitionDecision:
        """No state change (start times are never renewed); only promotion asked."""
        s = self.state
        return SemanticTransitionDecision(
            s.phase, s.round, s.fix_retries, reason, s, requires_fix_promotion=promotion
        )

    def move(
        self, phase: SemanticReviewPhase, reason: str, **changes: Any
    ) -> SemanticTransitionDecision:
        s, now = self.state, self.now
        changes.setdefault("round", s.round)
        changes.setdefault("fix_retries", s.fix_retries)
        if phase != s.phase:
            changes["phase_started_at"] = now
        if changes["round"] != s.round:
            changes["round_started_at"] = now
        if phase == _P.ESCALATED:
            changes["escalation_reason"] = reason
            changes["escalated_from"] = s.phase
        nxt = replace(
            s,
            phase=phase,
            state_revision=s.state_revision + 1,
            updated_at=now,
            **changes,
        )
        return SemanticTransitionDecision(
            phase,
            nxt.round,
            nxt.fix_retries,
            reason,
            nxt,
            requires_fix_dispatch=nxt.operation_status
            == SemanticOperationStatus.RESERVED
            and nxt.operation_id != s.operation_id,
            requires_review_trigger=phase == _P.REVIEW_PENDING,
            requires_escalation=phase == _P.ESCALATED,
        )

    def escalate(self, reason: str) -> SemanticTransitionDecision:
        return self.move(_P.ESCALATED, reason)

    def reserve(
        self, reason: str, *, retry: int, **changes: Any
    ) -> SemanticTransitionDecision:
        """Reserve the round's judgment/fix job for the current head."""
        s = self.state
        kind = SemanticOperationKind.FIX
        restart = changes.get("restart_count", s.restart_count)
        return self.move(
            _P.FIX_IN_PROGRESS,
            reason,
            fix_retries=retry,
            operation_id=semantic_operation_id(
                s.attempt_id, s.round, kind, retry, restart
            ),
            operation_kind=kind,
            operation_status=SemanticOperationStatus.RESERVED,
            provider_handle=None,
            fix_input_sha=s.head_sha,
            fix_output_sha=None,
            fix_completed=False,
            **changes,
        )

    def retry(self, reason: str, limit_reason: str) -> SemanticTransitionDecision:
        """One more bounded job, or escalation once retries are exhausted."""
        if self.state.fix_retries >= self.obs.limits.max_fix_retries:
            return self.escalate(limit_reason)
        return self.reserve(reason, retry=self.state.fix_retries + 1)


def evaluate_semantic_review_transition(
    state: SemanticReviewStateV1, observation: SemanticTransitionObservation
) -> SemanticTransitionDecision:
    """Next phase, round, retry count, state and side effects of one evaluation.

    Pure: the caller performs the requested dispatch/promotion/trigger/escalation
    and posts `next_state` only when it differs from `state`.
    """
    if observation.now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    step = _Step(state, observation)
    if state.phase == _P.INVALIDATED:
        return step.hold("invalidated")
    changed = _input_change(step)
    if changed is not None:
        return step.move(_P.INVALIDATED, changed)
    if state.phase in TERMINAL_PASSED_PHASES:
        return step.hold("terminal_passed")
    override = observation.override
    if (
        override is not None
        and override.attempt_id == state.attempt_id
        and override.head_sha == observation.observed_head_sha
    ):
        return step.move(_P.SKIPPED_BY_HUMAN, "human_override")
    if state.phase == _P.ESCALATED:
        return _on_escalated(step)
    if observation.mode == SemanticReviewMode.OFF:
        return step.move(_P.SKIPPED_BY_HUMAN, "mode_off")
    expired = _expired(step)
    if expired is not None:
        return step.escalate(expired)
    return _PHASE_HANDLERS[state.phase](step)


def _input_change(step: _Step) -> str | None:
    """Base/child-set/head changes the attempt itself did not produce."""
    s, obs = step.state, step.obs
    if (
        not obs.identity.matches_state(s)
        or obs.identity.child_sources != s.child_sources
    ):
        return "attempt_inputs_changed"
    allowed = {s.head_sha}
    output = _completed_output(step)
    if output is not None:
        allowed.add(output)
    return None if obs.observed_head_sha in allowed else "head_changed"


def _current_operation(step: _Step) -> SemanticOperationObservation | None:
    op, s = step.obs.operation, step.state
    if op is None or s.operation_id is None or op.operation_id != s.operation_id:
        return None
    return op


def _completed_output(step: _Step) -> str | None:
    if step.state.phase != _P.FIX_IN_PROGRESS:
        return None
    op = _current_operation(step)
    if op is None or op.status != SemanticOperationStatus.COMPLETED:
        return None
    return op.output_sha


def _parse_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _expired(step: _Step) -> str | None:
    """Timeouts from recorded start times; missing/unparsable ones fail closed."""
    s, obs = step.state, step.obs
    if s.phase == _P.NOT_STARTED:
        started = _parse_time(s.attempt_started_at)
        if s.attempt_started_at is not None and started is None:
            return "attempt_started_at_invalid"
        phase_limit = None
    else:
        started = _parse_time(s.attempt_started_at)
        if started is None:
            return "attempt_started_at_invalid"
        phase_started = _parse_time(s.phase_started_at)
        if phase_started is None:
            return "phase_started_at_invalid"
        phase_limit = getattr(obs.limits, _PHASE_TIMEOUTS[s.phase])
        if (obs.now - phase_started).total_seconds() >= phase_limit:
            return f"{s.phase.value}_timeout"
    if started is not None and (
        (obs.now - started).total_seconds() >= obs.limits.attempt_timeout_seconds
    ):
        return "attempt_timeout"
    return None


def _on_escalated(step: _Step) -> SemanticTransitionDecision:
    """Apply one unconsumed retry/restart request for this attempt and head.

    Neither renews the consumed rounds, retries or attempt start unless the
    request explicitly resets the limits; a new generation keeps late results
    of earlier jobs from being taken for the jobs reserved afterwards.
    """
    s, request = step.state, step.obs.retry_request
    if (
        request is None
        or request.attempt_id != s.attempt_id
        or request.head_sha != s.head_sha
        or request.request_id == s.last_retry_request_id
    ):
        return step.hold("escalated")
    resumed: dict[str, Any] = {
        "restart_count": s.restart_count + 1,
        "last_retry_request_id": request.request_id,
        "escalation_reason": None,
        "escalated_from": None,
        "attempt_started_at": s.attempt_started_at or step.now,
    }
    if request.reset_limits:
        resumed |= {"attempt_started_at": step.now}
        return step.move(
            _P.REVIEW_PENDING,
            "limits_reset",
            round=1,
            fix_retries=0,
            limits_reset_reason=request.reason,
            **_FRESH_REVIEW,
            **resumed,
        )
    if request.kind == "restart":
        if s.round >= step.obs.limits.max_rounds:
            return step.hold("restart_max_rounds")
        return step.move(
            _P.REVIEW_PENDING,
            "restart_requested",
            round=s.round + 1,
            **_FRESH_REVIEW,
            **resumed,
        )
    phase = s.escalated_from
    if phase is None or phase not in _RETRYABLE_PHASES:
        return step.hold("retry_phase_unknown")
    if phase == _P.FIX_IN_PROGRESS:
        return step.reserve("retry_requested", retry=s.fix_retries, **resumed)
    if phase == _P.NOT_STARTED:
        resumed["attempt_started_at"] = s.attempt_started_at
    return step.move(phase, "retry_requested", **resumed)


def _on_not_started(step: _Step) -> SemanticTransitionDecision:
    return step.move(
        _P.REVIEW_PENDING,
        "review_requested",
        round=1,
        attempt_started_at=step.state.attempt_started_at or step.now,
    )


def _review_evidence(step: _Step) -> bool:
    obs = step.obs
    return obs.review_complete and obs.reviewed_sha == step.state.head_sha


def _on_review_pending(step: _Step) -> SemanticTransitionDecision:
    if not _review_evidence(step):
        return step.hold("review_pending")
    return step.reserve("review_completed", retry=step.state.fix_retries)


def _on_fix(step: _Step) -> SemanticTransitionDecision:
    """Wait for the reserved job; CI of the pre-job head never completes it."""
    s = step.state
    op = _current_operation(step)
    if s.operation_kind is None or s.operation_status is None:
        return step.escalate("operation_missing")
    if op is None or op.status in IN_FLIGHT_OPERATION_STATUSES:
        return step.hold("operation_in_flight")
    if op.status == SemanticOperationStatus.INDETERMINATE:
        return step.escalate("operation_indeterminate")
    if op.status == SemanticOperationStatus.FAILED:
        return step.retry("operation_failed", "fix_retry_limit")
    if op.input_sha is not None and op.input_sha != s.fix_input_sha:
        return step.escalate("operation_input_mismatch")
    if op.output_sha is None:
        return step.escalate("operation_output_unknown")
    if op.output_sha == s.head_sha:
        if not _review_evidence(step):
            return step.hold("review_evidence_missing")
        return _route_judgments(step)
    if step.obs.observed_head_sha != op.output_sha:
        return step.hold("awaiting_fix_promotion", promotion=True)
    return step.move(
        _P.CI_VERIFICATION,
        "head_changed_by_operation",
        head_sha=op.output_sha,
        operation_status=SemanticOperationStatus.COMPLETED,
        fix_output_sha=op.output_sha,
        fix_completed=True,
        **_EVIDENCE_CLEARED,
    )


def _route_judgments(step: _Step) -> SemanticTransitionDecision:
    """Route the table the unchanged-head job posted for this round."""
    s, verdict = step.state, step.obs.judgments
    if verdict is None:
        return step.escalate("judgments_missing")
    if not verdict.valid:
        return step.escalate(verdict.reason)
    if verdict.round != s.round:
        return step.escalate("judgments_round_mismatch")
    if verdict.deferred:
        return step.escalate("deferred_judgments")
    if verdict.unresolved:
        return step.retry("unresolved_after_fix", "fix_retry_limit")
    if not verdict.passable:
        return step.escalate(verdict.reason)
    evidence = {
        "reviewed_sha": s.head_sha,
        "judgment_digest": verdict.digest,
        "operation_status": SemanticOperationStatus.COMPLETED,
    }
    if s.ci_verified_sha == s.head_sha:
        return step.move(_P.PASSED, "passed", **evidence)
    return step.move(_P.CI_VERIFICATION, "review_passed", **evidence)


def _on_ci_verification(step: _Step) -> SemanticTransitionDecision:
    s, obs = step.state, step.obs
    ci = obs.ci
    if ci is None or ci.sha != s.head_sha or ci.conclusion == "pending":
        return step.hold("ci_pending")
    if ci.conclusion == SemanticCiConclusion.FAILURE:
        return step.retry("ci_failed", "ci_retry_limit")
    if s.reviewed_sha is None:
        if s.round >= obs.limits.max_rounds:
            return step.escalate("max_rounds")
        return step.move(
            _P.REVIEW_PENDING,
            "re_review_changed_head",
            round=s.round + 1,
            ci_verified_sha=s.head_sha,
        )
    verdict = obs.judgments
    if (
        s.reviewed_sha == s.head_sha
        and _review_evidence(step)
        and verdict is not None
        and verdict.passable
        and verdict.round == s.round
        and verdict.digest == s.judgment_digest
    ):
        return step.move(_P.PASSED, "passed", ci_verified_sha=s.head_sha)
    return step.escalate("pass_evidence_invalid")


_PHASE_HANDLERS = {
    _P.NOT_STARTED: _on_not_started,
    _P.REVIEW_PENDING: _on_review_pending,
    _P.FIX_IN_PROGRESS: _on_fix,
    _P.CI_VERIFICATION: _on_ci_verification,
}

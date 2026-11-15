"""Build shared-artifact-v1 records from sources the caller already holds.

The extractor does not fetch GitHub, write a canonical record, open SQLite,
or call a dispatch target.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from orchestune.context.contracts import (
    CORRECTION_MARKER,
    SCHEMA_VERSION,
    STATEMENTS_MARKER,
    TASK_KIND_UNSPECIFIED,
    ArtifactStaleStatus,
    EvidenceRefs,
    ExtractionRequest,
    ExtractionResult,
    PlanMetadataSnapshot,
    SharedArtifactKey,
    SharedArtifactV1,
    StaleJudgment,
    VerificationSummary,
)
from orchestune.outcome_record import (
    OUTCOME_MARKER,
    OutcomeLookupState,
    OutcomeRecord,
    parse_from_comments,
)

StatementTriple = tuple[str | None, tuple[str, ...] | None, tuple[str, ...] | None]


def source_revision_digest(body: str) -> str:
    """SHA-256 of the exact comment body. The same text reimports the same digest."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def extract_shared_artifacts(request: ExtractionRequest) -> ExtractionResult:
    """Extract records for one issue from a passed snapshot.

    An incomplete fetch yields ``UNKNOWN`` and no records. A complete fetch
    with no outcome yields ``ABSENT``. The same comments produce equal records.
    """
    comments = request.comments
    if comments is None or not request.fetch_complete or not _identity_ok(request):
        return _unknown()
    if _has_unkeyed_outcome(comments):
        return _unknown()
    drafts = _sorted_unique(_drafts(comments, request.issue_number))
    if not drafts:
        return ExtractionResult(OutcomeLookupState.ABSENT, ())
    artifacts = tuple(_artifact(draft, drafts, request) for draft in drafts)
    return ExtractionResult(OutcomeLookupState.FOUND, _derive_superseded_by(artifacts))


@dataclass(frozen=True, slots=True)
class _Draft:
    comment_id: str
    body: str
    outcome: OutcomeRecord
    recorded_at: str | None
    source_url: str | None
    summary: str | None
    assumptions: tuple[str, ...] | None
    changed_files: tuple[str, ...] | None
    supersedes_comment_id: str | None


def _unknown() -> ExtractionResult:
    return ExtractionResult(OutcomeLookupState.UNKNOWN, ())


def _identity_ok(request: ExtractionRequest) -> bool:
    if isinstance(request.issue_number, bool) or request.issue_number < 1:
        return False
    return _repository_ok(request.repository)


def _repository_ok(repository: str) -> bool:
    owner, separator, name = repository.partition("/")
    return bool(
        separator and owner and name and "/" not in name and " " not in repository
    )


def _has_unkeyed_outcome(comments: Sequence[Mapping[str, Any]]) -> bool:
    for comment in comments:
        body = _body(comment)
        if body is not None and OUTCOME_MARKER in body and _comment_id(comment) is None:
            return True
    return False


def _drafts(
    comments: Sequence[Mapping[str, Any]], issue_number: int
) -> tuple[_Draft, ...]:
    notes = _review_statements(comments)
    drafts: list[_Draft] = []
    for comment in comments:
        draft = _draft_from_comment(comment, issue_number, notes)
        if draft is not None:
            drafts.append(draft)
    return tuple(drafts)


def _draft_from_comment(
    comment: Mapping[str, Any],
    issue_number: int,
    notes: Mapping[str, StatementTriple],
) -> _Draft | None:
    body = _body(comment)
    comment_id = _comment_id(comment)
    if body is None or comment_id is None:
        return None
    outcome = parse_from_comments(({"body": body},))
    if outcome is None or outcome.issue != issue_number:
        return None
    summary, assumptions, changed_files = _statements_from(
        _fenced_object(body, STATEMENTS_MARKER)
    )
    external = notes.get(comment_id)
    if external is not None:
        summary, assumptions, changed_files = _fill_statements(
            (summary, assumptions, changed_files), external
        )
    return _Draft(
        comment_id,
        body,
        outcome,
        _recorded_at(comment),
        _comment_url(comment),
        summary,
        assumptions,
        changed_files,
        _correction_target(body),
    )


def _sorted_unique(drafts: tuple[_Draft, ...]) -> tuple[_Draft, ...]:
    ordered = sorted(
        drafts,
        key=lambda draft: (draft.comment_id, source_revision_digest(draft.body)),
    )
    seen: set[tuple[str, str]] = set()
    unique: list[_Draft] = []
    for draft in ordered:
        identity = (draft.comment_id, source_revision_digest(draft.body))
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(draft)
    return tuple(unique)


def _artifact(
    draft: _Draft,
    drafts: tuple[_Draft, ...],
    request: ExtractionRequest,
) -> SharedArtifactV1:
    plan = _plan_fields(request.plan, request.issue_number)
    digest = _evidence_digest(draft.outcome)
    return SharedArtifactV1(
        schema_version=SCHEMA_VERSION,
        repository=request.repository,
        issue_number=request.issue_number,
        source_comment_id=draft.comment_id,
        source_revision_digest=source_revision_digest(draft.body),
        parent_issue=plan.parent_issue,
        subtask_id=plan.subtask_id,
        claim_id=draft.outcome.claim_id,
        attempt=draft.outcome.attempt,
        completion_id=draft.outcome.completion_id,
        base_sha=draft.outcome.base_sha,
        target_commit=draft.outcome.head_sha,
        task_kind=plan.task_kind,
        result=draft.outcome.result,
        summary=draft.summary,
        assumptions=draft.assumptions,
        evidence_refs=_evidence(request.repository, draft, digest),
        verification_summary=_verification(draft.outcome),
        supersedes=_supersedes(draft, drafts, request),
        superseded_by=(),
        stale_status=_stale_status(
            draft.outcome.head_sha, digest, request.stale_judgments
        ),
        recorded_at=draft.recorded_at,
        indexed_at=None,
        source_url=draft.source_url,
        plan_revision_digest=plan.plan_revision_digest,
        depends_on=plan.depends_on,
        footprint=plan.footprint,
        symbols=plan.symbols,
        dependency_issue_numbers=plan.dependency_issue_numbers,
    )


def _evidence(repository: str, draft: _Draft, digest: str | None) -> EvidenceRefs:
    return EvidenceRefs(
        pr_url=_pr_url(repository, draft.outcome.pr),
        comment_url=draft.source_url,
        changed_files=draft.changed_files,
        read_evidence_digest=digest,
    )


def _verification(outcome: OutcomeRecord) -> VerificationSummary:
    return VerificationSummary(
        ci=outcome.ci,
        review_verdict=outcome.review.verdict,
        review_rounds=outcome.review.rounds,
    )


def _evidence_digest(outcome: OutcomeRecord) -> str | None:
    evidence = outcome.read_evidence
    if evidence is None:
        return None
    return evidence.digest


def _supersedes(
    draft: _Draft,
    drafts: Sequence[_Draft],
    request: ExtractionRequest,
) -> SharedArtifactKey | None:
    target = draft.supersedes_comment_id
    if target is None or target == draft.comment_id:
        return None
    found = _unique_by_id(drafts).get(target)
    if found is None:
        return None
    return SharedArtifactKey(
        request.repository,
        request.issue_number,
        found.comment_id,
        source_revision_digest(found.body),
    )


def _unique_by_id(drafts: Sequence[_Draft]) -> dict[str, _Draft]:
    counts: dict[str, int] = {}
    for draft in drafts:
        counts[draft.comment_id] = counts.get(draft.comment_id, 0) + 1
    return {
        draft.comment_id: draft for draft in drafts if counts[draft.comment_id] == 1
    }


def _derive_superseded_by(
    artifacts: tuple[SharedArtifactV1, ...],
) -> tuple[SharedArtifactV1, ...]:
    incoming: dict[SharedArtifactKey, list[SharedArtifactKey]] = {}
    for artifact in artifacts:
        if artifact.supersedes is not None:
            incoming.setdefault(artifact.supersedes, []).append(artifact.key())
    derived: list[SharedArtifactV1] = []
    for artifact in artifacts:
        refs = tuple(sorted(incoming.get(artifact.key(), ()), key=_key_order))
        derived.append(replace(artifact, superseded_by=refs))
    return tuple(derived)


def _key_order(key: SharedArtifactKey) -> tuple[str, int, str, str]:
    return (
        key.repository,
        key.issue_number,
        key.source_comment_id,
        key.source_revision_digest,
    )


def _stale_status(
    target_commit: str | None,
    report_digest: str | None,
    judgments: Sequence[StaleJudgment],
) -> ArtifactStaleStatus:
    if not target_commit or not report_digest:
        return ArtifactStaleStatus.UNKNOWN
    matched = [
        judgment.status
        for judgment in judgments
        if judgment.target_commit == target_commit
        and judgment.report_digest == report_digest
        and isinstance(judgment.status, ArtifactStaleStatus)
    ]
    if not matched or any(status != matched[0] for status in matched):
        return ArtifactStaleStatus.UNKNOWN
    return matched[0]


@dataclass(frozen=True, slots=True)
class _PlanFields:
    parent_issue: int | None
    subtask_id: str | None
    task_kind: str
    plan_revision_digest: str | None
    depends_on: tuple[str, ...] | None
    footprint: tuple[str, ...] | None
    symbols: tuple[str, ...] | None
    dependency_issue_numbers: tuple[int | None, ...] | None


def _plan_fields(plan: PlanMetadataSnapshot | None, issue_number: int) -> _PlanFields:
    empty = _PlanFields(None, None, TASK_KIND_UNSPECIFIED, None, None, None, None, None)
    if plan is None:
        return empty
    matched = [
        subtask for subtask in plan.subtasks if subtask.issue_number == issue_number
    ]
    if len(matched) != 1:
        return empty
    subtask = matched[0]
    return _PlanFields(
        plan.parent_issue,
        subtask.subtask_id,
        _task_kind(subtask.task_kind),
        plan.revision_digest,
        subtask.depends_on,
        subtask.footprint,
        subtask.symbols,
        _dependency_issues(plan, subtask.depends_on),
    )


def _task_kind(value: str | None) -> str:
    if value is None:
        return TASK_KIND_UNSPECIFIED
    stripped = value.strip()
    return stripped or TASK_KIND_UNSPECIFIED


def _dependency_issues(
    plan: PlanMetadataSnapshot, depends_on: tuple[str, ...]
) -> tuple[int | None, ...]:
    counts: dict[str, int] = {}
    issues: dict[str, int | None] = {}
    for subtask in plan.subtasks:
        counts[subtask.subtask_id] = counts.get(subtask.subtask_id, 0) + 1
        issues[subtask.subtask_id] = subtask.issue_number
    return tuple(issues[item] if counts.get(item) == 1 else None for item in depends_on)


def _review_statements(
    comments: Sequence[Mapping[str, Any]],
) -> dict[str, StatementTriple]:
    grouped: dict[str, list[StatementTriple]] = {}
    for comment in comments:
        body = _body(comment)
        if body is None or OUTCOME_MARKER in body or STATEMENTS_MARKER not in body:
            continue
        payload = _fenced_object(body, STATEMENTS_MARKER)
        if payload is None:
            continue
        target = _named_id(payload.get("source_comment_id"))
        if target is None:
            continue
        grouped.setdefault(target, []).append(_statements_from(payload))
    return {target: _agreed_statements(items) for target, items in grouped.items()}


def _agreed_statements(items: Sequence[StatementTriple]) -> StatementTriple:
    return (
        _agreed([item[0] for item in items]),
        _agreed([item[1] for item in items]),
        _agreed([item[2] for item in items]),
    )


def _fill_statements(
    own: StatementTriple, external: StatementTriple
) -> StatementTriple:
    summary, assumptions, changed = own
    external_summary, external_assumptions, external_changed = external
    return (
        summary if summary is not None else external_summary,
        assumptions if assumptions is not None else external_assumptions,
        changed if changed is not None else external_changed,
    )


def _agreed[T](values: Sequence[T | None]) -> T | None:
    present = [value for value in values if value is not None]
    if not present or any(value != present[0] for value in present):
        return None
    return present[0]


def _statements_from(payload: Mapping[str, Any] | None) -> StatementTriple:
    if payload is None:
        return None, None, None
    summary = _optional_text(payload.get("summary")) if "summary" in payload else None
    assumptions = (
        _string_tuple(payload.get("assumptions")) if "assumptions" in payload else None
    )
    changed = (
        _string_tuple(payload.get("changed_files"))
        if "changed_files" in payload
        else None
    )
    return summary, assumptions, changed


def _correction_target(body: str) -> str | None:
    payload = _fenced_object(body, CORRECTION_MARKER)
    if payload is None:
        return None
    return _named_id(payload.get("supersedes_source_comment_id"))


def _fenced_object(body: str, marker: str) -> Mapping[str, Any] | None:
    start = body.find(marker)
    if start < 0:
        return None
    rest = body[start + len(marker) :]
    fence = rest.find("```json")
    if fence < 0:
        return None
    content_start = fence + len("```json")
    fence_end = rest.find("```", content_start)
    if fence_end < 0:
        return None
    return _json_object(rest[content_start:fence_end])


def _json_object(raw: str) -> Mapping[str, Any] | None:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(value, Mapping):
        return value
    return None


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _string_tuple(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None
    return tuple(value)


def _named_id(value: object) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _comment_id(comment: Mapping[str, Any]) -> str | None:
    return _named_id(comment.get("id"))


def _body(comment: Mapping[str, Any]) -> str | None:
    body = comment.get("body")
    if isinstance(body, str):
        return body
    return None


def _recorded_at(comment: Mapping[str, Any]) -> str | None:
    for key in ("updated_at", "updatedAt", "created_at", "createdAt"):
        value = comment.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _comment_url(comment: Mapping[str, Any]) -> str | None:
    for key in ("html_url", "htmlUrl"):
        value = comment.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _pr_url(repository: str, pr: int | None) -> str | None:
    if pr is None or not _repository_ok(repository):
        return None
    return f"https://github.com/{repository}/pull/{pr}"

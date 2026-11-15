"""Acceptance tests for shared-artifact-v1 extraction."""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from read_dependency_test_support import EMPTY_DECLARATION, SHA_HEAD

from orchestune.context.contracts import (
    CORRECTION_MARKER,
    SCHEMA_VERSION,
    STATEMENTS_MARKER,
    TASK_KIND_UNSPECIFIED,
    ArtifactStaleStatus,
    ExtractionRequest,
    PlanMetadataSnapshot,
    PlanSubtaskMetadata,
    StaleJudgment,
)
from orchestune.context.extract import extract_shared_artifacts, source_revision_digest
from orchestune.outcome_record import (
    OUTCOME_MARKER,
    RESULT_BLOCKED,
    RESULT_DONE,
    RESULT_NOT_NEEDED,
    OutcomeLookupState,
    OutcomeRecord,
    ReviewSummary,
)

REPOSITORY = "Saltmu/orchestune"
ISSUE = 1851
EXTRACTOR_FILES = (
    Path("orchestune/context/__init__.py"),
    Path("orchestune/context/contracts.py"),
    Path("orchestune/context/extract.py"),
)
FORBIDDEN_IMPORTS = (
    "sqlite3",
    "orchestune.forge",
    "orchestune.dispatch",
    "orchestune.targets",
)


def _request(
    comments: tuple[Mapping[str, Any], ...] | None,
    *,
    fetch_complete: bool = True,
    plan: PlanMetadataSnapshot | None = None,
    stale_judgments: tuple[StaleJudgment, ...] = (),
    repository: str = REPOSITORY,
    issue_number: int = ISSUE,
) -> ExtractionRequest:
    return ExtractionRequest(
        repository=repository,
        issue_number=issue_number,
        fetch_complete=fetch_complete,
        comments=comments,
        plan=plan,
        stale_judgments=stale_judgments,
    )


def _comment(comment_id: object, body: str, **extra: object) -> dict[str, object]:
    comment: dict[str, object] = {
        "id": comment_id,
        "body": body,
        "created_at": "2026-11-01T00:00:00Z",
    }
    comment.update(extra)
    return comment


def _outcome(body_extra: str = "", **fields: Any) -> str:
    record = OutcomeRecord(
        result=fields.pop("result", RESULT_DONE),
        issue=fields.pop("issue", ISSUE),
        **fields,
    )
    return record.render() + body_extra


def _statements(**payload: object) -> str:
    return f"{STATEMENTS_MARKER}\n```json\n{json.dumps(payload)}\n```\n"


def _correction(target: object) -> str:
    payload = {"supersedes_source_comment_id": target}
    return f"{CORRECTION_MARKER}\n```json\n{json.dumps(payload)}\n```\n"


def _plan(
    *subtasks: PlanSubtaskMetadata, parent_issue: int | None = 1841
) -> PlanMetadataSnapshot:
    return PlanMetadataSnapshot(
        revision_digest="plan-rev-1",
        parent_issue=parent_issue,
        subtasks=subtasks,
    )


def _subtask(
    subtask_id: str,
    issue_number: int | None,
    depends_on: tuple[str, ...] = (),
    task_kind: str | None = None,
) -> PlanSubtaskMetadata:
    return PlanSubtaskMetadata(
        subtask_id=subtask_id,
        issue_number=issue_number,
        depends_on=depends_on,
        footprint=("orchestune/context/extract.py",),
        symbols=("extract_shared_artifacts",),
        task_kind=task_kind,
    )


def test_stale_status_has_no_fresh_value() -> None:
    assert "fresh" not in {item.value for item in ArtifactStaleStatus}


def test_old_outcome_keeps_missing_identity_fields_null() -> None:
    body = (
        f"{OUTCOME_MARKER}\n```json\n"
        '{"result": "done", "issue": 1851, "claim_id": null, "attempt": null,'
        ' "head_sha": null, "completion_id": null}\n```\n'
    )
    result = extract_shared_artifacts(_request((_comment("9", body),)))
    artifact = result.artifacts[0]

    assert result.state is OutcomeLookupState.FOUND
    assert artifact.schema_version == SCHEMA_VERSION
    assert artifact.claim_id is None
    assert artifact.attempt is None
    assert artifact.target_commit is None
    assert artifact.completion_id is None
    assert artifact.summary is None
    assert artifact.assumptions is None
    assert artifact.indexed_at is None
    assert artifact.task_kind == TASK_KIND_UNSPECIFIED
    assert artifact.stale_status is ArtifactStaleStatus.UNKNOWN
    assert artifact.supersedes is None
    assert artifact.superseded_by == ()
    assert artifact.depends_on is None
    assert artifact.proves_current_dependencies() is False


def test_omitted_legacy_keys_stay_missing() -> None:
    body = f"{OUTCOME_MARKER}\n```json\n" '{"result": "done", "issue": 1851}\n```\n'
    artifact = extract_shared_artifacts(
        _request(({"id": 3, "body": body, "htmlUrl": "https://example.test/c"},))
    ).artifacts[0]

    assert artifact.source_comment_id == "3"
    assert artifact.claim_id is None
    assert artifact.attempt is None
    assert artifact.completion_id is None
    assert artifact.target_commit is None
    assert artifact.base_sha is None
    assert artifact.recorded_at is None
    assert artifact.source_url == "https://example.test/c"


def test_same_snapshot_reimports_equal_records_independent_of_order() -> None:
    done = _outcome(attempt=1, claim_id="claim-a")
    blocked = _outcome(result=RESULT_BLOCKED, reason="review-timeout", attempt=2)
    comments = (_comment("2", blocked), _comment("1", done))
    forward = extract_shared_artifacts(_request(comments))
    backward = extract_shared_artifacts(_request(tuple(reversed(comments))))
    again = extract_shared_artifacts(_request(comments))

    assert forward == backward == again
    assert [item.source_comment_id for item in forward.artifacts] == ["1", "2"]
    assert forward.artifacts[0].source_revision_digest == source_revision_digest(done)


def test_comment_edit_changes_digest_without_a_new_attempt_or_supersede() -> None:
    original = _outcome(attempt=1, claim_id="claim-a")
    edited = original + "\nedited\n"
    first = extract_shared_artifacts(_request((_comment("8", original),))).artifacts[0]
    second = extract_shared_artifacts(_request((_comment("8", edited),))).artifacts[0]

    assert first.attempt == second.attempt == 1
    assert first.claim_id == second.claim_id == "claim-a"
    assert first.source_revision_digest != second.source_revision_digest
    assert first.supersedes is None
    assert second.supersedes is None
    assert first.superseded_by == ()
    assert second.superseded_by == ()


def test_later_blocked_outcome_coexists_with_earlier_done() -> None:
    comments = (
        _comment(
            "1",
            _outcome(attempt=1, claim_id="claim-old", pr=12),
            html_url="https://github.com/Saltmu/orchestune/issues/1851#issuecomment-1",
        ),
        _comment(
            "2",
            _outcome(
                result=RESULT_BLOCKED,
                reason="review-timeout",
                attempt=2,
                claim_id="claim-new",
            ),
            updated_at="2026-11-02T00:00:00Z",
        ),
    )
    artifacts = extract_shared_artifacts(_request(comments)).artifacts
    done, blocked = artifacts

    assert done.result == RESULT_DONE
    assert blocked.result == RESULT_BLOCKED
    assert done.history_role() == "recorded_completion"
    assert blocked.history_role() == "reference_history"
    assert done.superseded_by == ()
    assert blocked.supersedes is None
    assert done.proves_current_dependencies() is False
    assert blocked.attempt == 2
    assert done.evidence_refs.pr_url == "https://github.com/Saltmu/orchestune/pull/12"
    assert done.source_url == comments[0]["html_url"]
    assert blocked.recorded_at == "2026-11-02T00:00:00Z"


def test_explicit_correction_sets_supersedes_and_derives_superseded_by() -> None:
    done = _outcome(attempt=1, claim_id="claim-old")
    corrected = _outcome(
        result=RESULT_BLOCKED, reason="review-timeout", attempt=2
    ) + _correction(1)
    artifacts = extract_shared_artifacts(
        _request((_comment(1, done), _comment(2, corrected)))
    ).artifacts

    assert artifacts[1].supersedes == artifacts[0].key()
    assert artifacts[0].superseded_by == (artifacts[1].key(),)
    assert artifacts[1].attempt == 2


def test_not_needed_is_a_recorded_completion_and_not_a_correction() -> None:
    body = _outcome(result=RESULT_NOT_NEEDED)
    artifact = extract_shared_artifacts(_request((_comment("4", body),))).artifacts[0]

    assert artifact.history_role() == "recorded_completion"
    assert replace(artifact, result="other").history_role() == "unknown"
    assert artifact.supersedes is None
    assert artifact.proves_current_dependencies() is False


def test_self_correction_and_unknown_target_do_not_set_supersedes() -> None:
    self_target = _outcome() + _correction("1")
    missing_target = _outcome() + _correction("99")
    malformed = _outcome() + f"{CORRECTION_MARKER}\n```json\nnot-json\n```\n"

    for body in (self_target, missing_target, malformed):
        artifact = extract_shared_artifacts(_request((_comment("1", body),))).artifacts[
            0
        ]
        assert artifact.supersedes is None


def test_ambiguous_comment_revisions_are_not_treated_as_a_correction_target() -> None:
    first = _outcome(attempt=1)
    second = first + "\nsecond\n"
    corrected = _outcome(result=RESULT_BLOCKED, reason="review-timeout") + _correction(
        "1"
    )
    artifacts = extract_shared_artifacts(
        _request(
            (_comment("1", first), _comment("1", second), _comment("2", corrected))
        )
    ).artifacts

    assert len(artifacts) == 3
    assert all(artifact.supersedes is None for artifact in artifacts)
    assert all(artifact.superseded_by == () for artifact in artifacts)


def test_duplicate_identical_comment_is_one_record() -> None:
    body = _outcome(attempt=1)
    result = extract_shared_artifacts(
        _request((_comment("1", body), _comment("1", body)))
    )

    assert len(result.artifacts) == 1


def test_summary_and_assumptions_come_only_from_explicit_statements() -> None:
    outcome = _outcome(
        review=ReviewSummary(bot="codex", rounds=2, verdict="pass"),
        ci="passing",
    )
    review = _comment(
        "review",
        "The prose verdict is pass.\n"
        + _statements(
            source_comment_id="1",
            summary="Kept the nulls",
            assumptions=["old records stay null"],
        ),
    )
    artifact = extract_shared_artifacts(
        _request((_comment("1", outcome), review))
    ).artifacts[0]

    assert artifact.summary == "Kept the nulls"
    assert artifact.assumptions == ("old records stay null",)
    assert artifact.verification_summary.review_verdict == "pass"
    assert artifact.verification_summary.review_rounds == 2
    assert artifact.verification_summary.ci == "passing"


def test_statements_on_the_outcome_win_and_blank_text_stays_missing() -> None:
    body = _outcome() + _statements(
        summary="  ", assumptions="not-a-list", changed_files=["a.py"]
    )
    review = _comment(
        "review", _statements(source_comment_id="1", summary="from review")
    )
    artifact = extract_shared_artifacts(
        _request((_comment("1", body), review))
    ).artifacts[0]

    assert artifact.summary == "from review"
    assert artifact.assumptions is None
    assert artifact.evidence_refs.changed_files == ("a.py",)


def test_conflicting_review_statements_stay_missing() -> None:
    reviews = (
        _comment("r1", _statements(source_comment_id="1", summary="one")),
        _comment("r2", _statements(source_comment_id="1", summary="two")),
    )
    artifact = extract_shared_artifacts(
        _request((_comment("1", _outcome()), *reviews))
    ).artifacts[0]

    assert artifact.summary is None


def test_explicit_empty_assumptions_stay_empty() -> None:
    body = _outcome() + _statements(assumptions=[])
    artifact = extract_shared_artifacts(_request((_comment("1", body),))).artifacts[0]

    assert artifact.assumptions == ()
    assert artifact.summary is None


def test_plan_revision_fills_dependency_fields_without_proving_satisfaction() -> None:
    plan = _plan(
        _subtask(
            "context-schema-extract", ISSUE, ("context-other",), task_kind="bugfix"
        ),
        _subtask("context-other", 1850),
        _subtask("missing-target", None),
    )
    body = _outcome() + _statements(changed_files=["kept.py"])
    artifact = extract_shared_artifacts(
        _request((_comment("1", body),), plan=plan)
    ).artifacts[0]

    assert artifact.plan_revision_digest == "plan-rev-1"
    assert artifact.parent_issue == 1841
    assert artifact.subtask_id == "context-schema-extract"
    assert artifact.task_kind == "bugfix"
    assert artifact.depends_on == ("context-other",)
    assert artifact.dependency_issue_numbers == (1850,)
    assert artifact.footprint == ("orchestune/context/extract.py",)
    assert artifact.symbols == ("extract_shared_artifacts",)
    assert artifact.evidence_refs.changed_files == ("kept.py",)
    assert artifact.proves_current_dependencies() is False


def test_unmatched_or_ambiguous_plan_leaves_plan_fields_missing() -> None:
    other = _plan(_subtask("elsewhere", 1800))
    ambiguous = _plan(_subtask("one", ISSUE), _subtask("two", ISSUE))
    blank_kind = _plan(_subtask("context-schema-extract", ISSUE, task_kind="  "))
    duplicate_dependency = _plan(
        _subtask("context-schema-extract", ISSUE, ("repeated",)),
        _subtask("repeated", 1),
        _subtask("repeated", 2),
    )

    unmatched = extract_shared_artifacts(
        _request((_comment("1", _outcome()),), plan=other)
    ).artifacts[0]
    both = extract_shared_artifacts(
        _request((_comment("1", _outcome()),), plan=ambiguous)
    ).artifacts[0]
    blank = extract_shared_artifacts(
        _request((_comment("1", _outcome()),), plan=blank_kind)
    ).artifacts[0]
    repeated = extract_shared_artifacts(
        _request((_comment("1", _outcome()),), plan=duplicate_dependency)
    ).artifacts[0]

    assert unmatched.depends_on is None
    assert unmatched.footprint is None
    assert unmatched.plan_revision_digest is None
    assert unmatched.task_kind == TASK_KIND_UNSPECIFIED
    assert both.subtask_id is None
    assert blank.task_kind == TASK_KIND_UNSPECIFIED
    assert blank.subtask_id == "context-schema-extract"
    assert repeated.dependency_issue_numbers == (None,)


def test_absent_and_unknown_fetches_stay_distinct() -> None:
    done = (_comment("1", _outcome()),)
    absent = extract_shared_artifacts(_request(()))
    chatter = extract_shared_artifacts(_request((_comment("1", "no outcome here"),)))
    failed = extract_shared_artifacts(_request(done, fetch_complete=False))
    omitted = extract_shared_artifacts(_request(None))
    unkeyed = extract_shared_artifacts(
        _request(({"body": _outcome(), "created_at": "2026-11-01T00:00:00Z"},))
    )
    other_issue = extract_shared_artifacts(
        _request((_comment("1", _outcome(issue=9)),))
    )
    bad_repository = extract_shared_artifacts(_request(done, repository="not a repo"))

    assert absent.state is OutcomeLookupState.ABSENT
    assert absent.artifacts == ()
    assert chatter.state is OutcomeLookupState.ABSENT
    assert failed.state is OutcomeLookupState.UNKNOWN
    assert failed.artifacts == ()
    assert omitted.state is OutcomeLookupState.UNKNOWN
    assert unkeyed.state is OutcomeLookupState.UNKNOWN
    assert unkeyed.artifacts == ()
    assert other_issue.state is OutcomeLookupState.ABSENT
    assert bad_repository.state is OutcomeLookupState.UNKNOWN


def test_read_evidence_alone_is_not_stale_and_judgments_must_match() -> None:
    body = _outcome(
        head_sha=SHA_HEAD,
        claim_id="claim-reader",
        completion_id="completion-1",
        read_evidence=EMPTY_DECLARATION,
    )
    digest = EMPTY_DECLARATION.digest
    assert digest is not None
    comments = (_comment("1", body),)
    candidate = StaleJudgment(
        target_commit=SHA_HEAD,
        report_digest=digest,
        status=ArtifactStaleStatus.CANDIDATE_STALE,
    )
    confirmed = StaleJudgment(
        target_commit=SHA_HEAD,
        report_digest=digest,
        status=ArtifactStaleStatus.CONFIRMED_STALE,
    )
    wrong_head = StaleJudgment(
        target_commit="a" * 40,
        report_digest=digest,
        status=ArtifactStaleStatus.CONFIRMED_STALE,
    )
    wrong_digest = StaleJudgment(
        target_commit=SHA_HEAD,
        report_digest="f" * 64,
        status=ArtifactStaleStatus.CANDIDATE_STALE,
    )

    alone = extract_shared_artifacts(_request(comments)).artifacts[0]
    marked = extract_shared_artifacts(
        _request(comments, stale_judgments=(candidate,))
    ).artifacts[0]
    accepted = extract_shared_artifacts(
        _request(comments, stale_judgments=(confirmed, confirmed))
    ).artifacts[0]
    mismatched = extract_shared_artifacts(
        _request(comments, stale_judgments=(wrong_head, wrong_digest))
    ).artifacts[0]
    conflict = extract_shared_artifacts(
        _request(comments, stale_judgments=(candidate, confirmed))
    ).artifacts[0]

    assert alone.evidence_refs.read_evidence_digest == digest
    assert alone.stale_status is ArtifactStaleStatus.UNKNOWN
    assert marked.stale_status is ArtifactStaleStatus.CANDIDATE_STALE
    assert accepted.stale_status is ArtifactStaleStatus.CONFIRMED_STALE
    assert mismatched.stale_status is ArtifactStaleStatus.UNKNOWN
    assert conflict.stale_status is ArtifactStaleStatus.UNKNOWN
    assert alone.proves_current_dependencies() is False


def test_malformed_notes_and_bad_identity_do_not_invent_records() -> None:
    done = _outcome() + _statements(summary=1, assumptions=["kept"]) + _correction(1.5)
    reviews = (
        {"id": "missing-body"},
        {"id": "non-text-body", "body": None},
        _comment("orphan", _statements(summary="not attached")),
        _comment("blank-target", _statements(source_comment_id="", summary="nope")),
        _comment("bool-target", _statements(source_comment_id=True, summary="nope")),
        _comment("bad-json", f"{STATEMENTS_MARKER}\n```json\nnot-json\n```\n"),
        _comment("list-json", f"{STATEMENTS_MARKER}\n```json\n[]\n```\n"),
        _comment("no-fence", f"{STATEMENTS_MARKER}\nsummary only\n"),
        _comment("open-fence", f"{STATEMENTS_MARKER}\n```json\n{{}}\n"),
    )
    artifact = extract_shared_artifacts(
        _request((_comment("1", done), *reviews))
    ).artifacts[0]
    comments = (_comment("1", _outcome()),)
    zero = extract_shared_artifacts(_request(comments, issue_number=0))
    flagged = extract_shared_artifacts(
        replace(_request(comments), issue_number=cast(int, True))
    )
    nested = extract_shared_artifacts(
        _request(comments, repository="Saltmu/orchestune/extra")
    )

    assert artifact.summary is None
    assert artifact.assumptions == ("kept",)
    assert artifact.supersedes is None
    assert zero.state is OutcomeLookupState.UNKNOWN
    assert zero.artifacts == ()
    assert flagged.state is OutcomeLookupState.UNKNOWN
    assert nested.state is OutcomeLookupState.UNKNOWN


def test_context_modules_are_registered_on_l0() -> None:
    from architecture_layers_support import EXPECTED_LAYERS

    expected = {"context", "context.contracts", "context.extract"}
    assert expected <= EXPECTED_LAYERS[0]


def test_extractor_does_not_import_forge_sqlite_or_dispatch_targets() -> None:
    for path in EXTRACTOR_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = _imported_modules(tree)
        assert imported.isdisjoint(_forbidden_prefixes(imported))


def _imported_modules(tree: ast.AST) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def _forbidden_prefixes(imported: set[str]) -> set[str]:
    return {
        name
        for name in imported
        if name == "sqlite3" or name.startswith(FORBIDDEN_IMPORTS)
    }

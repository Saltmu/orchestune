"""Semantic review identity, markers, and fix-result records."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from orchestune.review.semantic_gate import (
    ACTIVE_PENDING_PHASES,
    TERMINAL_PASSED_PHASES,
    VALID_SEMANTIC_REVIEW_MODES,
    ChildSourceRef,
    IntegrationAttemptIdentity,
    SemanticFixResultV1,
    SemanticOperationKind,
    SemanticOperationObservation,
    SemanticReviewHumanOverrideV1,
    SemanticReviewRetryV1,
    SemanticReviewStateV1,
    format_semantic_fix_result_marker,
    format_semantic_review_override_marker,
    format_semantic_review_retry_marker,
    format_semantic_review_state_marker,
    parse_semantic_fix_result_marker,
    parse_semantic_review_override_marker,
    parse_semantic_review_retry_marker,
    parse_semantic_review_state_marker,
    restore_latest_fix_result,
    restore_latest_human_override,
    restore_latest_retry_request,
    restore_latest_semantic_review_state,
    semantic_fix_output_branch,
    semantic_operation_id,
)
from tests.review.semantic_review_state_support import (
    BASE,
    CHILDREN,
    EARLIER,
    FIXED,
    HEAD,
    IDENTITY,
    KIND,
    OK,
    OTHER,
    P,
    make_state,
    retry_req,
)


def test_attempt_id_is_deterministic_and_order_independent() -> None:
    again = IntegrationAttemptIdentity.create(
        1033, f" {BASE} ", [ChildSourceRef(11, "alpha", "1" * 40), CHILDREN[0]]
    )
    assert again.attempt_id == IDENTITY.attempt_id
    assert len(IDENTITY.attempt_id) == 64
    assert IDENTITY.child_sources[0].issue_number == 11
    assert IDENTITY.temp_branch == (
        f"integration/temp-parent-issue-1033-{IDENTITY.attempt_id[:12]}"
    )


@pytest.mark.parametrize(
    "args",
    [
        (1034, BASE, CHILDREN),
        (1033, OTHER, CHILDREN),
        (1033, BASE, CHILDREN[:1]),
        (1033, BASE, [(12, "beta", "3" * 40), CHILDREN[1]]),
        (1033, BASE, [(12, "gamma", "2" * 40), CHILDREN[1]]),
    ],
)
def test_any_input_change_yields_a_new_attempt_id(args: tuple[Any, ...]) -> None:
    assert IntegrationAttemptIdentity.create(*args).attempt_id != IDENTITY.attempt_id


@pytest.mark.parametrize(
    "children",
    [
        [CHILDREN[0], CHILDREN[0]],
        [(0, "x", "1")],
        [(True, "x", "1")],
        [(1, "", "1")],
        [(1, "x")],
        [{"issue_number": 1, "subtask_id": "x"}],
        ["nope"],
    ],
)
def test_invalid_child_sources_are_rejected(children: list[Any]) -> None:
    with pytest.raises(ValueError):
        IntegrationAttemptIdentity.create(1033, BASE, children)


@pytest.mark.parametrize("parent,base", [(0, BASE), (1033, " "), (1033, None)])
def test_invalid_parent_or_base_is_rejected(parent: int, base: Any) -> None:
    with pytest.raises(ValueError):
        IntegrationAttemptIdentity.create(parent, base, CHILDREN)


def test_matches_state_checks_attempt_parent_and_base() -> None:
    state = make_state()
    assert IDENTITY.matches_state(state)
    assert not IDENTITY.matches_state(replace(state, base_sha=OTHER))
    assert not IDENTITY.matches_state(replace(state, parent_issue_number=1))
    assert not IDENTITY.matches_state(replace(state, attempt_id="x"))


def test_modes_and_phase_sets() -> None:
    assert VALID_SEMANTIC_REVIEW_MODES == {"required", "advisory", "off"}
    assert not TERMINAL_PASSED_PHASES & ACTIVE_PENDING_PHASES
    assert P.ESCALATED not in TERMINAL_PASSED_PHASES | ACTIVE_PENDING_PHASES


def full_state() -> SemanticReviewStateV1:
    return make_state(
        phase=P.FIX_IN_PROGRESS,
        round=2,
        claim_holder="host-1",
        claim_expires_at="2026-10-18T13:00:00Z",
        ci_verified_sha=HEAD,
        judgment_digest="j" * 64,
        preflight_notice_key="notice-1",
        escalation_reason="why --> <b>",
        escalated_from=P.CI_VERIFICATION,
        fix_retries=1,
        updated_at=EARLIER,
        state_revision=9,
        claim_generation=3,
        attempt_started_at=EARLIER,
        phase_started_at=EARLIER,
        round_started_at=EARLIER,
        reviewed_sha=HEAD,
        review_source_digest="s" * 64,
        operation_id="op-1",
        operation_kind=KIND.FIX,
        operation_status=OK.INDETERMINATE,
        provider_handle="handle-1",
        fix_input_sha=HEAD,
        fix_output_sha=FIXED,
        fix_completed=True,
        restart_count=2,
        last_retry_request_id="req-0",
        limits_reset_reason="config fixed",
        read_report_digest="a" * 64,
        reviewed_read_report_digest="b" * 64,
    )


def test_state_marker_round_trips_every_field() -> None:
    state = full_state()
    marker = format_semantic_review_state_marker(state)
    assert "\n" not in marker and marker.count("-->") == 1
    assert parse_semantic_review_state_marker(f"Status\n\n{marker}\n") == state
    assert SemanticReviewStateV1.from_dict(state.to_dict()) == state


def test_quoted_and_fenced_state_markers_are_ignored() -> None:
    marker = format_semantic_review_state_marker(make_state())
    assert parse_semantic_review_state_marker(f"> {marker}") is None
    assert parse_semantic_review_state_marker(f"```\n{marker}\n```") is None
    assert parse_semantic_review_state_marker(f"    {marker}") is None
    assert parse_semantic_review_state_marker("no marker") is None


@pytest.mark.parametrize(
    "body",
    [
        "<!-- orchestune:semantic-review-state:v1 {not json} -->",
        "<!-- orchestune:semantic-review-state:v1 [] -->",
        "<!-- orchestune:semantic-review-state:v1 {}",
        "<!-- orchestune:semantic-review-state:v1 {} -->\n\n"
        "<!-- orchestune:semantic-review-state:v1 {} -->",
    ],
)
def test_malformed_state_markers_raise(body: str) -> None:
    with pytest.raises(ValueError):
        parse_semantic_review_state_marker(body)


@pytest.mark.parametrize(
    "key,value",
    [
        ("phase", "done"),
        ("phase", 3),
        ("round", -1),
        ("fix_retries", True),
        ("state_revision", "1"),
        ("parent_issue_number", 0),
        ("operation_status", "paused"),
        ("operation_kind", "judgment"),
        ("escalated_from", "changes_requested"),
        ("last_retry_request_id", 3),
        ("fix_completed", "yes"),
        ("head_sha", ""),
        ("claim_holder", 5),
        ("child_sources", "x"),
        ("surprise", 1),
        ("read_report_digest", 7),
        ("read_report_digest", "x"),
        ("reviewed_read_report_digest", "A" * 64),
    ],
)
def test_state_fields_are_strictly_typed(key: str, value: Any) -> None:
    data = full_state().to_dict()
    data[key] = value
    with pytest.raises(ValueError):
        SemanticReviewStateV1.from_dict(data)


def test_state_requires_core_fields_and_a_mapping() -> None:
    data = make_state().to_dict()
    del data["head_sha"]
    with pytest.raises(ValueError, match="missing"):
        SemanticReviewStateV1.from_dict(data)
    with pytest.raises(ValueError):
        SemanticReviewStateV1.from_dict([])  # type: ignore[arg-type]


def test_legacy_state_without_read_digests_remains_readable() -> None:
    data = full_state().to_dict()
    del data["read_report_digest"], data["reviewed_read_report_digest"]
    state = SemanticReviewStateV1.from_dict(data)
    assert state.read_report_digest is state.reviewed_read_report_digest is None


def test_restore_returns_latest_matching_attempt() -> None:
    first = format_semantic_review_state_marker(make_state(state_revision=1))
    other = format_semantic_review_state_marker(
        make_state(attempt_id="other", state_revision=5)
    )
    second = format_semantic_review_state_marker(make_state(state_revision=2))
    bodies = [first, second, other, "chatter"]
    assert restore_latest_semantic_review_state(bodies).attempt_id == "other"  # type: ignore[union-attr]
    restored = restore_latest_semantic_review_state(
        bodies, attempt_id=IDENTITY.attempt_id
    )
    assert restored is not None and restored.state_revision == 2
    assert restore_latest_semantic_review_state(bodies, attempt_id="none") is None
    assert restore_latest_semantic_review_state([]) is None


def test_restore_never_falls_back_past_a_corrupt_newer_marker() -> None:
    passed = format_semantic_review_state_marker(make_state(phase=P.PASSED))
    corrupt = "<!-- orchestune:semantic-review-state:v1 {broken -->"
    with pytest.raises(ValueError):
        restore_latest_semantic_review_state(
            [passed, corrupt], attempt_id=IDENTITY.attempt_id
        )
    restored = restore_latest_semantic_review_state([corrupt, passed])
    assert restored is not None and restored.phase == P.PASSED


@pytest.mark.parametrize(
    "revisions,conflict",
    [
        ([1, 2, 3], False),
        ([1, 2, 2], False),
        ([1, 3, 2], True),
        ([2, 2], True),
        ([4, 1], True),
    ],
)
def test_restore_rejects_revision_conflicts(
    revisions: list[int], conflict: bool
) -> None:
    states = [make_state(state_revision=r) for r in revisions]
    if revisions == [2, 2]:
        states[1] = replace(states[1], phase=P.PASSED)
    other = format_semantic_review_state_marker(make_state(attempt_id="x"))
    bodies = [other, *map(format_semantic_review_state_marker, states)]
    if conflict:
        with pytest.raises(ValueError, match="revision conflict"):
            restore_latest_semantic_review_state(bodies)
        return
    assert restore_latest_semantic_review_state(bodies) == states[-1]


def test_override_marker_round_trip_and_restore() -> None:
    override = SemanticReviewHumanOverrideV1(IDENTITY.attempt_id, HEAD, "ok", "alice")
    marker = format_semantic_review_override_marker(override)
    assert parse_semantic_review_override_marker(marker) == override
    assert parse_semantic_review_override_marker(f"> {marker}") is None
    stale = format_semantic_review_override_marker(replace(override, head_sha=OTHER))
    bodies = [marker, stale, f"```\n{marker}\n```"]
    kwargs = {"attempt_id": IDENTITY.attempt_id}
    assert restore_latest_human_override(bodies, head_sha=HEAD, **kwargs) == override
    assert restore_latest_human_override(bodies, head_sha=FIXED, **kwargs) is None
    assert restore_latest_human_override(bodies, attempt_id="x", head_sha=HEAD) is None


@pytest.mark.parametrize(
    "payload",
    [
        '{"attempt_id":"a","head_sha":"h"}',
        '{"attempt_id":"a","head_sha":"h","reason":"r","extra":1}',
        '{"attempt_id":"a","head_sha":"h","reason":""}',
        '{"attempt_id":"a","head_sha":"h","reason":"r","actor":3}',
        "[]",
    ],
)
def test_invalid_override_markers_raise(payload: str) -> None:
    with pytest.raises(ValueError):
        parse_semantic_review_override_marker(
            f"<!-- orchestune:semantic-review-override:v1 {payload} -->"
        )


def test_retry_marker_round_trip_and_restore() -> None:
    request = retry_req("restart", reset_limits=True, actor="alice")
    marker = format_semantic_review_retry_marker(request)
    assert "\n" not in marker and parse_semantic_review_retry_marker(marker) == request
    assert parse_semantic_review_retry_marker(f"> {marker}") is None
    assert parse_semantic_review_retry_marker(f"```\n{marker}\n```") is None
    older = format_semantic_review_retry_marker(retry_req(request_id="req-0"))
    stale = format_semantic_review_retry_marker(replace(request, head_sha=OTHER))
    bodies = [older, marker, stale, f"> {older}"]
    kwargs: dict[str, Any] = {"attempt_id": IDENTITY.attempt_id, "head_sha": HEAD}
    assert restore_latest_retry_request(bodies, consumed_request_ids=(), **kwargs) == (
        request
    )
    # A consumed newest request never lets an older one be revived.
    consumed = restore_latest_retry_request(
        bodies, consumed_request_ids={"req-1"}, **kwargs
    )
    assert consumed is None
    kwargs["head_sha"] = FIXED
    assert (
        restore_latest_retry_request(bodies, consumed_request_ids=(), **kwargs) is None
    )


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "resume"},
        {"kind": ["retry"]},
        {"reset_limits": "yes"},
        {"request_id": ""},
        {"actor": 3},
        {"extra": 1},
        {"reason": None},
    ],
)
def test_invalid_retry_markers_raise(change: dict[str, Any]) -> None:
    data = {**retry_req().to_dict(), **change}
    with pytest.raises(ValueError):
        SemanticReviewRetryV1.from_dict(data)
    with pytest.raises(ValueError):
        SemanticReviewRetryV1.from_dict([])  # type: ignore[arg-type]


OP = semantic_operation_id(IDENTITY.attempt_id, 1, SemanticOperationKind.FIX, 0)

FIX_RESULT = SemanticFixResultV1(IDENTITY.attempt_id, OP, HEAD, FIXED, 42, "completed")


def test_fix_result_marker_round_trip_and_restore() -> None:
    marker = format_semantic_fix_result_marker(FIX_RESULT)
    assert parse_semantic_fix_result_marker(marker) == FIX_RESULT
    assert parse_semantic_fix_result_marker(f"> {marker}") is None
    assert parse_semantic_fix_result_marker(f"```\n{marker}\n```") is None
    unchanged = replace(FIX_RESULT, output_sha=None, judgment_comment_id=None)
    newer = format_semantic_fix_result_marker(unchanged)
    other = format_semantic_fix_result_marker(
        replace(FIX_RESULT, operation_id=f"{OP}x")
    )
    bodies = [marker, newer, other]
    kwargs = {"attempt_id": IDENTITY.attempt_id, "operation_id": OP}
    assert restore_latest_fix_result(bodies, **kwargs) == unchanged
    assert restore_latest_fix_result([other], **kwargs) is None


@pytest.mark.parametrize(
    "change",
    [
        {"status": "done"},
        {"status": ["failed"]},
        {"status": "failed"},
        {"operation_id": "zzz-g0-r1-fix-0"},
        {"judgment_comment_id": 0},
        {"judgment_comment_id": True},
        {"input_sha": None},
        {"output_sha": ""},
        {"extra": 1},
    ],
)
def test_invalid_fix_results_raise(change: dict[str, Any]) -> None:
    data = {**FIX_RESULT.to_dict(), **change}
    with pytest.raises(ValueError):
        SemanticFixResultV1.from_dict(data)
    del data["output_sha"]
    with pytest.raises(ValueError):
        SemanticFixResultV1.from_dict(data)


def test_fix_output_branch_is_outside_temp_gc() -> None:
    branch = semantic_fix_output_branch(OP)
    assert branch == f"integration/semantic-fix/{OP}"
    assert OP.startswith(IDENTITY.attempt_id[:12])
    assert not branch.startswith("integration/temp-")
    for bad in ("", "a/b", "a..b", "-a", "a b", "x.lock", None):
        with pytest.raises(ValueError):
            semantic_fix_output_branch(bad)  # type: ignore[arg-type]


def test_fix_result_maps_to_an_operation_observation() -> None:
    changed = SemanticOperationObservation.from_fix_result(FIX_RESULT)
    assert changed == SemanticOperationObservation(OP, OK.COMPLETED, FIXED, HEAD)
    same = replace(FIX_RESULT, output_sha=None)
    assert SemanticOperationObservation.from_fix_result(same).output_sha == HEAD
    failed = replace(FIX_RESULT, output_sha=None, status="failed")
    assert SemanticOperationObservation.from_fix_result(failed) == (
        SemanticOperationObservation(OP, OK.FAILED, None, HEAD)
    )

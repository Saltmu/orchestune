"""Layer-2 semantic review contracts: identity, markers, fast path, transitions."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from orchestune.review.semantic_gate import (
    ACTIVE_PENDING_PHASES,
    TERMINAL_PASSED_PHASES,
    ChildSourceRef,
    FastPathDecision,
    IntegrationAttemptIdentity,
    SemanticCiConclusion,
    SemanticCiObservation,
    SemanticFixResultV1,
    SemanticJudgmentDecision,
    SemanticOperationObservation,
    SemanticRetryKind,
    SemanticReviewHumanOverrideV1,
    SemanticReviewLimits,
    SemanticReviewMode,
    SemanticReviewPhase,
    SemanticReviewRetryV1,
    SemanticReviewStateV1,
    SemanticTransitionDecision,
    SemanticTransitionObservation,
    evaluate_semantic_judgments,
    evaluate_semantic_review_transition,
    evaluate_single_task_fast_path,
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
    NOW,
    OK,
    OTHER,
    P,
    make_state,
    retry_req,
)

# --- fast path --------------------------------------------------------------


def fast_path(**changes: Any) -> FastPathDecision:
    args: dict[str, Any] = {
        "child_count": 1,
        "force_integration_review": False,
        "base_sha_matches": True,
        "tree_matches_without_conflict": True,
        "layer1_verdict": "pass",
        "layer1_reviewed_sha": HEAD,
        "source_sha": HEAD,
    }
    return evaluate_single_task_fast_path(**{**args, **changes})


@pytest.mark.parametrize(
    "changes,reason",
    [
        (
            {"force_integration_review": True, "child_count": 2},
            "forced_integration_review",
        ),
        ({"child_count": 2}, "multiple_children"),
        ({"child_count": 0}, "multiple_children"),
        ({"base_sha_matches": False}, "base_sha_mismatch"),
        ({"tree_matches_without_conflict": False}, "tree_or_conflict_diff"),
        ({"layer1_verdict": "skipped"}, "layer1_evidence_insufficient"),
        ({"layer1_verdict": None}, "layer1_evidence_insufficient"),
        ({"layer1_reviewed_sha": OTHER}, "layer1_evidence_insufficient"),
        ({"source_sha": "", "layer1_reviewed_sha": ""}, "layer1_evidence_insufficient"),
    ],
)
def test_fast_path_rejections(changes: dict[str, Any], reason: str) -> None:
    assert fast_path(**changes) == FastPathDecision(False, reason, P.NOT_STARTED)


def test_fast_path_passes_only_with_matching_layer1_evidence() -> None:
    assert fast_path() == FastPathDecision(
        True, "single_task_fast_path", P.FAST_PATH_PASSED
    )


# --- judgments --------------------------------------------------------------

SOURCE = "issue_comment:1"
RESULT = {"review_items": [{"id": 1, "provenance": "current", "body": "x"}]}


def table(*rows: tuple[str, str], round_num: int = 1) -> dict[str, Any]:
    return {
        "round": round_num,
        "findings": [
            {
                "source": SOURCE,
                "location": "a.py:1",
                "judgment": judgment,
                "status": status,
                "basis": "because",
                "evidence": "link",
            }
            for judgment, status in rows
        ],
    }


VALID_PAIRS = [
    ("adopt", "unresolved"),
    ("adopt", "resolved"),
    ("decline", "declined"),
    ("already_addressed", "resolved"),
    ("duplicate", "resolved"),
    ("needs_information", "deferred"),
]
ALL_PAIRS = [
    (j, s)
    for j in ("adopt", "decline", "already_addressed", "needs_information", "duplicate")
    for s in ("unresolved", "resolved", "declined", "deferred")
]


@pytest.mark.parametrize("pair", ALL_PAIRS)
def test_every_judgment_status_pair(pair: tuple[str, str]) -> None:
    decision = evaluate_semantic_judgments(table(pair), RESULT, expected_round=1)
    assert decision.valid is (pair in VALID_PAIRS)
    if not decision.valid:
        assert decision.reason == "judgment_status_pair_invalid"
        assert not decision.passable and not decision.requires_fix
        return
    assert decision.requires_fix is (pair[1] == "unresolved")
    assert decision.passable is (pair[1] in {"resolved", "declined"})


def test_judgment_decision_counts_statuses_not_adopts() -> None:
    fixed = evaluate_semantic_judgments(
        table(("adopt", "resolved"), ("decline", "declined")), RESULT, expected_round=1
    )
    assert fixed.passable and fixed.reason == "judgments_settled"
    assert fixed.digest is not None and fixed.round == 1 and fixed.rows == 2
    mixed = evaluate_semantic_judgments(
        table(("adopt", "unresolved"), ("needs_information", "deferred")),
        RESULT,
        expected_round=1,
    )
    assert (mixed.unresolved, mixed.deferred, mixed.reason) == (
        1,
        1,
        "deferred_judgments",
    )
    assert not mixed.passable


def test_judgments_from_a_comment_body() -> None:
    body = (
        "```orchestune-review-judgments\nround: 1\nfindings:\n"
        f"  - source: {SOURCE}\n    location: a.py\n    judgment: decline\n"
        "    status: declined\n    basis: b\n    evidence: e\n```\n"
    )
    assert evaluate_semantic_judgments(body, RESULT, expected_round=1).passable


@pytest.mark.parametrize(
    "judgments,result,round_num,reason",
    [
        (None, RESULT, 1, "judgments_missing"),
        (table(("decline", "declined")), None, 1, "review_result_missing"),
        (table(("decline", "declined")), RESULT, 2, "judgments_invalid"),
        (table(), RESULT, 1, "judgments_invalid"),
        (
            {"round": 1, "findings": [{"source": SOURCE}]},
            RESULT,
            1,
            "judgments_invalid",
        ),
        ("no table", RESULT, 1, "judgments_invalid"),
    ],
)
def test_unverifiable_judgments_never_pass(
    judgments: Any, result: Any, round_num: int, reason: str
) -> None:
    decision = evaluate_semantic_judgments(judgments, result, expected_round=round_num)
    assert not decision.valid and decision.reason.startswith(reason)
    assert not decision.passable


def test_empty_table_is_not_proof_of_review() -> None:
    decision = evaluate_semantic_judgments(table(), {}, expected_round=1)
    assert decision.valid and decision.reason == "empty_judgment_table"
    assert not decision.passable


# --- transitions ------------------------------------------------------------

LIMITS = SemanticReviewLimits(
    max_rounds=2,
    max_fix_retries=1,
    review_timeout_seconds=600,
    fix_timeout_seconds=600,
    ci_timeout_seconds=600,
    attempt_timeout_seconds=3600,
)
PASS = SemanticJudgmentDecision(True, "judgments_settled", 1, "d" * 64, rows=1)
FIX_NEEDED = SemanticJudgmentDecision(
    True, "unresolved_adopt", 1, "e" * 64, rows=1, unresolved=1
)
DEFER = SemanticJudgmentDecision(
    True, "deferred_judgments", 1, "f" * 64, rows=1, deferred=1
)
EMPTY = SemanticJudgmentDecision(True, "empty_judgment_table", 1, "0" * 64)
EVIDENCE: dict[str, Any] = {"review_complete": True, "reviewed_sha": HEAD}


def obs(**changes: Any) -> SemanticTransitionObservation:
    args: dict[str, Any] = {
        "identity": IDENTITY,
        "observed_head_sha": HEAD,
        "now": NOW,
        "limits": LIMITS,
    }
    return SemanticTransitionObservation(**{**args, **changes})


def active(phase: SemanticReviewPhase, **changes: Any) -> SemanticReviewStateV1:
    defaults: dict[str, Any] = {
        "round": 1,
        "attempt_started_at": EARLIER,
        "phase_started_at": EARLIER,
        "round_started_at": EARLIER,
        "state_revision": 4,
    }
    return make_state(phase=phase, **{**defaults, **changes})


def reserved(**c: Any) -> SemanticReviewStateV1:
    op = semantic_operation_id(
        IDENTITY.attempt_id,
        c.get("round", 1),
        KIND.FIX,
        c.get("fix_retries", 0),
        c.get("restart_count", 0),
    )
    return active(
        P.FIX_IN_PROGRESS,
        operation_id=op,
        operation_kind=KIND.FIX,
        operation_status=OK.RESERVED,
        fix_input_sha=HEAD,
        **c,
    )


def done(state: SemanticReviewStateV1, output: str | None = HEAD, status=OK.COMPLETED):  # type: ignore[no-untyped-def]
    assert state.operation_id is not None
    return SemanticOperationObservation(state.operation_id, status, output)


def run(state: SemanticReviewStateV1, **changes: Any) -> SemanticTransitionDecision:
    return evaluate_semantic_review_transition(state, obs(**changes))


def assert_hold(
    state: SemanticReviewStateV1, reason: str, *, promotion: bool = False, **c: Any
) -> None:
    decision = run(state, **c)
    assert decision.reason == reason
    assert decision.next_state is state
    assert decision.requires_fix_promotion is promotion
    assert not (
        decision.requires_fix_dispatch
        or decision.requires_review_trigger
        or decision.requires_escalation
    )


def test_not_started_requests_round_one() -> None:
    decision = run(make_state())
    assert (decision.next_phase, decision.next_round) == (P.REVIEW_PENDING, 1)
    assert decision.requires_review_trigger and not decision.requires_fix_dispatch
    nxt = decision.next_state
    assert nxt.attempt_started_at == nxt.phase_started_at == nxt.round_started_at
    assert nxt.state_revision == 1 and nxt.updated_at == "2026-10-18T12:00:00Z"


def test_review_pending_waits_for_a_complete_review_of_this_head() -> None:
    state = active(P.REVIEW_PENDING)
    assert_hold(state, "review_pending")
    assert_hold(state, "review_pending", review_complete=True, reviewed_sha=OTHER)
    assert_hold(state, "review_pending", reviewed_sha=HEAD, judgments=PASS)


@pytest.mark.parametrize("judgments", [None, PASS, FIX_NEEDED, DEFER, EMPTY])
def test_completed_review_reserves_one_judgment_and_fix_job(
    judgments: SemanticJudgmentDecision | None,
) -> None:
    state = active(P.REVIEW_PENDING)
    decision = run(state, judgments=judgments, **EVIDENCE)
    assert decision.next_phase == P.FIX_IN_PROGRESS and decision.requires_fix_dispatch
    nxt = decision.next_state
    assert nxt.operation_kind == KIND.FIX and nxt.operation_status == OK.RESERVED
    assert nxt.operation_id == semantic_operation_id(
        IDENTITY.attempt_id, 1, KIND.FIX, 0
    )
    assert (nxt.fix_retries, nxt.round, nxt.fix_input_sha) == (0, 1, HEAD)
    # Re-evaluating with the job reserved never reserves a second one.
    assert_hold(nxt, "operation_in_flight", **EVIDENCE)


@pytest.mark.parametrize("status", [OK.RESERVED, OK.DISPATCHING, OK.RUNNING])
def test_jobs_in_flight_are_awaited_even_with_green_pre_fix_ci(status: OK) -> None:
    state = reserved()
    green = SemanticCiObservation(HEAD, SemanticCiConclusion.SUCCESS)
    assert_hold(
        state, "operation_in_flight", operation=done(state, None, status), ci=green
    )
    assert_hold(state, "operation_in_flight", ci=green)
    foreign = SemanticOperationObservation("other-op", OK.COMPLETED, FIXED)
    assert_hold(state, "operation_in_flight", operation=foreign)


def test_operation_without_reservation_escalates() -> None:
    decision = run(active(P.FIX_IN_PROGRESS))
    assert decision.reason == "operation_missing" and decision.requires_escalation


def test_indeterminate_unknown_or_foreign_input_escalates() -> None:
    state = reserved()
    assert run(state, operation=done(state, None, OK.INDETERMINATE)).reason == (
        "operation_indeterminate"
    )
    assert run(state, operation=done(state, None)).reason == "operation_output_unknown"
    foreign = replace(done(state, FIXED), input_sha=OTHER)
    decision = run(state, operation=foreign, observed_head_sha=FIXED)
    assert decision.reason == "operation_input_mismatch"
    assert decision.next_state.escalated_from == P.FIX_IN_PROGRESS


def test_failed_jobs_retry_once_then_escalate() -> None:
    state = reserved()
    retry = run(state, operation=done(state, None, OK.FAILED))
    assert retry.next_phase == P.FIX_IN_PROGRESS and retry.next_fix_retries == 1
    assert retry.next_state.operation_id != state.operation_id
    assert retry.requires_fix_dispatch
    assert retry.next_state.phase_started_at == state.phase_started_at
    again = retry.next_state
    final = run(again, operation=done(again, None, OK.FAILED))
    assert final.next_phase == P.ESCALATED and final.reason == "fix_retry_limit"


def test_fix_output_waits_for_promotion_then_awaits_ci() -> None:
    state = reserved(
        reviewed_sha=HEAD,
        judgment_digest="x",
        ci_verified_sha=HEAD,
        review_source_digest="y",
    )
    result = SemanticFixResultV1(
        IDENTITY.attempt_id, str(state.operation_id), HEAD, FIXED, 7, "completed"
    )
    job = SemanticOperationObservation.from_fix_result(result)
    assert_hold(state, "awaiting_fix_promotion", promotion=True, operation=job)
    decision = run(state, operation=job, observed_head_sha=FIXED)
    assert decision.next_phase == P.CI_VERIFICATION
    assert not decision.requires_fix_promotion
    nxt = decision.next_state
    assert (nxt.head_sha, nxt.fix_output_sha, nxt.fix_completed) == (FIXED, FIXED, True)
    assert nxt.reviewed_sha is nxt.judgment_digest is nxt.ci_verified_sha is None
    assert nxt.review_source_digest is None and nxt.round == 1
    assert nxt.operation_status == OK.COMPLETED


def test_head_change_outside_the_job_invalidates() -> None:
    state = reserved()
    decision = run(state, operation=done(state, FIXED), observed_head_sha=OTHER)
    assert decision.next_phase == P.INVALIDATED and decision.reason == "head_changed"


@pytest.mark.parametrize(
    "judgments,reason",
    [
        (None, "judgments_missing"),
        (
            replace(PASS, valid=False, reason="judgments_invalid: x"),
            "judgments_invalid: x",
        ),
        (replace(PASS, round=2), "judgments_round_mismatch"),
        (DEFER, "deferred_judgments"),
        (EMPTY, "empty_judgment_table"),
    ],
)
def test_unchanged_job_with_unsettled_contract_escalates(
    judgments: SemanticJudgmentDecision | None, reason: str
) -> None:
    state = reserved()
    decision = run(state, operation=done(state), judgments=judgments, **EVIDENCE)
    assert decision.next_phase == P.ESCALATED and decision.reason == reason
    assert decision.next_state.escalation_reason == reason


def test_unchanged_job_with_unresolved_adopt_retries_then_escalates() -> None:
    state = reserved()
    assert_hold(state, "review_evidence_missing", operation=done(state))
    retry = run(state, operation=done(state), judgments=FIX_NEEDED, **EVIDENCE)
    assert retry.reason == "unresolved_after_fix" and retry.next_fix_retries == 1
    assert retry.requires_fix_dispatch and retry.next_phase == P.FIX_IN_PROGRESS
    nxt = retry.next_state
    final = run(nxt, operation=done(nxt), judgments=FIX_NEEDED, **EVIDENCE)
    assert final.next_phase == P.ESCALATED and final.reason == "fix_retry_limit"


def test_unchanged_settled_job_needs_ci_of_this_head_to_pass() -> None:
    state = reserved()
    unverified = run(state, operation=done(state), judgments=PASS, **EVIDENCE)
    assert unverified.next_phase == P.CI_VERIFICATION
    nxt = unverified.next_state
    assert (nxt.reviewed_sha, nxt.judgment_digest) == (HEAD, PASS.digest)
    assert nxt.operation_status == OK.COMPLETED
    verified = replace(state, ci_verified_sha=HEAD)
    passed = run(verified, operation=done(verified), judgments=PASS, **EVIDENCE)
    assert passed.next_phase == P.PASSED and passed.reason == "passed"
    stale = replace(state, ci_verified_sha=OTHER)
    assert run(stale, operation=done(stale), judgments=PASS, **EVIDENCE).next_phase == (
        P.CI_VERIFICATION
    )


CI_OK = SemanticCiObservation(HEAD, SemanticCiConclusion.SUCCESS)
CI_FAIL = SemanticCiObservation(HEAD, SemanticCiConclusion.FAILURE)


def test_ci_verification_accepts_only_current_head_results() -> None:
    state = active(P.CI_VERIFICATION, reviewed_sha=HEAD, judgment_digest=PASS.digest)
    assert_hold(state, "ci_pending")
    assert_hold(
        state,
        "ci_pending",
        ci=SemanticCiObservation(OTHER, SemanticCiConclusion.SUCCESS),
    )
    assert_hold(
        state,
        "ci_pending",
        ci=SemanticCiObservation(HEAD, SemanticCiConclusion.PENDING),
    )


def test_ci_success_with_current_review_evidence_passes() -> None:
    state = active(P.CI_VERIFICATION, reviewed_sha=HEAD, judgment_digest=PASS.digest)
    evidence = {**EVIDENCE, "judgments": PASS}
    decision = run(state, ci=CI_OK, **evidence)
    assert (
        decision.next_phase == P.PASSED and decision.next_state.ci_verified_sha == HEAD
    )
    assert not decision.requires_review_trigger
    assert_hold(decision.next_state, "terminal_passed", ci=CI_OK, **evidence)


@pytest.mark.parametrize(
    "evidence",
    [
        {"review_complete": False, "reviewed_sha": HEAD, "judgments": PASS},
        {"review_complete": True, "reviewed_sha": OTHER, "judgments": PASS},
        {**EVIDENCE, "judgments": FIX_NEEDED},
        {**EVIDENCE, "judgments": replace(PASS, digest="z")},
        {**EVIDENCE, "judgments": replace(PASS, round=2)},
        {**EVIDENCE, "judgments": None},
    ],
)
def test_ci_success_without_valid_pass_evidence_escalates(
    evidence: dict[str, Any],
) -> None:
    state = active(P.CI_VERIFICATION, reviewed_sha=HEAD, judgment_digest=PASS.digest)
    decision = run(state, ci=CI_OK, **evidence)
    assert (
        decision.next_phase == P.ESCALATED
        and decision.reason == "pass_evidence_invalid"
    )


def test_ci_success_after_head_change_starts_a_new_review_round() -> None:
    state = active(P.CI_VERIFICATION, fix_completed=True)
    decision = run(state, ci=CI_OK, judgments=PASS, **EVIDENCE)
    assert decision.next_phase == P.REVIEW_PENDING and decision.next_round == 2
    assert decision.requires_review_trigger
    assert decision.next_state.round_started_at == "2026-10-18T12:00:00Z"
    assert decision.next_state.ci_verified_sha == HEAD
    last = active(P.CI_VERIFICATION, round=2)
    capped = run(last, ci=CI_OK)
    assert capped.next_phase == P.ESCALATED and capped.reason == "max_rounds"
    assert not capped.requires_review_trigger


def test_ci_failure_retries_fix_then_escalates() -> None:
    state = active(P.CI_VERIFICATION, reviewed_sha=HEAD)
    retry = run(state, ci=CI_FAIL)
    assert retry.next_phase == P.FIX_IN_PROGRESS and retry.next_fix_retries == 1
    assert retry.requires_fix_dispatch and retry.next_state.fix_input_sha == HEAD
    final = run(replace(state, fix_retries=1), ci=CI_FAIL)
    assert final.next_phase == P.ESCALATED and final.reason == "ci_retry_limit"


@pytest.mark.parametrize(
    "phase,started,reason",
    [
        (P.REVIEW_PENDING, "2026-10-18T11:50:00Z", "review_pending_timeout"),
        (P.FIX_IN_PROGRESS, "2026-10-18T11:50:00Z", "fix_in_progress_timeout"),
        (P.CI_VERIFICATION, "2026-10-18T11:50:00Z", "ci_verification_timeout"),
        (P.REVIEW_PENDING, None, "phase_started_at_invalid"),
        (P.REVIEW_PENDING, "garbage", "phase_started_at_invalid"),
        (P.REVIEW_PENDING, "2026-10-18T11:59:00", "phase_started_at_invalid"),
    ],
)
def test_phase_timeouts_escalate(
    phase: SemanticReviewPhase, started: str | None, reason: str
) -> None:
    decision = run(active(phase, phase_started_at=started))
    assert decision.next_phase == P.ESCALATED and decision.reason == reason
    assert decision.requires_escalation and decision.next_state.escalated_from == phase


@pytest.mark.parametrize(
    "phase,attempt_started,reason",
    [
        (P.CI_VERIFICATION, "2026-10-18T10:59:00Z", "attempt_timeout"),
        (P.NOT_STARTED, "2026-10-18T10:59:00Z", "attempt_timeout"),
        (P.NOT_STARTED, "garbage", "attempt_started_at_invalid"),
        (P.REVIEW_PENDING, None, "attempt_started_at_invalid"),
    ],
)
def test_attempt_timeout_escalates(
    phase: SemanticReviewPhase, attempt_started: str | None, reason: str
) -> None:
    decision = run(active(phase, attempt_started_at=attempt_started))
    assert decision.next_phase == P.ESCALATED and decision.reason == reason


def test_waiting_never_renews_start_times() -> None:
    state = active(P.REVIEW_PENDING, phase_started_at="2026-10-18T11:51:00Z")
    held = run(state).next_state
    later = evaluate_semantic_review_transition(
        held, obs(now=NOW + timedelta(minutes=1))
    )
    assert later.reason == "review_pending_timeout"


def test_naive_now_is_rejected() -> None:
    with pytest.raises(ValueError):
        run(make_state(), now=NOW.replace(tzinfo=None))


@pytest.mark.parametrize(
    "changes",
    [
        {"identity": IntegrationAttemptIdentity.create(1033, OTHER, CHILDREN)},
        {"identity": replace(IDENTITY, child_sources=IDENTITY.child_sources[:1])},
    ],
)
def test_base_or_child_set_change_invalidates(changes: dict[str, Any]) -> None:
    decision = run(active(P.CI_VERIFICATION), **changes)
    assert decision.next_phase == P.INVALIDATED
    assert decision.reason == "attempt_inputs_changed"


def test_passed_terminal_is_invalidated_by_a_new_head() -> None:
    decision = run(make_state(phase=P.PASSED), observed_head_sha=OTHER)
    assert decision.next_phase == P.INVALIDATED


def test_invalidated_is_never_revived() -> None:
    state = make_state(phase=P.INVALIDATED)
    override = SemanticReviewHumanOverrideV1(IDENTITY.attempt_id, HEAD, "skip")
    assert_hold(state, "invalidated", override=override, retry_request=retry_req())


@pytest.mark.parametrize("phase", sorted(ACTIVE_PENDING_PHASES | {P.ESCALATED}))
def test_valid_override_skips_active_or_escalated(phase: SemanticReviewPhase) -> None:
    override = SemanticReviewHumanOverrideV1(IDENTITY.attempt_id, HEAD, "skip")
    decision = run(active(phase), override=override)
    assert decision.next_phase == P.SKIPPED_BY_HUMAN
    assert decision.reason == "human_override"
    assert not decision.requires_review_trigger


@pytest.mark.parametrize(
    "override",
    [
        SemanticReviewHumanOverrideV1("other", HEAD, "skip"),
        SemanticReviewHumanOverrideV1(IDENTITY.attempt_id, OTHER, "skip"),
    ],
)
def test_override_for_another_attempt_or_head_is_ignored(
    override: SemanticReviewHumanOverrideV1,
) -> None:
    assert_hold(active(P.ESCALATED), "escalated", override=override)


@pytest.mark.parametrize("phase", sorted(TERMINAL_PASSED_PHASES))
def test_passed_terminals_trigger_nothing(phase: SemanticReviewPhase) -> None:
    override = SemanticReviewHumanOverrideV1(IDENTITY.attempt_id, HEAD, "skip")
    assert_hold(make_state(phase=phase), "terminal_passed", override=override)


# --- explicit retry / restart -----------------------------------------------


def escalated(phase: SemanticReviewPhase, **c: Any) -> SemanticReviewStateV1:
    """Escalate a state of `phase` through the state machine itself."""
    late = NOW - timedelta(minutes=1)
    state = active(phase, phase_started_at="2026-10-18T11:40:00Z", **c)
    decision = evaluate_semantic_review_transition(state, obs(now=late))
    assert decision.next_phase == P.ESCALATED
    assert decision.next_state.escalated_from == phase
    return decision.next_state


@pytest.mark.parametrize(
    "request_",
    [
        None,
        replace(retry_req(), attempt_id="other"),
        replace(retry_req(), head_sha=OTHER),
        retry_req(request_id="req-0"),
    ],
)
def test_escalated_ignores_absent_foreign_or_consumed_requests(
    request_: SemanticReviewRetryV1 | None,
) -> None:
    state = escalated(P.REVIEW_PENDING, last_retry_request_id="req-0")
    assert_hold(state, "escalated", retry_request=request_)
    assert_hold(state, "escalated", mode=SemanticReviewMode.OFF)


@pytest.mark.parametrize(
    "phase,trigger,dispatch",
    [
        (P.REVIEW_PENDING, True, False),
        (P.CI_VERIFICATION, False, False),
        (P.FIX_IN_PROGRESS, False, True),
    ],
)
def test_retry_resumes_the_escalated_step_keeping_limits(
    phase: SemanticReviewPhase, trigger: bool, dispatch: bool
) -> None:
    state = escalated(phase, round=2, fix_retries=1, reviewed_sha=HEAD)
    decision = run(state, retry_request=retry_req())
    assert decision.next_phase == phase and decision.reason == "retry_requested"
    assert decision.requires_review_trigger is trigger
    assert decision.requires_fix_dispatch is dispatch
    nxt = decision.next_state
    assert (nxt.round, nxt.fix_retries, nxt.attempt_started_at) == (2, 1, EARLIER)
    assert (nxt.restart_count, nxt.last_retry_request_id) == (1, "req-1")
    assert nxt.escalation_reason is nxt.escalated_from is None
    assert nxt.phase_started_at == "2026-10-18T12:00:00Z"
    assert nxt.reviewed_sha == HEAD and nxt.limits_reset_reason is None
    if dispatch:
        assert nxt.operation_id == semantic_operation_id(
            IDENTITY.attempt_id, 2, KIND.FIX, 1, 1
        )
    # The consumed request is never applied twice.
    assert run(replace(nxt, phase=P.ESCALATED), retry_request=retry_req()).reason == (
        "escalated"
    )


def test_retry_of_an_exhausted_attempt_escalates_again() -> None:
    state = escalated(P.NOT_STARTED, attempt_started_at="2026-10-18T10:00:00Z")
    resumed = run(state, retry_request=retry_req())
    assert resumed.next_phase == P.NOT_STARTED
    assert resumed.next_state.attempt_started_at == "2026-10-18T10:00:00Z"
    assert run(resumed.next_state).reason == "attempt_timeout"


def test_retry_without_a_recorded_step_is_held() -> None:
    state = active(P.ESCALATED, escalation_reason="legacy")
    assert_hold(state, "retry_phase_unknown", retry_request=retry_req())


def test_restart_reviews_the_head_in_the_next_round() -> None:
    state = escalated(P.CI_VERIFICATION, fix_retries=1, reviewed_sha=HEAD)
    decision = run(state, retry_request=retry_req("restart"))
    assert decision.next_phase == P.REVIEW_PENDING and decision.next_round == 2
    assert decision.reason == "restart_requested" and decision.requires_review_trigger
    nxt = decision.next_state
    assert (nxt.fix_retries, nxt.attempt_started_at, nxt.restart_count) == (
        1,
        EARLIER,
        1,
    )
    assert nxt.reviewed_sha is nxt.operation_id is nxt.escalated_from is None
    capped = replace(state, round=2)
    assert_hold(capped, "restart_max_rounds", retry_request=retry_req("restart"))


@pytest.mark.parametrize("kind", ["retry", "restart"])
def test_reset_limits_starts_a_fresh_round_one(kind: SemanticRetryKind) -> None:
    state = escalated(P.CI_VERIFICATION, round=2, fix_retries=1, reviewed_sha=HEAD)
    request = retry_req(kind, reset_limits=True)
    decision = run(state, retry_request=request)
    assert decision.next_phase == P.REVIEW_PENDING and decision.reason == "limits_reset"
    nxt = decision.next_state
    assert (nxt.round, nxt.fix_retries) == (1, 0)
    assert nxt.attempt_started_at == "2026-10-18T12:00:00Z"
    assert nxt.limits_reset_reason == "config fixed" and nxt.reviewed_sha is None


def test_restart_reserves_operations_distinct_from_pre_restart_jobs() -> None:
    old = reserved()
    stale = done(old, FIXED)
    restarted = run(
        replace(old, phase=P.ESCALATED, escalated_from=P.FIX_IN_PROGRESS),
        retry_request=retry_req(),
    ).next_state
    assert restarted.restart_count == 1 and restarted.phase == P.FIX_IN_PROGRESS
    assert restarted.operation_id != old.operation_id
    assert_hold(restarted, "operation_in_flight", operation=stale)
    decision = run(restarted, operation=stale, observed_head_sha=FIXED)
    assert decision.reason == "head_changed"


# --- misc -------------------------------------------------------------------


def test_mode_off_records_explicit_reason() -> None:
    decision = run(active(P.REVIEW_PENDING), mode=SemanticReviewMode.OFF)
    assert decision.next_phase == P.SKIPPED_BY_HUMAN and decision.reason == "mode_off"


def test_advisory_mode_runs_the_same_state_machine() -> None:
    decision = run(make_state(), mode=SemanticReviewMode.ADVISORY)
    assert decision.next_phase == P.REVIEW_PENDING


def test_limits_must_be_positive() -> None:
    with pytest.raises(ValueError):
        SemanticReviewLimits(max_rounds=0)


def test_full_loop_review_fix_ci_rereview_pass() -> None:
    step = run(make_state()).next_state
    step = run(step, **EVIDENCE).next_state
    assert step.phase == P.FIX_IN_PROGRESS
    step = run(step, operation=done(step, FIXED), observed_head_sha=FIXED).next_state
    fixed_ci = SemanticCiObservation(FIXED, SemanticCiConclusion.SUCCESS)
    step = run(step, observed_head_sha=FIXED, ci=fixed_ci).next_state
    assert (step.phase, step.round, step.ci_verified_sha) == (
        P.REVIEW_PENDING,
        2,
        FIXED,
    )
    fixed: dict[str, Any] = {
        "observed_head_sha": FIXED,
        "review_complete": True,
        "reviewed_sha": FIXED,
    }
    step = run(step, **fixed).next_state
    assert step.phase == P.FIX_IN_PROGRESS and step.fix_input_sha == FIXED
    final = run(
        step, operation=done(step, FIXED), judgments=replace(PASS, round=2), **fixed
    )
    assert final.next_phase == P.PASSED and final.next_state.ci_verified_sha == FIXED


def test_optional_state_fields_default_and_override_needs_a_mapping() -> None:
    data = make_state().to_dict()
    minimal = {k: data[k] for k in ("attempt_id", "parent_issue_number", "pr_number")}
    minimal |= {k: data[k] for k in ("phase", "round", "head_sha", "base_sha")}
    minimal["child_sources"] = data["child_sources"]
    assert SemanticReviewStateV1.from_dict(minimal) == make_state()
    with pytest.raises(ValueError):
        SemanticReviewHumanOverrideV1.from_dict([])  # type: ignore[arg-type]


def test_child_source_ref_instances_are_validated_and_normalized() -> None:
    with pytest.raises(ValueError):
        IntegrationAttemptIdentity.create(1033, BASE, [ChildSourceRef(0, "", "")])
    padded: list[Any] = [ChildSourceRef(11, " alpha ", f" {'1' * 40} "), CHILDREN[0]]
    assert IntegrationAttemptIdentity.create(1033, BASE, padded) == IDENTITY

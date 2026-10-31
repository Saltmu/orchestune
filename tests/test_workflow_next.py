"""The next-step advisor derives actions from existing read-only evidence."""

import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from orchestune.workflow.contracts import WorkflowCheck, WorkflowResult
from orchestune.workflow.next import next_step
from orchestune.workflow.stages import load_stage_definitions
from tests.test_workflow_readers import prepared_context, ready_context


def test_next_chooses_scope_before_implementation(tmp_path):
    context = prepared_context(tmp_path)
    context.state.pop("impact_initial")

    result = next_step(context)

    assert result.stage_id == "scope"
    assert result.status == "ready", result.to_dict()
    stages = {stage.id: stage for stage in load_stage_definitions()}
    assert {check.id for check in result.checks} == set(
        stages[result.stage_id].exit_conditions
    )
    assert result.actions[0]["id"] == "scope"
    assert result.gate_id is None


def test_next_chooses_claim_when_no_current_claim(tmp_path):
    context = prepared_context(tmp_path)
    context.active = None
    context.state["claim_id"] = None
    context.ledger.active_worktrees.clear()
    context.issue = replace(
        context.issue,
        body=context.issue.body.replace("claim_id: claim-current\n", ""),
    )

    result = next_step(context)

    assert result.stage_id == "claim"
    assert result.status == "ready", result.reason_codes
    assert result.reason_codes == ("claim_missing",)
    assert result.actions[0]["id"] == "claim"


def test_next_reports_interactive_reviewer_wait(tmp_path):
    context = prepared_context(tmp_path)
    context.state.update(mode="interactive", reviewer=None)
    context.state["plan_approval"] = {
        "required": True,
        "status": "pending",
        "evidence": None,
    }

    result = next_step(context)

    assert result.stage_id == "prepare"
    assert result.status == "waiting_human", result.to_dict()
    assert result.exit_code == 3
    assert result.actions
    assert any("reviewer" in str(action).lower() for action in result.actions)


def test_next_preserves_stale_ci_as_unmet_and_suggests_recovery(tmp_path):
    context = ready_context(tmp_path)
    path = context.root / ".orchestune/ci/ci_evidence.json"
    evidence = json.loads(path.read_text())
    evidence["head_sha"] = "stale"
    path.write_text(json.dumps(evidence))

    result = next_step(context)

    assert result.stage_id == "implement"
    assert result.status == "unmet"
    assert result.exit_code == 2
    assert "ci_stale" in result.reason_codes
    assert result.actions[0]["id"] == "rerun_ci"


def test_next_selects_pr_when_pre_pr_evidence_is_ready(tmp_path):
    context = ready_context(tmp_path)
    context.pr = None
    context.pr_number = None

    result = next_step(context)

    assert result.stage_id == "pr"
    assert result.status == "ready"
    assert result.reason_codes == ("pr_missing",)
    assert result.actions[0]["id"] == "create_pr"


def test_invalid_evidence_takes_priority_over_human_wait(tmp_path):
    context = ready_context(tmp_path)
    context.state.update(mode="interactive", reviewer=None)
    context.state["plan_approval"] = {
        "required": True,
        "status": "pending",
        "evidence": None,
    }
    path = context.root / ".orchestune/ci/ci_evidence.json"
    evidence = json.loads(path.read_text())
    evidence["head_sha"] = "stale"
    path.write_text(json.dumps(evidence))

    result = next_step(context)

    assert result.stage_id == "prepare"
    assert result.status == "unmet"
    assert "ci_stale" in result.reason_codes
    assert any(
        action.get("decision") == "reviewer_selected" for action in result.actions
    )
    assert any(action.get("id") == "rerun_ci" for action in result.actions)


def test_next_uses_review_stage_for_review_in_progress(tmp_path):
    context = ready_context(tmp_path)
    review_check = WorkflowCheck(
        id="review_acquired",
        status="unmet",
        evidence=(),
        missing=("Current review round is still in progress",),
    )
    # The existing gate owns review acquisition; this injected aggregate models its
    # documented in-progress result without starting another wait process.
    aggregate = WorkflowResult(
        issue_number=context.issue_number,
        claim_id=context.state["claim_id"],
        stage_id="review",
        gate_id="pre-complete",
        status="unmet",
        reason_codes=("review_unavailable",),
        checks=(review_check,),
        actions=(),
        references=(),
        artifacts={},
        observed_head_sha=context.head_sha,
    )
    with patch("orchestune.workflow.next.check_gate", return_value=aggregate):
        result = next_step(context)

    assert result.stage_id == "review"
    assert result.status == "ready"
    assert result.actions[0]["id"] == "review"
    assert result.checks[0].missing


def test_next_review_error_guides_resuming_existing_wait(tmp_path):
    context = ready_context(tmp_path)
    aggregate = WorkflowResult(
        issue_number=context.issue_number,
        claim_id=context.state["claim_id"],
        stage_id="review",
        gate_id="pre-complete",
        status="unmet",
        reason_codes=("review_evidence_invalid",),
        checks=(
            WorkflowCheck(
                id="review_acquired",
                status="unmet",
                evidence=(),
                missing=("Review acquisition is incomplete or not final",),
            ),
        ),
        actions=(),
        references=(),
        artifacts={},
        observed_head_sha=context.head_sha,
    )
    with patch("orchestune.workflow.next.check_gate", return_value=aggregate):
        result = next_step(context)

    assert result.stage_id == "review"
    assert result.status == "unmet"
    assert result.actions[0]["id"] == "inspect_review"
    assert "Do not start a concurrent wait" in result.actions[0]["description"]


def test_next_returns_complete_when_all_current_evidence_is_ready(tmp_path):
    context = ready_context(tmp_path)

    result = next_step(context)

    assert result.stage_id == "complete"
    assert result.status == "ready"
    assert result.actions[0]["id"] == "complete"


@pytest.mark.parametrize(
    ("stage_id", "status", "reason", "actions"),
    [
        ("handed-off", "ready", "handed_off", ()),
        (
            "blocked",
            "terminal_blocked",
            "blocked",
            ({"description": "wait for unblock"},),
        ),
    ],
)
def test_next_preserves_verified_terminal_states(
    tmp_path, stage_id, status, reason, actions
):
    context = prepared_context(tmp_path)
    terminal = WorkflowResult(
        issue_number=context.issue_number,
        claim_id=context.state["claim_id"],
        stage_id=stage_id,
        gate_id="pre-complete",
        status=status,
        reason_codes=(reason,),
        checks=(),
        actions=actions,
        references=(),
        artifacts={},
        observed_head_sha=context.head_sha,
    )
    with patch("orchestune.workflow.next.check_gate", return_value=terminal):
        result = next_step(context)

    assert result.stage_id == stage_id
    assert result.status == status
    assert result.reason_codes == (reason,)
    assert result.exit_code == {"ready": 0, "terminal_blocked": 4}[status]
    if stage_id == "handed-off":
        assert result.actions == ()
    else:
        assert result.actions == actions


def test_next_only_returns_conditions_for_current_stage(tmp_path):
    context = ready_context(tmp_path)
    result = next_step(context)
    stages = {stage.id: stage for stage in load_stage_definitions()}

    assert {check.id for check in result.checks} == set(
        stages[result.stage_id].exit_conditions
    )
    assert all(action["stage_id"] == result.stage_id for action in result.actions)


def test_unclassified_gate_reason_fails_closed(tmp_path):
    context = ready_context(tmp_path)
    aggregate = next_step(context)
    aggregate = replace(
        aggregate,
        gate_id="pre-complete",
        reason_codes=(*aggregate.reason_codes, "future_unclassified_reason"),
    )
    with patch("orchestune.workflow.next.check_gate", return_value=aggregate):
        result = next_step(context)

    assert result.status == "unmet"
    assert "evidence_unavailable" in result.reason_codes


def test_unavailable_stage_definition_returns_unmet(tmp_path):
    context = ready_context(tmp_path)
    aggregate = WorkflowResult(
        issue_number=context.issue_number,
        claim_id=context.state["claim_id"],
        stage_id="future-stage",
        gate_id="pre-complete",
        status="ready",
        reason_codes=(),
        checks=(),
        actions=(),
        references=(),
        artifacts={},
        observed_head_sha=context.head_sha,
    )
    with patch("orchestune.workflow.next.check_gate", return_value=aggregate):
        result = next_step(context, stages=())

    assert result.status == "unmet"
    assert "stage_unavailable" in result.reason_codes
    assert result.actions == ()


def test_next_does_not_mutate_context_or_session_files(tmp_path):
    context = prepared_context(tmp_path)
    before = {
        path.relative_to(context.session_dir): path.read_bytes()
        for path in context.session_dir.rglob("*")
        if path.is_file()
    }
    state = json.loads(json.dumps(context.state))

    next_step(context)

    after = {
        path.relative_to(context.session_dir): path.read_bytes()
        for path in context.session_dir.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert context.state == state

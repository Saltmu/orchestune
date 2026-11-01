"""Advisory checks must fail closed without invoking lifecycle services."""

import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from orchestune.exit_codes import TaskExitCode
from orchestune.workflow.check import check_gate
from orchestune.workflow.contracts import WorkflowExitCode
from orchestune.workflow.stages import load_stage_definitions
from tests.test_workflow_readers import prepared_context, ready_context


def test_workflow_exit_codes_do_not_reuse_task_failures():
    assert [int(x) for x in WorkflowExitCode] == [0, 2, 3, 4]
    assert not ({2, 3, 4} & {int(x) for x in TaskExitCode})


def test_stage_gate_and_validator_coverage(tmp_path):
    context = prepared_context(tmp_path)
    for stage in load_stage_definitions():
        if stage.gate:
            result = check_gate(context, stage.gate)
            assert set(stage.exit_conditions) <= {c.id for c in result.checks}
    with pytest.raises(ValueError, match="gate"):
        check_gate(context, "unknown")


def test_auto_preclaim_ready_and_skip_is_a_selection(tmp_path):
    context = prepared_context(tmp_path)
    context.state["reviewer"] = "skip"
    result = check_gate(context, "pre-claim")
    assert result.status == "ready"
    assert result.exit_code == 0


@pytest.mark.parametrize("gate", ["pre-claim", "pre-edit", "pre-pr", "pre-complete"])
def test_pending_human_is_not_terminal(tmp_path, gate):
    context = prepared_context(tmp_path)
    context.state.update(mode="interactive", reviewer=None)
    context.state["plan_approval"] = {
        "required": True,
        "status": "pending",
        "evidence": None,
    }
    result = check_gate(context, gate)
    assert result.status in {"waiting_human", "unmet"}
    assert result.status != "terminal_blocked"
    if gate == "pre-claim":
        assert result.exit_code == 3


def test_invalid_reviewer_is_unmet(tmp_path):
    context = prepared_context(tmp_path)
    context.state["reviewer"] = "someone"
    assert check_gate(context, "pre-claim").status == "unmet"


def test_preedit_claim_generation_and_scope_digest(tmp_path):
    context = prepared_context(tmp_path)
    assert check_gate(context, "pre-edit").status == "ready"
    context.state["claim_id"] = "old-generation"
    assert "claim_conflict" in check_gate(context, "pre-edit").reason_codes


@pytest.mark.parametrize("fault", ["digest", "rationale", "escape", "worktree"])
def test_preedit_rejects_invalid_scope_or_worktree(tmp_path, fault):
    context = prepared_context(tmp_path)
    impact = context.state["impact_initial"]
    if fault == "digest":
        impact["digest"] = "0" * 64
    elif fault == "rationale":
        (context.session_dir / "impact.md").write_text(
            "| Reference | Decision | Rationale |\n|---|---|---|\n| x | out of scope | |\n"
        )
    elif fault == "escape":
        impact["relative_path"] = "../impact.md"
    else:
        context.active = context.active.with_core(
            replace(context.active.core, worktree_path=str(tmp_path / "other"))
        )
    assert check_gate(context, "pre-edit").status == "unmet"


def test_unmet_precedes_waiting_human(tmp_path):
    context = ready_context(tmp_path)
    (context.root / ".orchestune/ci/ci_evidence.json").unlink()
    context.state.update(mode="interactive", reviewer=None)
    result = check_gate(context, "pre-pr")
    assert result.status == "unmet"
    assert result.stage_id == "prepare"
    assert "ci_missing" in result.reason_codes


@pytest.mark.parametrize("gate", ["pre-claim", "pre-edit", "pre-pr", "pre-complete"])
@pytest.mark.parametrize("change_kind", ["code", "docs"])
def test_all_gates_ready_with_current_evidence(tmp_path, gate, change_kind):
    c = ready_context(tmp_path)
    c.state["change_kind"] = change_kind
    result = check_gate(c, gate)
    assert result.status == "ready", result.to_dict()
    assert result.stage_id == "complete"


@pytest.mark.parametrize(
    "field",
    [
        "head_sha",
        "tree_sha",
        "ci_definition_digest",
        "lockfile_digest",
        "python_version",
    ],
)
def test_stale_ci_is_unmet(tmp_path, field):
    c = ready_context(tmp_path)
    path = c.root / ".orchestune/ci/ci_evidence.json"
    evidence = json.loads(path.read_text())
    evidence[field] = "stale"
    path.write_text(json.dumps(evidence))
    assert "ci_stale" in check_gate(c, "pre-pr").reason_codes


def test_another_worktrees_ci_evidence_is_rejected(tmp_path, monkeypatch):
    c = ready_context(tmp_path)
    monkeypatch.setenv(
        "ORCHESTUNE_CI_EVIDENCE_PATH", str(tmp_path.parent / "other-ci.json")
    )
    assert "ci_stale" in check_gate(c, "pre-pr").reason_codes


@pytest.mark.parametrize("fence", ["```markdown", "~~~markdown", "````markdown"])
def test_pr_sections_inside_code_fences_are_not_evidence(tmp_path, fence):
    c = ready_context(tmp_path)
    closing = fence.removesuffix("markdown")
    c.pr_body = f"{fence}\n{c.pr_body}\n{closing}\n"
    result = check_gate(c, "pre-pr")
    assert result.status == "unmet"
    assert "pr_body_invalid" in result.reason_codes


def test_pr_sections_after_fence_close_are_evidence(tmp_path):
    c = ready_context(tmp_path)
    c.pr_body = f"````text\n```\n# Example\n````\n{c.pr_body}"
    assert check_gate(c, "pre-pr").status == "ready"


@pytest.mark.parametrize("fault", ["missing", "garbled"])
def test_invalid_reply_is_rejected_by_existing_review_validator(tmp_path, fault):
    c = ready_context(tmp_path)
    if fault == "missing":
        c.review_reply.unlink()
    else:
        c.review_reply.write_text("invalid")
    result = check_gate(c, "pre-complete")
    assert result.status == "unmet"
    review = next(check for check in result.checks if check.id == "review_current")
    assert review.status == "unmet"
    assert "review_evidence_invalid" in result.reason_codes
    assert "evidence_unavailable" not in result.reason_codes


@pytest.mark.parametrize("fault", ["missing", "garbled", "encoding"])
def test_reply_failure_after_validation_preserves_review(tmp_path, monkeypatch, fault):
    import orchestune.workflow.check as checks

    c = ready_context(tmp_path)
    verify = checks.verify_review_evidence

    def verify_then_change_reply(*args):
        summary = verify(*args)
        if fault == "missing":
            c.review_reply.unlink()
        elif fault == "garbled":
            c.review_reply.write_text("invalid")
        else:
            c.review_reply.write_bytes(b"\xff")
        return summary

    monkeypatch.setattr(checks, "verify_review_evidence", verify_then_change_reply)
    result = check_gate(c, "pre-complete")
    observations = {check.id: check for check in result.checks}
    assert observations["review_acquired"].status == "ready"
    assert observations["reply_posted"].status == "unmet"
    assert result.status == "unmet"
    assert "reply_invalid" in result.reason_codes
    assert "evidence_unavailable" not in result.reason_codes


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("sha", "review_head_mismatch"),
        ("reply_missing", "reply_missing"),
        ("reply_duplicate", "reply_duplicate"),
        ("partial", "evidence_missing"),
        ("judgments", "review_evidence_invalid"),
        ("pr_head", "pr_head_mismatch"),
    ],
)
def test_review_failure_cases(tmp_path, fault, reason):
    c = ready_context(tmp_path)
    if fault == "sha":
        c.forge.comments[0]["body"] = c.forge.comments[0]["body"].replace(
            c.head_sha, "b" * 40
        )
    elif fault == "reply_missing":
        c.forge.comments.pop()
    elif fault == "reply_duplicate":
        c.forge.comments.append({**c.forge.comments[-1], "id": 4})
    elif fault == "partial":
        c.forge.list_pull_request_review_comments = lambda _: (_ for _ in ()).throw(
            OSError("partial pagination")
        )
    elif fault == "judgments":
        c.review_reply.write_text("invalid")
    else:
        c.pr = replace(c.pr, head_sha="b" * 40)
    result = check_gate(c, "pre-complete")
    assert result.status == "unmet"
    assert reason in result.reason_codes, result.to_dict()


def test_skip_preserves_human_integration_gate(tmp_path):
    c = ready_context(tmp_path)
    c.state["reviewer"] = "skip"
    c.forge.comments[:] = [
        {
            "id": 4,
            "body": f"<!-- orchestune:review-selection reviewer=skip head={c.head_sha} -->",
        }
    ]
    result = check_gate(c, "pre-complete")
    assert result.status == "ready", result.to_dict()
    summary = next(x for x in result.checks if x.id == "review_current")
    assert summary.evidence[0]["value"]["verdict"] == "skipped"


def test_checks_never_write_or_call_lifecycle_services(tmp_path):
    from pathlib import Path

    c = ready_context(tmp_path)
    before = {
        p.relative_to(tmp_path): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    with (
        patch.object(Path, "write_text", side_effect=AssertionError("write")),
        patch.object(Path, "write_bytes", side_effect=AssertionError("write")),
        patch.object(Path, "mkdir", side_effect=AssertionError("mkdir/lock")),
        patch(
            "orchestune.claim.service.claim_task", side_effect=AssertionError("claim")
        ),
        patch(
            "orchestune.complete.ci_evidence.run_local_ci_if_needed",
            side_effect=AssertionError("CI"),
        ),
    ):
        for gate in ("pre-claim", "pre-edit", "pre-pr", "pre-complete"):
            assert check_gate(c, gate).status == "ready"
    after = {
        p.relative_to(tmp_path): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    assert after == before


def terminal_record(c, result="blocked", generation=None):
    from orchestune.complete.contracts import CompleteStage
    from orchestune.complete.journal_models import CompletionJournalRecord

    return CompletionJournalRecord(
        repository_id=c.repository_identity,
        issue_number=1496,
        generation_id=generation or c.state["claim_id"],
        completion_id=f"completion-{result}",
        owner_token_digest="a" * 64,
        request_fingerprint="b" * 64,
        result=result,
        target_label=f"status:{result}",
        outcome_payload={"issue": 1496, "result": result, "reason": "test-block"}
        if result == "blocked"
        else {"issue": 1496, "result": result, "pr": 42},
        stage=CompleteStage.HANDED_OFF,
        posting_evidence={
            "comment_id": "99",
            "comment_url": "https://github.com/owner/repo/issues/1496#issuecomment-99",
        },
        label_evidence={"status": "confirmed", "observed_labels": [f"status:{result}"]},
    ).to_dict()


@pytest.mark.parametrize("gate", ["pre-claim", "pre-edit", "pre-pr", "pre-complete"])
def test_only_verified_current_terminal_block_returns_four(tmp_path, gate):
    c = prepared_context(tmp_path)
    c.ledger.completion_journal["old"] = terminal_record(c, generation="old")
    assert check_gate(c, gate).exit_code != 4
    c.ledger.completion_journal["current"] = terminal_record(c)
    result = check_gate(c, gate)
    assert result.exit_code == 4
    assert result.stage_id == "blocked"
    assert result.claim_id == "claim-current"


def test_conflicting_terminals_are_unmet_and_handoff_has_no_actions(tmp_path):
    c = prepared_context(tmp_path)
    c.ledger.completion_replay_receipts["done"] = terminal_record(c, "done")
    result = check_gate(c, "pre-claim")
    assert result.status == "ready" and result.stage_id == "handed-off"
    assert not result.actions
    assert result.references == ("docs/operator-completion-lifecycle.md",)
    c.ledger.completion_journal["blocked"] = terminal_record(c)
    result = check_gate(c, "pre-claim")
    assert result.status == "unmet"
    assert "terminal_receipt_conflict" in result.reason_codes


def test_terminal_receipt_without_comment_url_is_unmet(tmp_path):
    c = prepared_context(tmp_path)
    record = terminal_record(c)
    record["posting_evidence"].pop("comment_url")
    c.ledger.completion_journal["current"] = record
    result = check_gate(c, "pre-claim")
    assert result.status == "unmet"
    assert "terminal_receipt_conflict" in result.reason_codes


@pytest.mark.parametrize(
    "field,reason",
    [
        ("mode", "context_invalid"),
        ("backend", "context_invalid"),
        ("change_kind", "context_invalid"),
        ("reviewer", "reviewer_invalid"),
        ("plan_approval", "approval_invalid"),
    ],
)
def test_malformed_state_values_are_evidence_errors(tmp_path, field, reason):
    c = prepared_context(tmp_path)
    c.state[field] = {"invalid": True}
    result = check_gate(c, "pre-claim")
    assert result.status == "unmet"
    assert reason in result.reason_codes
    assert "gate_not_applicable" not in result.reason_codes
    if field in ("mode", "backend", "change_kind"):
        check = next(ch for ch in result.checks if ch.id == "context_recorded")
        assert check.status == "unmet"
        assert "Record valid mode, change_kind and backend" in check.missing[0]


def test_missing_or_invalid_plan_reports_context_invalid_without_gate_not_applicable(
    tmp_path,
):
    c = prepared_context(tmp_path)
    c.errors["context"] = "implementation-plan.md not found"
    result = check_gate(c, "pre-claim")
    assert result.status == "unmet"
    assert "context_invalid" in result.reason_codes
    assert "gate_not_applicable" not in result.reason_codes
    check = next(ch for ch in result.checks if ch.id == "context_recorded")
    assert check.status == "unmet"
    assert "implementation-plan.md not found" in check.missing[0]


def test_non_matching_branches_retains_gate_not_applicable_when_state_is_valid(
    tmp_path,
):
    from dataclasses import replace

    from orchestune.workflow.stages import load_stage_definitions

    c = prepared_context(tmp_path)
    c.state["change_kind"] = "docs"
    # Create custom stage definitions where owner only accepts code
    base_stages = list(load_stage_definitions())
    for idx, stage in enumerate(base_stages):
        if stage.gate == "pre-edit":
            branches = replace(stage.branches, change_kind=("code",))
            base_stages[idx] = replace(stage, branches=branches)
    stages = tuple(base_stages)
    result = check_gate(c, "pre-edit", stages=stages)
    assert result.status == "unmet"
    assert "gate_not_applicable" in result.reason_codes

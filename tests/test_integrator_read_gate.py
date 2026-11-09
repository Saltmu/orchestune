"""Read gates cover both review shortcuts and the final parent publication."""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from orchestune.claim.read_checkpoint import checkpoint_marker
from orchestune.integrator import attempt as attempt_module
from orchestune.integrator import read_gate as read_gate_module
from orchestune.integrator import steps as steps_module
from orchestune.integrator.attempt import start_required_review
from orchestune.integrator.finalization import render_integration_receipt
from orchestune.integrator.proofs import TaskIntegrationProof
from orchestune.integrator.read_gate import (
    build_integration_read_report,
    evaluate_report_gate,
    prepare_read_gate,
    validate_read_finalization,
)
from orchestune.models import IssueRecord
from orchestune.outcome_record import OutcomeRecord, ReviewSummary
from orchestune.read_dependencies.contracts import (
    CheckpointAvailability,
    ReadCheckpointV1,
    ReadCoverage,
    ReadDirection,
    ReadEvidenceV1,
    ReadSource,
    StaleReadGate,
    StaleVerdict,
    seal_evidence,
    seal_report,
)
from orchestune.read_dependencies.git import lookup_resource
from orchestune.review.semantic_gate import (
    ChildSourceRef,
    IntegrationAttemptIdentity,
    SemanticFixResultV1,
    SemanticReviewPhase,
    SemanticReviewStateV1,
    format_semantic_fix_result_marker,
)
from tests.test_read_dependency_git import _commit, _git
from tests.test_read_report_codec import sample_report
from tests.test_read_report_transport import FakeForge


def report(*, fresh=False):
    return seal_report(
        replace(
            sample_report(),
            coverage=ReadCoverage.COMPLETE,
            unknowns=(),
            candidates=() if fresh else sample_report().candidates,
            verdict=StaleVerdict.FRESH if fresh else StaleVerdict.CANDIDATE_STALE,
            digest=None,
        )
    )


class Forge(FakeForge):
    def add_comment(self, number, body):
        return self.create_issue_comment(number, body)

    def get_repository_slug(self):
        return report().repository


def context(mode="required"):
    current = report()
    child = ChildSourceRef(11, "writer", current.candidate_head_sha)
    identity = IntegrationAttemptIdentity.create(1230, current.parent_sha, (child,))
    state = SemanticReviewStateV1(
        identity.attempt_id,
        1230,
        42,
        SemanticReviewPhase.PASSED,
        1,
        current.candidate_head_sha,
        current.parent_sha,
        (child,),
        ci_verified_sha=current.candidate_head_sha,
        read_report_digest=current.digest,
        reviewed_read_report_digest=current.digest,
    )
    return SimpleNamespace(
        config=SimpleNamespace(stale_read_gate=mode, force_integration_review=False),
        forge=Forge(),
        repository_root=Path("."),
        attempt_identity=identity,
        integration_pr_number=42,
        semantic_review_state=state,
        read_report=None,
        read_gate_error=None,
        read_gate_decision=None,
    )


@pytest.mark.parametrize("mode", list(StaleReadGate))
def test_modes_distinguish_candidate_and_unknown_from_fresh(mode):
    current = report()
    decision = evaluate_report_gate(current, mode, head_sha=current.candidate_head_sha)
    assert decision.allow_fast_path == (mode is not StaleReadGate.REQUIRED)
    assert decision.allow_parent_update == (mode is not StaleReadGate.REQUIRED)
    unknown = evaluate_report_gate(None, mode, head_sha=current.candidate_head_sha)
    assert unknown.allow_parent_update == (mode is not StaleReadGate.REQUIRED)
    assert evaluate_report_gate(
        report(fresh=True), mode, head_sha=current.candidate_head_sha
    ).allow_fast_path


def test_report_digest_tampering_cannot_allow_a_required_fast_path():
    changed = replace(report(fresh=True), digest="0" * 64)
    assert not evaluate_report_gate(
        changed, "required", head_sha=changed.candidate_head_sha
    ).allow_fast_path


def test_single_fast_forward_candidate_keeps_the_existing_patch_path_but_skips_it():
    ctx = context()
    ctx.semantic_review_state = None
    with (
        patch.object(
            read_gate_module,
            "build_integration_read_report",
            return_value=report(),
        ),
        patch.object(attempt_module, "run_git") as git,
        patch.object(attempt_module, "_single_fast_path") as fast,
    ):
        git.return_value.stdout = report().candidate_head_sha
        assert start_required_review(ctx, MagicMock()) == "semantic_review_pending"
    fast.assert_not_called()
    assert ctx.semantic_review_state.phase is SemanticReviewPhase.NOT_STARTED
    assert ctx.semantic_review_state.read_report_digest == report().digest


def test_fresh_single_child_can_use_existing_fast_path_and_binds_report():
    ctx = context()
    ctx.semantic_review_state = None
    with (
        patch.object(
            read_gate_module,
            "build_integration_read_report",
            return_value=report(fresh=True),
        ),
        patch.object(attempt_module, "run_git") as git,
        patch.object(attempt_module, "_single_fast_path") as fast,
    ):
        git.return_value.stdout = report().candidate_head_sha
        fast.return_value.phase = SemanticReviewPhase.FAST_PATH_PASSED
        assert start_required_review(ctx, MagicMock()) == "success"
    fast.assert_called_once()
    assert (
        ctx.semantic_review_state.reviewed_read_report_digest
        == report(fresh=True).digest
    )


def test_report_is_durable_before_state_is_created_and_reused_by_runner():
    from orchestune.dispatch.semantic_review.read_context import SemanticReadContext

    ctx = context()
    current = report()
    with patch.object(
        read_gate_module,
        "build_integration_read_report",
        return_value=current,
    ):
        prepare_read_gate(ctx, current.candidate_head_sha)
    restored = SemanticReadContext(ctx.forge, "required").restore(
        ctx.semantic_review_state
    )
    assert restored.report == current
    assert restored.digest == current.digest


@pytest.mark.parametrize(
    "change", ["digest", "head", "ci", "unknown", "missing_validation"]
)
def test_restored_passed_attempt_rejects_changed_or_unverified_inputs(change):
    ctx = context()
    current = report(fresh=change != "missing_validation")
    ctx.semantic_review_state = replace(
        ctx.semantic_review_state,
        read_report_digest=current.digest,
        reviewed_read_report_digest=current.digest,
    )
    if change == "digest":
        ctx.semantic_review_state = replace(
            ctx.semantic_review_state, reviewed_read_report_digest="0" * 64
        )
    elif change == "head":
        current = seal_report(
            replace(current, candidate_head_sha="e" * 40, digest=None)
        )
    elif change == "ci":
        ctx.semantic_review_state = replace(
            ctx.semantic_review_state, ci_verified_sha="e" * 40
        )
    elif change == "unknown":
        current = sample_report()
    with (
        patch.object(
            read_gate_module,
            "build_integration_read_report",
            return_value=current,
        ),
        patch.object(read_gate_module, "run_git") as git,
    ):
        git.return_value.stdout = report().candidate_head_sha
        assert validate_read_finalization(ctx) is not None


@pytest.mark.parametrize("mode", ["advisory", "off"])
def test_non_required_modes_report_diagnostics_without_blocking(mode):
    ctx = context(mode)
    with patch.object(
        read_gate_module,
        "build_integration_read_report",
        side_effect=OSError("unavailable"),
    ):
        assert validate_read_finalization(ctx) is None
    assert ctx.read_gate_decision.mode.value == mode


def test_fresh_passed_attempt_requires_exact_report_and_ci():
    ctx = context()
    current = report(fresh=True)
    ctx.semantic_review_state = replace(
        ctx.semantic_review_state,
        phase=SemanticReviewPhase.FAST_PATH_PASSED,
        read_report_digest=current.digest,
        reviewed_read_report_digest=current.digest,
    )
    with (
        patch.object(
            read_gate_module,
            "build_integration_read_report",
            return_value=current,
        ),
        patch.object(read_gate_module, "run_git") as git,
    ):
        git.return_value.stdout = current.candidate_head_sha
        assert validate_read_finalization(ctx) is None


def _judged_context(**changes):
    ctx = context()
    current = report()
    operation = ctx.semantic_review_state.attempt_id[:12] + "-fix-1"
    ctx.semantic_review_state = replace(
        ctx.semantic_review_state, operation_id=operation
    )
    payload = {
        "version": 1,
        "round_number": 1,
        "head_sha": current.candidate_head_sha,
        "report_digest": current.digest,
        "decisions": [
            {
                "candidate_id": c.candidate_id,
                "conclusion": "compatible",
                "rationale": "verified consumer",
            }
            for c in current.candidates
        ],
    } | changes
    ctx.forge.comments = [
        {
            "id": 5,
            "user": {"login": "reviewer"},
            "body": "```orchestune-read-validation\n" + json.dumps(payload) + "\n```",
        },
        {
            "id": 6,
            "user": {"login": "reviewer"},
            "body": format_semantic_fix_result_marker(
                SemanticFixResultV1(
                    ctx.semantic_review_state.attempt_id,
                    operation,
                    current.candidate_head_sha,
                    None,
                    5,
                    "completed",
                )
            ),
        },
    ]
    return ctx


@pytest.mark.parametrize(
    "change", [{}, {"round_number": 2}, {"decisions": []}, {"report_digest": "0" * 64}]
)
def test_candidate_publish_requires_the_trusted_jobs_exact_validation(change):
    ctx = _judged_context(**change)
    with (
        patch.object(
            read_gate_module,
            "build_integration_read_report",
            return_value=report(),
        ),
        patch.object(read_gate_module, "run_git") as git,
    ):
        git.return_value.stdout = report().candidate_head_sha
        error = validate_read_finalization(ctx)
    assert (error is None) == (not change)


class HistoryForge(Forge):
    def __init__(self):
        super().__init__()
        self.issues = []
        self.issue_comments = {}

    def list_sub_issues(self, _parent):
        return self.issues

    def find_issues_by_parent_metadata(self, _parent):
        return self.issues

    def list_all_issue_comments(self, number):
        return self.issue_comments.get(number, [])


def _store_reader(forge, repo, issue, base, head, reads, *, integrated=False):
    claim = f"claim-{issue}"
    value = seal_evidence(
        ReadEvidenceV1(
            repository=forge.get_repository_slug(),
            issue_number=issue,
            claim_id=claim,
            input_base_sha=base,
            head_sha=head,
            declared_reads=reads,
            observations=tuple(lookup_resource(base, p, cwd=repo) for p in reads),
            source=ReadSource.ADAPTER_OBSERVED,
            coverage=ReadCoverage.COMPLETE,
        )
    )
    outcome = OutcomeRecord(
        result="done",
        issue=issue,
        claim_id=claim,
        completion_id=f"completion-{issue}",
        head_sha=head,
        read_evidence=value,
        review=ReviewSummary(bot="codex", verdict="pass", reviewed_head_sha=head),
    )
    proof = TaskIntegrationProof(issue, f"child-{issue}", "deleted/child", head)
    bodies = [
        outcome.render(),
        checkpoint_marker(
            ReadCheckpointV1(
                repository=value.repository,
                issue_number=issue,
                claim_id=claim,
                input_base_sha=base,
                availability=CheckpointAvailability.RECORDED,
            )
        ),
    ]
    if integrated:
        bodies.append(render_integration_receipt(proof, "parent/issue-1230"))
    forge.issue_comments[issue] = [
        {"body": b, "user": {"login": "reviewer"}} for b in bodies
    ]
    forge.issues.append(
        IssueRecord(
            issue,
            "child",
            f"## Footprint\n```yaml\nsubtask_id: child-{issue}\nparent_issue_number: 1230\n```",
            (),
            "2026-01-01",
            state="CLOSED" if integrated else "OPEN",
        )
    )
    return ChildSourceRef(issue, f"child-{issue}", head)


@pytest.fixture
def git_world(tmp_path):
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "registry.py").write_text("v1\n", encoding="utf-8")
    base = _commit(tmp_path, "original")
    ctx = context()
    ctx.repository_root = tmp_path
    ctx.forge = HistoryForge()
    return ctx, tmp_path, base


def _set_attempt(ctx, base, *children):
    ctx.attempt_identity = IntegrationAttemptIdentity.create(1230, base, children)


def test_real_git_single_fast_forward_detects_closed_reverse_reader(git_world):
    ctx, repo, base = git_world
    (repo / "adapter.py").write_text("reads v1\n", encoding="utf-8")
    parent = _commit(repo, "integrated reader B")
    _store_reader(ctx.forge, repo, 10, base, parent, ("registry.py",), integrated=True)
    (repo / "registry.py").write_text("v2\n", encoding="utf-8")
    head = _commit(repo, "writer A fast-forward")
    child = _store_reader(ctx.forge, repo, 11, parent, head, ())
    _set_attempt(ctx, parent, child)
    current = build_integration_read_report(ctx, head)
    assert current.coverage is ReadCoverage.COMPLETE
    assert ReadDirection.REVERSE in {c.identity.direction for c in current.candidates}
    assert not evaluate_report_gate(current, "required", head_sha=head).allow_fast_path


def test_real_git_clean_rebase_keeps_the_original_read_base(git_world):
    ctx, repo, base = git_world
    (repo / "registry.py").write_text("v2\n", encoding="utf-8")
    parent = _commit(repo, "parent advanced")
    _store_reader(ctx.forge, repo, 11, base, parent, (), integrated=True)
    (repo / "adapter.py").write_text("implemented against v1\n", encoding="utf-8")
    head = _commit(repo, "reader rebased cleanly")
    child = _store_reader(ctx.forge, repo, 10, base, head, ("registry.py",))
    _set_attempt(ctx, parent, child)
    current = build_integration_read_report(ctx, head)
    assert ReadDirection.FORWARD in {c.identity.direction for c in current.candidates}
    assert current.verdict is StaleVerdict.CANDIDATE_STALE


def test_real_git_same_attempt_child_detected_before_fast_path(git_world):
    ctx, repo, base = git_world
    (repo / "adapter.py").write_text("reads registry v1\n", encoding="utf-8")
    reader_head = _commit(repo, "reader B")
    reader = _store_reader(ctx.forge, repo, 10, base, reader_head, ("registry.py",))
    _git(repo, "checkout", "--detach", base)
    (repo / "registry.py").write_text("v2\n", encoding="utf-8")
    writer_head = _commit(repo, "writer A")
    writer = _store_reader(ctx.forge, repo, 11, base, writer_head, ())
    _git(repo, "merge", "--no-edit", reader_head)
    head = _git(repo, "rev-parse", "HEAD")
    _set_attempt(ctx, base, reader, writer)
    current = build_integration_read_report(ctx, head)
    assert ReadDirection.SAME_ATTEMPT in {
        c.identity.direction for c in current.candidates
    }


def test_history_failure_and_untrusted_generation_are_unknown(git_world):
    ctx, repo, base = git_world
    child = _store_reader(ctx.forge, repo, 10, base, base, ())
    _set_attempt(ctx, base, child)
    ctx.forge.issue_comments[10][0]["user"]["login"] = "untrusted"
    decision = prepare_read_gate(ctx, base)
    assert not decision.allow_parent_update
    assert "generation" in ctx.read_gate_error


def test_parent_push_crash_window_cannot_drop_an_open_historical_reader(git_world):
    ctx, repo, base = git_world
    _store_reader(ctx.forge, repo, 10, base, base, ("registry.py",))
    child = _store_reader(ctx.forge, repo, 11, base, base, ())
    _set_attempt(ctx, base, child)
    # Child 10 already reached P, but receipt/label/close were not delivered.
    decision = prepare_read_gate(ctx, base)
    assert not decision.allow_parent_update
    assert "receipt" in ctx.read_gate_error


@pytest.mark.parametrize("current", [None, sample_report(), report()])
def test_unknown_and_unresolved_candidates_stop_before_any_parent_push(current):
    from orchestune.integrator.steps import AutoMergeChildIntegrationStep

    ctx = context()
    step = AutoMergeChildIntegrationStep()
    with (
        patch.object(
            read_gate_module,
            "build_integration_read_report",
            return_value=current,
        ),
        patch.object(read_gate_module, "run_git") as git,
        patch.object(steps_module, "publish_parent_under_claim") as publish,
        patch.object(step, "handle_review_gate_block") as escalation,
    ):
        git.return_value.stdout = report().candidate_head_sha
        result = step._update_parent_branch(ctx)
    assert result["status"] == "review_gate_blocked"
    publish.assert_not_called()
    escalation.assert_called_once()


def test_final_parent_push_revalidates_after_acquiring_the_claim():
    from orchestune.integrator.attempt import publish_parent_under_claim
    from orchestune.integrator.semantic_claim import ClaimStatus

    ctx = context()
    acquired = SimpleNamespace(status=ClaimStatus.ACQUIRED, claim=object())
    with (
        patch.object(
            attempt_module,
            "acquire_semantic_claim",
            return_value=acquired,
        ),
        patch.object(
            attempt_module,
            "validate_finalization_context",
            return_value="report changed",
        ) as validate,
        patch.object(attempt_module, "push_refs_under_claim") as push,
    ):
        result = publish_parent_under_claim(ctx)
    assert result.handled and not result.pushed
    assert result.detail == "report changed"
    validate.assert_called_once_with(ctx)
    push.assert_not_called()

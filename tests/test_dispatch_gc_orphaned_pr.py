"""Orphan recovery evidence is bound to the retired execution and PR."""

import subprocess
from unittest.mock import patch

import pytest

from orchestune.claim.adopt_pr import orphan_marker
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.gc.completion import (
    _apply_completed_worktree_outcome,
    _decide_completed_worktree_outcome,
)
from orchestune.dispatch.gc.completion_contracts import CompletedWorktreeDecision
from orchestune.dispatch.gc.orphaned_pr import orphan_receipt
from orchestune.dispatch.launch import DuplicateCandidateDecision, _apply_duplicate_skip
from orchestune.ledger.run_state import RunState
from orchestune.models import PrRecord
from orchestune.outcome_record import OutcomeRecord
from tests.dispatch_gc_test_support import _active, _task
from tests.test_dispatch_launch_basic import _task as _launch_task


def test_receipt_binds_pr_and_old_claim():
    active = _active(claim_id="claim-1", repository_id="repo-1")
    pr = PrRecord(
        number=9,
        head_ref=active.core.branch,
        changed_files=(),
        head_sha="a" * 40,
        is_cross_repository=False,
    )
    key, receipt = orphan_receipt(active, pr)
    assert key == "orphaned-pr::repo-1::280::claim-1"
    assert receipt["active"]["claim_id"] == "claim-1"
    assert receipt["pr_number"] == 9
    assert receipt["head_sha"] == "a" * 40


@pytest.mark.parametrize("overrides", [{"claim_id": None}, {"repository_id": None}])
def test_missing_generation_identity_cannot_be_recorded(overrides):
    active = _active(**overrides)
    pr = PrRecord(
        number=9,
        head_ref=active.core.branch,
        changed_files=(),
        head_sha="a" * 40,
        is_cross_repository=False,
    )
    with pytest.raises(ValueError):
        orphan_receipt(active, pr)


def test_receipt_is_saved_before_escalation(tmp_path, fake_forge):
    active = _active(claim_id="claim-1", repository_id="repo-1")
    pr = PrRecord(
        number=9,
        head_ref=active.core.branch,
        changed_files=(),
        head_sha="a" * 40,
        is_cross_repository=False,
    )
    state = RunState(active_worktrees={"280": active})
    config = DispatcherConfig(
        parent_issue_number=100,
        apply=True,
        forge=fake_forge,
        run_state_path=tmp_path / "state.json",
        events_log_path=tmp_path / "events.jsonl",
    )
    decision = CompletedWorktreeDecision(action="completed_orphaned_pr", orphaned_pr=pr)
    with (
        patch(
            "orchestune.dispatch.gc.completion.fresh_external_hold", return_value=None
        ),
        patch("orchestune.dispatch.gc.completion.save_run_state") as save,
        patch("orchestune.dispatch.gc.completion._apply_escalation") as escalate,
    ):
        _apply_completed_worktree_outcome(active, decision, config, run_state=state)
    assert (
        state.recovery_receipts["orphaned-pr::repo-1::280::claim-1"]["head_sha"]
        == pr.head_sha
    )
    save.assert_called_once()
    escalate.assert_called_once()


def test_failed_receipt_save_prevents_escalation(tmp_path, fake_forge):
    active = _active(claim_id="claim-1", repository_id="repo-1")
    pr = PrRecord(
        number=9,
        head_ref=active.core.branch,
        changed_files=(),
        head_sha="a" * 40,
        is_cross_repository=False,
    )
    state = RunState(active_worktrees={"280": active})
    config = DispatcherConfig(
        parent_issue_number=100,
        apply=True,
        forge=fake_forge,
        run_state_path=tmp_path / "state.json",
        events_log_path=tmp_path / "events.jsonl",
    )
    decision = CompletedWorktreeDecision(action="completed_orphaned_pr", orphaned_pr=pr)
    with (
        patch(
            "orchestune.dispatch.gc.completion.fresh_external_hold", return_value=None
        ),
        patch(
            "orchestune.dispatch.gc.completion.save_run_state",
            side_effect=OSError("disk full"),
        ),
        patch("orchestune.dispatch.gc.completion._apply_escalation") as escalate,
        pytest.raises(OSError, match="disk full"),
    ):
        _apply_completed_worktree_outcome(active, decision, config, run_state=state)
    escalate.assert_not_called()
    assert state.recovery_receipts == {}


def test_existing_orphan_notice_is_not_posted_twice(tmp_path, fake_forge):
    active = _active(claim_id="claim-1", repository_id="repo-1")
    pr = PrRecord(
        number=9,
        head_ref=active.core.branch,
        changed_files=(),
        head_sha="a" * 40,
        is_cross_repository=False,
    )
    state = RunState(active_worktrees={"280": active})
    config = DispatcherConfig(
        parent_issue_number=100,
        apply=True,
        forge=fake_forge,
        run_state_path=tmp_path / "state.json",
        events_log_path=tmp_path / "events.jsonl",
    )
    fake_forge.list_comments.return_value = [
        {"body": orphan_marker("claim-1", pr), "author": "operator"}
    ]
    fake_forge.get_authenticated_user.return_value = "operator"
    decision = CompletedWorktreeDecision(action="completed_orphaned_pr", orphaned_pr=pr)
    with (
        patch(
            "orchestune.dispatch.gc.completion.fresh_external_hold", return_value=None
        ),
        patch("orchestune.dispatch.gc.completion.save_run_state"),
        patch("orchestune.dispatch.gc.completion.remove_worktree"),
    ):
        _apply_completed_worktree_outcome(active, decision, config, run_state=state)
    fake_forge.add_comment.assert_not_called()


class TestOrphanClassification:
    def test_saved_escalation_cannot_be_reclassified_after_worktree_loss(self):
        active = _active(claim_id="claim-1", repository_id="repo-1")
        pr = PrRecord(
            number=77,
            head_ref=active.core.branch,
            changed_files=(),
            state="OPEN",
            is_cross_repository=False,
            head_sha="a" * 40,
        )
        key, receipt = orphan_receipt(active, pr)
        state = RunState(recovery_receipts={key: receipt})
        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.run_git",
                side_effect=OSError("missing worktree"),
            ),
        ):
            matching = _decide_completed_worktree_outcome(
                active, _task(), run_state=state, open_prs=[pr]
            )
            missing = _decide_completed_worktree_outcome(
                active, _task(), run_state=state, open_prs=[]
            )
        assert matching.action == "completed_orphaned_pr"
        assert missing.action == "completion_skipped_forge_error"

    def test_done_outcome_without_detectable_commits_still_reports_pr(self):
        sha = "a" * 40
        pr = PrRecord(
            number=77,
            head_ref="claude/issue-280-task-a",
            changed_files=(),
            state="OPEN",
            is_cross_repository=False,
            head_sha=sha,
        )
        outcome = OutcomeRecord(result="done", issue=280, pr=77)
        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion._resolve_active_outcome",
                return_value=(outcome, None),
            ),
            patch(
                "orchestune.dispatch.gc.completion.run_git",
                return_value=subprocess.CompletedProcess([], 0, stdout=sha + "\n"),
            ),
        ):
            decision = _decide_completed_worktree_outcome(
                _active(), _task(), open_prs=[pr]
            )
        assert decision.action == "completed_orphaned_pr"

    @pytest.mark.parametrize("local_commits", [False, True, None])
    def test_open_pr_at_branch_tip_is_orphaned(self, local_commits):
        sha = "a" * 40
        pr = PrRecord(
            number=77,
            head_ref="claude/issue-280-task-a",
            changed_files=(),
            state="OPEN",
            is_cross_repository=False,
            head_sha=sha,
        )
        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                return_value=local_commits,
            ),
            patch(
                "orchestune.dispatch.gc.completion.remote_branch_commit_sha_if_ahead",
                return_value=None,
            ),
            patch(
                "orchestune.dispatch.gc.completion.run_git",
                return_value=subprocess.CompletedProcess([], 0, stdout=sha + "\n"),
            ),
        ):
            decision = _decide_completed_worktree_outcome(
                _active(), _task(), open_prs=[pr]
            )
        assert decision.action == "completed_orphaned_pr"
        assert decision.commit_sha == sha

    @pytest.mark.parametrize(
        "case", ["fork", "unknown_repo", "wrong_head", "closed", "duplicate"]
    )
    def test_unverified_pr_does_not_become_orphan(self, case):
        sha = "a" * 40
        pr = PrRecord(
            number=77,
            head_ref="claude/issue-280-task-a",
            changed_files=(),
            state="CLOSED" if case == "closed" else "OPEN",
            is_cross_repository=True
            if case == "fork"
            else None
            if case == "unknown_repo"
            else False,
            head_sha="b" * 40 if case == "wrong_head" else sha,
        )
        prs = [pr, pr] if case == "duplicate" else [pr]
        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.run_git",
                return_value=subprocess.CompletedProcess([], 0, stdout=sha + "\n"),
            ),
        ):
            decision = _decide_completed_worktree_outcome(
                _active(), _task(), open_prs=prs
            )
        assert decision.action == "completed_no_commits"

    @pytest.mark.parametrize(
        ("remote_sha", "expected"),
        [
            ("a" * 40, "completed_without_outcome"),
            (None, "completion_skipped_forge_error"),
        ],
    )
    def test_failed_local_comparison_rechecks_remote(self, remote_sha, expected):
        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                return_value=None,
            ),
            patch(
                "orchestune.dispatch.gc.completion.remote_branch_commit_sha_if_ahead",
                return_value=remote_sha,
            ) as remote,
        ):
            decision = _decide_completed_worktree_outcome(_active(), _task())
        remote.assert_called_once()
        assert decision.action == expected


def test_duplicate_pr_diagnostics_provide_adoption_steps(tmp_path, fake_forge, capsys):
    task = _launch_task(1)
    pr = PrRecord(
        number=5, head_ref="claude/issue-1-task-1", changed_files=(), head_sha="a" * 40
    )
    config = DispatcherConfig(
        parent_issue_number=100,
        apply=True,
        forge=fake_forge,
        run_state_path=tmp_path / "state.json",
        events_log_path=tmp_path / "events.jsonl",
    )
    with patch("orchestune.dispatch.launch.apply_human_review_escalation") as escalate:
        assert (
            _apply_duplicate_skip([DuplicateCandidateDecision(task, True, pr)], config)
            == []
        )
    text = escalate.call_args.args[2]
    assert "orchestune claim 1 --adopt-pr 5" in text
    assert pr.head_sha in text and "Layer-1" in text
    assert "orchestune claim 1 --adopt-pr 5" in capsys.readouterr().err

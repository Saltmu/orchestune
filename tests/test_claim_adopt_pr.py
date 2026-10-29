"""Orphan PR adoption must reject ambiguous or untrusted PR identity."""

from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from orchestune.claim.adopt_pr import (
    duplicate_marker,
    orphan_marker,
    validate_adoption_pr,
    validate_escalation_reason,
    validate_orphan_evidence,
)
from orchestune.claim.cli import _build_parser
from orchestune.claim.contracts import ClaimRequest
from orchestune.claim.resume_validation import _validate_issue_for_resume
from orchestune.claim.service import claim_task, resume_claim
from orchestune.claim.workspace import resolve_claim_workspace
from orchestune.complete.journal import CompletionJournalError
from orchestune.complete.service import _check_adopted_pr
from orchestune.dispatch.gc.orphaned_pr import orphan_receipt
from orchestune.infra.process_utils import run_state_lock
from orchestune.ledger.active_records import ClaimInfo
from orchestune.ledger.run_state import RunState, load_run_state, save_run_state
from orchestune.models import PrRecord
from orchestune.worktree_ops.preparation import WorktreePreparation
from tests.claim_helpers import MockForge, _make_issue
from tests.dispatch_gc_test_support import _active


@pytest.fixture
def pr():
    return PrRecord(
        number=12,
        head_ref="claude/issue-42-task",
        changed_files=(),
        base_ref="parent/issue-4",
        state="OPEN",
        is_cross_repository=False,
        head_sha="a" * 40,
    )


def test_valid_adoption_pr(pr):
    assert validate_adoption_pr([pr], 42, 12, pr.head_ref, pr.base_ref) == pr


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "CLOSED"},
        {"state": "MERGED"},
        {"is_cross_repository": True},
        {"is_cross_repository": None},
        {"head_ref": "other/issue-42-task"},
        {"base_ref": "main"},
        {"head_sha": ""},
        {"head_sha": "bad"},
    ],
)
def test_invalid_pr_is_rejected(pr, changes):
    with pytest.raises(ValueError):
        validate_adoption_pr([replace(pr, **changes)], 42, 12, pr.head_ref, pr.base_ref)


def test_multiple_issue_prs_are_rejected(pr):
    other = replace(pr, number=13, head_ref="other/issue-42-extra")
    with pytest.raises(ValueError):
        validate_adoption_pr([pr, other], 42, 12, pr.head_ref, pr.base_ref)


def test_adoption_option_is_explicit_and_exclusive_with_resume():
    parser = _build_parser()
    assert parser.parse_args(["42", "--adopt-pr", "12"]).adopt_pr == 12
    with pytest.raises(SystemExit):
        parser.parse_args(["42", "--adopt-pr", "12", "--resume", "claim-id"])


def test_adoption_does_not_fall_back_to_ordinary_claim(claim_env):
    forge = MockForge({42: _make_issue(number=42)})
    outcome = claim_task(
        ClaimRequest(issue_number=42, adopt_pr=12, state_path=claim_env["state_path"]),
        forge=forge,
        cwd=claim_env["repo_root"],
    )
    assert not outcome.success


def test_adopted_claim_rejects_changed_pr_or_head():
    claim = ClaimInfo(adopted_pr_number=12, adopted_pr_head_sha="a" * 40)
    _check_adopted_pr(claim, "done", 12, "a" * 40)
    with pytest.raises(CompletionJournalError):
        _check_adopted_pr(claim, "done", 13, "a" * 40)
    with pytest.raises(CompletionJournalError):
        _check_adopted_pr(claim, "done", 12, "b" * 40)
    _check_adopted_pr(claim, "blocked", None, "b" * 40)


def test_adopted_claim_completes_with_matching_head_and_ci(tmp_path, monkeypatch):
    from complete_lifecycle_test_support import lifecycle_environment

    from orchestune.complete.journal import completion_journal_lock
    from orchestune.complete.service import complete_task

    request, forge, _ = lifecycle_environment(tmp_path, monkeypatch, "done")
    with completion_journal_lock(request.state_path):
        state = load_run_state(request.state_path)
        active = state.active_worktrees["1110"]
        active.claim = replace(
            active.claim, adopted_pr_number=42, adopted_pr_head_sha="a" * 40
        )
        save_run_state(state, request.state_path)
    result = complete_task(request, forge=forge)
    assert result.success, result.failure
    assert forge.labels == {"status:done"}


@pytest.mark.parametrize(
    "labels",
    [
        (),
        ("status:queued",),
        ("status:done",),
        ("status:blocked-human-review", "status:queued"),
    ],
)
def test_adopted_resume_rejects_missing_or_unexpected_status(labels):
    assert (
        _validate_issue_for_resume(_make_issue(number=42, labels=labels), adopted=True)
        is not None
    )


def test_orphan_evidence_requires_exact_generation_and_confirmed_stop(pr):
    old = _active(
        issue_number=42,
        branch=pr.head_ref,
        claim_id="old",
        repository_id="repo",
        pid=43210,
    )
    key, receipt = orphan_receipt(old, pr)
    state = RunState(recovery_receipts={key: receipt})
    assert (
        validate_orphan_evidence(state, "repo", 42, pr, lambda _: True).claim.claim_id
        == "old"
    )
    with pytest.raises(ValueError):
        validate_orphan_evidence(state, "repo", 42, pr, lambda _: False)
    with pytest.raises(ValueError):
        validate_orphan_evidence(
            state, "repo", 42, replace(pr, head_sha="b" * 40), lambda _: True
        )
    state.active_worktrees["42"] = old
    with pytest.raises(ValueError):
        validate_orphan_evidence(state, "repo", 42, pr, lambda _: True)


def test_orphan_evidence_rejects_tampered_snapshot_and_ambiguous_generation(pr):
    old = _active(
        issue_number=42,
        branch=pr.head_ref,
        claim_id="old",
        repository_id="repo",
        pid=43210,
    )
    key, receipt = orphan_receipt(old, pr)
    receipt["active"]["pid"] = "invalid"
    state = RunState(recovery_receipts={key: receipt})
    with pytest.raises(ValueError):
        validate_orphan_evidence(state, "repo", 42, pr, lambda _: True)
    key, receipt = orphan_receipt(old, pr)
    state.recovery_receipts = {key: receipt, "other": dict(receipt)}
    with pytest.raises(ValueError):
        validate_orphan_evidence(state, "repo", 42, pr, lambda _: True)


def test_resume_rejects_duplicate_active_entries_for_same_claim(pr):
    old = _active(
        issue_number=42,
        branch=pr.head_ref,
        claim_id="old",
        repository_id="repo",
        pid=43210,
    )
    key, receipt = orphan_receipt(old, pr)
    current = _active(
        issue_number=42,
        branch=pr.head_ref,
        claim_id="new",
        repository_id="repo",
        pid=None,
    )
    state = RunState(
        active_worktrees={"42": current, "duplicate": current},
        recovery_receipts={key: receipt},
    )
    with pytest.raises(ValueError):
        validate_orphan_evidence(
            state, "repo", 42, pr, lambda _: True, allowed_claim_id="new"
        )


def test_escalation_must_be_latest_and_trusted(pr):
    marker = orphan_marker("old", pr)

    def trusted(_):
        return True

    validate_escalation_reason([{"body": marker}], marker, trusted)
    duplicate = duplicate_marker(pr)
    validate_escalation_reason(
        [{"body": marker}, {"body": duplicate}], marker, trusted, duplicate=duplicate
    )
    with pytest.raises(ValueError):
        validate_escalation_reason([{"body": marker}], marker, lambda _: False)
    with pytest.raises(ValueError):
        validate_escalation_reason(
            [{"body": marker}, {"body": "status:blocked-human-review: other failure"}],
            marker,
            trusted,
        )


def test_adoption_preview_validates_before_side_effects(claim_env):
    root, state_path = claim_env["repo_root"], claim_env["state_path"]
    workspace = resolve_claim_workspace(root, explicit_state_path=state_path)
    issue = _make_issue(number=42, labels=("status:blocked-human-review",))
    pr = PrRecord(
        number=12,
        head_ref="claude/issue-42-test-task",
        changed_files=(),
        base_ref="main",
        state="OPEN",
        is_cross_repository=False,
        head_sha="a" * 40,
    )
    old = _active(
        issue_number=42,
        branch=pr.head_ref,
        claim_id="old",
        repository_id=workspace.repository_identity,
        pid=43210,
    )
    key, receipt = orphan_receipt(old, pr)
    with run_state_lock(workspace.lock_path):
        save_run_state(RunState(recovery_receipts={key: receipt}), state_path)
    forge = MockForge({42: issue})
    with (
        patch.object(forge, "list_prs", return_value=[pr]),
        patch.object(
            forge,
            "list_comments",
            return_value=[{"body": orphan_marker("old", pr), "author": "operator"}],
        ),
        patch.object(forge, "get_authenticated_user", return_value="operator"),
        patch("orchestune.claim.service.is_process_alive", return_value=False),
        patch(
            "orchestune.claim.service.run_git",
            return_value=MagicMock(
                returncode=0, stdout=f"{pr.head_sha}\trefs/heads/{pr.head_ref}\n"
            ),
        ) as git,
        patch("orchestune.claim.service.prepare_task_worktree") as prepare,
    ):
        outcome = claim_task(
            ClaimRequest(
                issue_number=42, adopt_pr=12, state_path=state_path, dry_run=True
            ),
            forge=forge,
            cwd=root,
        )
    assert outcome.success, outcome.failure
    assert all("fetch" not in call.args[0] for call in git.call_args_list)
    prepare.assert_not_called()


@pytest.mark.parametrize("move_head", [False, True])
def test_adoption_reserves_exact_pr_head_and_finalizes(claim_env, move_head):
    root, state_path = claim_env["repo_root"], claim_env["state_path"]
    workspace = resolve_claim_workspace(root, explicit_state_path=state_path)
    issue = _make_issue(number=42, labels=("status:blocked-human-review",))
    pr = PrRecord(
        number=12,
        head_ref="claude/issue-42-test-task",
        changed_files=(),
        base_ref="main",
        state="OPEN",
        is_cross_repository=False,
        head_sha="a" * 40,
    )
    old = _active(
        issue_number=42,
        branch=pr.head_ref,
        claim_id="old",
        repository_id=workspace.repository_identity,
        pid=43210,
    )
    key, receipt = orphan_receipt(old, pr)
    with run_state_lock(workspace.lock_path):
        save_run_state(RunState(recovery_receipts={key: receipt}), state_path)
    forge = MockForge({42: issue})

    def git(args, **kwargs):
        output = (
            f"{pr.head_sha}\trefs/heads/{pr.head_ref}\n"
            if "ls-remote" in args
            else pr.head_sha + "\n"
        )
        return MagicMock(returncode=0, stdout=output)

    def prepare(branch, worktree_root, base, claim_id, **kwargs):
        path = worktree_root / branch.replace("/", "-")
        path.mkdir(parents=True, exist_ok=True)
        return WorktreePreparation(
            worktree_path=path,
            branch=branch,
            accepted=True,
            created=True,
            base_sha="b" * 40,
        )

    with (
        patch.object(
            forge,
            "list_prs",
            side_effect=[
                [pr],
                [replace(pr, head_sha="c" * 40) if move_head else pr],
                [pr],
            ],
        ),
        patch.object(
            forge,
            "list_comments",
            return_value=[{"body": orphan_marker("old", pr), "author": "operator"}],
        ),
        patch.object(forge, "get_authenticated_user", return_value="operator"),
        patch("orchestune.claim.service.is_process_alive", return_value=False),
        patch("orchestune.claim.service.run_git", side_effect=git),
        patch("orchestune.claim.service.prepare_task_worktree", side_effect=prepare),
    ):
        outcome = claim_task(
            ClaimRequest(issue_number=42, adopt_pr=12, state_path=state_path),
            forge=forge,
            cwd=root,
        )
    active = load_run_state(state_path).active_worktrees["42"]
    assert (active.claim.adopted_pr_number, active.claim.adopted_pr_head_sha) == (
        12,
        pr.head_sha,
    )
    if move_head:
        assert not outcome.success
        assert not forge.labels_added
    else:
        assert outcome.success, outcome.failure


def test_interrupted_adoption_revalidates_before_resume(claim_env):
    root, state_path = claim_env["repo_root"], claim_env["state_path"]
    workspace = resolve_claim_workspace(root, explicit_state_path=state_path)
    issue = _make_issue(number=42, labels=("status:blocked-human-review",))
    pr = PrRecord(
        number=12,
        head_ref="claude/issue-42-test-task",
        changed_files=(),
        base_ref="main",
        is_cross_repository=False,
        head_sha="a" * 40,
    )
    old = _active(
        issue_number=42,
        branch=pr.head_ref,
        claim_id="old",
        repository_id=workspace.repository_identity,
        pid=43210,
    )
    key, receipt = orphan_receipt(old, pr)
    with run_state_lock(workspace.lock_path):
        save_run_state(RunState(recovery_receipts={key: receipt}), state_path)
    forge = MockForge({42: issue})

    def git(args, **kwargs):
        output = (
            f"{pr.head_sha}\trefs/heads/{pr.head_ref}\n"
            if "ls-remote" in args
            else pr.head_sha + "\n"
        )
        return MagicMock(returncode=0, stdout=output)

    def prepare(branch, worktree_root, base, claim_id, **kwargs):
        path = worktree_root / branch.replace("/", "-")
        path.mkdir(parents=True, exist_ok=True)
        return WorktreePreparation(
            worktree_path=path,
            branch=branch,
            accepted=True,
            created=True,
            base_sha="b" * 40,
        )

    with (
        patch.object(forge, "list_prs", return_value=[pr]),
        patch.object(
            forge,
            "list_comments",
            return_value=[{"body": orphan_marker("old", pr), "author": "operator"}],
        ),
        patch.object(forge, "get_authenticated_user", return_value="operator"),
        patch("orchestune.claim.service.is_process_alive", return_value=False),
        patch("orchestune.claim.service.run_git", side_effect=git),
        patch("orchestune.claim.service._check_resume_identity", return_value=None),
        patch(
            "orchestune.claim.service.prepare_task_worktree",
            side_effect=[
                OSError("interrupted"),
                prepare(
                    "claude/issue-42-test-task", workspace.worktree_root, "main", "new"
                ),
            ],
        ),
    ):
        first = claim_task(
            ClaimRequest(issue_number=42, adopt_pr=12, state_path=state_path),
            forge=forge,
            cwd=root,
        )
        assert not first.success and first.claim_id
        result = resume_claim(
            first.claim_id,
            first.owner_token or "",
            forge=forge,
            cwd=root,
            state_path=state_path,
        )
    assert result.success, result.failure

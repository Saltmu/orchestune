from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from orchestune.claim.workspace import resolve_claim_workspace
from orchestune.dispatch.claim_marker import claim_marker_path, write_claim_marker
from orchestune.dispatch.state import ActiveWorktree
from orchestune.infra.git_cli import run_git
from orchestune.models import PrRecord
from orchestune.outcome_record import OutcomeRecord


class FakeHandoffForge:
    def __init__(self, comment: dict, pr: PrRecord, *, reachable: bool = True):
        self.comment = comment
        self.pr = pr
        self.reachable = reachable
        self.comment_calls = 0
        self.pr_calls: list[int] = []
        self.reachability_calls: list[tuple[str, str]] = []

    def list_all_issue_comments(self, issue_number: int | str):
        self.comment_calls += 1
        assert int(issue_number) == 250
        return [self.comment]

    def get_pull_request(self, pr_number: int | str):
        self.pr_calls.append(int(pr_number))
        return self.pr

    def is_merge_commit_reachable_from(self, commit_oid: str, base: str) -> bool:
        self.reachability_calls.append((commit_oid, base))
        return self.reachable


def _create_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(["init", "-b", "main"], cwd=repo)
    run_git(["config", "user.name", "Test User"], cwd=repo)
    run_git(["config", "user.email", "test@example.com"], cwd=repo)
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    run_git(["add", "README.md"], cwd=repo)
    run_git(["commit", "-m", "initial"], cwd=repo)

    branch = "claude/issue-250-task"
    worktree = repo / "worktrees" / "issue-250"
    worktree.parent.mkdir()
    run_git(["worktree", "add", "-b", branch, str(worktree), "main"], cwd=repo)
    return repo, worktree, branch


def _make_active(
    repo: Path, worktree: Path, branch: str
) -> tuple[ActiveWorktree, dict]:
    head_sha = run_git(["rev-parse", "HEAD"], cwd=worktree).stdout.strip()
    workspace = resolve_claim_workspace(repo)
    claim_id = "claim-250"
    completion_id = "completion-250"
    comment_id = "98765"
    comment_url = "https://github.com/Saltmu/orchestune/issues/250#issuecomment-98765"
    outcome = OutcomeRecord(
        result="done",
        issue=250,
        pr=125,
        claim_id=claim_id,
        head_sha=head_sha,
        completion_id=completion_id,
    )
    comment = {
        "id": int(comment_id),
        "html_url": comment_url,
        "created_at": "2026-09-25T00:00:00Z",
        "body": outcome.render(),
    }
    active = ActiveWorktree(
        issue_number=250,
        branch=branch,
        worktree_path=str(worktree),
        pid=None,
        started_at=1.0,
        declared_footprint=(),
        owner_kind="interactive",
        claim_id=claim_id,
        claim_stage="completed",
        base_ref="origin/main",
        base_branch="main",
        reservation_kind="repository",
        claimed_at=1.0,
        base_sha=head_sha,
        owner_token_digest="d" * 64,
        repository_id=workspace.repository_identity,
        completion_id=completion_id,
        completion_result="done",
        completion_stage="handed_off_to_gc",
        completion_payload={"outcome": outcome.render()},
        completion_comment_id=comment_id,
        completion_comment_url=comment_url,
        completion_handoff_ready=True,
    )
    write_claim_marker(
        worktree,
        claim_id=claim_id,
        branch=branch,
        base_sha=head_sha,
        branch_created=True,
    )
    return active, comment


def _head_sha(active: ActiveWorktree) -> str:
    assert active.base_sha is not None
    return active.base_sha


def _forge(comment: dict, branch: str, head_sha: str) -> FakeHandoffForge:
    pr = PrRecord(
        number=125,
        head_ref=branch,
        changed_files=(),
        state="MERGED",
        merged_at="2026-09-25T00:00:00Z",
        merge_commit_oid=head_sha,
        base_ref="main",
        is_cross_repository=False,
        closes_issue_numbers=(250,),
    )
    return FakeHandoffForge(comment, pr)


def _write_state(repo: Path, active: ActiveWorktree) -> Path:
    running = ActiveWorktree(
        issue_number=251,
        branch="claude/issue-251-running",
        worktree_path=str(repo / "worktrees" / "issue-251"),
        pid=123,
        started_at=2.0,
        declared_footprint=("orchestune/example.py",),
        owner_kind="dispatch",
        claim_id="claim-251",
        claim_stage="completed",
        base_ref="origin/main",
        reservation_kind="footprint",
        repository_id=resolve_claim_workspace(repo).repository_identity,
        claimed_at=2.0,
        base_sha=None,
        owner_token_digest="e" * 64,
        completion_id="completion-251",
        completion_result="done",
        completion_stage="handed_off_to_gc",
        completion_handoff_ready=True,
    )
    state = {
        "active_worktrees": {"250": asdict(active), "251": asdict(running)},
        "launch_history": [1.0, 2.0],
        "completed_worktrees": [],
        "task_reclaim_counts": {"251": {"count": 3, "last_reclaimed_at": 2.0}},
        "pending_lock_release_notices": [251],
        "extension_data": {"keep": ["verbatim", 7]},
    }
    path = repo / "run_state.json"
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return path


def test_apply_releases_verified_done_and_preserves_unrelated_state(
    tmp_path: Path, monkeypatch
):
    from orchestune.claim.contracts import (
        ClaimFailureReason,
        ClaimRequest,
        ClaimStage,
    )
    from orchestune.claim.service import claim_task
    from orchestune.dispatch.gc.handoff import GcRequest
    from orchestune.dispatch.gc_service import run_handoff_gc
    from tests.claim_helpers import MockForge, _make_issue

    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    state_path = _write_state(repo, active)
    original_state = json.loads(state_path.read_text(encoding="utf-8"))
    head_sha = _head_sha(active)
    forge = _forge(comment, branch, head_sha)
    monkeypatch.chdir(repo)
    claim_forge = MockForge({252: _make_issue(number=252)})
    claim_request = ClaimRequest(
        issue_number=252,
        state_path=state_path,
        dry_run=True,
    )
    conflict = claim_task(claim_request, forge=claim_forge, cwd=repo)
    assert conflict.success is False
    assert conflict.failure is not None
    assert conflict.failure.reason == ClaimFailureReason.CLAIM_CONFLICT
    assert conflict.failure.conflicting_issue_number == 250

    result = run_handoff_gc(GcRequest(), forge_factory=lambda: forge)

    assert result.exit_code == 0
    assert len(result.items) == 1
    assert result.items[0].action == "released"
    assert result.items[0].worktree_action == "remove"
    assert result.receipts and result.receipts[0].issue_number == 250
    assert not worktree.exists()
    assert not claim_marker_path(worktree).exists()
    stored = json.loads(state_path.read_text(encoding="utf-8"))
    assert set(stored["active_worktrees"]) == {"251"}
    assert (
        stored["active_worktrees"]["251"] == original_state["active_worktrees"]["251"]
    )
    assert stored["launch_history"] == [1.0, 2.0]
    assert stored["task_reclaim_counts"] == {
        "251": {"count": 3, "last_reclaimed_at": 2.0}
    }
    assert stored["pending_lock_release_notices"] == [251]
    assert stored["extension_data"] == {"keep": ["verbatim", 7]}
    assert len(stored["completed_worktrees"]) == 1
    assert stored["completed_worktrees"][0]["commit_sha"] == head_sha
    assert forge.reachability_calls == [(head_sha, "main")]

    claim_after_gc = claim_task(claim_request, forge=claim_forge, cwd=repo)
    assert claim_after_gc.success is True
    assert claim_after_gc.stage == ClaimStage.VALIDATING

    second_run = run_handoff_gc(
        GcRequest(), forge_factory=lambda: pytest.fail("no target")
    )
    assert second_run.items == ()
    assert second_run.receipts == ()


def test_preview_does_not_change_state_worktree_marker_or_create_locks(
    tmp_path: Path, monkeypatch
):
    from orchestune.dispatch.gc.handoff import GcRequest
    from orchestune.dispatch.gc_service import run_handoff_gc

    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    state_path = _write_state(repo, active)
    marker_path = claim_marker_path(worktree)
    before_state = state_path.read_bytes()
    before_marker = marker_path.read_bytes()
    forge = _forge(comment, branch, _head_sha(active))
    monkeypatch.chdir(repo)

    result = run_handoff_gc(GcRequest(apply=False), forge_factory=lambda: forge)

    assert result.exit_code == 0
    assert len(result.items) == 1
    assert result.skipped == 1
    assert result.items[0].action == "would_release"
    assert result.items[0].worktree_action == "remove"
    assert result.receipts == ()
    assert state_path.read_bytes() == before_state
    assert marker_path.read_bytes() == before_marker
    assert worktree.exists()
    assert not (repo / "run_state.lock").exists()
    assert not marker_path.with_suffix(".lock").exists()


@pytest.mark.parametrize(
    ("result_name", "reason"),
    [("not-needed", None), ("blocked", "review_timeout")],
)
@pytest.mark.parametrize("dirty", [False, True])
def test_apply_releases_non_done_without_receipt_and_preserves_dirty_worktree(
    tmp_path: Path, monkeypatch, result_name: str, reason: str | None, dirty: bool
):
    from orchestune.dispatch.gc.handoff import GcRequest
    from orchestune.dispatch.gc_service import run_handoff_gc

    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    head_sha = _head_sha(active)
    comment["body"] = OutcomeRecord(
        result=result_name,
        issue=250,
        reason=reason,
        claim_id=active.claim_id,
        head_sha=head_sha,
        completion_id=active.completion_id,
    ).render()
    active.completion_result = result_name
    active.completion_payload = {"outcome": comment["body"]}
    if dirty:
        (worktree / "README.md").write_text("keep this work\n", encoding="utf-8")
    state_path = _write_state(repo, active)
    forge = _forge(comment, branch, head_sha)
    monkeypatch.chdir(repo)

    result = run_handoff_gc(GcRequest(), forge_factory=lambda: forge)

    assert result.exit_code == 0
    assert result.items[0].action == "released"
    assert result.items[0].worktree_action == ("retain" if dirty else "remove")
    assert result.receipts == ()
    assert worktree.exists() is dirty
    assert claim_marker_path(worktree).exists() is dirty
    stored = json.loads(state_path.read_text(encoding="utf-8"))
    assert "250" not in stored["active_worktrees"]
    assert stored["completed_worktrees"] == []
    assert forge.pr_calls == []


def test_save_failure_keeps_reservation_and_mints_no_receipt_then_recovers(
    tmp_path: Path, monkeypatch
):
    from unittest.mock import patch

    from orchestune.dispatch.gc.handoff import GcRequest
    from orchestune.dispatch.gc_service import run_handoff_gc

    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    state_path = _write_state(repo, active)
    original = state_path.read_bytes()
    forge = _forge(comment, branch, _head_sha(active))
    monkeypatch.chdir(repo)

    with patch(
        "orchestune.dispatch.gc_service.write_json_atomic",
        side_effect=OSError("disk full"),
    ):
        failed = run_handoff_gc(GcRequest(), forge_factory=lambda: forge)

    assert failed.exit_code == 1
    assert failed.items[0].action == "failed"
    assert failed.items[0].reason == "state_save_failed"
    assert failed.receipts == ()
    assert state_path.read_bytes() == original
    assert not worktree.exists()

    recovered = run_handoff_gc(GcRequest(), forge_factory=lambda: forge)
    assert recovered.exit_code == 0
    assert recovered.items[0].action == "released"
    assert len(recovered.receipts) == 1
    stored = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(stored["completed_worktrees"]) == 1


def test_unmerged_pr_is_held_and_run_state_is_unchanged(tmp_path: Path, monkeypatch):
    from orchestune.dispatch.gc.handoff import GcRequest
    from orchestune.dispatch.gc_service import run_handoff_gc

    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    state_path = _write_state(repo, active)
    before = state_path.read_bytes()
    forge = _forge(comment, branch, _head_sha(active))
    forge.pr = PrRecord(
        number=125,
        head_ref=branch,
        changed_files=(),
        state="OPEN",
        base_ref="main",
        is_cross_repository=False,
    )
    monkeypatch.chdir(repo)

    result = run_handoff_gc(GcRequest(), forge_factory=lambda: forge)

    assert result.exit_code == 0
    assert result.items[0].action == "held"
    assert result.items[0].reason == "pr_not_merged"
    assert result.receipts == ()
    assert state_path.read_bytes() == before
    assert worktree.exists()


def test_preview_of_invalid_state_does_not_quarantine_or_rewrite_it(
    tmp_path: Path, monkeypatch
):
    from orchestune.dispatch.gc.handoff import GcRequest
    from orchestune.dispatch.gc_service import run_handoff_gc

    repo, _, _ = _create_repo(tmp_path)
    state_path = repo / "run_state.json"
    state_path.write_bytes(b"{")
    before = state_path.read_bytes()
    monkeypatch.chdir(repo)

    result = run_handoff_gc(
        GcRequest(apply=False),
        forge_factory=lambda: pytest.fail("invalid state must not use Forge"),
    )

    assert result.exit_code == 1
    assert state_path.read_bytes() == before
    assert not list(repo.glob("run_state.json.*"))
    assert not (repo / "run_state.lock").exists()


@pytest.mark.parametrize(
    "state",
    [[], {"active_worktrees": {}, "completed_worktrees": {"bad": True}}],
)
def test_preview_of_invalid_state_schema_is_read_only(
    tmp_path: Path, monkeypatch, state
):
    from orchestune.dispatch.gc.handoff import GcRequest
    from orchestune.dispatch.gc_service import run_handoff_gc

    repo, _, _ = _create_repo(tmp_path)
    state_path = repo / "run_state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = state_path.read_bytes()
    monkeypatch.chdir(repo)

    result = run_handoff_gc(
        GcRequest(apply=False),
        forge_factory=lambda: pytest.fail("invalid state must not use Forge"),
    )

    assert result.exit_code == 1
    assert state_path.read_bytes() == before
    assert not (repo / "run_state.lock").exists()


def test_apply_releases_done_when_worktree_was_already_removed(
    tmp_path: Path, monkeypatch
):
    from orchestune.dispatch.gc.handoff import GcRequest
    from orchestune.dispatch.gc_service import run_handoff_gc

    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    state_path = _write_state(repo, active)
    head_sha = _head_sha(active)
    run_git(["worktree", "remove", str(worktree)], cwd=repo)
    marker_path = claim_marker_path(worktree)
    assert marker_path.exists()
    forge = _forge(comment, branch, head_sha)
    monkeypatch.chdir(repo)

    result = run_handoff_gc(GcRequest(), forge_factory=lambda: forge)

    assert result.exit_code == 0
    assert result.items[0].action == "released"
    assert result.items[0].worktree_action == "absent"
    assert [receipt.issue_number for receipt in result.receipts] == [250]
    assert not marker_path.exists()
    stored = json.loads(state_path.read_text(encoding="utf-8"))
    assert "250" not in stored["active_worktrees"]
    assert len(stored["completed_worktrees"]) == 1
    assert stored["completed_worktrees"][0]["commit_sha"] == head_sha


def test_preview_with_missing_state_is_a_read_only_noop(tmp_path: Path, monkeypatch):
    from orchestune.dispatch.gc.handoff import GcRequest
    from orchestune.dispatch.gc_service import run_handoff_gc

    repo, _, _ = _create_repo(tmp_path)
    state_path = repo / "missing.json"
    monkeypatch.chdir(repo)

    result = run_handoff_gc(
        GcRequest(state_path=Path("missing.json"), apply=False),
        forge_factory=lambda: pytest.fail("missing state has no target"),
    )

    assert result.exit_code == 0
    assert result.items == ()
    assert not state_path.exists()
    assert not (repo / "missing.lock").exists()


def test_state_lock_contention_returns_exit_22_without_changes(
    tmp_path: Path, monkeypatch
):
    from unittest.mock import patch

    from orchestune.dispatch.gc.handoff import GcRequest
    from orchestune.dispatch.gc_service import run_handoff_gc

    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    state_path = _write_state(repo, active)
    before = state_path.read_bytes()
    forge = _forge(comment, branch, _head_sha(active))
    monkeypatch.chdir(repo)

    with patch(
        "orchestune.dispatch.gc_service.run_state_lock",
        side_effect=RuntimeError("Another instance is already running"),
    ):
        result = run_handoff_gc(GcRequest(), forge_factory=lambda: forge)

    assert result.exit_code == 22
    assert state_path.read_bytes() == before
    assert worktree.exists()
    assert result.receipts == ()


def test_claim_lock_contention_returns_exit_22_without_changes(
    tmp_path: Path, monkeypatch
):
    from unittest.mock import patch

    from orchestune.dispatch.gc.handoff import GcRequest
    from orchestune.dispatch.gc_service import run_handoff_gc
    from orchestune.infra.process_utils import FileLockContentionError

    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    state_path = _write_state(repo, active)
    before = state_path.read_bytes()
    forge = _forge(comment, branch, _head_sha(active))
    monkeypatch.chdir(repo)

    with patch(
        "orchestune.dispatch.gc_service.file_lock",
        side_effect=FileLockContentionError("busy"),
    ):
        result = run_handoff_gc(GcRequest(), forge_factory=lambda: forge)

    assert result.exit_code == 22
    assert result.items[0].reason == "lock_timeout"
    assert state_path.read_bytes() == before
    assert worktree.exists()
    assert result.receipts == ()


def test_worktree_removal_failure_keeps_reservation_and_no_receipt(
    tmp_path: Path, monkeypatch
):
    from types import SimpleNamespace
    from unittest.mock import patch

    from orchestune.dispatch.gc.handoff import GcRequest
    from orchestune.dispatch.gc_service import run_handoff_gc

    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    state_path = _write_state(repo, active)
    before = state_path.read_bytes()
    forge = _forge(comment, branch, _head_sha(active))
    monkeypatch.chdir(repo)

    with patch(
        "orchestune.dispatch.gc_service.remove_verified_worktree",
        return_value=SimpleNamespace(success=False, removed=False, error="busy"),
    ):
        result = run_handoff_gc(GcRequest(), forge_factory=lambda: forge)

    assert result.exit_code == 1
    assert result.items[0].reason == "worktree_remove_failed"
    assert state_path.read_bytes() == before
    assert worktree.exists()
    assert result.receipts == ()


def test_no_interactive_handoff_targets_do_not_construct_forge_or_rewrite_state(
    tmp_path: Path, monkeypatch
):
    from orchestune.dispatch.gc.handoff import GcRequest
    from orchestune.dispatch.gc_service import run_handoff_gc

    repo, worktree, branch = _create_repo(tmp_path)
    active, _ = _make_active(repo, worktree, branch)
    active.completion_handoff_ready = False
    active.completion_stage = "journaling"
    state_path = _write_state(repo, active)
    before = state_path.read_bytes()
    monkeypatch.chdir(repo)

    result = run_handoff_gc(GcRequest(), forge_factory=lambda: pytest.fail("no target"))

    assert result.exit_code == 0
    assert result.items == ()
    assert result.skipped == 2
    assert state_path.read_bytes() == before
    assert worktree.exists()

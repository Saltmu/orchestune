"""#935: prepare_task_worktree / rollback_task_worktreeの所有権テスト群。

`tests/test_dispatch_worktree.py`の肥大化を避けるため、claim所有権の
確認・拒否・ロールバックに関するテストのみをこのファイルに切り出す。
"""

import json
import subprocess
from unittest.mock import patch

import pytest

from orchestune.dispatch.worktree import (
    WorktreePreparation,
    _claim_marker_path,
    prepare_task_worktree,
    rollback_task_worktree,
)
from orchestune.infra.git_cli import GitResult


def _init_repo(path):
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=path, check=True
    )
    (path / "README.md").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=path, check=True)


class TestPrepareTaskWorktree:
    """#935: 所有権を確認する安全なworktree準備関数。"""

    def test_creates_fresh_worktree_and_branch_when_nothing_exists(
        self, tmp_path, monkeypatch
    ):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _init_repo(repo_dir)
        monkeypatch.chdir(repo_dir)
        worktree_root = tmp_path / "worktrees"

        result = prepare_task_worktree(
            "claim/issue-1-task-1", worktree_root, None, "claim-abc"
        )

        assert result.accepted is True
        assert result.created is True
        assert result.branch_created is True
        assert result.base_sha
        assert result.worktree_path.exists()
        marker = json.loads(_claim_marker_path(result.worktree_path).read_text())
        assert marker == {
            "claim_id": "claim-abc",
            "branch": "claim/issue-1-task-1",
            "base_sha": result.base_sha,
            "branch_created": True,
        }

    def test_reuses_worktree_owned_by_same_claim_without_touching_filesystem(
        self, tmp_path, monkeypatch
    ):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _init_repo(repo_dir)
        monkeypatch.chdir(repo_dir)
        worktree_root = tmp_path / "worktrees"

        first = prepare_task_worktree(
            "claim/issue-2-task-2", worktree_root, None, "claim-xyz"
        )
        marker_path = _claim_marker_path(first.worktree_path)
        marker_bytes_before = marker_path.read_bytes()
        # 未コミット作業を残し、再開がそれを一切検査・削除しないことを確認する
        (first.worktree_path / "scratch.txt").write_text("in progress")

        second = prepare_task_worktree(
            "claim/issue-2-task-2", worktree_root, None, "claim-xyz"
        )

        assert second.accepted is True
        assert second.created is False
        assert second.base_sha == first.base_sha
        assert (first.worktree_path / "scratch.txt").exists()
        assert marker_path.read_bytes() == marker_bytes_before

    def test_rejects_existing_worktree_owned_by_different_claim(self, tmp_path):
        worktree_root = tmp_path / "worktrees"
        worktree_root.mkdir()
        worktree_path = worktree_root / "claim-issue-3-task-3"
        worktree_path.mkdir()
        (worktree_path / "in_progress.txt").write_text("agent work")
        _claim_marker_path(worktree_path).write_text(
            json.dumps(
                {
                    "claim_id": "someone-else",
                    "branch": "claim/issue-3-task-3",
                    "base_sha": "deadbeef",
                    "branch_created": True,
                }
            )
        )

        with patch(
            "orchestune.dispatch.worktree.run_git", autospec=True
        ) as mock_run_git:
            result = prepare_task_worktree(
                "claim/issue-3-task-3", worktree_root, None, "claim-mine"
            )

        assert result.accepted is False
        assert result.rejection_reason == "claim_id_mismatch"
        mock_run_git.assert_not_called()
        assert worktree_path.exists()
        assert (worktree_path / "in_progress.txt").exists()

    def test_rejects_unclaimed_existing_worktree_without_deleting(self, tmp_path):
        worktree_root = tmp_path / "worktrees"
        worktree_path = worktree_root / "claim-issue-4-task-4"
        worktree_path.mkdir(parents=True)
        (worktree_path / "in_progress.txt").write_text("agent work")

        with patch(
            "orchestune.dispatch.worktree.run_git", autospec=True
        ) as mock_run_git:
            result = prepare_task_worktree(
                "claim/issue-4-task-4", worktree_root, None, "claim-mine"
            )

        assert result.accepted is False
        assert result.rejection_reason == "unclaimed_existing_worktree"
        mock_run_git.assert_not_called()
        assert (worktree_path / "in_progress.txt").exists()

    def test_rejects_unclaimed_existing_branch_without_reusing_it(self, tmp_path):
        worktree_root = tmp_path / "worktrees"
        with (
            patch(
                "orchestune.dispatch.worktree._branch_exists",
                autospec=True,
                return_value=True,
            ),
            patch(
                "orchestune.dispatch.worktree.run_git", autospec=True
            ) as mock_run_git,
        ):
            result = prepare_task_worktree(
                "claim/issue-5-task-5", worktree_root, None, "claim-mine"
            )

        assert result.accepted is False
        assert result.rejection_reason == "unclaimed_existing_branch"
        mock_run_git.assert_not_called()

    def test_recreates_worktree_from_owned_branch_after_directory_removed(
        self, tmp_path, monkeypatch
    ):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _init_repo(repo_dir)
        monkeypatch.chdir(repo_dir)
        worktree_root = tmp_path / "worktrees"

        first = prepare_task_worktree(
            "claim/issue-6-task-6", worktree_root, None, "claim-resume"
        )
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(first.worktree_path)],
            cwd=repo_dir,
            check=True,
        )
        assert not first.worktree_path.exists()

        second = prepare_task_worktree(
            "claim/issue-6-task-6", worktree_root, None, "claim-resume"
        )

        assert second.accepted is True
        assert second.created is True
        assert second.branch_created is False
        assert second.base_sha == first.base_sha
        assert second.worktree_path.exists()

    def test_allow_force_preserves_legacy_force_cleanup_semantics(self, tmp_path):
        worktree_root = tmp_path / "worktrees"
        worktree_path = worktree_root / "claim-issue-7-task-7"
        worktree_path.mkdir(parents=True)

        with (
            patch(
                "orchestune.dispatch.worktree.dispatch_gc.backup_wip_commit",
                autospec=True,
                return_value=None,
            ) as mock_backup,
            patch(
                "orchestune.dispatch.worktree._branch_exists",
                autospec=True,
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.worktree.run_git", autospec=True
            ) as mock_run_git,
        ):
            mock_run_git.return_value = GitResult(
                returncode=0, stdout="deadbeefcafe\n", stderr=""
            )
            result = prepare_task_worktree(
                "claim/issue-7-task-7",
                worktree_root,
                None,
                "dispatch:7",
                allow_force=True,
            )

        mock_backup.assert_called_once()
        assert result.accepted is True
        assert result.created is True
        assert result.branch_created is True
        # force経路はdispatch専用の互換パスであり、base_shaや所有権マーカーは
        # 記録しない（`_create_worktree`をまるごとpatchする既存テスト群が
        # 実worktreeを前提としない追加git呼び出しと衝突しないようにするため）。
        assert result.base_sha is None
        assert not _claim_marker_path(worktree_path).exists()

    def test_invalid_branch_name_raises_value_error(self, tmp_path):
        with pytest.raises(ValueError, match="ブランチ名が不正です"):
            prepare_task_worktree("--evil", tmp_path / "worktrees", None, "claim-x")


class TestRollbackTaskWorktree:
    """#935: prepare_task_worktreeが新規作成したworktree/branchの後始末。"""

    def test_noop_when_preparation_was_rejected(self, tmp_path):
        preparation = WorktreePreparation(
            worktree_path=tmp_path / "unused",
            branch="b",
            accepted=False,
        )
        assert rollback_task_worktree(preparation, "claim-x") is None

    def test_refuses_to_roll_back_a_reused_worktree(self, tmp_path):
        preparation = WorktreePreparation(
            worktree_path=tmp_path / "wt",
            branch="b",
            accepted=True,
            created=False,
            base_sha="abc",
        )
        assert (
            rollback_task_worktree(preparation, "claim-x")
            == "reused_existing_worktree_not_rolled_back"
        )

    def test_withholds_cleanup_when_ownership_marker_missing(self, tmp_path):
        worktree_path = tmp_path / "worktrees" / "b"
        worktree_path.mkdir(parents=True)
        preparation = WorktreePreparation(
            worktree_path=worktree_path,
            branch="b",
            accepted=True,
            created=True,
            branch_created=True,
            base_sha="abc",
        )

        result = rollback_task_worktree(preparation, "claim-x")

        assert result == "ownership_marker_missing_or_reassigned"
        assert worktree_path.exists()

    def test_withholds_cleanup_when_reassigned_to_another_claim(self, tmp_path):
        worktree_path = tmp_path / "worktrees" / "b"
        worktree_path.mkdir(parents=True)
        _claim_marker_path(worktree_path).write_text(
            json.dumps(
                {
                    "claim_id": "someone-else",
                    "branch": "b",
                    "base_sha": "abc",
                    "branch_created": True,
                }
            )
        )
        preparation = WorktreePreparation(
            worktree_path=worktree_path,
            branch="b",
            accepted=True,
            created=True,
            branch_created=True,
            base_sha="abc",
        )

        result = rollback_task_worktree(preparation, "claim-x")

        assert result == "ownership_marker_missing_or_reassigned"
        assert worktree_path.exists()

    def test_withholds_cleanup_when_worktree_is_dirty(self, tmp_path, monkeypatch):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _init_repo(repo_dir)
        monkeypatch.chdir(repo_dir)
        worktree_root = tmp_path / "worktrees"
        preparation = prepare_task_worktree(
            "claim/issue-8-task-8", worktree_root, None, "claim-8"
        )
        (preparation.worktree_path / "dirty.txt").write_text("wip")

        result = rollback_task_worktree(preparation, "claim-8")

        assert result == "worktree_dirty"
        assert preparation.worktree_path.exists()

    def test_withholds_cleanup_when_advanced_beyond_base_sha(
        self, tmp_path, monkeypatch
    ):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _init_repo(repo_dir)
        monkeypatch.chdir(repo_dir)
        worktree_root = tmp_path / "worktrees"
        preparation = prepare_task_worktree(
            "claim/issue-9-task-9", worktree_root, None, "claim-9"
        )
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "progress"],
            cwd=preparation.worktree_path,
            check=True,
        )

        result = rollback_task_worktree(preparation, "claim-9")

        assert result == "advanced_beyond_base_sha"
        assert preparation.worktree_path.exists()

    def test_rolls_back_newly_created_branch_and_worktree(self, tmp_path, monkeypatch):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _init_repo(repo_dir)
        monkeypatch.chdir(repo_dir)
        worktree_root = tmp_path / "worktrees"
        preparation = prepare_task_worktree(
            "claim/issue-10-task-10", worktree_root, None, "claim-10"
        )

        result = rollback_task_worktree(preparation, "claim-10")

        assert result is None
        assert not preparation.worktree_path.exists()
        assert not _claim_marker_path(preparation.worktree_path).exists()
        branches = subprocess.run(
            ["git", "branch", "--list", "claim/issue-10-task-10"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert branches.strip() == ""

    def test_rolls_back_worktree_only_when_branch_preexisted(
        self, tmp_path, monkeypatch
    ):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _init_repo(repo_dir)
        monkeypatch.chdir(repo_dir)
        worktree_root = tmp_path / "worktrees"
        first = prepare_task_worktree(
            "claim/issue-11-task-11", worktree_root, None, "claim-11"
        )
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(first.worktree_path)],
            cwd=repo_dir,
            check=True,
        )
        second = prepare_task_worktree(
            "claim/issue-11-task-11", worktree_root, None, "claim-11"
        )
        assert second.created is True
        assert second.branch_created is False

        result = rollback_task_worktree(second, "claim-11")

        assert result is None
        assert not second.worktree_path.exists()
        branches = subprocess.run(
            ["git", "branch", "--list", "claim/issue-11-task-11"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert "claim/issue-11-task-11" in branches

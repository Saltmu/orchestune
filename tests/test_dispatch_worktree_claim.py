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
    _claim_lock_path,
    _claim_marker_path,
    prepare_task_worktree,
    rollback_task_worktree,
)
from orchestune.infra.git_cli import GitResult
from orchestune.infra.git_cli import run_git as real_run_git
from orchestune.infra.process_utils import FileLock, FileLockContentionError


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

    def test_allow_force_invalidates_stale_marker_from_previous_claim(
        self, tmp_path, monkeypatch
    ):
        """#935レビュー対応(P1): forceで奪取した後、旧claimがマーカー一致を
        根拠に自分のものだと誤認してresumeできてはならない。"""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _init_repo(repo_dir)
        monkeypatch.chdir(repo_dir)
        worktree_root = tmp_path / "worktrees"

        original = prepare_task_worktree(
            "claim/issue-12-task-12", worktree_root, None, "claim-original"
        )
        assert _claim_marker_path(original.worktree_path).exists()

        result = prepare_task_worktree(
            "claim/issue-12-task-12",
            worktree_root,
            None,
            "dispatch:12",
            allow_force=True,
        )

        assert result.accepted is True
        assert not _claim_marker_path(result.worktree_path).exists()

        stale_attempt = prepare_task_worktree(
            "claim/issue-12-task-12", worktree_root, None, "claim-original"
        )
        assert stale_attempt.accepted is False
        assert stale_attempt.rejection_reason == "unclaimed_existing_worktree"

    def test_rejects_resume_when_directory_is_not_the_claimed_branch_checkout(
        self, tmp_path, monkeypatch
    ):
        """#935レビュー対応(P2): マーカーは一致していても、実際にそのbranchを
        checkoutした有効なworktreeでなければresumeを受理しない。"""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _init_repo(repo_dir)
        monkeypatch.chdir(repo_dir)
        worktree_root = tmp_path / "worktrees"

        preparation = prepare_task_worktree(
            "claim/issue-15-task-15", worktree_root, None, "claim-15"
        )
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(preparation.worktree_path)],
            cwd=repo_dir,
            check=True,
        )
        # markerは残るが、同じpathへ手動で無関係なディレクトリが作られたケースを模す
        preparation.worktree_path.mkdir()
        (preparation.worktree_path / "unrelated.txt").write_text("not a worktree")

        result = prepare_task_worktree(
            "claim/issue-15-task-15", worktree_root, None, "claim-15"
        )

        assert result.accepted is False
        assert result.rejection_reason == "stale_marker_unverified_worktree"

    def test_rejects_resume_when_directory_is_unrelated_repo_with_same_branch_name(
        self, tmp_path, monkeypatch
    ):
        """#935レビュー対応(P2, round2): symbolic-refだけの検証では、同名branchを
        持つ無関係な別リポジトリを見分けられない。共有git-dirの一致も確認する。"""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _init_repo(repo_dir)
        monkeypatch.chdir(repo_dir)
        worktree_root = tmp_path / "worktrees"

        preparation = prepare_task_worktree(
            "claim/issue-16-task-16", worktree_root, None, "claim-16"
        )
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(preparation.worktree_path)],
            cwd=repo_dir,
            check=True,
        )
        # markerは残るが、同じpathへ「同名branchを持つ別リポジトリ」が
        # 後から作られたケースを模す
        unrelated_repo = preparation.worktree_path
        unrelated_repo.mkdir()
        _init_repo(unrelated_repo)
        subprocess.run(
            ["git", "checkout", "-b", "claim/issue-16-task-16"],
            cwd=unrelated_repo,
            check=True,
        )

        result = prepare_task_worktree(
            "claim/issue-16-task-16", worktree_root, None, "claim-16"
        )

        assert result.accepted is False
        assert result.rejection_reason == "stale_marker_unverified_worktree"

    def test_rejects_resume_when_path_is_subdirectory_of_another_checkout(
        self, tmp_path, monkeypatch
    ):
        """#935レビュー対応(P2, round3): branch名とgit-common-dirの一致だけでは、
        このリポジトリの別checkoutの単なるサブディレクトリ（それ自体は独立した
        worktree登録を持たない）を誤って受理してしまう。candidate pathが
        自分自身のcheckoutのtoplevelであることも確認する。"""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _init_repo(repo_dir)
        monkeypatch.chdir(repo_dir)

        # 先に「別のworktree」を用意し、claimed branchへswitchしておく
        other_checkout = tmp_path / "other-checkout"
        subprocess.run(
            [
                "git",
                "worktree",
                "add",
                "-b",
                "claim/issue-21-task-21",
                str(other_checkout),
            ],
            cwd=repo_dir,
            check=True,
        )

        # stale markerが指すpathは、その別checkout内部の単なるサブ
        # ディレクトリであり、それ自体は独立したworktree registrationを
        # 持たない。
        worktree_root = other_checkout / "worktrees"
        worktree_root.mkdir()
        stale_slug_dir = worktree_root / "claim-issue-21-task-21"
        stale_slug_dir.mkdir()
        _claim_marker_path(stale_slug_dir).write_text(
            json.dumps(
                {
                    "claim_id": "claim-21",
                    "branch": "claim/issue-21-task-21",
                    "base_sha": "irrelevant",
                    "branch_created": True,
                }
            )
        )

        result = prepare_task_worktree(
            "claim/issue-21-task-21", worktree_root, None, "claim-21"
        )

        assert result.accepted is False
        assert result.rejection_reason == "stale_marker_unverified_worktree"

    def test_prepare_and_rollback_share_a_mutual_exclusion_lock(
        self, tmp_path, monkeypatch
    ):
        """#935レビュー対応(P1, round2): force奪取とrollbackが同一branchに
        対して同じロックを取ることを、ロック競合の検出で直接確認する。
        これにより、奪取の「作成→旧マーカー無効化」の中間状態を並行する
        rollbackが観測できてしまうTOCTOUが塞がれていることを保証する。"""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _init_repo(repo_dir)
        monkeypatch.chdir(repo_dir)
        worktree_root = tmp_path / "worktrees"
        preparation = prepare_task_worktree(
            "claim/issue-17-task-17", worktree_root, None, "claim-17"
        )

        held_lock = FileLock(_claim_lock_path(preparation.worktree_path))
        held_lock.acquire()
        try:
            with pytest.raises(FileLockContentionError):
                rollback_task_worktree(preparation, "claim-17")
            with pytest.raises(FileLockContentionError):
                prepare_task_worktree(
                    "claim/issue-17-task-17",
                    worktree_root,
                    None,
                    "dispatch:17",
                    allow_force=True,
                )
        finally:
            held_lock.release()


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

    def test_withholds_marker_removal_when_worktree_removal_fails(
        self, tmp_path, monkeypatch
    ):
        """#935レビュー対応(P2): `_cleanup_failed_worktree`が実際には削除
        できなかった場合、マーカーを消して所有権を手放してはならない。"""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _init_repo(repo_dir)
        monkeypatch.chdir(repo_dir)
        worktree_root = tmp_path / "worktrees"
        preparation = prepare_task_worktree(
            "claim/issue-13-task-13", worktree_root, None, "claim-13"
        )

        with patch(
            "orchestune.dispatch.worktree._cleanup_failed_worktree", autospec=True
        ):
            result = rollback_task_worktree(preparation, "claim-13")

        assert result == "worktree_removal_failed"
        assert preparation.worktree_path.exists()
        assert _claim_marker_path(preparation.worktree_path).exists()

    def test_withholds_marker_removal_when_branch_deletion_fails(
        self, tmp_path, monkeypatch
    ):
        """#935レビュー対応(P2): branch削除が失敗した場合、worktree自体は
        削除済みでもマーカーを消してはならない（他所有者なし判定に必要な
        情報を失い、以後の復旧診断ができなくなるため）。"""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _init_repo(repo_dir)
        monkeypatch.chdir(repo_dir)
        worktree_root = tmp_path / "worktrees"
        preparation = prepare_task_worktree(
            "claim/issue-14-task-14", worktree_root, None, "claim-14"
        )
        assert preparation.branch_created is True

        def fake_run_git(args, **kwargs):
            if args[:2] == ["branch", "-D"]:
                return GitResult(
                    returncode=1, stdout="", stderr="error: branch is checked out"
                )
            return real_run_git(args, **kwargs)

        with patch("orchestune.dispatch.worktree.run_git", side_effect=fake_run_git):
            result = rollback_task_worktree(preparation, "claim-14")

        assert result == "branch_deletion_failed"
        assert not preparation.worktree_path.exists()
        assert _claim_marker_path(preparation.worktree_path).exists()

    def test_retrying_rollback_after_branch_deletion_failure_completes(
        self, tmp_path, monkeypatch
    ):
        """#935レビュー対応(P2, round2): worktree削除は既に完了済みの状態で
        再試行した場合、dirty/base_sha確認がworktree不在で誤ってブロックせず、
        branch削除を再試行して最終的に完了できる。"""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _init_repo(repo_dir)
        monkeypatch.chdir(repo_dir)
        worktree_root = tmp_path / "worktrees"
        preparation = prepare_task_worktree(
            "claim/issue-18-task-18", worktree_root, None, "claim-18"
        )

        def failing_branch_delete(args, **kwargs):
            if args[:2] == ["branch", "-D"]:
                return GitResult(
                    returncode=1, stdout="", stderr="error: branch is checked out"
                )
            return real_run_git(args, **kwargs)

        with patch(
            "orchestune.dispatch.worktree.run_git", side_effect=failing_branch_delete
        ):
            first_attempt = rollback_task_worktree(preparation, "claim-18")
        assert first_attempt == "branch_deletion_failed"
        assert not preparation.worktree_path.exists()

        second_attempt = rollback_task_worktree(preparation, "claim-18")

        assert second_attempt is None
        assert not _claim_marker_path(preparation.worktree_path).exists()
        branches = subprocess.run(
            ["git", "branch", "--list", "claim/issue-18-task-18"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert branches.strip() == ""

    def test_retry_after_branch_deletion_failure_rejects_if_branch_advanced_meanwhile(
        self, tmp_path, monkeypatch
    ):
        """#935レビュー対応(P1, round3): worktree不在で再試行する経路でも、
        branch自体がbase_shaから進んでいれば削除を拒否しなければならない。
        そうしないと、再試行までの間に積まれた正当なコミットごと
        `git branch -D`で失ってしまう。"""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _init_repo(repo_dir)
        monkeypatch.chdir(repo_dir)
        worktree_root = tmp_path / "worktrees"
        preparation = prepare_task_worktree(
            "claim/issue-19-task-19", worktree_root, None, "claim-19"
        )

        def failing_branch_delete(args, **kwargs):
            if args[:2] == ["branch", "-D"]:
                return GitResult(
                    returncode=1, stdout="", stderr="error: branch is checked out"
                )
            return real_run_git(args, **kwargs)

        with patch(
            "orchestune.dispatch.worktree.run_git", side_effect=failing_branch_delete
        ):
            first_attempt = rollback_task_worktree(preparation, "claim-19")
        assert first_attempt == "branch_deletion_failed"
        assert not preparation.worktree_path.exists()

        # worktreeが無い間に、別のcheckoutからbranchへ新しいコミットを積む
        other_worktree = tmp_path / "other"
        subprocess.run(
            ["git", "worktree", "add", str(other_worktree), "claim/issue-19-task-19"],
            cwd=repo_dir,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "progress"],
            cwd=other_worktree,
            check=True,
        )
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(other_worktree)],
            cwd=repo_dir,
            check=True,
        )

        second_attempt = rollback_task_worktree(preparation, "claim-19")

        assert second_attempt == "advanced_beyond_base_sha"
        branches = subprocess.run(
            ["git", "branch", "--list", "claim/issue-19-task-19"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert "claim/issue-19-task-19" in branches

    def test_rollback_refuses_deletion_when_worktree_identity_unverified(
        self, tmp_path, monkeypatch
    ):
        """#935レビュー対応(P2, round3): dirty/base_sha確認だけでは、markerの
        pathへ後から置かれた「branch/base_shaまで完全一致する独立クローン」を
        見分けられない。削除前にworktreeの身元も確認しなければならない。"""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _init_repo(repo_dir)
        monkeypatch.chdir(repo_dir)
        worktree_root = tmp_path / "worktrees"
        preparation = prepare_task_worktree(
            "claim/issue-20-task-20", worktree_root, None, "claim-20"
        )
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(preparation.worktree_path)],
            cwd=repo_dir,
            check=True,
        )

        # markerは残るが、同じpathへ「branch/base_shaまで一致する独立クローン」
        # が後から置かれたケースを模す（clone元と同一の履歴なのでSHAも一致する）
        subprocess.run(
            [
                "git",
                "clone",
                "--branch",
                "claim/issue-20-task-20",
                str(repo_dir),
                str(preparation.worktree_path),
            ],
            check=True,
            capture_output=True,
        )

        result = rollback_task_worktree(preparation, "claim-20")

        assert result == "stale_marker_unverified_worktree"
        assert preparation.worktree_path.exists()

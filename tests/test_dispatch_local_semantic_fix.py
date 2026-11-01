"""Tests for orchestune.dispatch.local_semantic_fix."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestune.dispatch.gc.git import WorktreeRemovalEvaluation
from orchestune.dispatch.local_semantic_fix import (
    build_local_semantic_fix_cmd,
    cleanup_semantic_fix_worktree,
    create_semantic_fix_worktree,
)
from orchestune.dispatch.targets import LocalProcessDispatchTarget
from orchestune.targets.contracts import DispatchHandle


class TestBuildLocalSemanticFixCmd:
    def test_claude_cli_command(self) -> None:
        cmd = build_local_semantic_fix_cmd("claude-cli", prompt="Fix this finding")
        assert cmd is not None
        assert cmd[0] == "claude"
        assert cmd[1] == "-p"
        assert cmd[2] == "Fix this finding"
        assert "--permission-mode" in cmd
        assert "bypassPermissions" in cmd
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert "--verbose" in cmd

    def test_codex_cli_command(self) -> None:
        cmd = build_local_semantic_fix_cmd("codex-cli", prompt="Fix this finding")
        assert cmd is not None
        assert cmd[0] == "codex"
        assert cmd[1] == "exec"
        assert cmd[2] == "Fix this finding"
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd

    def test_agy_cli_command(self) -> None:
        cmd = build_local_semantic_fix_cmd("agy-cli", prompt="Fix this finding")
        assert cmd is not None
        assert cmd[0] == "agy"
        assert cmd[1] == "-p"
        assert cmd[2] == "Fix this finding"
        assert "--dangerously-skip-permissions" in cmd

    def test_custom_local_cmd_unsupported(self) -> None:
        cmd = build_local_semantic_fix_cmd(
            "claude-cli",
            prompt="Fix this finding",
            custom_local_cmd="my-custom-runner {prompt}",
        )
        assert cmd is None

    def test_unknown_target_unsupported(self) -> None:
        cmd = build_local_semantic_fix_cmd("unknown-target", prompt="Fix this finding")
        assert cmd is None


class TestSemanticFixWorktreeLifecycle:
    def test_create_semantic_fix_worktree(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree_root = tmp_path / "worktrees"
        worktree_root.mkdir()

        with patch("orchestune.dispatch.local_semantic_fix.run_git") as mock_git:
            mock_git.return_value = MagicMock(returncode=0)
            wt_path = create_semantic_fix_worktree(
                repo_root=repo_root,
                worktree_root=worktree_root,
                operation_id="op-12345",
                input_sha="deadbeef" * 5,
            )
            assert wt_path == worktree_root / "semantic-fix-op-12345"
            mock_git.assert_called_once_with(
                [
                    "worktree",
                    "add",
                    "--detach",
                    str(wt_path),
                    "deadbeef" * 5,
                ],
                cwd=repo_root,
                check=True,
            )

    def test_cleanup_semantic_fix_worktree_success(self, tmp_path: Path) -> None:
        wt_path = tmp_path / "worktrees" / "semantic-fix-op-12345"
        repo_root = tmp_path / "repo"

        mock_eval = MagicMock(return_value=WorktreeRemovalEvaluation(can_remove=True))
        with patch("orchestune.dispatch.local_semantic_fix.run_git") as mock_git:
            mock_git.return_value = MagicMock(returncode=0)
            removed = cleanup_semantic_fix_worktree(
                wt_path, repo_root=repo_root, evaluate_fn=mock_eval
            )
            assert removed is True
            mock_eval.assert_called_once_with(
                worktree_path=wt_path, repo_root=repo_root
            )
            mock_git.assert_called_once_with(
                ["worktree", "remove", str(wt_path)], cwd=repo_root, check=False
            )

    def test_cleanup_semantic_fix_worktree_skips_when_cannot_remove(
        self, tmp_path: Path
    ) -> None:
        wt_path = tmp_path / "worktrees" / "semantic-fix-op-12345"
        repo_root = tmp_path / "repo"

        mock_eval = MagicMock(
            return_value=WorktreeRemovalEvaluation(
                can_remove=False, rejection_reason="dirty_worktree"
            )
        )
        with patch("orchestune.dispatch.local_semantic_fix.run_git") as mock_git:
            removed = cleanup_semantic_fix_worktree(
                wt_path, repo_root=repo_root, evaluate_fn=mock_eval
            )
            assert removed is False
            mock_git.assert_not_called()


class TestLocalProcessDispatchTargetLaunchSemanticFix:
    def test_delegates_to_launch_local_semantic_fix(self, tmp_path: Path) -> None:
        target = LocalProcessDispatchTarget(
            log_dir=tmp_path / "logs",
            target_name="claude-cli",
        )
        with patch(
            "orchestune.dispatch.targets.launch_local_semantic_fix",
            return_value=DispatchHandle(pid=1234, target_name="claude-cli"),
        ) as mock_launch:
            handle = target.launch_semantic_fix(
                operation_id="op-1",
                prompt="fix prompt",
                expected_head_sha="a" * 40,
                worktree_root=tmp_path / "worktrees",
                repo_root=tmp_path / "repo",
            )
            assert handle is not None
            assert handle.pid == 1234
            mock_launch.assert_called_once_with(
                target=target,
                operation_id="op-1",
                prompt="fix prompt",
                expected_head_sha="a" * 40,
                worktree_root=tmp_path / "worktrees",
                repo_root=tmp_path / "repo",
            )

    def test_custom_local_cmd_returns_none(self, tmp_path: Path) -> None:
        target = LocalProcessDispatchTarget(
            log_dir=tmp_path / "logs",
            local_cmd="custom runner",
            target_name="claude-cli",
        )
        handle = target.launch_semantic_fix(
            operation_id="op-1",
            prompt="fix prompt",
            expected_head_sha="a" * 40,
        )
        assert handle is None

    def test_popen_failure_cleans_up_created_worktree(self, tmp_path: Path) -> None:
        import pytest

        from orchestune.dispatch.local_semantic_fix import launch_local_semantic_fix

        target = LocalProcessDispatchTarget(
            log_dir=tmp_path / "logs",
            target_name="claude-cli",
        )
        with (
            patch(
                "orchestune.dispatch.local_semantic_fix.create_semantic_fix_worktree",
                return_value=tmp_path / "worktrees" / "semantic-fix-op-err",
            ),
            patch(
                "orchestune.dispatch.local_semantic_fix.subprocess.Popen",
                side_effect=OSError("executable not found"),
            ),
            patch(
                "orchestune.dispatch.local_semantic_fix.cleanup_semantic_fix_worktree"
            ) as mock_cleanup,
        ):
            with pytest.raises(OSError, match="executable not found"):
                launch_local_semantic_fix(
                    target=target,
                    operation_id="op-err",
                    prompt="fix prompt",
                    expected_head_sha="b" * 40,
                    repo_root=tmp_path / "repo",
                    worktree_root=tmp_path / "worktrees",
                )
            mock_cleanup.assert_called_once_with(
                tmp_path / "worktrees" / "semantic-fix-op-err",
                repo_root=tmp_path / "repo",
            )

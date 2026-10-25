"""Tests for config wizard CLI entrypoint, non-TTY handling, and exit codes."""

from pathlib import Path
from unittest.mock import patch

import pytest

from orchestune.config_wizard.cli import main, resolve_project_dir
from orchestune.config_wizard.storage import ConfigConflictError


class TestConfigWizardCLI:
    def test_resolve_project_dir_explicit(self, tmp_path: Path):
        explicit = tmp_path / "custom"
        explicit.mkdir()
        resolved = resolve_project_dir(explicit)
        assert resolved == explicit.resolve()

    def test_resolve_project_dir_explicit_not_found(self, tmp_path: Path):
        explicit = tmp_path / "nonexistent"
        with pytest.raises(SystemExit) as exc_info:
            resolve_project_dir(explicit)
        assert exc_info.value.code == 2

    def test_resolve_project_dir_walk_up_to_git_dir(self, tmp_path: Path):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        sub_dir = repo_root / "src" / "deep"
        sub_dir.mkdir(parents=True)

        assert resolve_project_dir(None, cwd=sub_dir) == repo_root.resolve()

    def test_resolve_project_dir_walk_up_to_linked_worktree_git_file(
        self, tmp_path: Path
    ):
        worktree_root = tmp_path / "worktree"
        worktree_root.mkdir()
        (worktree_root / ".git").write_text("gitdir: /path/to/main/.git/worktrees/wt\n")

        sub_dir = worktree_root / "nested"
        sub_dir.mkdir()

        assert resolve_project_dir(None, cwd=sub_dir) == worktree_root.resolve()

    def test_non_tty_exits_with_code_2(self, tmp_path: Path, capsys):
        with (
            patch("sys.stdin.isatty", return_value=False),
            patch("sys.stdout.isatty", return_value=False),
        ):
            code = main(["init", "--project-dir", str(tmp_path)])
            assert code == 2
            captured = capsys.readouterr()
            assert (
                "TTY" in captured.err
                or "tty" in captured.err.lower()
                or "対話" in captured.err
            )

    def test_help_in_non_tty_succeeds(self, capsys):
        with (
            patch("sys.stdin.isatty", return_value=False),
            patch("sys.stdout.isatty", return_value=False),
        ):
            with pytest.raises(SystemExit) as exc_info:
                main(["--help"])
            assert exc_info.value.code == 0

    def test_conflict_exits_with_code_3(self, tmp_path: Path):
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
            patch(
                "orchestune.config_wizard.cli.run_config_wizard",
                side_effect=ConfigConflictError("conflict"),
            ),
        ):
            code = main(["init", "--project-dir", str(tmp_path)])
            assert code == 3

    def test_ctrl_c_exits_with_code_130(self, tmp_path: Path):
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
            patch(
                "orchestune.config_wizard.cli.run_config_wizard",
                side_effect=KeyboardInterrupt(),
            ),
        ):
            code = main(["init", "--project-dir", str(tmp_path)])
            assert code == 130


class TestConfigWizardCLIDisplay:
    def test_non_tty_error_is_labelled_on_stderr(self, tmp_path: Path, capsys):
        with (
            patch("sys.stdin.isatty", return_value=False),
            patch("sys.stdout.isatty", return_value=False),
        ):
            main(["init", "--project-dir", str(tmp_path)])

        captured = capsys.readouterr()
        assert captured.err.startswith("[FAIL] エラー: orchestune config")
        assert captured.out == ""

    def test_ctrl_c_is_labelled_as_warning(self, tmp_path: Path, capsys):
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
            patch(
                "orchestune.config_wizard.cli.run_config_wizard",
                side_effect=KeyboardInterrupt(),
            ),
        ):
            main(["init", "--project-dir", str(tmp_path)])

        assert "[WARN] " in capsys.readouterr().err

    def test_display_helpers_write_text_unchanged_in_plain_mode(self, capsys):
        from orchestune.config_wizard.cli import TerminalPromptPort

        prompt = TerminalPromptPort()
        prompt.display_message("\n--- 設定差分プレビュー ---")
        prompt.display_diff("+ a = 1\n- b = 2")

        assert capsys.readouterr().out == (
            "\n--- 設定差分プレビュー ---\n+ a = 1\n- b = 2\n"
        )

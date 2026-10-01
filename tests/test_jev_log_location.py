"""Tests for where Jev evaluation logs are stored relative to worktrees."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.jev_filter import DEFAULT_JEV_LOG_PATH, _append_jev_log


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def primary_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(primary, "init", "-q")
    _git(
        primary,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
    )
    linked = tmp_path / "linked"
    _git(primary, "worktree", "add", "-q", "-b", "task", str(linked))
    return primary.resolve(), linked.resolve()


class TestJevLogSharedLocation:
    def test_default_log_is_written_to_primary_checkout_from_linked_worktree(
        self,
        primary_with_worktree: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        primary, linked = primary_with_worktree
        monkeypatch.delenv("JEV_LOG_PATH", raising=False)
        monkeypatch.chdir(linked)

        _append_jev_log({"k": 1})

        assert (primary / DEFAULT_JEV_LOG_PATH).read_text(encoding="utf-8") == (
            '{"k": 1}\n'
        )
        assert not (linked / DEFAULT_JEV_LOG_PATH).exists()

    def test_relative_env_log_path_resolves_against_primary_checkout(
        self,
        primary_with_worktree: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        primary, linked = primary_with_worktree
        monkeypatch.setenv("JEV_LOG_PATH", "logs/jev.jsonl")
        monkeypatch.chdir(linked)

        _append_jev_log({"k": 2})

        assert (primary / "logs" / "jev.jsonl").exists()
        assert not (linked / "logs" / "jev.jsonl").exists()

    def test_default_log_falls_back_to_cwd_outside_git(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        monkeypatch.delenv("JEV_LOG_PATH", raising=False)
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
        monkeypatch.chdir(outside)

        _append_jev_log({"k": 3})

        assert (outside / DEFAULT_JEV_LOG_PATH).exists()

    def test_separate_git_dir_checkout_uses_its_own_toplevel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        metadata = tmp_path / "metadata"
        metadata.mkdir()
        checkout = tmp_path / "checkout"
        _git(
            tmp_path,
            "init",
            "-q",
            f"--separate-git-dir={metadata / '.git'}",
            str(checkout),
        )
        monkeypatch.delenv("JEV_LOG_PATH", raising=False)
        monkeypatch.chdir(checkout)

        _append_jev_log({"k": 4})

        assert (checkout.resolve() / DEFAULT_JEV_LOG_PATH).exists()
        assert not (metadata / DEFAULT_JEV_LOG_PATH).exists()

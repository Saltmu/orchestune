"""Real-Git regressions for the claim preflight fetch."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from orchestune.claim.service import _perform_git_fetch


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(directory: Path, name: str) -> str:
    (directory / "content.txt").write_text(name, encoding="utf-8")
    _git("add", "content.txt", cwd=directory)
    _git("commit", "-m", name, cwd=directory)
    return _git("rev-parse", "HEAD", cwd=directory)


@pytest.mark.parametrize("fetch_head_exists", [True, False])
def test_fetch_updates_remote_ref_without_writing_fetch_head(
    tmp_path: Path, fetch_head_exists: bool
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git("init", "--initial-branch=main", cwd=source)
    _git("config", "user.name", "Test", cwd=source)
    _git("config", "user.email", "test@example.invalid", cwd=source)
    _commit(source, "initial")

    origin = tmp_path / "origin.git"
    _git("clone", "--bare", str(source), str(origin), cwd=tmp_path)
    checkout = tmp_path / "checkout"
    _git("clone", str(origin), str(checkout), cwd=tmp_path)

    fetch_head = Path(_git("rev-parse", "--git-path", "FETCH_HEAD", cwd=checkout))
    if not fetch_head.is_absolute():
        fetch_head = checkout / fetch_head
    if fetch_head_exists:
        fetch_head.write_text("sentinel FETCH_HEAD contents\n", encoding="utf-8")
        os.utime(fetch_head, ns=(1_600_000_000_123_456_789,) * 2)
        before = (fetch_head.read_bytes(), fetch_head.stat().st_mtime_ns)
    else:
        fetch_head.unlink(missing_ok=True)

    expected_remote_head = _commit(source, "updated")
    _git("push", str(origin), "main", cwd=source)

    assert _perform_git_fetch(checkout, 1719) is None

    assert _git("rev-parse", "refs/remotes/origin/main", cwd=checkout) == (
        expected_remote_head
    )
    if fetch_head_exists:
        assert (fetch_head.read_bytes(), fetch_head.stat().st_mtime_ns) == before
    else:
        assert not fetch_head.exists()

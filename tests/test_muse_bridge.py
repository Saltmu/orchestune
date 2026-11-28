"""Tests for the Muse bridge supervisor script (scripts/muse_bridge.py)."""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "muse_bridge.py"


@pytest.fixture
def muse_bridge(monkeypatch: pytest.MonkeyPatch) -> Any:
    spec = importlib.util.spec_from_file_location("muse_bridge_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    monkeypatch.delenv(module.ENV_INBOX_DIR, raising=False)
    return module


def _argv(inbox: Path, *extra: str) -> list[str]:
    return [
        "--issue",
        "42",
        "--branch",
        "claude/issue-42-example",
        "--worktree",
        "/tmp/worktree-42",
        "--context",
        ".orchestune/context.json",
        "--inbox-dir",
        str(inbox),
        "--poll-seconds",
        "0.01",
        *extra,
    ]


def _write_marker(inbox: Path, payload: dict[str, Any]) -> None:
    (inbox / "done-42.json").write_text(json.dumps(payload), encoding="utf-8")


def test_main_writes_request_with_expected_fields(
    muse_bridge: Any, tmp_path: Path
) -> None:
    _write_marker(tmp_path, {"status": "done"})

    assert muse_bridge.main(_argv(tmp_path)) == 0

    request = json.loads((tmp_path / "request-42.json").read_text(encoding="utf-8"))
    assert request["schema_version"] == muse_bridge.SCHEMA_VERSION
    assert request["status"] == "pending"
    assert request["issue_number"] == 42
    assert request["branch_name"] == "claude/issue-42-example"
    assert request["worktree_path"] == str(Path("/tmp/worktree-42").expanduser())
    assert request["context_json"] == ".orchestune/context.json"
    assert request["pid"] > 0
    assert request["created_at"].endswith("Z")


def test_main_returns_zero_on_done_marker(muse_bridge: Any, tmp_path: Path) -> None:
    _write_marker(tmp_path, {"status": "done"})
    assert muse_bridge.main(_argv(tmp_path)) == 0


def test_main_returns_one_on_blocked_marker(muse_bridge: Any, tmp_path: Path) -> None:
    _write_marker(tmp_path, {"status": "blocked", "reason": "needs human"})
    assert muse_bridge.main(_argv(tmp_path)) == 1


def test_main_returns_one_on_unreadable_marker(
    muse_bridge: Any, tmp_path: Path
) -> None:
    (tmp_path / "done-42.json").write_text("not json", encoding="utf-8")
    assert muse_bridge.main(_argv(tmp_path)) == 1


def test_main_returns_two_on_timeout(muse_bridge: Any, tmp_path: Path) -> None:
    started = time.monotonic()
    result = muse_bridge.main(_argv(tmp_path, "--timeout-seconds", "0.05"))
    assert result == 2
    assert time.monotonic() - started < 5


def test_wait_updates_heartbeat_and_exits_when_marker_appears(
    muse_bridge: Any, tmp_path: Path
) -> None:
    results: dict[str, Any] = {}

    def run() -> None:
        results["code"] = muse_bridge.main(_argv(tmp_path))

    thread = threading.Thread(target=run)
    thread.start()
    try:
        heartbeat = tmp_path / "heartbeat-42.txt"
        deadline = time.monotonic() + 5
        while not heartbeat.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert heartbeat.exists()
        assert "pid=" in heartbeat.read_text(encoding="utf-8")
        _write_marker(tmp_path, {"status": "done"})
        thread.join(timeout=5)
    finally:
        if thread.is_alive():
            _write_marker(tmp_path, {"status": "blocked", "reason": "test cleanup"})
            thread.join(timeout=5)
    assert results["code"] == 0


def test_resolve_inbox_dir_prefers_flag_then_env_then_default(
    muse_bridge: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert muse_bridge.resolve_inbox_dir(str(tmp_path)) == tmp_path
    monkeypatch.setenv(muse_bridge.ENV_INBOX_DIR, str(tmp_path / "from-env"))
    assert muse_bridge.resolve_inbox_dir(None) == tmp_path / "from-env"
    assert muse_bridge.resolve_inbox_dir(str(tmp_path / "flag")) == tmp_path / "flag"
    monkeypatch.delenv(muse_bridge.ENV_INBOX_DIR)
    assert muse_bridge.resolve_inbox_dir(None) == (
        Path.home() / ".orchestune-muse" / "inbox"
    )


def test_paths_use_issue_number(muse_bridge: Any, tmp_path: Path) -> None:
    assert muse_bridge.request_path(tmp_path, 7) == tmp_path / "request-7.json"
    assert muse_bridge.marker_path(tmp_path, 7) == tmp_path / "done-7.json"
    assert muse_bridge.heartbeat_path(tmp_path, 7) == tmp_path / "heartbeat-7.txt"

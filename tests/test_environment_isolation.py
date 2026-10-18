"""Regression tests for environment isolation fixtures (Jev and Git)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

import pytest

from tests.environment_support import _isolate_jev_env

_raw_isolate_jev_env = cast(Any, _isolate_jev_env).__wrapped__


def test_jev_isolated_dir_is_session_scoped_directory(
    request: pytest.FixtureRequest,
) -> None:
    session_dir = request.getfixturevalue("_jev_isolated_dir")
    assert isinstance(session_dir, Path)
    assert session_dir.is_dir()
    assert ".orchestune" in str(session_dir)


def test_isolate_jev_env_uses_shared_dir_with_unique_delayed_file(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    session_dir = request.getfixturevalue("_jev_isolated_dir")
    paths_seen = set()

    # Run generator multiple times to simulate multiple tests
    for _ in range(3):
        gen = _raw_isolate_jev_env(monkeypatch, session_dir)
        next(gen)
        assigned_path = Path(os.environ["JEV_LOG_PATH"])
        assert assigned_path.parent == session_dir
        assert assigned_path.name.startswith("evaluations-")
        assert assigned_path.name.endswith(".jsonl")
        assert not assigned_path.exists(), "Log file should not be created eagerly"
        paths_seen.add(assigned_path)
        with pytest.raises(StopIteration):
            next(gen)

    assert len(paths_seen) == 3, "Each test invocation must get a unique log path"


def test_isolate_jev_env_cleans_up_assigned_file_on_teardown(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    session_dir = request.getfixturevalue("_jev_isolated_dir")
    gen = _raw_isolate_jev_env(monkeypatch, session_dir)
    next(gen)
    assigned_path = Path(os.environ["JEV_LOG_PATH"])
    # Simulate writing to the log file during test
    assigned_path.write_text('{"event": "evaluated"}\n', encoding="utf-8")
    assert assigned_path.exists()

    with pytest.raises(StopIteration):
        next(gen)

    assert not assigned_path.exists(), "Assigned log file must be deleted on teardown"


def test_isolate_jev_env_does_not_delete_custom_jev_log_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    session_dir = request.getfixturevalue("_jev_isolated_dir")
    gen = _raw_isolate_jev_env(monkeypatch, session_dir)
    next(gen)
    default_assigned_path = Path(os.environ["JEV_LOG_PATH"])
    default_assigned_path.write_text("default log", encoding="utf-8")

    # A test customizes JEV_LOG_PATH to its own path
    custom_path = tmp_path / "custom_eval.jsonl"
    custom_path.write_text("custom log", encoding="utf-8")
    monkeypatch.setenv("JEV_LOG_PATH", str(custom_path))

    with pytest.raises(StopIteration):
        next(gen)

    assert (
        not default_assigned_path.exists()
    ), "Original assigned path should be cleaned"
    assert custom_path.exists(), "User-specified custom path must NOT be deleted"

from pathlib import Path

import pytest

from orchestune.dispatch_config import DispatcherConfig


def test_guard_events_log_path_fails_on_default_init():
    """`DispatcherConfig` initialized with default `Path('events.jsonl')` should fail immediately in tests."""
    with pytest.raises(pytest.fail.Exception) as exc_info:
        DispatcherConfig()

    assert (
        "DispatcherConfig initialized with default events_log_path ('events.jsonl')"
        in str(exc_info.value)
    )


def test_guard_events_log_path_succeeds_with_explicit_tmp_path(tmp_path: Path):
    """`DispatcherConfig` initialized with explicit isolated `events_log_path` should succeed."""
    config = DispatcherConfig(events_log_path=tmp_path / "events.jsonl")
    assert config.events_log_path == tmp_path / "events.jsonl"


def test_guard_dispatch_cycle_ensure_parent_branch_fails_when_unmocked():
    """`ensure_parent_branch` inside dispatch_cycle should fail in unit tests when unmocked."""
    import orchestune.dispatch_cycle

    with pytest.raises(pytest.fail.Exception) as exc_info:
        orchestune.dispatch_cycle.ensure_parent_branch(181)

    assert "called unmocked `ensure_parent_branch(181)`" in str(exc_info.value)

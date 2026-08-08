from pathlib import Path

import pytest
from _pytest.outcomes import Failed

from orchestune.dispatch_config import DispatcherConfig


def test_guard_events_log_path_fails_on_default_init():
    """`DispatcherConfig` initialized with default `Path('events.jsonl')` should fail immediately in tests."""
    with pytest.raises(Failed) as exc_info:
        DispatcherConfig()

    assert (
        "DispatcherConfig initialized with default events_log_path ('events.jsonl')"
        in str(exc_info.value)
    )


def test_guard_events_log_path_succeeds_with_explicit_tmp_path(tmp_path: Path):
    """`DispatcherConfig` initialized with explicit isolated `events_log_path` should succeed."""
    config = DispatcherConfig(events_log_path=tmp_path / "events.jsonl")
    assert config.events_log_path == tmp_path / "events.jsonl"

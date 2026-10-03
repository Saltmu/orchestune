"""Configuration defaults have one owner; entry points forward explicit values (#1189)."""

from __future__ import annotations

import ast
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from orchestune.dispatch import config_values, retry_policy
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.config_loader import _assemble_dispatcher_config
from orchestune.dispatch.config_values import (
    COMPLETION_POLICY_KEYS,
    RUNTIME_TUNING_KEYS,
    completion_policy_overrides,
    runtime_tuning_overrides,
)
from orchestune.dispatch.execution_profiles import ExecutionProfileConfig

PACKAGE = Path(config_values.__file__).parent


def _field_default(name: str) -> Any:
    return next(f.default for f in fields(DispatcherConfig) if f.name == name)


class TestSelection:
    def test_only_explicit_keys_are_selected(self):
        values = {"window_seconds": 60, "unrelated": 1, "apply": True}
        assert runtime_tuning_overrides(values) == {"window_seconds": 60}

    def test_missing_keys_are_left_to_the_config_defaults(self):
        assert runtime_tuning_overrides({}) == {}
        assert completion_policy_overrides({}) == {}

    def test_an_explicit_none_is_forwarded_not_treated_as_missing(self):
        values = {"max_launches_per_window": None}
        assert runtime_tuning_overrides(values) == {"max_launches_per_window": None}

    def test_completion_policy_keys_are_selected_separately(self):
        values = {
            "max_review_timeout_retries": 5,
            "review_timeout_backoff_seconds": 7,
            "not_needed_review_timeout_seconds": 9,
            "window_seconds": 60,
        }
        assert completion_policy_overrides(values) == {
            "max_review_timeout_retries": 5,
            "review_timeout_backoff_seconds": 7,
            "not_needed_review_timeout_seconds": 9,
        }

    def test_selection_only_reads_normalized_keys(self):
        assert runtime_tuning_overrides({"window-seconds": 60}) == {}

    def test_every_selectable_key_is_a_dispatcher_config_field(self):
        names = {f.name for f in fields(DispatcherConfig)}
        assert set(RUNTIME_TUNING_KEYS) <= names
        assert set(COMPLETION_POLICY_KEYS) <= names


class TestRetryDefaultsHaveOneOwner:
    @pytest.mark.parametrize(
        ("field", "constant"),
        [
            ("max_review_timeout_retries", "DEFAULT_REVIEW_TIMEOUT_MAX_ATTEMPTS"),
            (
                "review_timeout_backoff_seconds",
                "DEFAULT_REVIEW_TIMEOUT_BACKOFF_SECONDS",
            ),
            ("max_early_death_retries", "DEFAULT_EARLY_DEATH_MAX_RETRIES"),
            ("early_death_backoff_seconds", "DEFAULT_EARLY_DEATH_BACKOFF_SECONDS"),
        ],
    )
    def test_config_field_default_is_the_retry_policy_constant(self, field, constant):
        assert _field_default(field) == getattr(retry_policy, constant)

    def test_previous_effective_defaults_are_preserved(self):
        assert retry_policy.DEFAULT_REVIEW_TIMEOUT_MAX_ATTEMPTS == 2
        assert retry_policy.DEFAULT_REVIEW_TIMEOUT_BACKOFF_SECONDS == 60
        assert retry_policy.DEFAULT_EARLY_DEATH_MAX_RETRIES == 2
        assert retry_policy.DEFAULT_EARLY_DEATH_BACKOFF_SECONDS == 60


def _paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "run_state_path": tmp_path / "run_state.json",
        "worktree_root": tmp_path / "worktrees",
        "log_dir": tmp_path / "logs",
        "events_log_path": tmp_path / "events.jsonl",
    }


def _assemble(
    tmp_path: Path, toml_data: dict[str, Any], **cli: Any
) -> DispatcherConfig:
    args = SimpleNamespace(
        **{"apply": None, "max_concurrent": None, "profile": None, **cli}
    )
    return _assemble_dispatcher_config(
        args,
        toml_data,
        _paths(tmp_path),
        1,
        None,
        ExecutionProfileConfig(),
        forge=Mock(),
    )


class TestDispatcherEntry:
    def test_omitted_keys_resolve_to_the_config_defaults(self, tmp_path):
        config = _assemble(tmp_path, {})
        for key in RUNTIME_TUNING_KEYS:
            assert getattr(config, key) == _field_default(key), key

    def test_explicit_keys_override_the_defaults(self, tmp_path):
        config = _assemble(
            tmp_path,
            {
                "window_seconds": 111,
                "max_early_death_retries": 5,
                "early_death_backoff_seconds": 7,
                "not_needed_review_timeout_seconds": 99,
            },
        )
        assert config.window_seconds == 111
        assert config.max_early_death_retries == 5
        assert config.early_death_backoff_seconds == 7
        assert config.not_needed_review_timeout_seconds == 99

    def test_cli_max_concurrent_beats_the_config_file(self, tmp_path):
        assert (
            _assemble(tmp_path, {"max_concurrent": 5}, max_concurrent=3).max_concurrent
            == 3
        )
        assert _assemble(tmp_path, {"max_concurrent": 5}).max_concurrent == 5
        assert _assemble(tmp_path, {}).max_concurrent == _field_default(
            "max_concurrent"
        )

    def test_the_apply_entry_difference_is_kept(self, tmp_path):
        assert _field_default("apply") is False
        assert _assemble(tmp_path, {}).apply is True
        assert _assemble(tmp_path, {"apply": False}).apply is False
        assert _assemble(tmp_path, {"apply": False}, apply=True).apply is True


class TestStandaloneGcEntry:
    @staticmethod
    def _resolve(monkeypatch, tmp_path, raw: dict[str, Any]) -> DispatcherConfig:
        from orchestune.dispatch.gc import policies

        captured: dict[str, DispatcherConfig] = {}

        def fake_process(state, config, *, repository_id=None):
            captured["config"] = config
            return []

        monkeypatch.setattr(policies, "policy_candidates", lambda state: [object()])
        monkeypatch.setattr(policies, "find_and_load_config_file", lambda root: raw)
        monkeypatch.setattr(policies, "load_run_state_readonly", lambda path: Mock())
        monkeypatch.setattr(policies, "process_completion_policies", fake_process)
        workspace = SimpleNamespace(
            repository_root=tmp_path,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            repository_identity="repo",
        )
        policies.standalone_policies(workspace, Mock(), apply=False)
        return captured["config"]

    def test_omitted_keys_resolve_to_the_config_defaults(self, monkeypatch, tmp_path):
        config = self._resolve(monkeypatch, tmp_path, {})
        for key in COMPLETION_POLICY_KEYS:
            assert getattr(config, key) == _field_default(key), key

    def test_explicit_keys_are_forwarded_including_hyphenated_ones(
        self, monkeypatch, tmp_path
    ):
        raw = {
            "max-review-timeout-retries": 4,
            "review_timeout_backoff_seconds": 9,
            "not-needed-review-timeout-seconds": 123,
        }
        config = self._resolve(monkeypatch, tmp_path, raw)
        assert config.max_review_timeout_retries == 4
        assert config.review_timeout_backoff_seconds == 9
        assert config.not_needed_review_timeout_seconds == 123

    def test_keys_outside_the_completion_policy_are_not_read(
        self, monkeypatch, tmp_path
    ):
        config = self._resolve(monkeypatch, tmp_path, {"window_seconds": 5})
        assert config.window_seconds == _field_default("window_seconds")


def _numeric_default_restatements(path: Path, keys: set[str]) -> list[str]:
    """`<mapping>.get("<setting>", <literal>)` re-states a default value."""
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and len(node.args) == 2
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value in keys
            and isinstance(node.args[1], ast.Constant)
        ):
            found.append(f"{path.name}:{node.lineno}:{node.args[0].value!s}")
    return found


def test_entry_points_do_not_restate_numeric_defaults():
    keys = set(RUNTIME_TUNING_KEYS) | set(COMPLETION_POLICY_KEYS)
    restated = [
        hit
        for name in ("config_loader.py", "gc/policies.py")
        for hit in _numeric_default_restatements(PACKAGE / name, keys)
    ]
    assert restated == []

"""Tests for child-review-gate configuration precedence and validation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from orchestune.dag.models import ConfigError
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.config_loader import (
    build_arg_parser,
    load_and_resolve_config,
    validate_toml_config,
)
from orchestune.dispatch.postcycle import _build_integrator_config
from orchestune.integrator.types import IntegratorConfig


class TestChildReviewGateConfigPrecedence:
    """CLI --child-review-gate > TOML child-review-gate > env ORCHESTUNE_CHILD_REVIEW_GATE > default ("required")."""

    @pytest.fixture(autouse=True)
    def _inject_fake_forge(self, fake_forge):
        pass

    def test_default_is_required(self, tmp_path: Path):
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )
        assert config.child_review_gate == "required"

        integrator_cfg = _build_integrator_config(config, semantic_review_enabled=False)
        assert integrator_cfg.child_review_gate == "required"

    def test_env_var_overrides_default(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("ORCHESTUNE_CHILD_REVIEW_GATE", "off")
        config = load_and_resolve_config(
            ["-p", "100"],
            cwd=tmp_path,
            load_config_fn=lambda _: {},
            build_target_fn=lambda _: MagicMock(),
            resolve_paths_fn=lambda _a, _c: (
                tmp_path / "run_state.json",
                tmp_path / "worktrees",
            ),
        )
        assert config.child_review_gate == "off"

    def test_toml_overrides_env_var(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("ORCHESTUNE_CHILD_REVIEW_GATE", "required")
        config = load_and_resolve_config(
            ["-p", "100"],
            cwd=tmp_path,
            load_config_fn=lambda _: {"child_review_gate": "off"},
            build_target_fn=lambda _: MagicMock(),
            resolve_paths_fn=lambda _a, _c: (
                tmp_path / "run_state.json",
                tmp_path / "worktrees",
            ),
        )
        assert config.child_review_gate == "off"

    def test_toml_hyphen_key_supported(self, tmp_path: Path):
        validated = validate_toml_config({"child-review-gate": "off"})
        assert validated["child_review_gate"] == "off"

    def test_cli_overrides_toml(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("ORCHESTUNE_CHILD_REVIEW_GATE", "off")
        config = load_and_resolve_config(
            ["-p", "100", "--child-review-gate", "required"],
            cwd=tmp_path,
            load_config_fn=lambda _: {"child_review_gate": "off"},
            build_target_fn=lambda _: MagicMock(),
            resolve_paths_fn=lambda _a, _c: (
                tmp_path / "run_state.json",
                tmp_path / "worktrees",
            ),
        )
        assert config.child_review_gate == "required"

    def test_cli_can_disable_gate(self, tmp_path: Path):
        config = load_and_resolve_config(
            ["-p", "100", "--child-review-gate", "off"],
            cwd=tmp_path,
            load_config_fn=lambda _: {"child_review_gate": "required"},
            build_target_fn=lambda _: MagicMock(),
            resolve_paths_fn=lambda _a, _c: (
                tmp_path / "run_state.json",
                tmp_path / "worktrees",
            ),
        )
        assert config.child_review_gate == "off"


class TestChildReviewGateValidation:
    """Invalid values must result in configuration errors."""

    @pytest.fixture(autouse=True)
    def _inject_fake_forge(self, fake_forge):
        pass

    def test_toml_rejects_invalid_value(self):
        with pytest.raises(
            ConfigError, match=r"child-review-gate.*must be one of: 'off', 'required'"
        ):
            validate_toml_config({"child-review-gate": "invalid"})

    def test_env_rejects_invalid_value(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("ORCHESTUNE_CHILD_REVIEW_GATE", "invalid")
        with pytest.raises(
            ConfigError,
            match=r"ORCHESTUNE_CHILD_REVIEW_GATE.*must be one of: 'off', 'required'",
        ):
            load_and_resolve_config(
                ["-p", "100"],
                cwd=tmp_path,
                load_config_fn=lambda _: {},
                build_target_fn=lambda _: MagicMock(),
                resolve_paths_fn=lambda _a, _c: (
                    tmp_path / "run_state.json",
                    tmp_path / "worktrees",
                ),
            )

    def test_cli_rejects_invalid_value(self):
        parser = build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["-p", "100", "--child-review-gate", "invalid"])

    def test_dispatcher_config_rejects_invalid_value(self, tmp_path: Path):
        with pytest.raises(ValueError, match="child_review_gate"):
            DispatcherConfig(
                parent_issue_number=100,
                child_review_gate="invalid",  # type: ignore[arg-type]
                events_log_path=tmp_path / "events.jsonl",
                run_state_path=tmp_path / "run_state.json",
                worktree_root=tmp_path / "worktrees",
            )

    def test_integrator_config_rejects_invalid_value(self):
        with pytest.raises(ValueError, match="child_review_gate"):
            IntegratorConfig(
                parent_issue_number=100,
                child_review_gate="invalid",  # type: ignore[arg-type]
            )

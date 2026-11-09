"""stale_read_gate loading, combination checks, and integrator handoff."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from orchestune.config_contracts import ConfigError
from orchestune.dispatch.config.loader import build_arg_parser, load_and_resolve_config
from orchestune.dispatch.config.model import DispatcherConfig
from orchestune.dispatch.config.schema import validate_toml_config
from orchestune.dispatch.pre_scheduling_integration import (
    _build_integrator_config,
    _decide_semantic_review_enabled,
)
from orchestune.integrator.types import IntegratorConfig

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(tmp_path: Path, toml: dict[str, object]) -> DispatcherConfig:
    return load_and_resolve_config(
        ["-p", "1767"],
        cwd=tmp_path,
        forge=MagicMock(),
        load_config_fn=lambda _: toml,
        build_target_fn=lambda _: SimpleNamespace(target_name="grok-cli"),
        resolve_paths_fn=lambda _args, _cwd: (
            tmp_path / "run_state.json",
            tmp_path / "worktrees",
        ),
    )


def _dispatcher(tmp_path: Path, **overrides: object) -> DispatcherConfig:
    values: dict[str, object] = {
        "parent_issue_number": 1767,
        "forge": MagicMock(),
        "events_log_path": tmp_path / "events.jsonl",
    }
    values.update(overrides)
    return DispatcherConfig(**values)  # type: ignore[arg-type]


class TestTomlModes:
    @pytest.mark.parametrize("mode", ["advisory", "required", "off"])
    def test_hyphen_key_accepts_each_mode(self, mode: str) -> None:
        validated = validate_toml_config({"stale-read-gate": mode})

        assert validated["stale_read_gate"] == mode

    def test_underscore_key_accepts_advisory(self) -> None:
        validated = validate_toml_config({"stale_read_gate": "advisory"})

        assert validated["stale_read_gate"] == "advisory"

    def test_invalid_value_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="stale-read-gate"):
            validate_toml_config({"stale-read-gate": "sometimes"})

    @pytest.mark.parametrize("mode", ["off", "advisory"])
    def test_required_conflicts_with_non_required_semantic_mode(
        self, mode: str
    ) -> None:
        with pytest.raises(ConfigError, match="stale_read_gate"):
            validate_toml_config(
                {"stale-read-gate": "required", "semantic-review-mode": mode}
            )

    def test_required_is_accepted_with_required_semantic_mode(self) -> None:
        validated = validate_toml_config(
            {"stale-read-gate": "required", "semantic-review-mode": "required"}
        )

        assert validated["stale_read_gate"] == "required"

    def test_required_without_semantic_mode_is_left_to_resolution(self) -> None:
        validated = validate_toml_config({"stale-read-gate": "required"})

        assert validated["stale_read_gate"] == "required"
        assert "semantic_review_mode" not in validated


class TestResolution:
    def test_default_is_advisory_and_keeps_existing_settings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ORCHESTUNE_SEMANTIC_REVIEW", raising=False)
        monkeypatch.setenv("ORCHESTUNE_STALE_READ_GATE", "off")

        config = _load(tmp_path, {"ci-command": "./scripts/local-ci.sh"})

        assert config.stale_read_gate == "advisory"
        assert config.semantic_review_mode == "required"
        assert config.child_review_gate == "required"
        assert config.force_integration_review is False
        assert config.ci_command == ["./scripts/local-ci.sh"]

    @pytest.mark.parametrize("mode", ["advisory", "required", "off"])
    def test_toml_mode_is_loaded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
    ) -> None:
        monkeypatch.delenv("ORCHESTUNE_SEMANTIC_REVIEW", raising=False)
        semantic_mode = "required" if mode == "required" else "advisory"

        config = _load(
            tmp_path,
            {
                "stale-read-gate": mode,
                "semantic-review-mode": semantic_mode,
                "ci-command": "pytest -q",
            },
        )

        assert config.stale_read_gate == mode
        assert config.semantic_review_mode == semantic_mode
        assert config.ci_command == ["pytest", "-q"]

    def test_cli_flag_is_not_added(self) -> None:
        with pytest.raises(SystemExit):
            build_arg_parser().parse_args(["-p", "1767", "--stale-read-gate", "off"])

    def test_legacy_disable_rejects_required_when_mode_falls_back_to_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ORCHESTUNE_SEMANTIC_REVIEW", "0")

        with pytest.raises(ConfigError, match="stale_read_gate"):
            _load(tmp_path, {"stale-read-gate": "required"})

    def test_legacy_disable_rejects_required_even_when_mode_is_explicit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ORCHESTUNE_SEMANTIC_REVIEW", "0")

        with pytest.raises(ConfigError, match="stale_read_gate"):
            _load(
                tmp_path,
                {
                    "stale-read-gate": "required",
                    "semantic-review-mode": "required",
                },
            )

    def test_legacy_disable_still_allows_advisory_and_keeps_explicit_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ORCHESTUNE_SEMANTIC_REVIEW", "0")

        config = _load(
            tmp_path,
            {
                "stale-read-gate": "advisory",
                "semantic-review-mode": "required",
                "child-review-gate": "required",
                "ci-command": "./scripts/local-ci.sh",
            },
        )

        assert config.stale_read_gate == "advisory"
        assert config.semantic_review_mode == "required"
        assert config.child_review_gate == "required"
        assert config.ci_command == ["./scripts/local-ci.sh"]
        assert _decide_semantic_review_enabled() is False


class TestConfigObjects:
    def test_dispatcher_rejects_unknown_mode(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="stale_read_gate"):
            _dispatcher(tmp_path, stale_read_gate="sometimes")

    def test_dispatcher_rejects_required_without_required_semantic_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ORCHESTUNE_SEMANTIC_REVIEW", raising=False)

        with pytest.raises(ValueError, match="stale_read_gate"):
            _dispatcher(
                tmp_path,
                semantic_review_mode="advisory",
                stale_read_gate="required",
            )

    def test_dispatcher_rejects_required_when_legacy_env_disables_review(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ORCHESTUNE_SEMANTIC_REVIEW", "0")

        with pytest.raises(ValueError, match="stale_read_gate"):
            _dispatcher(
                tmp_path,
                semantic_review_mode="required",
                stale_read_gate="required",
            )

    def test_integrator_rejects_required_when_enable_flag_is_false(self) -> None:
        with pytest.raises(ValueError, match="stale_read_gate"):
            IntegratorConfig(
                parent_issue_number=1767,
                forge=MagicMock(),
                semantic_review_mode="required",
                enable_semantic_review=False,
                stale_read_gate="required",
            )

    def test_child_review_gate_and_force_flag_do_not_block_required(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ORCHESTUNE_SEMANTIC_REVIEW", raising=False)
        config = _dispatcher(
            tmp_path,
            semantic_review_mode="required",
            child_review_gate="off",
            force_integration_review=True,
            stale_read_gate="required",
        )

        integrator = _build_integrator_config(config, True)

        assert config.child_review_gate == "off"
        assert config.force_integration_review is True
        assert integrator.stale_read_gate == "required"
        assert integrator.child_review_gate == "off"
        assert integrator.force_integration_review is True
        assert integrator.enable_semantic_review is True


class TestIntegratorHandoff:
    @pytest.mark.parametrize("gate", ["advisory", "required", "off"])
    def test_build_copies_the_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: str
    ) -> None:
        monkeypatch.delenv("ORCHESTUNE_SEMANTIC_REVIEW", raising=False)
        config = _dispatcher(
            tmp_path,
            semantic_review_mode="required",
            stale_read_gate=gate,
        )

        integrator = _build_integrator_config(config, True)

        assert integrator.stale_read_gate == gate

    def test_build_rejects_required_when_the_enable_flag_is_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ORCHESTUNE_SEMANTIC_REVIEW", raising=False)
        config = _dispatcher(
            tmp_path,
            semantic_review_mode="required",
            stale_read_gate="required",
        )

        with pytest.raises(ValueError, match="stale_read_gate"):
            _build_integrator_config(config, False)


def test_example_documents_the_gate_without_changing_effective_settings() -> None:
    text = (_REPO_ROOT / "orchestune.toml.example").read_text(encoding="utf-8")

    assert re.search(r"(?m)^#\s*stale-read-gate\s*=", text)
    assert "stale-read-gate" not in tomllib.loads(text)

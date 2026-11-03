"""#1560: `[reviewer_by_target]` configures the auto reviewer per execution target."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from orchestune.config_contracts import ConfigError
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.config_loader import load_and_resolve_config
from orchestune.dispatch.config_schema import (
    extract_reviewer_by_target,
    validate_toml_config,
)
from orchestune.dispatch.postcycle import _final_acceptance_reviewer
from orchestune.dispatch.semantic_review.adapters import (
    validate_semantic_review_preflight,
)
from orchestune.dispatch.semantic_review.postcycle import SemanticReviewCycleRunner
from orchestune.dispatch.targets import TargetBuildConfig, build_dispatch_target

MAPPING = {"agy-cli": "claude", "claude-cli": "codex", "grok-cli": "codex"}


class TestSchema:
    @pytest.mark.parametrize("table_key", ["reviewer_by_target", "reviewer-by-target"])
    def test_accepts_target_to_reviewer_table(self, table_key):
        validated = validate_toml_config({table_key: dict(MAPPING)})
        assert extract_reviewer_by_target(validated) == MAPPING

    def test_absent_table_extracts_to_empty_mapping(self):
        assert extract_reviewer_by_target(validate_toml_config({})) == {}

    def test_inner_target_names_are_not_normalized(self):
        validated = validate_toml_config({"reviewer_by_target": {"grok-cli": "codex"}})
        assert set(extract_reviewer_by_target(validated)) == {"grok-cli"}

    def test_every_concrete_target_is_a_valid_key(self):
        targets = {
            "local",
            "cloud-routine",
            "codex-cloud",
            "claude-cli",
            "agy-cli",
            "codex-cli",
            "grok-cli",
        }
        table = {name: "claude" for name in targets}
        validated = validate_toml_config({"reviewer_by_target": table})
        assert extract_reviewer_by_target(validated) == table

    def test_same_vendor_pair_is_allowed(self):
        validated = validate_toml_config(
            {"reviewer_by_target": {"claude-cli": "claude"}}
        )
        assert extract_reviewer_by_target(validated) == {"claude-cli": "claude"}

    @pytest.mark.parametrize(
        "table",
        [
            {"auto": "claude"},  # `auto` is resolved to a concrete target first
            {"gemini-cli": "claude"},  # unknown target
            {"grok_cli": "claude"},  # target names are matched verbatim
            {"grok-cli": "auto"},  # a mapping must name a concrete vendor
            {"grok-cli": "gemini"},
            {"grok-cli": ""},
            {"grok-cli": 1},
            {"grok-cli": ["codex"]},
        ],
    )
    def test_rejects_invalid_entries(self, table):
        with pytest.raises(ConfigError, match="reviewer.by.target"):
            validate_toml_config({"reviewer_by_target": table})

    @pytest.mark.parametrize("value", ["codex", ["codex"], 1, True])
    def test_rejects_non_table(self, value):
        with pytest.raises(ConfigError, match="must be a table"):
            validate_toml_config({"reviewer_by_target": value})

    def test_rejects_both_spellings_at_once(self):
        with pytest.raises(ConfigError, match="reviewer.by.target"):
            validate_toml_config(
                {
                    "reviewer_by_target": {"grok-cli": "codex"},
                    "reviewer-by-target": {"grok-cli": "claude"},
                }
            )

    def test_does_not_change_scalar_reviewer_bot_validation(self):
        validated = validate_toml_config(
            {"reviewer-bot": "auto", "reviewer_by_target": dict(MAPPING)}
        )
        assert validated["reviewer_bot"] == "auto"
        with pytest.raises(ConfigError):
            validate_toml_config({"reviewer-bot": "gemini"})


def _load(tmp_path: Path, toml: dict, build_target_fn=None) -> DispatcherConfig:
    return load_and_resolve_config(
        ["-p", "100"],
        cwd=tmp_path,
        load_config_fn=lambda _: toml,
        build_target_fn=build_target_fn or (lambda _: MagicMock()),
        resolve_paths_fn=lambda _a, _c: (
            tmp_path / "run_state.json",
            tmp_path / "worktrees",
        ),
    )


class TestLoader:
    @pytest.fixture(autouse=True)
    def _inject_fake_forge(self, fake_forge):
        pass

    def test_table_reaches_target_build_config_and_dispatcher_config(self, tmp_path):
        built: list[TargetBuildConfig] = []

        def build(config: TargetBuildConfig):
            built.append(config)
            return MagicMock()

        config = _load(
            tmp_path,
            {"reviewer-bot": "auto", "reviewer-by-target": dict(MAPPING)},
            build,
        )

        assert built[0].reviewer_by_target == MAPPING
        assert config.reviewer_by_target == MAPPING

    def test_default_is_an_empty_mapping(self, tmp_path):
        built: list[TargetBuildConfig] = []

        def build(config: TargetBuildConfig):
            built.append(config)
            return MagicMock()

        config = _load(tmp_path, {}, build)

        assert built[0].reviewer_by_target == {}
        assert config.reviewer_by_target == {}

    def test_dispatcher_config_default_is_not_shared_between_instances(self, tmp_path):
        log = tmp_path / "events.jsonl"
        first = DispatcherConfig(parent_issue_number=1, events_log_path=log)
        second = DispatcherConfig(parent_issue_number=2, events_log_path=log)
        assert first.reviewer_by_target is not second.reviewer_by_target


class TestBuildDispatchTarget:
    @pytest.mark.parametrize(
        ("target_name", "expected"),
        [("agy-cli", "claude"), ("claude-cli", "codex"), ("grok-cli", "codex")],
    )
    def test_local_cli_target_uses_the_configured_reviewer(
        self, tmp_path, target_name, expected
    ):
        target = build_dispatch_target(
            TargetBuildConfig(
                target_name,
                None,
                None,
                tmp_path / "logs",
                allow_unsafe_agent_execution=True,
                reviewer_by_target=dict(MAPPING),
            )
        )
        assert target._reviewer_bot == expected
        assert f"`{expected}`" in target._local_cmd

    def test_unlisted_target_keeps_the_default(self, tmp_path):
        target = build_dispatch_target(
            TargetBuildConfig(
                "codex-cli",
                None,
                None,
                tmp_path / "logs",
                allow_unsafe_agent_execution=True,
                reviewer_by_target=dict(MAPPING),
            )
        )
        assert target._reviewer_bot == "claude"

    def test_explicit_reviewer_bot_beats_the_table(self, tmp_path):
        target = build_dispatch_target(
            TargetBuildConfig(
                "grok-cli",
                None,
                None,
                tmp_path / "logs",
                allow_unsafe_agent_execution=True,
                reviewer_bot="claude",
                reviewer_by_target=dict(MAPPING),
            )
        )
        assert target._reviewer_bot == "claude"

    def test_generic_local_target_can_be_configured(self, tmp_path, capsys):
        target = build_dispatch_target(
            TargetBuildConfig(
                "local",
                None,
                None,
                tmp_path / "logs",
                local_cmd="runner --reviewer {reviewer_bot}",
                reviewer_by_target={"local": "codex"},
            )
        )
        assert target._reviewer_bot == "codex"
        assert "自動選択できません" not in capsys.readouterr().err

    @pytest.mark.parametrize("cloud_target", ["cloud-routine", "codex-cloud"])
    def test_cloud_fallback_re_resolves_with_the_table(
        self, tmp_path, monkeypatch, cloud_target
    ):
        for name in (
            "ORCHESTUNE_CODEX_CLOUD_ENV",
            "ORCHESTUNE_ROUTINE_ID",
            "ORCHESTUNE_ROUTINE_TOKEN",
        ):
            monkeypatch.delenv(name, raising=False)
        target = build_dispatch_target(
            TargetBuildConfig(
                cloud_target,
                None,
                None,
                tmp_path / "logs",
                local_cmd="runner --reviewer {reviewer_bot}",
                reviewer_by_target={"local": "claude"},
            )
        )
        assert target._reviewer_bot == "claude"


def _config(tmp_path, provider, target_name, table, **extra) -> DispatcherConfig:
    return DispatcherConfig(
        parent_issue_number=100,
        apply=True,
        forge=MagicMock(),
        dispatch_target=cast(Any, SimpleNamespace(target_name=target_name)),
        semantic_review_provider=provider,
        semantic_review_mode="advisory",
        final_acceptance_review="advisory",
        reviewer_by_target=table,
        events_log_path=tmp_path / "events.jsonl",
        **extra,
    )


class TestSemanticReviewProvider:
    """`semantic-review-provider = "auto"` reads the same table (#1560)."""

    @pytest.mark.parametrize(
        ("provider", "target", "expected"),
        [
            ("auto", "grok-cli", "codex"),
            ("auto", "agy-cli", "claude"),
            ("auto", "codex-cli", "claude"),
            ("claude", "grok-cli", "claude"),
            ("codex", "agy-cli", "codex"),
        ],
    )
    def test_final_acceptance_reviewer(self, tmp_path, provider, target, expected):
        config = _config(tmp_path, provider, target, dict(MAPPING))
        assert _final_acceptance_reviewer(config) == expected

    def test_preflight_resolves_the_configured_reviewer(self, tmp_path):
        config = _config(tmp_path, "auto", "grok-cli", dict(MAPPING))
        result = validate_semantic_review_preflight(
            config, repository_root=tmp_path, which_fn=lambda _name: "/bin/true"
        )
        assert result.resolved_reviewer == "codex"

    def test_preflight_reports_unresolved_local_without_a_table(self, tmp_path):
        config = _config(tmp_path, "auto", "local", {})
        result = validate_semantic_review_preflight(
            config, repository_root=tmp_path, which_fn=lambda _name: "/bin/true"
        )
        assert result.ok is False
        assert result.resolved_reviewer is None

    def test_attempt_runner_resolves_the_configured_reviewer(self, tmp_path):
        runner = SemanticReviewCycleRunner.__new__(SemanticReviewCycleRunner)
        runner._config = _config(tmp_path, "auto", "grok-cli", dict(MAPPING))
        assert runner._reviewer() == "codex"


class TestWizardAndExample:
    @pytest.mark.parametrize("table_key", ["reviewer_by_target", "reviewer-by-target"])
    def test_wizard_does_not_flag_the_table_as_unknown(self, table_key):
        from orchestune.config_wizard.validation import diagnose_existing_document

        assert diagnose_existing_document({table_key: dict(MAPPING)}) == []

    def test_wizard_still_reports_invalid_entries(self):
        from orchestune.config_wizard.validation import diagnose_existing_document

        diagnostics = diagnose_existing_document(
            {"reviewer_by_target": {"gemini-cli": "claude"}}
        )
        assert any("gemini-cli" in line for line in diagnostics)

    def test_example_documents_a_table_that_validates_once_uncommented(self):
        import tomllib

        repo_root = Path(__file__).resolve().parent.parent
        lines = (repo_root / "orchestune.toml.example").read_text(encoding="utf-8")
        start = lines.index("# [reviewer_by_target]")
        block = [line.removeprefix("# ") for line in lines[start:].splitlines() if line]
        data = tomllib.loads("\n".join(block))
        validated = validate_toml_config(data)
        assert extract_reviewer_by_target(validated) == MAPPING

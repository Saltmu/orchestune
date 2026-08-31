"""Tests for execution profile configuration and deterministic resolution selector."""

from __future__ import annotations

import logging
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from orchestune.dag.models import ConfigError
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.execution_profiles import (
    DEFAULT_EXECUTION_PROFILE,
    ExecutionProfileConfig,
    ExecutionSelection,
    TargetExecutionConfig,
    TargetProfileConfig,
    extract_execution_profile_config,
    load_execution_profile_config,
    resolve_execution_profile,
    validate_model_name,
    validate_profile_name,
    validate_reasoning_effort,
)
from orchestune.dispatch.targets import (
    ClaudeCodeCloudRoutineDispatchTarget,
    CodexCloudDispatchTarget,
    LocalProcessDispatchTarget,
)


class TestValidationAndAllowlist:
    @pytest.mark.parametrize(
        "name",
        [
            "balanced",
            "deep",
            "fast",
            "profile_1",
            "custom-profile-v2",
            "BALANCED",
            "A1",
        ],
    )
    def test_valid_profile_names(self, name: str) -> None:
        assert validate_profile_name(name) == name

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "-leading-hyphen",
            "has space",
            "semi;colon",
            "back`tick`",
            "sub$(command)",
            "quote'single",
            'quote"double',
            "new\nline",
            "pipe|command",
            "a" * 65,  # too long
        ],
    )
    def test_invalid_profile_names_rejected(self, name: str) -> None:
        with pytest.raises(ConfigError):
            validate_profile_name(name)

    @pytest.mark.parametrize(
        "model",
        [
            "claude-3-7-sonnet-20250219",
            "claude-3-5-haiku-20241022",
            "o3-mini",
            "gpt-4o",
            "gemini-2.5-pro",
            "anthropic/claude-3.5-sonnet:beta",
            "meta-llama/Llama-3-70b-chat",
            "text-embedding-3-small",
        ],
    )
    def test_valid_model_names(self, model: str) -> None:
        assert validate_model_name(model) == model

    @pytest.mark.parametrize(
        "model",
        [
            "",
            "--dangerously-skip-permissions",
            "-m",
            "claude 3.7",
            "model; rm -rf /",
            "model && whoami",
            "model | cat",
            "`whoami`",
            "$(id)",
            "model'quote",
            'model"quote',
            "model\nnewline",
            "a" * 129,
        ],
    )
    def test_invalid_model_names_rejected(self, model: str) -> None:
        with pytest.raises(ConfigError):
            validate_model_name(model)

    @pytest.mark.parametrize(
        "effort",
        [
            "low",
            "medium",
            "high",
            "none",
            "off",
            "min",
            "max",
            "budget_2048",
            "default",
        ],
    )
    def test_valid_reasoning_effort(self, effort: str) -> None:
        assert validate_reasoning_effort(effort) == effort

    @pytest.mark.parametrize(
        "effort",
        [
            "",
            "--flag",
            "-r",
            "high effort",
            "high;rm",
            "effort`cmd`",
            "effort'quote",
            "effort\nnewline",
            "a" * 33,
        ],
    )
    def test_invalid_reasoning_effort_rejected(self, effort: str) -> None:
        with pytest.raises(ConfigError):
            validate_reasoning_effort(effort)


class TestConfigExtractionAndLoading:
    def test_empty_config_returns_defaults(self) -> None:
        config = extract_execution_profile_config({})
        assert config.default_execution_profile == DEFAULT_EXECUTION_PROFILE
        assert config.default_profile == "balanced"
        assert config.profiles == {}

    def test_extract_valid_execution_profiles(self) -> None:
        raw_config = {
            "default_execution_profile": "deep",
            "execution_profiles": {
                "balanced": {
                    "claude-cli": {
                        "model": "claude-3-7-sonnet-20250219",
                        "reasoning_effort": "medium",
                    },
                    "codex-cli": {
                        "model": "o3-mini",
                        "reasoning_effort": "medium",
                    },
                },
                "deep": {
                    "claude-cli": {
                        "model": "claude-3-7-sonnet-20250219",
                        "reasoning_effort": "high",
                    },
                    "cloud-routine": {
                        "model": "claude-3-7-sonnet-20250219",
                    },
                },
            },
        }
        config = extract_execution_profile_config(raw_config)
        assert config.default_execution_profile == "deep"
        assert "balanced" in config.profiles
        assert "deep" in config.profiles

        claude_balanced = config.profiles["balanced"]["claude-cli"]
        assert isinstance(claude_balanced, TargetExecutionConfig)
        assert claude_balanced.model == "claude-3-7-sonnet-20250219"
        assert claude_balanced.reasoning_effort == "medium"

        cloud_deep = config.profiles["deep"]["cloud-routine"]
        assert cloud_deep.model == "claude-3-7-sonnet-20250219"
        assert cloud_deep.reasoning_effort is None

    def test_hyphen_aliases_supported(self) -> None:
        raw_config = {
            "default-execution-profile": "fast",
            "execution-profiles": {
                "fast": {
                    "claude-cli": {
                        "model": "claude-3-5-haiku-20241022",
                        "reasoning-effort": "low",
                    }
                }
            },
        }
        config = extract_execution_profile_config(raw_config)
        assert config.default_execution_profile == "fast"
        assert "fast" in config.profiles
        assert (
            config.profiles["fast"]["claude-cli"].model == "claude-3-5-haiku-20241022"
        )
        assert config.profiles["fast"]["claude-cli"].reasoning_effort == "low"

    def test_load_from_orchestune_toml_file(self, tmp_path: Path) -> None:
        toml_content = """
default_execution_profile = "balanced"

[execution_profiles.balanced.claude-cli]
model = "claude-3-7-sonnet-20250219"
reasoning_effort = "medium"

[execution_profiles.fast.claude-cli]
model = "claude-3-5-haiku-20241022"
reasoning_effort = "none"
"""
        (tmp_path / "orchestune.toml").write_text(toml_content, encoding="utf-8")
        config = load_execution_profile_config(tmp_path)
        assert config.default_execution_profile == "balanced"
        assert set(config.profiles.keys()) == {"balanced", "fast"}
        assert (
            config.profiles["balanced"]["claude-cli"].model
            == "claude-3-7-sonnet-20250219"
        )

    def test_load_from_pyproject_toml_file(self, tmp_path: Path) -> None:
        toml_content = """
[tool.orchestune]
default-execution-profile = "deep"

[tool.orchestune.execution_profiles.deep.codex-cli]
model = "o3-mini"
reasoning-effort = "high"
"""
        (tmp_path / "pyproject.toml").write_text(toml_content, encoding="utf-8")
        config = load_execution_profile_config(tmp_path)
        assert config.default_execution_profile == "deep"
        assert "deep" in config.profiles
        assert config.profiles["deep"]["codex-cli"].model == "o3-mini"
        assert config.profiles["deep"]["codex-cli"].reasoning_effort == "high"

    def test_orchestune_toml_takes_precedence_over_pyproject(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "orchestune.toml").write_text(
            'default_execution_profile = "balanced"\n', encoding="utf-8"
        )
        (tmp_path / "pyproject.toml").write_text(
            '[tool.orchestune]\ndefault_execution_profile = "deep"\n',
            encoding="utf-8",
        )
        config = load_execution_profile_config(tmp_path)
        assert config.default_execution_profile == "balanced"

    @pytest.mark.parametrize(
        ("invalid_data", "expected_err"),
        [
            ({"default_execution_profile": 123}, "must be a string"),
            (
                {"default_execution_profile": "invalid name!"},
                "invalid execution profile name",
            ),
            ({"execution_profiles": "not-a-dict"}, "must be a table"),
            ({"execution_profiles": {"deep": "not-a-dict"}}, "must be a table"),
            (
                {"execution_profiles": {"deep": {"claude-cli": "not-a-dict"}}},
                "must be a table",
            ),
            (
                {
                    "execution_profiles": {
                        "deep": {"claude-cli": {"unknown_key": "foo"}}
                    }
                },
                "unknown key",
            ),
            (
                {
                    "execution_profiles": {
                        "deep": {"claude-cli": {"model": "--malicious-flag"}}
                    }
                },
                "invalid model name",
            ),
            (
                {
                    "execution_profiles": {
                        "deep": {"claude-cli": {"reasoning_effort": "high; evil"}}
                    }
                },
                "invalid reasoning_effort",
            ),
            (
                {"execution_profiles": {"deep": {"claude-cli": {"model": 123}}}},
                "must be a string",
            ),
            (
                {
                    "execution_profiles": {
                        "deep": {"claude-cli": {"reasoning_effort": 456}}
                    }
                },
                "must be a string",
            ),
            (
                {
                    "default_execution_profile": "deap",
                    "execution_profiles": {
                        "deep": {"claude-cli": {"model": "claude-3-7-sonnet-20250219"}}
                    },
                },
                "is not defined under 'execution_profiles'",
            ),
        ],
    )
    def test_rejects_invalid_configs(
        self, invalid_data: dict, expected_err: str
    ) -> None:
        with pytest.raises(ConfigError) as exc_info:
            extract_execution_profile_config(invalid_data)
        assert expected_err in str(exc_info.value)

    def test_target_name_normalized_at_parse_time(self) -> None:
        raw_config = {
            "default_execution_profile": "deep",
            "execution_profiles": {
                "deep": {
                    "claude_cli": {
                        "model": "claude-3-7-sonnet-20250219",
                        "reasoning_effort": "high",
                    }
                }
            },
        }
        config = extract_execution_profile_config(raw_config)
        assert "claude-cli" in config.profiles["deep"]
        selection = resolve_execution_profile("deep", "claude-cli", config)
        assert selection.model == "claude-3-7-sonnet-20250219"
        assert selection.reasoning_effort == "high"

    def test_duplicate_target_aliases_rejected(self) -> None:
        raw_config = {
            "default_execution_profile": "deep",
            "execution_profiles": {
                "deep": {
                    "claude-cli": {"model": "claude-3-7-sonnet-20250219"},
                    "claude_cli": {"model": "claude-3-5-haiku-20241022"},
                }
            },
        }
        with pytest.raises(ConfigError) as exc_info:
            extract_execution_profile_config(raw_config)
        assert "duplicate target definition" in str(exc_info.value)
        assert "claude_cli" in str(exc_info.value)

    def test_implicit_default_missing_from_profiles_rejected(self) -> None:
        raw_config = {
            "execution_profiles": {
                "deep": {"claude-cli": {"model": "claude-3-7-sonnet-20250219"}}
            }
        }
        with pytest.raises(ConfigError) as exc_info:
            extract_execution_profile_config(raw_config)
        assert "default_execution_profile 'balanced' is not defined" in str(
            exc_info.value
        )


class TestResolveExecutionProfile:
    @pytest.fixture
    def sample_config(self) -> ExecutionProfileConfig:
        return ExecutionProfileConfig(
            default_execution_profile="balanced",
            profiles={
                "balanced": {
                    "claude-cli": TargetExecutionConfig(
                        model="claude-3-7-sonnet-20250219",
                        reasoning_effort="medium",
                    ),
                    "codex-cli": TargetExecutionConfig(
                        model="o3-mini",
                        reasoning_effort="medium",
                    ),
                    "cloud-routine": TargetExecutionConfig(
                        model="claude-3-7-sonnet-20250219",
                        reasoning_effort=None,
                    ),
                },
                "deep": {
                    "claude-cli": TargetExecutionConfig(
                        model="claude-3-7-sonnet-20250219",
                        reasoning_effort="high",
                    ),
                    "codex-cli": TargetExecutionConfig(
                        model="o3-mini",
                        reasoning_effort="high",
                    ),
                },
                "fast": {
                    "claude-cli": TargetExecutionConfig(
                        model="claude-3-5-haiku-20241022",
                        reasoning_effort="low",
                    ),
                },
            },
        )

    def test_unspecified_profile_resolves_to_default_profile(
        self, sample_config: ExecutionProfileConfig
    ) -> None:
        selection = resolve_execution_profile(
            profile=None,
            target="claude-cli",
            config=sample_config,
        )
        assert selection.profile == "balanced"
        assert selection.model == "claude-3-7-sonnet-20250219"
        assert selection.reasoning_effort == "medium"
        assert "default" in selection.reason.lower()

    def test_empty_string_profile_resolves_to_default_profile(
        self, sample_config: ExecutionProfileConfig
    ) -> None:
        selection = resolve_execution_profile(
            profile="",
            target="claude-cli",
            config=sample_config,
        )
        assert selection.profile == "balanced"
        assert selection.model == "claude-3-7-sonnet-20250219"
        assert selection.reasoning_effort == "medium"

    def test_explicit_known_profile_resolves_correctly(
        self, sample_config: ExecutionProfileConfig
    ) -> None:
        selection = resolve_execution_profile(
            profile="deep",
            target="claude-cli",
            config=sample_config,
        )
        assert selection.profile == "deep"
        assert selection.model == "claude-3-7-sonnet-20250219"
        assert selection.reasoning_effort == "high"
        assert "deep" in selection.reason

    def test_unknown_profile_falls_back_to_default_with_warning(
        self,
        sample_config: ExecutionProfileConfig,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING):
            selection = resolve_execution_profile(
                profile="super-quantum",
                target="claude-cli",
                config=sample_config,
            )
        assert selection.profile == "balanced"
        assert selection.model == "claude-3-7-sonnet-20250219"
        assert selection.reasoning_effort == "medium"
        assert "unknown" in selection.reason.lower()
        assert "super-quantum" in selection.reason
        assert "super-quantum" in caplog.text
        assert "falling back to default" in caplog.text

    def test_target_without_mapping_returns_none_model_and_effort(
        self, sample_config: ExecutionProfileConfig
    ) -> None:
        selection = resolve_execution_profile(
            profile="fast",
            target="cloud-routine",
            config=sample_config,
        )
        assert selection.profile == "fast"
        assert selection.model is None
        assert selection.reasoning_effort is None

    def test_resolving_with_dispatcher_config(
        self, sample_config: ExecutionProfileConfig, tmp_path: Path
    ) -> None:
        dispatcher_config = DispatcherConfig(
            execution_profile_config=sample_config,
            events_log_path=tmp_path / "events.jsonl",
        )
        selection = resolve_execution_profile(
            profile="deep",
            target="codex-cli",
            config=dispatcher_config,
        )
        assert selection.profile == "deep"
        assert selection.model == "o3-mini"
        assert selection.reasoning_effort == "high"

    def test_resolving_with_dispatch_target_instances(
        self, sample_config: ExecutionProfileConfig, tmp_path: Path
    ) -> None:
        routine_target = ClaudeCodeCloudRoutineDispatchTarget("r-123", "tok-abc")
        sel_routine = resolve_execution_profile(
            profile="balanced",
            target=routine_target,
            config=sample_config,
        )
        assert sel_routine.profile == "balanced"
        assert sel_routine.model == "claude-3-7-sonnet-20250219"
        assert sel_routine.reasoning_effort is None

        codex_cloud = CodexCloudDispatchTarget("env-1", log_dir=tmp_path)
        sel_cloud = resolve_execution_profile(
            profile="balanced",
            target=codex_cloud,
            config=sample_config,
        )
        assert sel_cloud.profile == "balanced"

        local_target = LocalProcessDispatchTarget(log_dir=tmp_path)
        sel_local = resolve_execution_profile(
            profile="balanced",
            target=local_target,
            config=sample_config,
        )
        assert sel_local.profile == "balanced"

    def test_target_aliases_matching(
        self, sample_config: ExecutionProfileConfig
    ) -> None:
        # "claude" matches "claude-cli"
        sel1 = resolve_execution_profile(
            profile="deep",
            target="claude",
            config=sample_config,
        )
        assert sel1.model == "claude-3-7-sonnet-20250219"
        assert sel1.reasoning_effort == "high"

        # "codex" matches "codex-cli"
        sel2 = resolve_execution_profile(
            profile="deep",
            target="codex",
            config=sample_config,
        )
        assert sel2.model == "o3-mini"
        assert sel2.reasoning_effort == "high"

        # "agy" matches "agy-cli"
        custom_cfg = ExecutionProfileConfig(
            profiles={
                "balanced": {
                    "agy-cli": TargetExecutionConfig(
                        model="gemini-2.5-pro", reasoning_effort="medium"
                    ),
                    "cloud-routine": TargetExecutionConfig(
                        model="claude-3-7-sonnet-20250219"
                    ),
                    "codex-cloud": TargetExecutionConfig(model="o3-mini"),
                }
            }
        )
        sel3 = resolve_execution_profile("balanced", "agy", custom_cfg)
        assert sel3.model == "gemini-2.5-pro"
        sel4 = resolve_execution_profile("balanced", "cloud_routine", custom_cfg)
        assert sel4.model == "claude-3-7-sonnet-20250219"
        sel5 = resolve_execution_profile("balanced", "codex_cloud", custom_cfg)
        assert sel5.model == "o3-mini"

    def test_immutability_and_alias(self) -> None:
        assert TargetProfileConfig is TargetExecutionConfig
        selection = ExecutionSelection(
            profile="balanced",
            model="o3-mini",
            reasoning_effort="medium",
            reason="test",
        )
        with pytest.raises(FrozenInstanceError):
            selection.model = "other"  # type: ignore[misc]

    def test_load_execution_profile_config_from_dict(self) -> None:
        cfg = load_execution_profile_config(
            {"default_execution_profile": "fast", "execution_profiles": {}}
        )
        assert cfg.default_execution_profile == "fast"

    def test_resolve_execution_profile_default_none_config(self) -> None:
        sel = resolve_execution_profile(None, "claude-cli", config=None)
        assert sel.profile == "balanced"
        assert sel.model is None
        assert sel.reasoning_effort is None

    def test_resolve_execution_profile_with_object_having_none_execution_profile_config(
        self,
    ) -> None:
        class DummyHolder:
            execution_profile_config = None

        sel = resolve_execution_profile("balanced", "claude-cli", config=DummyHolder())
        assert sel.profile == "balanced"

    def test_resolve_with_custom_target_instances(self, tmp_path: Path) -> None:
        claude_target = LocalProcessDispatchTarget(
            log_dir=tmp_path, local_cmd='claude -p "{issue_number}"'
        )
        agy_target = LocalProcessDispatchTarget(
            log_dir=tmp_path, local_cmd='agy -p "{issue_number}"'
        )
        codex_target = LocalProcessDispatchTarget(
            log_dir=tmp_path, local_cmd='codex exec "{issue_number}"'
        )
        other_target = LocalProcessDispatchTarget(
            log_dir=tmp_path, local_cmd='echo "{issue_number}"'
        )
        no_cmd_target = LocalProcessDispatchTarget(log_dir=tmp_path, local_cmd=None)

        custom_cfg = ExecutionProfileConfig(
            profiles={
                "balanced": {
                    "claude-cli": TargetExecutionConfig(
                        model="claude-3-7-sonnet-20250219"
                    ),
                    "agy-cli": TargetExecutionConfig(model="gemini-2.5-pro"),
                    "codex-cli": TargetExecutionConfig(model="o3-mini"),
                    "local": TargetExecutionConfig(model="dummy-local"),
                }
            }
        )
        assert (
            resolve_execution_profile("balanced", claude_target, custom_cfg).model
            == "claude-3-7-sonnet-20250219"
        )
        assert (
            resolve_execution_profile("balanced", agy_target, custom_cfg).model
            == "gemini-2.5-pro"
        )
        assert (
            resolve_execution_profile("balanced", codex_target, custom_cfg).model
            == "o3-mini"
        )
        assert (
            resolve_execution_profile("balanced", other_target, custom_cfg).model
            == "dummy-local"
        )
        assert (
            resolve_execution_profile("balanced", no_cmd_target, custom_cfg).model
            == "dummy-local"
        )

        class UnknownTarget:
            pass

        class TargetWithExplicitTargetName:
            target_name = "claude-cli"

        assert (
            resolve_execution_profile(
                "balanced", TargetWithExplicitTargetName(), custom_cfg
            ).model
            == "claude-3-7-sonnet-20250219"
        )

        class TargetWithExplicitName:
            name = "codex-cli"

        assert (
            resolve_execution_profile(
                "balanced", TargetWithExplicitName(), custom_cfg
            ).model
            == "o3-mini"
        )

    def test_unmapped_default_profile_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        custom_cfg = ExecutionProfileConfig(
            default_execution_profile="balanced",
            profiles={
                "deep": {
                    "claude-cli": TargetExecutionConfig(
                        model="claude-3-7-sonnet-20250219"
                    )
                }
            },
        )
        with caplog.at_level(logging.WARNING):
            selection = resolve_execution_profile(None, "claude-cli", custom_cfg)
        assert selection.profile == "balanced"
        assert selection.model is None
        assert "not configured in profiles" in caplog.text

    def test_validate_target_name(self) -> None:
        from orchestune.dispatch.execution_profiles import validate_target_name

        assert validate_target_name("claude-cli") == "claude-cli"
        assert validate_target_name("cloud_routine") == "cloud_routine"
        with pytest.raises(ConfigError):
            validate_target_name("")
        with pytest.raises(ConfigError):
            validate_target_name("-bad-target")
        with pytest.raises(ConfigError):
            validate_target_name("bad target!")


class TestModelTiersConfig:
    def test_extract_valid_model_tiers(self) -> None:
        raw_config = {
            "model_tiers": {
                "strong": {
                    "claude": "claude-3-7-sonnet",
                    "codex": "o3-mini",
                },
                "middle": {
                    "claude": "claude-3-5-sonnet",
                    "codex": "gpt-4o",
                },
                "weak": {
                    "claude": "claude-3-5-haiku",
                    "codex": "gpt-4o-mini",
                },
            }
        }
        config = extract_execution_profile_config(raw_config)
        assert "strong" in config.model_tiers
        assert config.model_tiers["strong"]["claude"] == "claude-3-7-sonnet"
        assert config.model_tiers["middle"]["codex"] == "gpt-4o"
        assert config.model_tiers["weak"]["claude"] == "claude-3-5-haiku"

    def test_hyphen_alias_model_tiers(self) -> None:
        raw_config = {
            "model-tiers": {
                "strong": {
                    "claude-cli": "claude-3-7-sonnet-20250219",
                }
            }
        }
        config = extract_execution_profile_config(raw_config)
        assert (
            config.model_tiers["strong"]["claude-cli"] == "claude-3-7-sonnet-20250219"
        )

    def test_invalid_tier_name_rejected(self) -> None:
        raw_config = {
            "model_tiers": {
                "ultra": {
                    "claude": "claude-3-7-sonnet",
                }
            }
        }
        with pytest.raises(ConfigError, match="invalid model tier"):
            extract_execution_profile_config(raw_config)

    @pytest.mark.parametrize(
        ("invalid_data", "expected_err"),
        [
            ({"model_tiers": "not-a-dict"}, "must be a table"),
            ({"model_tiers": {"strong": "not-a-dict"}}, "must be a table"),
            ({"model_tiers": {"strong": {"claude": 123}}}, "must be a string"),
            (
                {"model_tiers": {"strong": {"claude": "--dangerously-skip"}}},
                "invalid model name",
            ),
            (
                {"model_tiers": {"strong": {"-bad-target": "claude-3-7-sonnet"}}},
                "invalid target name",
            ),
        ],
    )
    def test_invalid_model_tiers_rejected(
        self, invalid_data: dict, expected_err: str
    ) -> None:
        with pytest.raises(ConfigError) as exc_info:
            extract_execution_profile_config(invalid_data)
        assert expected_err in str(exc_info.value)


class TestResolveModelTier:
    def test_default_built_in_tiers_resolution(self) -> None:
        # Without any config, built-in defaults are used
        sel_strong_claude = resolve_execution_profile(
            profile=None, target="claude-cli", config=None, model_tier="strong"
        )
        assert sel_strong_claude.model == "claude-3-7-sonnet"

        sel_middle_codex = resolve_execution_profile(
            profile=None, target="codex-cli", config=None, model_tier="middle"
        )
        assert sel_middle_codex.model == "gpt-4o"

        sel_weak_agy = resolve_execution_profile(
            profile=None, target="agy-cli", config=None, model_tier="weak"
        )
        assert sel_weak_agy.model == "gemini-2.5-flash-lite"

    def test_custom_model_tiers_override_builtin(self) -> None:
        config = ExecutionProfileConfig(
            model_tiers={
                "strong": {
                    "claude-cli": "custom-claude-strong",
                }
            }
        )
        sel = resolve_execution_profile(
            profile=None, target="claude-cli", config=config, model_tier="strong"
        )
        assert sel.model == "custom-claude-strong"

    def test_model_tier_overrides_profile_model_and_retains_reasoning_effort(
        self,
    ) -> None:
        config = ExecutionProfileConfig(
            profiles={
                "deep": {
                    "claude-cli": TargetExecutionConfig(
                        model="default-profile-model",
                        reasoning_effort="high",
                    )
                }
            }
        )
        sel = resolve_execution_profile(
            profile="deep",
            target="claude-cli",
            config=config,
            model_tier="weak",
        )
        assert sel.model == "claude-3-5-haiku"
        assert sel.reasoning_effort == "high"
        assert "model_tier" in sel.reason

    def test_unknown_model_tier_logs_warning_and_falls_back(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        config = ExecutionProfileConfig(
            profiles={
                "balanced": {
                    "claude-cli": TargetExecutionConfig(
                        model="claude-3-7-sonnet-20250219",
                        reasoning_effort="medium",
                    )
                }
            }
        )
        with caplog.at_level(logging.WARNING):
            sel = resolve_execution_profile(
                profile="balanced",
                target="claude-cli",
                config=config,
                model_tier="unknown-tier",
            )
        assert sel.model == "claude-3-7-sonnet-20250219"
        assert sel.reasoning_effort == "medium"
        assert "unknown-tier" in caplog.text

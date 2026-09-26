"""Tests for Issue #1035: orchestune-dispatch CLI & TOML configuration overhaul."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from orchestune.dispatch.config_loader import (
    ConfigError,
    load_and_resolve_config,
    resolve_parent_issue,
    validate_toml_config,
)
from orchestune.dispatch.dispatcher import _build_arg_parser
from orchestune.dispatch.execution_profiles import (
    ExecutionProfileConfig,
    TargetExecutionConfig,
    resolve_task_execution_selection,
    validate_profile_for_target,
)


@pytest.fixture(autouse=True)
def _resolve_legacy_temp_cwds(monkeypatch):
    from orchestune.dispatch import dispatcher

    resolve = dispatcher._resolve_dispatch_shared_paths
    repository_cwd = Path.cwd()

    def _resolve(args, cwd):
        if cwd is not None and not cwd.resolve().is_relative_to(repository_cwd):
            cwd = repository_cwd
        return resolve(args, cwd)

    monkeypatch.setattr(dispatcher, "_resolve_dispatch_shared_paths", _resolve)


class TestCliOptionsSurface:
    """Acceptance condition: CLI exposed options are strictly the 6 routine options + help."""

    def test_cli_accepts_six_public_options(self):
        parser = _build_arg_parser()
        args = parser.parse_args(
            [
                "-p",
                "1035",
                "--no-apply",
                "--dispatch-target",
                "local",
                "--max-concurrent",
                "4",
                "--profile",
                "fast-code",
                "--allow-unsafe-agent-execution",
            ]
        )
        assert args.parent_issue == 1035
        assert args.apply is False
        assert args.dispatch_target == "local"
        assert args.max_concurrent == 4
        assert args.profile == "fast-code"
        assert args.allow_unsafe_agent_execution is True

    def test_cli_parent_issue_long_and_short_options_agree(self):
        parser = _build_arg_parser()
        short_args = parser.parse_args(["-p", "1035"])
        long_args = parser.parse_args(["--parent-issue", "1035"])
        assert short_args.parent_issue == 1035
        assert long_args.parent_issue == 1035

    def test_cli_parent_issue_rejects_negative_or_zero(self):
        parser = _build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["-p", "0"])
        with pytest.raises(SystemExit):
            parser.parse_args(["--parent-issue", "-5"])

    @pytest.mark.parametrize(
        "removed_option",
        [
            "--model",
            "--reasoning-effort",
            "--effort",
            "--local-cmd",
            "--reviewer-bot",
            "--routine-id",
            "--routine-token",
            "--codex-cloud-env",
            "--ci-command",
            "--max-tokens-per-window",
            "--max-tokens-per-task",
            "--run-state-path",
            "--worktree-root",
            "--log-dir",
            "--events-log-path",
            "--not-needed-review-state-path",
            "--early-death-window-seconds",
            "--max-early-death-retries",
            "--early-death-backoff-seconds",
            "--deviation-buffer-lines",
            "--max-recompute-retries",
            "--task-timeout-seconds",
            "--max-task-reclaims",
            "--not-needed-review-timeout-seconds",
            "--window-seconds",
            "--max-launches-per-window",
            "--zombie-gc",
            "--consistency-mode",
            "--consistency-repair-code",
            "--consistency-max-repair-passes",
        ],
    )
    def test_cli_rejects_removed_options(self, removed_option):
        parser = _build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([removed_option, "val"])


class TestTomlSchemaAndValidation:
    """Acceptance condition: TOML configuration schema, type checks, and prohibited keys."""

    def test_toml_validates_types_and_ranges(self):
        valid_data = {
            "apply": False,
            "max_concurrent": 3,
            "window_seconds": 1800,
            "max_tokens_per_window": 50000,
            "max_tokens_per_task": 10000,
            "dispatch_target": "codex-cli",
            "reviewer_bot": "claude",
            "consistency_mode": "shadow",
            "consistency_repair_code": ["status.primary-conflict"],
            "consistency_max_repair_passes": 2,
            "run_state_path": "custom/state.json",
        }
        validated = validate_toml_config(valid_data)
        assert validated["apply"] is False
        assert validated["max_concurrent"] == 3
        assert validated["max_tokens_per_window"] == 50000

    def test_toml_rejects_boolean_for_integer_tokens(self):
        with pytest.raises(ConfigError, match="must be an integer"):
            validate_toml_config({"max_tokens_per_window": True})
        with pytest.raises(ConfigError, match="must be an integer"):
            validate_toml_config({"max_tokens_per_task": False})

    def test_toml_rejects_negative_tokens(self):
        with pytest.raises(ConfigError, match="greater than or equal to 0"):
            validate_toml_config({"max_tokens_per_window": -100})

    def test_toml_rejects_unknown_keys(self):
        with pytest.raises(ConfigError, match="unknown key 'foo_bar'"):
            validate_toml_config({"foo_bar": 123})

    @pytest.mark.parametrize(
        ("key", "val"),
        [
            ("routine_token", "secret123"),
            ("routine-token", "secret123"),
            ("parent_issue", 1035),
            ("parent-issue", 1035),
            ("parent_issue_number", 1035),
            ("parent-issue-number", 1035),
            ("allow_unsafe_agent_execution", True),
            ("allow-unsafe-agent-execution", True),
            ("model", "gpt-5"),
            ("reasoning_effort", "high"),
            ("effort", "high"),
        ],
    )
    def test_toml_rejects_prohibited_keys(self, key, val):
        with pytest.raises(ConfigError):
            validate_toml_config({key: val})

    def test_error_message_never_leaks_token(self):
        secret = "secret-token-value-never-leak-999"
        try:
            validate_toml_config({"routine_token": secret})
        except ConfigError as e:
            assert secret not in str(e)


class TestPrecedenceAndCloudSettings:
    """Acceptance condition: CLI > TOML > default precedence, env vars for cloud settings."""

    def test_cli_no_apply_overrides_toml_apply_true(self, tmp_path, fake_forge):
        toml_content = "apply = true\nmax-concurrent = 5\n"
        (tmp_path / "orchestune.toml").write_text(toml_content, encoding="utf-8")

        config = load_and_resolve_config(
            cli_args=["-p", "1035", "--no-apply", "--allow-unsafe-agent-execution"],
            cwd=tmp_path,
            forge=fake_forge,
        )
        assert config.apply is False
        assert config.max_concurrent == 5

    def test_cli_apply_overrides_toml_apply_false(self, tmp_path, fake_forge):
        toml_content = "apply = false\n"
        (tmp_path / "orchestune.toml").write_text(toml_content, encoding="utf-8")

        config = load_and_resolve_config(
            cli_args=["-p", "1035", "--apply", "--allow-unsafe-agent-execution"],
            cwd=tmp_path,
            forge=fake_forge,
        )
        assert config.apply is True

    def test_cloud_routine_id_precedence_env_over_toml(
        self, monkeypatch, tmp_path, fake_forge
    ):
        monkeypatch.setenv("ORCHESTUNE_ROUTINE_ID", "env-routine-id")
        monkeypatch.setenv("ORCHESTUNE_ROUTINE_TOKEN", "fake-token")
        toml_content = (
            "routine_id = 'toml-routine-id'\ndispatch-target = 'cloud-routine'\n"
        )
        (tmp_path / "orchestune.toml").write_text(toml_content, encoding="utf-8")

        config = load_and_resolve_config(
            cli_args=["-p", "1035"],
            cwd=tmp_path,
            forge=fake_forge,
        )
        assert getattr(config.dispatch_target, "_routine_id", None) == "env-routine-id"

    def test_codex_cloud_env_precedence_env_over_toml(
        self, monkeypatch, tmp_path, fake_forge
    ):
        monkeypatch.setenv("ORCHESTUNE_CODEX_CLOUD_ENV", "env-cloud-env")
        toml_content = (
            "codex_cloud_env = 'toml-cloud-env'\ndispatch-target = 'codex-cloud'\n"
        )
        (tmp_path / "orchestune.toml").write_text(toml_content, encoding="utf-8")

        config = load_and_resolve_config(
            cli_args=["-p", "1035"],
            cwd=tmp_path,
            forge=fake_forge,
        )
        assert (
            getattr(config.dispatch_target, "_environment_id", None) == "env-cloud-env"
        )


class TestParentIssueResolution:
    """Acceptance condition: CLI -p / --parent-issue > branch inference parent/issue-<int>."""

    def test_cli_explicit_parent_issue_overrides_branch_name(self, monkeypatch):
        monkeypatch.setattr(
            "orchestune.dispatch.config_loader._get_current_git_branch",
            lambda cwd: "parent/issue-9999",
        )
        resolved = resolve_parent_issue(cli_parent_issue=1035, cwd=Path.cwd())
        assert resolved == 1035

    def test_branch_inference_matches_parent_issue_pattern(self, monkeypatch):
        monkeypatch.setattr(
            "orchestune.dispatch.config_loader._get_current_git_branch",
            lambda cwd: "parent/issue-1035",
        )
        resolved = resolve_parent_issue(cli_parent_issue=None, cwd=Path.cwd())
        assert resolved == 1035

    @pytest.mark.parametrize(
        "branch",
        [
            "main",
            "parent/issue-0",
            "parent/issue-abc",
            "claude/issue-1035-task-1035",
            "HEAD",
            "",
        ],
    )
    def test_uninferrable_branch_raises_error_without_cli_parent(
        self, branch, monkeypatch
    ):
        monkeypatch.setattr(
            "orchestune.dispatch.config_loader._get_current_git_branch",
            lambda cwd: branch,
        )
        with pytest.raises(ConfigError, match="-p / --parent-issue"):
            resolve_parent_issue(cli_parent_issue=None, cwd=Path.cwd())


class TestProfileOverrideAndValidation:
    """Acceptance condition: --profile overrides task profile and model tier, validates before side-effects."""

    def test_cli_profile_overrides_task_profile_and_tier(self):
        profile_cfg = ExecutionProfileConfig(
            default_execution_profile="balanced",
            profiles={
                "balanced": {
                    "codex-cli": TargetExecutionConfig(
                        model="gpt-5.6-terra", reasoning_effort="medium"
                    )
                },
                "deep-reasoning": {
                    "codex-cli": TargetExecutionConfig(
                        model="gpt-5.6-sol", reasoning_effort="high"
                    )
                },
            },
            model_tiers={"weak": {"codex-cli": "gpt-5.6-luna"}},
        )
        task = MagicMock(
            issue_number=1,
            subtask_id="task-1",
            execution_profile="balanced",
            model_tier="weak",
        )
        # With profile override
        config_with_profile = MagicMock(
            profile="deep-reasoning",
            dispatch_target=MagicMock(target_name="codex-cli"),
            execution_profile_config=profile_cfg,
        )

        sel = resolve_task_execution_selection(task, config_with_profile)
        assert sel.profile == "deep-reasoning"
        assert sel.model == "gpt-5.6-sol"
        assert sel.reasoning_effort == "high"

        # Without profile override
        config_no_profile = MagicMock(
            profile=None,
            dispatch_target=MagicMock(target_name="codex-cli"),
            execution_profile_config=profile_cfg,
        )

        sel2 = resolve_task_execution_selection(task, config_no_profile)
        assert sel2.profile == "balanced"
        assert sel2.model == "gpt-5.6-luna"  # tier applied

    def test_unknown_profile_raises_before_side_effects(self):
        profile_cfg = ExecutionProfileConfig(
            profiles={"balanced": {}},
        )
        with pytest.raises(ConfigError, match="unknown profile"):
            validate_profile_for_target("nonexistent", "codex-cli", profile_cfg)

    def test_profile_missing_target_raises_before_side_effects(self):
        profile_cfg = ExecutionProfileConfig(
            profiles={"balanced": {"codex-cli": TargetExecutionConfig()}},
        )
        with pytest.raises(ConfigError, match="target 'claude-cli' is not configured"):
            validate_profile_for_target("balanced", "claude-cli", profile_cfg)

    def test_load_and_resolve_config_validates_profile_against_resolved_auto_target(
        self, tmp_path, fake_forge
    ):
        toml_content = """
dispatch_target = "auto"
default_execution_profile = "fast-code"

[execution_profiles.fast-code.claude-cli]
model = "claude-3-5-sonnet"
"""
        (tmp_path / "orchestune.toml").write_text(toml_content, encoding="utf-8")

        mock_target = MagicMock()
        mock_target.target_name = "claude-cli"

        config = load_and_resolve_config(
            cli_args=["-p", "1035", "--profile", "fast-code"],
            cwd=tmp_path,
            forge=fake_forge,
            build_target_fn=lambda _target_cfg: mock_target,
        )
        assert config.profile == "fast-code"

    def test_load_and_resolve_config_rejects_profile_missing_resolved_auto_target(
        self, tmp_path, fake_forge
    ):
        toml_content = """
dispatch_target = "auto"
default_execution_profile = "fast-code"

[execution_profiles.fast-code.codex-cli]
model = "gpt-5.6"
"""
        (tmp_path / "orchestune.toml").write_text(toml_content, encoding="utf-8")

        mock_target = MagicMock()
        mock_target.target_name = "claude-cli"

        with pytest.raises(
            ConfigError,
            match="target 'claude-cli' is not configured in profile 'fast-code'",
        ):
            load_and_resolve_config(
                cli_args=["-p", "1035", "--profile", "fast-code"],
                cwd=tmp_path,
                forge=fake_forge,
                build_target_fn=lambda _target_cfg: mock_target,
            )

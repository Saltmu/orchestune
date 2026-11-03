"""Build and detect local dispatch targets."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from orchestune.dispatch.targets import (
    AGY_CLI_LOCAL_CMD_TEMPLATE,
    CLAUDE_CLI_LOCAL_CMD_TEMPLATE,
    CODEX_CLI_LOCAL_CMD_TEMPLATE,
    ClaudeCodeCloudRoutineDispatchTarget,
    CodexCloudDispatchTarget,
    LocalProcessDispatchTarget,
    TargetBuildConfig,
    build_dispatch_target,
    detect_installed_local_cli,
    resolve_default_dispatch_target_name,
)
from tests.test_dispatch_targets_local import _task


class TestDetectInstalledLocalCli:
    def test_returns_claude_when_only_claude_installed(self):
        with patch(
            "orchestune.dispatch.targets.shutil.which",
            side_effect=lambda name: "/usr/bin/claude" if name == "claude" else None,
        ):
            assert detect_installed_local_cli() == "claude"

    def test_returns_agy_when_only_agy_installed(self):
        with patch(
            "orchestune.dispatch.targets.shutil.which",
            side_effect=lambda name: "/usr/bin/agy" if name == "agy" else None,
        ):
            assert detect_installed_local_cli() == "agy"

    def test_returns_codex_when_only_codex_installed(self):
        with patch(
            "orchestune.dispatch.targets.shutil.which",
            side_effect=lambda name: "/usr/bin/codex" if name == "codex" else None,
        ):
            assert detect_installed_local_cli() == "codex"

    def test_prefers_claude_when_all_three_installed(self):
        with patch(
            "orchestune.dispatch.targets.shutil.which",
            side_effect=lambda name: f"/usr/bin/{name}",
        ):
            assert detect_installed_local_cli() == "claude"

    def test_prefers_claude_over_codex_when_both_installed(self):
        with patch(
            "orchestune.dispatch.targets.shutil.which",
            side_effect=lambda name: f"/usr/bin/{name}"
            if name in ("claude", "codex")
            else None,
        ):
            assert detect_installed_local_cli() == "claude"

    def test_prefers_agy_over_codex_when_both_installed(self):
        with patch(
            "orchestune.dispatch.targets.shutil.which",
            side_effect=lambda name: f"/usr/bin/{name}"
            if name in ("agy", "codex")
            else None,
        ):
            assert detect_installed_local_cli() == "agy"

    def test_returns_none_when_none_installed(self):
        with patch("orchestune.dispatch.targets.shutil.which", return_value=None):
            assert detect_installed_local_cli() is None


class TestBuildDispatchTarget:
    def test_local_name_returns_local_process_target(self, tmp_path):
        target = build_dispatch_target(
            TargetBuildConfig("local", None, None, tmp_path / "logs")
        )
        assert isinstance(target, LocalProcessDispatchTarget)

    def test_cloud_routine_with_credentials_returns_cloud_target(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("ORCHESTUNE_ROUTINE_ID", raising=False)
        monkeypatch.delenv("ORCHESTUNE_ROUTINE_TOKEN", raising=False)
        target = build_dispatch_target(
            TargetBuildConfig("cloud-routine", "trig_1", "token", tmp_path / "logs")
        )
        assert isinstance(target, ClaudeCodeCloudRoutineDispatchTarget)

    def test_cloud_routine_resolves_credentials_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ORCHESTUNE_ROUTINE_ID", "trig_env")
        monkeypatch.setenv("ORCHESTUNE_ROUTINE_TOKEN", "token_env")
        target = build_dispatch_target(
            TargetBuildConfig("cloud-routine", None, None, tmp_path / "logs")
        )
        assert isinstance(target, ClaudeCodeCloudRoutineDispatchTarget)

    def test_builds_codex_cloud_target_with_explicit_environment(self, tmp_path):
        target = build_dispatch_target(
            TargetBuildConfig(
                "codex-cloud",
                None,
                None,
                tmp_path / "logs",
                codex_cloud_env="env_123",
            )
        )
        assert isinstance(target, CodexCloudDispatchTarget)

    def test_codex_cloud_without_environment_falls_back_to_dummy(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.delenv("ORCHESTUNE_CODEX_CLOUD_ENV", raising=False)
        target = build_dispatch_target(
            TargetBuildConfig("codex-cloud", None, None, tmp_path / "logs")
        )
        assert isinstance(target, LocalProcessDispatchTarget)
        assert target._local_cmd is None
        assert "ORCHESTUNE_CODEX_CLOUD_ENV" in capsys.readouterr().err

    @pytest.mark.parametrize("target_name", ["cloud-routine", "codex-cloud"])
    def test_cloud_fallbacks_re_resolve_auto_reviewer_for_custom_local_cmd(
        self, tmp_path, monkeypatch, capsys, target_name
    ):
        monkeypatch.delenv("ORCHESTUNE_CODEX_CLOUD_ENV", raising=False)
        monkeypatch.delenv("ORCHESTUNE_ROUTINE_ID", raising=False)
        monkeypatch.delenv("ORCHESTUNE_ROUTINE_TOKEN", raising=False)
        target = build_dispatch_target(
            TargetBuildConfig(
                target_name,
                None,
                None,
                tmp_path / "logs",
                local_cmd="runner --reviewer {reviewer_bot}",
                reviewer_bot="auto",
            )
        )

        assert isinstance(target, LocalProcessDispatchTarget)
        assert target._reviewer_bot is None
        assert "レビュアーボットを自動選択できません" in capsys.readouterr().err

    def test_cloud_routine_without_credentials_falls_back_to_local(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("ORCHESTUNE_ROUTINE_ID", raising=False)
        monkeypatch.delenv("ORCHESTUNE_ROUTINE_TOKEN", raising=False)
        target = build_dispatch_target(
            TargetBuildConfig("cloud-routine", None, None, tmp_path / "logs")
        )
        assert isinstance(target, LocalProcessDispatchTarget)

    def test_local_with_local_cmd_propagates_to_target(self, tmp_path):
        target = build_dispatch_target(
            TargetBuildConfig(
                "local", None, None, tmp_path / "logs", local_cmd="agy {issue_number}"
            )
        )
        assert isinstance(target, LocalProcessDispatchTarget)
        assert target._local_cmd == "agy {issue_number}"

    def test_claude_cli_without_local_cmd_uses_preset_template(self, tmp_path):
        target = build_dispatch_target(
            TargetBuildConfig(
                "claude-cli",
                None,
                None,
                tmp_path / "logs",
                allow_unsafe_agent_execution=True,
            )
        )
        assert isinstance(target, LocalProcessDispatchTarget)
        assert target._local_cmd == CLAUDE_CLI_LOCAL_CMD_TEMPLATE

    def test_claude_cli_preset_bypasses_permission_prompts(self, tmp_path):
        target = build_dispatch_target(
            TargetBuildConfig(
                "claude-cli",
                None,
                None,
                tmp_path / "logs",
                allow_unsafe_agent_execution=True,
            )
        )
        assert "--permission-mode bypassPermissions" in target._local_cmd

    def test_claude_cli_with_explicit_local_cmd_overrides_preset(self, tmp_path):
        target = build_dispatch_target(
            TargetBuildConfig(
                "claude-cli",
                None,
                None,
                tmp_path / "logs",
                local_cmd="claude -p 'custom {issue_number}'",
                allow_unsafe_agent_execution=True,
            )
        )
        assert isinstance(target, LocalProcessDispatchTarget)
        assert target._local_cmd == "claude -p 'custom {issue_number}'"

    def test_agy_cli_without_local_cmd_uses_preset_template(self, tmp_path):
        target = build_dispatch_target(
            TargetBuildConfig(
                "agy-cli",
                None,
                None,
                tmp_path / "logs",
                allow_unsafe_agent_execution=True,
            )
        )
        assert isinstance(target, LocalProcessDispatchTarget)
        assert target._local_cmd == AGY_CLI_LOCAL_CMD_TEMPLATE

    def test_agy_cli_preset_bypasses_permission_prompts(self, tmp_path):
        target = build_dispatch_target(
            TargetBuildConfig(
                "agy-cli",
                None,
                None,
                tmp_path / "logs",
                allow_unsafe_agent_execution=True,
            )
        )
        assert "--sandbox" not in target._local_cmd
        assert "--dangerously-skip-permissions" in target._local_cmd
        assert "--add-dir ." in target._local_cmd
        assert "--print-timeout 60m" in target._local_cmd

    def test_claude_cli_preset_instructs_noninteractive_execution(self, tmp_path):
        # #157: 非対話型のバックグラウンド起動ではplanning_modeの承認待ちで
        # 停止してしまうため、プロンプト側で自動実行であることを明示する。
        target = build_dispatch_target(
            TargetBuildConfig(
                "claude-cli",
                None,
                None,
                tmp_path / "logs",
                allow_unsafe_agent_execution=True,
            )
        )
        assert "非対話" in target._local_cmd
        assert "承認待ちで停止せず" in target._local_cmd

    def test_claude_cli_preset_instructs_base_branch(self, tmp_path):
        target = build_dispatch_target(
            TargetBuildConfig(
                "claude-cli",
                None,
                None,
                tmp_path / "logs",
                allow_unsafe_agent_execution=True,
            )
        )
        assert "{base_branch}" in target._local_cmd
        assert "ベースブランチ" in target._local_cmd

    def test_agy_cli_preset_instructs_noninteractive_execution(self, tmp_path):
        target = build_dispatch_target(
            TargetBuildConfig(
                "agy-cli",
                None,
                None,
                tmp_path / "logs",
                allow_unsafe_agent_execution=True,
            )
        )
        assert "非対話" in target._local_cmd
        assert "承認待ちで停止せず" in target._local_cmd

    def test_agy_cli_preset_instructs_base_branch(self, tmp_path):
        target = build_dispatch_target(
            TargetBuildConfig(
                "agy-cli",
                None,
                None,
                tmp_path / "logs",
                allow_unsafe_agent_execution=True,
            )
        )
        assert "{base_branch}" in target._local_cmd
        assert "ベースブランチ" in target._local_cmd

    def test_agy_cli_with_explicit_local_cmd_overrides_preset(self, tmp_path):
        target = build_dispatch_target(
            TargetBuildConfig(
                "agy-cli",
                None,
                None,
                tmp_path / "logs",
                local_cmd="agy -p 'custom {issue_number}'",
                allow_unsafe_agent_execution=True,
            )
        )
        assert isinstance(target, LocalProcessDispatchTarget)
        assert target._local_cmd == "agy -p 'custom {issue_number}'"

    def test_codex_cli_without_local_cmd_uses_preset_template(self, tmp_path):
        target = build_dispatch_target(
            TargetBuildConfig(
                "codex-cli",
                None,
                None,
                tmp_path / "logs",
                allow_unsafe_agent_execution=True,
            )
        )
        assert isinstance(target, LocalProcessDispatchTarget)
        assert target._local_cmd == CODEX_CLI_LOCAL_CMD_TEMPLATE

    def test_codex_cli_preset_instructs_base_branch(self, tmp_path):
        target = build_dispatch_target(
            TargetBuildConfig(
                "codex-cli",
                None,
                None,
                tmp_path / "logs",
                allow_unsafe_agent_execution=True,
            )
        )
        assert "{base_branch}" in target._local_cmd
        assert "ベースブランチ" in target._local_cmd

    @pytest.mark.parametrize(
        ("target_name", "expected_reviewer"),
        [
            ("claude-cli", "codex"),
            ("agy-cli", "claude"),
            ("codex-cli", "claude"),
        ],
    )
    def test_local_preset_injects_cross_vendor_reviewer(
        self, tmp_path, target_name, expected_reviewer
    ):
        target = build_dispatch_target(
            TargetBuildConfig(
                target_name,
                None,
                None,
                tmp_path / "logs",
                allow_unsafe_agent_execution=True,
                reviewer_bot="auto",
            )
        )

        assert f"レビュー担当には必ず `{expected_reviewer}`" in target._local_cmd

    def test_explicit_reviewer_overrides_local_cross_vendor_default(self, tmp_path):
        target = build_dispatch_target(
            TargetBuildConfig(
                "claude-cli",
                None,
                None,
                tmp_path / "logs",
                allow_unsafe_agent_execution=True,
                reviewer_bot="claude",
            )
        )

        assert "レビュー担当には必ず `claude`" in target._local_cmd

    def test_cloud_targets_inject_cross_vendor_reviewer(self, tmp_path):
        claude_target = build_dispatch_target(
            TargetBuildConfig(
                "cloud-routine",
                "routine",
                "token",
                tmp_path / "logs",
                reviewer_bot="auto",
            )
        )
        codex_target = build_dispatch_target(
            TargetBuildConfig(
                "codex-cloud",
                None,
                None,
                tmp_path / "logs",
                codex_cloud_env="env-1",
                reviewer_bot="auto",
            )
        )

        assert isinstance(claude_target, ClaudeCodeCloudRoutineDispatchTarget)
        assert "レビュー担当には必ず `codex`" in claude_target._build_text(
            _task(), "agent/issue-1"
        )
        assert isinstance(codex_target, CodexCloudDispatchTarget)
        assert "レビュー担当には必ず `claude`" in codex_target._build_prompt(
            _task(), "agent/issue-1"
        )

    def test_generic_local_auto_reviewer_warns_and_stays_unresolved(
        self, tmp_path, capsys
    ):
        target = build_dispatch_target(
            TargetBuildConfig(
                "local",
                None,
                None,
                tmp_path / "logs",
                reviewer_bot="auto",
            )
        )

        assert isinstance(target, LocalProcessDispatchTarget)
        assert target._reviewer_bot is None
        assert "レビュアーボットを自動選択できません" in capsys.readouterr().err

    def test_codex_cli_preset_bypasses_approvals_and_sandbox(self, tmp_path):
        target = build_dispatch_target(
            TargetBuildConfig(
                "codex-cli",
                None,
                None,
                tmp_path / "logs",
                allow_unsafe_agent_execution=True,
            )
        )
        assert "--dangerously-bypass-approvals-and-sandbox" in target._local_cmd

    def test_codex_cli_preset_instructs_noninteractive_execution(self, tmp_path):
        target = build_dispatch_target(
            TargetBuildConfig(
                "codex-cli",
                None,
                None,
                tmp_path / "logs",
                allow_unsafe_agent_execution=True,
            )
        )
        assert "非対話" in target._local_cmd
        assert "承認待ちで停止せず" in target._local_cmd

    def test_codex_cli_with_explicit_local_cmd_overrides_preset(self, tmp_path):
        target = build_dispatch_target(
            TargetBuildConfig(
                "codex-cli",
                None,
                None,
                tmp_path / "logs",
                local_cmd="codex exec 'custom {issue_number}'",
                allow_unsafe_agent_execution=True,
            )
        )
        assert isinstance(target, LocalProcessDispatchTarget)
        assert target._local_cmd == "codex exec 'custom {issue_number}'"

    def test_auto_resolves_to_claude_cli_when_claude_installed(self, tmp_path):
        with patch(
            "orchestune.dispatch.targets.shutil.which",
            side_effect=lambda name: "/usr/bin/claude" if name == "claude" else None,
        ):
            target = build_dispatch_target(
                TargetBuildConfig(
                    "auto",
                    None,
                    None,
                    tmp_path / "logs",
                    allow_unsafe_agent_execution=True,
                )
            )
        assert isinstance(target, LocalProcessDispatchTarget)
        assert target._local_cmd == CLAUDE_CLI_LOCAL_CMD_TEMPLATE

    def test_auto_prefers_claude_when_all_installed(self, tmp_path):
        with patch(
            "orchestune.dispatch.targets.shutil.which",
            side_effect=lambda name: f"/usr/bin/{name}",
        ):
            target = build_dispatch_target(
                TargetBuildConfig(
                    "auto",
                    None,
                    None,
                    tmp_path / "logs",
                    allow_unsafe_agent_execution=True,
                )
            )
        assert target._local_cmd == CLAUDE_CLI_LOCAL_CMD_TEMPLATE

    def test_auto_falls_back_to_agy_cli_when_only_agy_installed(self, tmp_path):
        with patch(
            "orchestune.dispatch.targets.shutil.which",
            side_effect=lambda name: "/usr/bin/agy" if name == "agy" else None,
        ):
            target = build_dispatch_target(
                TargetBuildConfig(
                    "auto",
                    None,
                    None,
                    tmp_path / "logs",
                    allow_unsafe_agent_execution=True,
                )
            )
        assert isinstance(target, LocalProcessDispatchTarget)
        assert target._local_cmd == AGY_CLI_LOCAL_CMD_TEMPLATE

    def test_auto_falls_back_to_codex_cli_when_only_codex_installed(self, tmp_path):
        with patch(
            "orchestune.dispatch.targets.shutil.which",
            side_effect=lambda name: "/usr/bin/codex" if name == "codex" else None,
        ):
            target = build_dispatch_target(
                TargetBuildConfig(
                    "auto",
                    None,
                    None,
                    tmp_path / "logs",
                    allow_unsafe_agent_execution=True,
                )
            )
        assert isinstance(target, LocalProcessDispatchTarget)
        assert target._local_cmd == CODEX_CLI_LOCAL_CMD_TEMPLATE

    def test_auto_warns_and_falls_back_to_dummy_when_none_installed(
        self, tmp_path, capsys
    ):
        with patch("orchestune.dispatch.targets.shutil.which", return_value=None):
            target = build_dispatch_target(
                TargetBuildConfig("auto", None, None, tmp_path / "logs")
            )
        assert isinstance(target, LocalProcessDispatchTarget)
        assert target._local_cmd is None
        captured = capsys.readouterr()
        assert "claude" in captured.err
        assert "agy" in captured.err
        assert "codex" in captured.err
        assert captured.err.count("警告:") == 1

    def test_auto_with_explicit_local_cmd_overrides_detected_preset(self, tmp_path):
        with patch(
            "orchestune.dispatch.targets.shutil.which",
            side_effect=lambda name: "/usr/bin/claude" if name == "claude" else None,
        ):
            target = build_dispatch_target(
                TargetBuildConfig(
                    "auto",
                    None,
                    None,
                    tmp_path / "logs",
                    local_cmd="claude -p 'custom {issue_number}'",
                    allow_unsafe_agent_execution=True,
                )
            )
        assert target._local_cmd == "claude -p 'custom {issue_number}'"

    def test_auto_with_explicit_local_cmd_used_even_when_none_detected(self, tmp_path):
        with patch("orchestune.dispatch.targets.shutil.which", return_value=None):
            target = build_dispatch_target(
                TargetBuildConfig(
                    "auto", None, None, tmp_path / "logs", local_cmd="custom-cmd"
                )
            )
        assert isinstance(target, LocalProcessDispatchTarget)
        assert target._local_cmd == "custom-cmd"

    def test_unsafe_cli_without_allow_unsafe_flag_raises_error(self, tmp_path):
        for target_name in ["claude-cli", "agy-cli", "codex-cli", "grok-cli"]:
            with pytest.raises(ValueError) as excinfo:
                build_dispatch_target(
                    TargetBuildConfig(target_name, None, None, tmp_path / "logs")
                )
            assert "完全権限実行となります" in str(excinfo.value)
            assert "--allow-unsafe-agent-execution" in str(excinfo.value)

    def test_auto_with_detected_cli_without_allow_unsafe_flag_raises_error(
        self, tmp_path
    ):
        with patch(
            "orchestune.dispatch.targets.shutil.which",
            side_effect=lambda name: "/usr/bin/claude" if name == "claude" else None,
        ):
            with pytest.raises(ValueError) as excinfo:
                build_dispatch_target(
                    TargetBuildConfig("auto", None, None, tmp_path / "logs")
                )
            assert "完全権限実行となります" in str(excinfo.value)

    def test_auto_detected_unsafe_cli_error_message_diagnostics(
        self, tmp_path, monkeypatch
    ):
        import shutil

        monkeypatch.setattr(
            shutil,
            "which",
            lambda name: "/usr/bin/codex" if name == "codex" else None,
        )
        with pytest.raises(ValueError) as excinfo:
            build_dispatch_target(
                TargetBuildConfig("auto", None, None, tmp_path / "logs")
            )
        msg = str(excinfo.value)
        assert "codex" in msg
        assert "claude" in msg
        assert "agy" in msg
        assert "--allow-unsafe-agent-execution" in msg
        assert "cloud-routine" in msg
        assert "--no-apply" in msg

    def test_auto_detected_claude_unsafe_cli_error_message(self, tmp_path, monkeypatch):
        import shutil

        monkeypatch.setattr(
            shutil,
            "which",
            lambda name: "/usr/bin/claude" if name == "claude" else None,
        )
        with pytest.raises(ValueError) as excinfo:
            build_dispatch_target(
                TargetBuildConfig("auto", None, None, tmp_path / "logs")
            )
        msg = str(excinfo.value)
        assert "`claude` を選択しました。" in msg
        assert "PATH にありません" not in msg

    def test_unsafe_execution_error_with_unknown_candidate(self):
        from orchestune.dispatch.targets import _unsafe_execution_error

        err = _unsafe_execution_error("custom-cli", auto_detected=True)
        assert "`custom` を選択しました。" in str(err)

    def test_explicit_unsafe_cli_error_message_diagnostics(self, tmp_path):
        with pytest.raises(ValueError) as excinfo:
            build_dispatch_target(
                TargetBuildConfig("claude-cli", None, None, tmp_path / "logs")
            )
        msg = str(excinfo.value)
        assert "--dispatch-target" in msg
        assert "dispatch_target" in msg
        assert "--no-apply" in msg
        assert "cloud-routine" in msg

    def test_unsafe_cli_with_allow_unsafe_flag_succeeds(self, tmp_path):
        for target_name in ["claude-cli", "agy-cli", "codex-cli", "grok-cli"]:
            target = build_dispatch_target(
                TargetBuildConfig(
                    target_name,
                    None,
                    None,
                    tmp_path / "logs",
                    allow_unsafe_agent_execution=True,
                )
            )
            assert isinstance(target, LocalProcessDispatchTarget)
            assert target.target_name == target_name

    def test_auto_with_detected_cli_with_allow_unsafe_flag_succeeds(self, tmp_path):
        with patch(
            "orchestune.dispatch.targets.shutil.which",
            side_effect=lambda name: "/usr/bin/claude" if name == "claude" else None,
        ):
            target = build_dispatch_target(
                TargetBuildConfig(
                    "auto",
                    None,
                    None,
                    tmp_path / "logs",
                    allow_unsafe_agent_execution=True,
                )
            )
        assert isinstance(target, LocalProcessDispatchTarget)
        assert target._local_cmd == CLAUDE_CLI_LOCAL_CMD_TEMPLATE

    def test_auto_without_detected_cli_and_without_allow_unsafe_flag_succeeds_with_dummy(
        self, tmp_path
    ):
        with patch("orchestune.dispatch.targets.shutil.which", return_value=None):
            target = build_dispatch_target(
                TargetBuildConfig("auto", None, None, tmp_path / "logs")
            )
        assert isinstance(target, LocalProcessDispatchTarget)
        assert target._local_cmd is None


class TestResolveDefaultDispatchTargetName:
    def test_defaults_to_auto_when_env_empty(self):
        assert resolve_default_dispatch_target_name({}) == "auto"

    def test_defaults_to_auto_when_github_actions_not_true(self):
        assert (
            resolve_default_dispatch_target_name({"GITHUB_ACTIONS": "false"}) == "auto"
        )

    def test_defaults_to_cloud_routine_in_github_actions(self):
        assert (
            resolve_default_dispatch_target_name({"GITHUB_ACTIONS": "true"})
            == "cloud-routine"
        )

    def test_ignores_unrelated_env_vars(self):
        assert (
            resolve_default_dispatch_target_name(
                {"GITHUB_ACTIONS": "true", "PATH": "/usr/bin"}
            )
            == "cloud-routine"
        )

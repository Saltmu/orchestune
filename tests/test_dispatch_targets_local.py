from unittest.mock import patch

import pytest

from orchestune.dispatch.targets import (
    AGY_CLI_LOCAL_CMD_TEMPLATE,
    CLAUDE_CLI_LOCAL_CMD_TEMPLATE,
    CODEX_CLI_LOCAL_CMD_TEMPLATE,
    ClaudeCodeCloudRoutineDispatchTarget,
    CodexCloudDispatchTarget,
    DispatchHandle,
    DispatchTarget,
    LocalProcessDispatchTarget,
    TargetBuildConfig,
    _local_cli_name,
    build_dispatch_target,
    default_dry_run_command_builder,
    detect_installed_local_cli,
    resolve_default_dispatch_target_name,
)
from orchestune.models import Task


def _task(issue_number=1, subtask_id="task-a", footprint=("src/foo.py",)):
    return Task(
        issue_number=issue_number,
        subtask_id=subtask_id,
        footprint=footprint,
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=("status:queued",),
        created_at="2026-01-01T00:00:00+00:00",
    )


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ([], None),
        (["/opt/tools/CLAUDE.EXE"], "claude"),
        (["runner", "codex"], None),
    ],
)
def test_local_cli_name_uses_only_the_executable(command, expected):
    assert _local_cli_name(command) == expected


class _IsCompleteOnlyTarget(DispatchTarget):
    def __init__(self, complete: bool):
        self.complete = complete

    def launch(
        self,
        task: Task,
        branch_name: str,
        worktree_path,
        *,
        force_push=False,
        execution_selection=None,
    ):
        return DispatchHandle(branch_name=branch_name)

    def is_complete(self, handle: DispatchHandle, forge=None) -> bool:
        return self.complete


class _LegacySignatureTarget(DispatchTarget):
    """#315レビュー対応: `forge`引数追加前の旧シグネチャを実装したままの
    dispatch_targetでも、`completion_status`がTypeErrorにせず動作し続ける
    ことを示す回帰テスト用ダブル。"""

    def __init__(self, complete: bool):
        self.complete = complete

    def launch(  # type: ignore[override]
        self, task: Task, branch_name: str, worktree_path, *, force_push=False
    ):
        return DispatchHandle(branch_name=branch_name)

    def is_complete(self, handle: DispatchHandle) -> bool:  # type: ignore[override]
        return self.complete


class TestDispatchTargetContract:
    def test_default_completion_status_delegates_to_concrete_is_complete(self):
        assert (
            _IsCompleteOnlyTarget(True).completion_status(DispatchHandle())
            == "completed"
        )
        assert (
            _IsCompleteOnlyTarget(False).completion_status(DispatchHandle())
            == "pending"
        )

    def test_completion_status_tolerates_legacy_is_complete_signature(self):
        """#315: `is_complete(self, handle)`（forge引数なし）を実装した
        既存のdispatch_targetでも、TypeErrorにならず正しく動作すること。"""
        assert (
            _LegacySignatureTarget(True).completion_status(DispatchHandle())
            == "completed"
        )
        assert (
            _LegacySignatureTarget(False).completion_status(DispatchHandle())
            == "pending"
        )


class TestLocalProcessDispatchTarget:
    def test_launch_starts_subprocess_and_returns_pid(self, tmp_path):
        target = LocalProcessDispatchTarget(
            default_dry_run_command_builder, log_dir=tmp_path / "logs"
        )
        with patch("orchestune.dispatch.targets.subprocess.Popen") as mock_popen:
            mock_popen.return_value.pid = 4242
            handle = target.launch(_task(), "claude/issue-1-task-a", tmp_path / "wt")

        assert handle.pid == 4242
        assert handle.external_id is None
        assert handle.branch_name == "claude/issue-1-task-a"
        assert mock_popen.call_args.kwargs["cwd"] == str(tmp_path / "wt")

    def test_launch_creates_log_file_under_log_dir(self, tmp_path):
        log_dir = tmp_path / "logs"
        target = LocalProcessDispatchTarget(
            default_dry_run_command_builder, log_dir=log_dir
        )
        with patch("orchestune.dispatch.targets.subprocess.Popen") as mock_popen:
            mock_popen.return_value.pid = 1
            target.launch(_task(), "claude/issue-1-task-a", tmp_path / "wt")
        assert (log_dir / "claude-issue-1-task-a.log").exists()

    def test_is_complete_true_when_pid_not_alive(self):
        target = LocalProcessDispatchTarget()
        with patch("orchestune.dispatch.targets._is_pid_alive", return_value=False):
            assert target.is_complete(DispatchHandle(pid=123)) is True

    def test_is_complete_false_when_pid_alive(self):
        target = LocalProcessDispatchTarget()
        with patch("orchestune.dispatch.targets._is_pid_alive", return_value=True):
            assert target.is_complete(DispatchHandle(pid=123)) is False

    def test_launch_with_local_cmd_templates(self, tmp_path):
        target = LocalProcessDispatchTarget(
            log_dir=tmp_path / "logs",
            local_cmd="agy --issue {issue_number} --subtask '{subtask_id}' --branch {branch_name} --path {worktree_path}",
        )
        with patch("orchestune.dispatch.targets.subprocess.Popen") as mock_popen:
            mock_popen.return_value.pid = 9999
            target.launch(
                _task(issue_number=42, subtask_id="sub-x"),
                "claude/issue-42-sub-x",
                tmp_path / "wt",
            )

        mock_popen.assert_called_once()
        args, _ = mock_popen.call_args
        cmd = args[0]
        assert cmd == [
            "agy",
            "--issue",
            "42",
            "--subtask",
            "sub-x",
            "--branch",
            "claude/issue-42-sub-x",
            "--path",
            str(tmp_path / "wt"),
        ]

    def test_launch_replaces_reviewer_bot_placeholder(self, tmp_path):
        target = LocalProcessDispatchTarget(
            log_dir=tmp_path / "logs",
            local_cmd="runner --reviewer {reviewer_bot} --issue {issue_number}",
            reviewer_bot="codex",
        )
        with patch("orchestune.dispatch.targets.subprocess.Popen") as mock_popen:
            mock_popen.return_value.pid = 9999
            target.launch(_task(issue_number=42), "agent/issue-42", tmp_path / "wt")

        assert mock_popen.call_args.args[0] == [
            "runner",
            "--reviewer",
            "codex",
            "--issue",
            "42",
        ]

    def test_launch_with_execution_selection_claude_cli_adds_model_and_skips_effort(
        self, tmp_path, caplog
    ):
        import logging

        from orchestune.dispatch.execution_profiles import ExecutionSelection

        target = LocalProcessDispatchTarget(
            log_dir=tmp_path / "logs",
            local_cmd=CLAUDE_CLI_LOCAL_CMD_TEMPLATE,
        )
        selection = ExecutionSelection(
            profile="deep",
            model="claude-3-7-sonnet-20250219",
            reasoning_effort="high",
            reason="test",
        )
        with (
            caplog.at_level(logging.WARNING),
            patch("orchestune.dispatch.targets.subprocess.Popen") as mock_popen,
        ):
            mock_popen.return_value.pid = 1001
            target.launch(
                _task(),
                "claude/issue-1-task-a",
                tmp_path / "wt",
                execution_selection=selection,
            )

        cmd = mock_popen.call_args[0][0]
        assert "--model" in cmd
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "claude-3-7-sonnet-20250219"
        assert "does not support reasoning_effort" in caplog.text

    def test_launch_with_execution_selection_agy_cli_adds_model_and_skips_effort(
        self, tmp_path, caplog
    ):
        import logging

        from orchestune.dispatch.execution_profiles import ExecutionSelection

        target = LocalProcessDispatchTarget(
            log_dir=tmp_path / "logs",
            local_cmd=AGY_CLI_LOCAL_CMD_TEMPLATE,
        )
        selection = ExecutionSelection(
            profile="balanced",
            model="gemini-2.5-pro",
            reasoning_effort="medium",
            reason="test",
        )
        with (
            caplog.at_level(logging.WARNING),
            patch("orchestune.dispatch.targets.subprocess.Popen") as mock_popen,
        ):
            mock_popen.return_value.pid = 1002
            target.launch(
                _task(),
                "claude/issue-1-task-a",
                tmp_path / "wt",
                execution_selection=selection,
            )

        cmd = mock_popen.call_args[0][0]
        assert "--model" in cmd
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "gemini-2.5-pro"
        assert "does not support reasoning_effort" in caplog.text

    def test_launch_with_execution_selection_codex_cli_adds_model_and_effort(
        self, tmp_path
    ):
        from orchestune.dispatch.execution_profiles import ExecutionSelection

        target = LocalProcessDispatchTarget(
            log_dir=tmp_path / "logs",
            local_cmd=CODEX_CLI_LOCAL_CMD_TEMPLATE,
        )
        selection = ExecutionSelection(
            profile="deep",
            model="o3-mini",
            reasoning_effort="high",
            reason="test",
        )
        with patch("orchestune.dispatch.targets.subprocess.Popen") as mock_popen:
            mock_popen.return_value.pid = 1003
            target.launch(
                _task(),
                "claude/issue-1-task-a",
                tmp_path / "wt",
                execution_selection=selection,
            )

        cmd = mock_popen.call_args[0][0]
        assert "--model" in cmd
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "o3-mini"
        assert "-c" in cmd
        c_idx = cmd.index("-c")
        assert cmd[c_idx + 1] == "model_reasoning_effort=high"

    def test_launch_with_execution_selection_custom_template_placeholders(
        self, tmp_path
    ):
        from orchestune.dispatch.execution_profiles import ExecutionSelection

        target = LocalProcessDispatchTarget(
            log_dir=tmp_path / "logs",
            local_cmd="custom-runner --model {model} --effort {reasoning_effort} --profile {profile} --issue {issue_number}",
        )
        selection = ExecutionSelection(
            profile="fast",
            model="claude-3-5-haiku-20241022",
            reasoning_effort="low",
            reason="test",
        )
        with patch("orchestune.dispatch.targets.subprocess.Popen") as mock_popen:
            mock_popen.return_value.pid = 1004
            target.launch(
                _task(issue_number=55),
                "claude/issue-55-task-a",
                tmp_path / "wt",
                execution_selection=selection,
            )

        cmd = mock_popen.call_args[0][0]
        assert cmd == [
            "custom-runner",
            "--model",
            "claude-3-5-haiku-20241022",
            "--effort",
            "low",
            "--profile",
            "fast",
            "--issue",
            "55",
        ]


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

    def test_cloud_fallback_re_resolves_auto_reviewer_for_custom_local_cmd(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.delenv("ORCHESTUNE_CODEX_CLOUD_ENV", raising=False)
        target = build_dispatch_target(
            TargetBuildConfig(
                "codex-cloud",
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
        for target_name in ["claude-cli", "agy-cli", "codex-cli"]:
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

    def test_unsafe_cli_with_allow_unsafe_flag_succeeds(self, tmp_path):
        for target_name in ["claude-cli", "agy-cli", "codex-cli"]:
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

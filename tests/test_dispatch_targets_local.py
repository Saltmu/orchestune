from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestune.dispatch.targets import (
    AGY_CLI_LOCAL_CMD_TEMPLATE,
    CLAUDE_CLI_LOCAL_CMD_TEMPLATE,
    CODEX_CLI_LOCAL_CMD_TEMPLATE,
    DispatchHandle,
    DispatchTarget,
    LocalProcessDispatchTarget,
    _local_cli_name,
    default_dry_run_command_builder,
)
from orchestune.infra.detached_process import DetachedProcess
from orchestune.models import Task
from orchestune.task_metadata import TaskMetadata


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
        task: TaskMetadata,
        branch_name: str,
        worktree_path,
        *,
        force_push=False,
        execution_selection=None,
        base_branch=None,
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
        self, task: TaskMetadata, branch_name: str, worktree_path, *, force_push=False
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
        mock_start = MagicMock(
            return_value=DetachedProcess(
                pid=4_204_242,
                log_path=tmp_path / "logs" / "claude-issue-1-task-a.log",
                log_offset=0,
            )
        )
        target = LocalProcessDispatchTarget(
            default_dry_run_command_builder,
            log_dir=tmp_path / "logs",
            start_process=mock_start,
        )
        handle = target.launch(_task(), "claude/issue-1-task-a", tmp_path / "wt")

        assert handle.pid == 4_204_242
        assert handle.external_id is None
        assert handle.branch_name == "claude/issue-1-task-a"
        assert mock_start.call_args.kwargs["cwd"] == tmp_path / "wt"

    def test_launch_creates_log_file_under_log_dir(self, tmp_path):
        log_dir = tmp_path / "logs"

        def _fake_start(args, *, cwd, log_path):
            Path(log_path).touch()
            return DetachedProcess(pid=4_200_001, log_path=Path(log_path), log_offset=0)

        target = LocalProcessDispatchTarget(
            default_dry_run_command_builder,
            log_dir=log_dir,
            start_process=_fake_start,
        )
        target.launch(_task(), "claude/issue-1-task-a", tmp_path / "wt")
        assert (log_dir / "claude-issue-1-task-a.log").exists()

    def test_launch_records_target_log_path_and_start_offset(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        log_path = log_dir / "claude-issue-1-task-a.log"
        log_path.write_bytes(b"previous run error\n")
        offset = len(b"previous run error\n")
        mock_start = MagicMock(
            return_value=DetachedProcess(
                pid=4_200_007,
                log_path=log_path,
                log_offset=offset,
            )
        )
        target = LocalProcessDispatchTarget(
            default_dry_run_command_builder,
            log_dir=log_dir,
            target_name="claude-cli",
            start_process=mock_start,
        )
        handle = target.launch(_task(), "claude/issue-1-task-a", tmp_path / "wt")

        assert handle.target_name == "claude-cli"
        assert handle.log_path == str(log_path)
        # The appended log keeps earlier runs: the new run starts after them.
        assert handle.log_offset == offset

    def test_launch_of_a_fresh_log_starts_at_offset_zero(self, tmp_path):
        log_path = tmp_path / "logs" / "claude-issue-1-task-a.log"
        mock_start = MagicMock(
            return_value=DetachedProcess(
                pid=4_200_007,
                log_path=log_path,
                log_offset=0,
            )
        )
        target = LocalProcessDispatchTarget(
            default_dry_run_command_builder,
            log_dir=tmp_path / "logs",
            start_process=mock_start,
        )
        handle = target.launch(_task(), "claude/issue-1-task-a", tmp_path / "wt")

        assert handle.log_offset == 0
        assert handle.target_name is None

    def test_is_complete_true_when_pid_not_alive(self):
        target = LocalProcessDispatchTarget()
        with patch(
            "orchestune.dispatch.targets._is_pid_alive",
            autospec=True,
            return_value=False,
        ):
            assert target.is_complete(DispatchHandle(pid=4_200_123)) is True

    def test_is_complete_false_when_pid_alive(self):
        target = LocalProcessDispatchTarget()
        with patch(
            "orchestune.dispatch.targets._is_pid_alive",
            autospec=True,
            return_value=True,
        ):
            assert target.is_complete(DispatchHandle(pid=4_200_123)) is False

    def test_launch_with_local_cmd_templates(self, tmp_path):
        mock_start = MagicMock(
            return_value=DetachedProcess(
                pid=4_209_999,
                log_path=tmp_path / "logs" / "claude-issue-42-sub-x.log",
                log_offset=0,
            )
        )
        target = LocalProcessDispatchTarget(
            log_dir=tmp_path / "logs",
            local_cmd="agy --issue {issue_number} --subtask '{subtask_id}' --branch {branch_name} --path {worktree_path}",
            start_process=mock_start,
        )
        target.launch(
            _task(issue_number=42, subtask_id="sub-x"),
            "claude/issue-42-sub-x",
            tmp_path / "wt",
        )

        mock_start.assert_called_once()
        args, _ = mock_start.call_args
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
        mock_start = MagicMock(
            return_value=DetachedProcess(
                pid=4_209_999,
                log_path=tmp_path / "logs" / "agent-issue-42.log",
                log_offset=0,
            )
        )
        target = LocalProcessDispatchTarget(
            log_dir=tmp_path / "logs",
            local_cmd="runner --reviewer {reviewer_bot} --issue {issue_number}",
            reviewer_bot="codex",
            start_process=mock_start,
        )
        target.launch(_task(issue_number=42), "agent/issue-42", tmp_path / "wt")

        assert mock_start.call_args.args[0] == [
            "runner",
            "--reviewer",
            "codex",
            "--issue",
            "42",
        ]

    def test_launch_replaces_base_branch_placeholder(self, tmp_path):
        mock_start = MagicMock(
            return_value=DetachedProcess(
                pid=4_209_999,
                log_path=tmp_path / "logs" / "agent-issue-42.log",
                log_offset=0,
            )
        )
        target = LocalProcessDispatchTarget(
            log_dir=tmp_path / "logs",
            local_cmd="runner --base {base_branch} --issue {issue_number}",
            start_process=mock_start,
        )
        target.launch(
            _task(issue_number=42),
            "agent/issue-42",
            tmp_path / "wt",
            base_branch="parent/issue-700",
        )

        assert mock_start.call_args.args[0] == [
            "runner",
            "--base",
            "parent/issue-700",
            "--issue",
            "42",
        ]

    def test_launch_defaults_base_branch_to_main_when_unspecified(self, tmp_path):
        mock_start = MagicMock(
            return_value=DetachedProcess(
                pid=4_209_999,
                log_path=tmp_path / "logs" / "agent-issue-42.log",
                log_offset=0,
            )
        )
        target = LocalProcessDispatchTarget(
            log_dir=tmp_path / "logs",
            local_cmd="runner --base {base_branch} --issue {issue_number}",
            start_process=mock_start,
        )
        target.launch(_task(issue_number=42), "agent/issue-42", tmp_path / "wt")

        assert mock_start.call_args.args[0] == [
            "runner",
            "--base",
            "main",
            "--issue",
            "42",
        ]

    def test_claude_cli_template_includes_verbose_flag_for_stream_json(self):
        """#714: Claude CLIは-pと--output-format stream-jsonの併用時に--verboseが必須。"""
        assert "--verbose" in CLAUDE_CLI_LOCAL_CMD_TEMPLATE

    def test_launch_claude_cli_includes_verbose_flag(self, tmp_path):
        mock_start = MagicMock(
            return_value=DetachedProcess(
                pid=4_209_999,
                log_path=tmp_path / "logs" / "claude-issue-42.log",
                log_offset=0,
            )
        )
        target = LocalProcessDispatchTarget(
            log_dir=tmp_path / "logs",
            local_cmd=CLAUDE_CLI_LOCAL_CMD_TEMPLATE,
            start_process=mock_start,
        )
        target.launch(_task(issue_number=42), "claude/issue-42", tmp_path / "wt")

        cmd = mock_start.call_args.args[0]
        assert "--verbose" in cmd
        assert "--output-format" in cmd
        assert "stream-json" in cmd

    def test_launch_with_execution_selection_claude_cli_adds_model_and_effort(
        self, tmp_path, caplog
    ):
        import logging

        from orchestune.dispatch.execution_profiles import ExecutionSelection

        mock_start = MagicMock(
            return_value=DetachedProcess(
                pid=4_201_001,
                log_path=tmp_path / "logs" / "claude-issue-1-task-a.log",
                log_offset=0,
            )
        )
        target = LocalProcessDispatchTarget(
            log_dir=tmp_path / "logs",
            local_cmd=CLAUDE_CLI_LOCAL_CMD_TEMPLATE,
            start_process=mock_start,
        )
        selection = ExecutionSelection(
            profile="deep",
            model="claude-3-7-sonnet-20250219",
            reasoning_effort="high",
            reason="test",
        )
        with caplog.at_level(logging.WARNING):
            target.launch(
                _task(),
                "claude/issue-1-task-a",
                tmp_path / "wt",
                execution_selection=selection,
            )

        cmd = mock_start.call_args[0][0]
        assert "--model" in cmd
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "claude-3-7-sonnet-20250219"
        assert "--effort" in cmd
        effort_idx = cmd.index("--effort")
        assert cmd[effort_idx + 1] == "high"

    def test_launch_with_execution_selection_agy_cli_adds_model_and_skips_effort(
        self, tmp_path, caplog
    ):
        import logging

        from orchestune.dispatch.execution_profiles import ExecutionSelection

        mock_start = MagicMock(
            return_value=DetachedProcess(
                pid=4_201_002,
                log_path=tmp_path / "logs" / "claude-issue-1-task-a.log",
                log_offset=0,
            )
        )
        target = LocalProcessDispatchTarget(
            log_dir=tmp_path / "logs",
            local_cmd=AGY_CLI_LOCAL_CMD_TEMPLATE,
            start_process=mock_start,
        )
        selection = ExecutionSelection(
            profile="balanced",
            model="gemini-2.5-pro",
            reasoning_effort="medium",
            reason="test",
        )
        with caplog.at_level(logging.WARNING):
            target.launch(
                _task(),
                "claude/issue-1-task-a",
                tmp_path / "wt",
                execution_selection=selection,
            )

        cmd = mock_start.call_args[0][0]
        assert "--model" in cmd
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "gemini-2.5-pro"
        assert "does not support reasoning_effort" in caplog.text

    def test_launch_with_execution_selection_codex_cli_adds_model_and_effort(
        self, tmp_path
    ):
        from orchestune.dispatch.execution_profiles import ExecutionSelection

        mock_start = MagicMock(
            return_value=DetachedProcess(
                pid=4_201_003,
                log_path=tmp_path / "logs" / "claude-issue-1-task-a.log",
                log_offset=0,
            )
        )
        target = LocalProcessDispatchTarget(
            log_dir=tmp_path / "logs",
            local_cmd=CODEX_CLI_LOCAL_CMD_TEMPLATE,
            start_process=mock_start,
        )
        selection = ExecutionSelection(
            profile="deep",
            model="o3-mini",
            reasoning_effort="high",
            reason="test",
        )
        target.launch(
            _task(),
            "claude/issue-1-task-a",
            tmp_path / "wt",
            execution_selection=selection,
        )

        cmd = mock_start.call_args[0][0]
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

        mock_start = MagicMock(
            return_value=DetachedProcess(
                pid=4_201_004,
                log_path=tmp_path / "logs" / "claude-issue-55-task-a.log",
                log_offset=0,
            )
        )
        target = LocalProcessDispatchTarget(
            log_dir=tmp_path / "logs",
            local_cmd="custom-runner --model {model} --effort {reasoning_effort} --profile {profile} --issue {issue_number}",
            start_process=mock_start,
        )
        selection = ExecutionSelection(
            profile="fast",
            model="claude-3-5-haiku-20241022",
            reasoning_effort="low",
            reason="test",
        )
        target.launch(
            _task(issue_number=55),
            "claude/issue-55-task-a",
            tmp_path / "wt",
            execution_selection=selection,
        )

        cmd = mock_start.call_args[0][0]
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

    def test_local_process_dispatch_target_properties(self, tmp_path):
        target = LocalProcessDispatchTarget(
            log_dir=tmp_path / "logs",
            local_cmd="my-cmd",
        )
        assert target.log_dir == tmp_path / "logs"
        assert target.local_cmd == "my-cmd"
        assert callable(target.start_process)


class TestFormatLocalCmdEffort:
    def test_claude_cli_appends_effort_flag(self, tmp_path):
        from orchestune.dispatch.targets import _format_local_cmd

        task = _task()
        cmd = _format_local_cmd(
            "claude -p 'test'",
            task,
            "claude/issue-1-task-a",
            tmp_path / "wt-1",
            model="claude-3-7-sonnet",
            reasoning_effort="high",
            profile_name="deep",
        )
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "claude-3-7-sonnet"
        assert "--effort" in cmd
        assert cmd[cmd.index("--effort") + 1] == "high"

    def test_codex_cli_appends_model_reasoning_effort_config(self, tmp_path):
        from orchestune.dispatch.targets import _format_local_cmd

        task = _task()
        cmd = _format_local_cmd(
            "codex exec --issue {issue_number}",
            task,
            "codex/issue-1-task-a",
            tmp_path / "wt-1",
            model="o3-mini",
            reasoning_effort="high",
            profile_name="deep",
        )
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "o3-mini"
        assert "-c" in cmd
        assert cmd[cmd.index("-c") + 1] == "model_reasoning_effort=high"


class TestLocalProcessDispatchTargetSemanticFix:
    def test_launch_semantic_fix_delegation(self, tmp_path):
        target = LocalProcessDispatchTarget(
            log_dir=tmp_path / "logs",
            target_name="claude-cli",
        )
        with patch(
            "orchestune.dispatch.targets.launch_local_semantic_fix",
            return_value=DispatchHandle(pid=456, target_name="claude-cli"),
        ) as mock_fn:
            handle = target.launch_semantic_fix(
                operation_id="op-abc",
                prompt="fix instructions",
                expected_head_sha="deadbeef" * 5,
            )
            assert handle is not None
            assert handle.pid == 456
            mock_fn.assert_called_once_with(
                target=target,
                operation_id="op-abc",
                prompt="fix instructions",
                expected_head_sha="deadbeef" * 5,
                worktree_root=None,
                repo_root=None,
            )

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from orchestune.dispatch_targets import (
    AGY_CLI_LOCAL_CMD_TEMPLATE,
    CLAUDE_CLI_LOCAL_CMD_TEMPLATE,
    ClaudeCodeCloudRoutineDispatchTarget,
    DispatchHandle,
    LocalProcessDispatchTarget,
    build_dispatch_target,
    default_dry_run_command_builder,
    resolve_default_dispatch_target_name,
)
from orchestune.dispatcher import Task
from orchestune.github import PrRecord


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


class TestLocalProcessDispatchTarget:
    def test_launch_starts_subprocess_and_returns_pid(self, tmp_path):
        target = LocalProcessDispatchTarget(
            default_dry_run_command_builder, log_dir=tmp_path / "logs"
        )
        with patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen:
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
        with patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen:
            mock_popen.return_value.pid = 1
            target.launch(_task(), "claude/issue-1-task-a", tmp_path / "wt")
        assert (log_dir / "claude-issue-1-task-a.log").exists()

    def test_is_complete_true_when_pid_not_alive(self):
        target = LocalProcessDispatchTarget()
        with patch("orchestune.dispatch_targets._is_pid_alive", return_value=False):
            assert target.is_complete(DispatchHandle(pid=123)) is True

    def test_is_complete_false_when_pid_alive(self):
        target = LocalProcessDispatchTarget()
        with patch("orchestune.dispatch_targets._is_pid_alive", return_value=True):
            assert target.is_complete(DispatchHandle(pid=123)) is False

    def test_launch_with_local_cmd_templates(self, tmp_path):
        target = LocalProcessDispatchTarget(
            log_dir=tmp_path / "logs",
            local_cmd="agy --issue {issue_number} --subtask '{subtask_id}' --branch {branch_name} --path {worktree_path}",
        )
        with patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen:
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


class TestClaudeCodeCloudRoutineDispatchTarget:
    def _response(
        self, session_id="session_1", session_url="https://claude.ai/code/session_1"
    ):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "type": "routine_fire",
                "claude_code_session_id": session_id,
                "claude_code_session_url": session_url,
            }
        ).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_response.__exit__.return_value = False
        return mock_response

    def test_launch_fires_routine_and_returns_session_handle(self, tmp_path):
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "sk-ant-oat01-xxx")
        with patch(
            "orchestune.dispatch_targets.urllib.request.urlopen",
            return_value=self._response(),
        ) as mock_urlopen:
            handle = target.launch(_task(), "claude/issue-1-task-a", tmp_path / "wt")

        assert handle.external_id == "session_1"
        assert handle.external_url == "https://claude.ai/code/session_1"
        assert handle.branch_name == "claude/issue-1-task-a"
        assert handle.pid is None

        request = mock_urlopen.call_args.args[0]
        assert (
            request.full_url
            == "https://api.anthropic.com/v1/claude_code/routines/trig_1/fire"
        )
        assert request.get_header("Authorization") == "Bearer sk-ant-oat01-xxx"
        assert (
            request.get_header("Anthropic-beta") == "experimental-cc-routine-2026-04-01"
        )
        body = json.loads(request.data.decode("utf-8"))
        assert "claude/issue-1-task-a" in body["text"]
        assert "#1" in body["text"]

    def test_fire_text_fires_arbitrary_prompt_and_returns_handle(self):
        # #186: 統合コーディネーターが同一ルーチンへ任意指示を投げる汎用fire。
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "sk-ant-oat01-xxx")
        with patch(
            "orchestune.dispatch_targets.urllib.request.urlopen",
            return_value=self._response(),
        ) as mock_urlopen:
            handle = target.fire_text("結合diffをレビューして")

        assert handle.external_id == "session_1"
        assert handle.external_url == "https://claude.ai/code/session_1"
        assert handle.branch_name is None

        request = mock_urlopen.call_args.args[0]
        assert (
            request.full_url
            == "https://api.anthropic.com/v1/claude_code/routines/trig_1/fire"
        )
        body = json.loads(request.data.decode("utf-8"))
        assert body["text"] == "結合diffをレビューして"

    def test_retries_on_transient_error_then_succeeds(self, tmp_path):
        target = ClaudeCodeCloudRoutineDispatchTarget(
            "trig_1", "token", max_retries=3, initial_delay=0.01
        )
        transient = urllib.error.HTTPError("url", 503, "unavailable", {}, None)
        with (
            patch(
                "orchestune.dispatch_targets.urllib.request.urlopen",
                side_effect=[transient, self._response()],
            ),
            patch("orchestune.dispatch_targets.time.sleep") as mock_sleep,
        ):
            handle = target.launch(_task(), "claude/issue-1-task-a", tmp_path / "wt")

        assert handle.external_id == "session_1"
        mock_sleep.assert_called_once()

    def test_gives_up_after_max_retries(self, tmp_path):
        target = ClaudeCodeCloudRoutineDispatchTarget(
            "trig_1", "token", max_retries=2, initial_delay=0.01
        )
        transient = urllib.error.HTTPError("url", 500, "error", {}, None)
        with (
            patch(
                "orchestune.dispatch_targets.urllib.request.urlopen",
                side_effect=[transient, transient, transient],
            ),
            patch("orchestune.dispatch_targets.time.sleep"),
        ):
            with pytest.raises(urllib.error.HTTPError):
                target.launch(_task(), "claude/issue-1-task-a", tmp_path / "wt")

    def test_does_not_retry_on_client_error(self, tmp_path):
        target = ClaudeCodeCloudRoutineDispatchTarget(
            "trig_1", "token", max_retries=3, initial_delay=0.01
        )
        auth_error = urllib.error.HTTPError("url", 401, "unauthorized", {}, None)
        with (
            patch(
                "orchestune.dispatch_targets.urllib.request.urlopen",
                side_effect=[auth_error, self._response()],
            ) as mock_urlopen,
            patch("orchestune.dispatch_targets.time.sleep") as mock_sleep,
        ):
            with pytest.raises(urllib.error.HTTPError):
                target.launch(_task(), "claude/issue-1-task-a", tmp_path / "wt")

        mock_sleep.assert_not_called()
        assert mock_urlopen.call_count == 1

    def test_is_complete_true_when_pr_open_for_branch(self):
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")
        with patch(
            "orchestune.dispatch_targets.github.list_open_prs",
            return_value=[
                PrRecord(number=1, head_ref="claude/issue-1-task-a", changed_files=())
            ],
        ):
            handle = DispatchHandle(
                external_id="session_1", branch_name="claude/issue-1-task-a"
            )
            assert target.is_complete(handle) is True

    def test_is_complete_false_when_no_matching_pr(self):
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")
        with patch(
            "orchestune.dispatch_targets.github.list_open_prs",
            return_value=[
                PrRecord(number=1, head_ref="other-branch", changed_files=())
            ],
        ):
            handle = DispatchHandle(
                external_id="session_1", branch_name="claude/issue-1-task-a"
            )
            assert target.is_complete(handle) is False

    def test_is_complete_false_without_branch_name(self):
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")
        assert target.is_complete(DispatchHandle(external_id="session_1")) is False

    def test_is_complete_true_via_closing_issue_reference_when_branch_mismatches(self):
        """#239: AIセッションがブランチ名指示に従わなかった場合でも、
        PRのclosingIssuesReferences経由で完了を検知できる。"""
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")
        with patch(
            "orchestune.dispatch_targets.github.list_open_prs",
            return_value=[
                PrRecord(
                    number=1,
                    head_ref="claude/elegant-noether-5rli7u",
                    changed_files=(),
                    closes_issue_numbers=(218,),
                )
            ],
        ):
            handle = DispatchHandle(
                external_id="session_1",
                branch_name="claude/issue-218-review-history-backend-api",
                issue_number=218,
            )
            assert target.is_complete(handle) is True

    def test_is_complete_false_when_neither_branch_nor_issue_match(self):
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")
        with patch(
            "orchestune.dispatch_targets.github.list_open_prs",
            return_value=[
                PrRecord(
                    number=1,
                    head_ref="other-branch",
                    changed_files=(),
                    closes_issue_numbers=(999,),
                )
            ],
        ):
            handle = DispatchHandle(
                external_id="session_1",
                branch_name="claude/issue-218-review-history-backend-api",
                issue_number=218,
            )
            assert target.is_complete(handle) is False


class TestBuildDispatchTarget:
    def test_local_name_returns_local_process_target(self, tmp_path):
        target = build_dispatch_target("local", None, None, tmp_path / "logs")
        assert isinstance(target, LocalProcessDispatchTarget)

    def test_cloud_routine_with_credentials_returns_cloud_target(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("ORCHESTUNE_ROUTINE_ID", raising=False)
        monkeypatch.delenv("ORCHESTUNE_ROUTINE_TOKEN", raising=False)
        target = build_dispatch_target(
            "cloud-routine", "trig_1", "token", tmp_path / "logs"
        )
        assert isinstance(target, ClaudeCodeCloudRoutineDispatchTarget)

    def test_cloud_routine_resolves_credentials_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ORCHESTUNE_ROUTINE_ID", "trig_env")
        monkeypatch.setenv("ORCHESTUNE_ROUTINE_TOKEN", "token_env")
        target = build_dispatch_target("cloud-routine", None, None, tmp_path / "logs")
        assert isinstance(target, ClaudeCodeCloudRoutineDispatchTarget)

    def test_cloud_routine_without_credentials_falls_back_to_local(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("ORCHESTUNE_ROUTINE_ID", raising=False)
        monkeypatch.delenv("ORCHESTUNE_ROUTINE_TOKEN", raising=False)
        target = build_dispatch_target("cloud-routine", None, None, tmp_path / "logs")
        assert isinstance(target, LocalProcessDispatchTarget)

    def test_local_with_local_cmd_propagates_to_target(self, tmp_path):
        target = build_dispatch_target(
            "local", None, None, tmp_path / "logs", local_cmd="agy {issue_number}"
        )
        assert isinstance(target, LocalProcessDispatchTarget)
        assert target._local_cmd == "agy {issue_number}"

    def test_claude_cli_without_local_cmd_uses_preset_template(self, tmp_path):
        target = build_dispatch_target("claude-cli", None, None, tmp_path / "logs")
        assert isinstance(target, LocalProcessDispatchTarget)
        assert target._local_cmd == CLAUDE_CLI_LOCAL_CMD_TEMPLATE

    def test_claude_cli_preset_bypasses_permission_prompts(self, tmp_path):
        target = build_dispatch_target("claude-cli", None, None, tmp_path / "logs")
        assert "--permission-mode bypassPermissions" in target._local_cmd

    def test_claude_cli_with_explicit_local_cmd_overrides_preset(self, tmp_path):
        target = build_dispatch_target(
            "claude-cli",
            None,
            None,
            tmp_path / "logs",
            local_cmd="claude -p 'custom {issue_number}'",
        )
        assert isinstance(target, LocalProcessDispatchTarget)
        assert target._local_cmd == "claude -p 'custom {issue_number}'"

    def test_agy_cli_without_local_cmd_uses_preset_template(self, tmp_path):
        target = build_dispatch_target("agy-cli", None, None, tmp_path / "logs")
        assert isinstance(target, LocalProcessDispatchTarget)
        assert target._local_cmd == AGY_CLI_LOCAL_CMD_TEMPLATE

    def test_agy_cli_preset_bypasses_permission_prompts(self, tmp_path):
        target = build_dispatch_target("agy-cli", None, None, tmp_path / "logs")
        assert "--sandbox" in target._local_cmd
        assert "--dangerously-skip-permissions" in target._local_cmd

    def test_agy_cli_with_explicit_local_cmd_overrides_preset(self, tmp_path):
        target = build_dispatch_target(
            "agy-cli",
            None,
            None,
            tmp_path / "logs",
            local_cmd="agy -p 'custom {issue_number}'",
        )
        assert isinstance(target, LocalProcessDispatchTarget)
        assert target._local_cmd == "agy -p 'custom {issue_number}'"


class TestResolveDefaultDispatchTargetName:
    def test_defaults_to_claude_cli_when_env_empty(self):
        assert resolve_default_dispatch_target_name({}) == "claude-cli"

    def test_defaults_to_claude_cli_when_github_actions_not_true(self):
        assert (
            resolve_default_dispatch_target_name({"GITHUB_ACTIONS": "false"})
            == "claude-cli"
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

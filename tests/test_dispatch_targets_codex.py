import json
import subprocess
from unittest.mock import patch

import pytest

from orchestune.dispatch_targets import (
    CodexCloudDispatchTarget,
    DispatchHandle,
)
from orchestune.models import PrRecord, Task
from orchestune.outcome_record import OutcomeRecord


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


class TestCodexCloudDispatchTarget:
    def test_launch_pushes_branch_and_submits_codex_cloud_task(self, tmp_path):
        target = CodexCloudDispatchTarget("env_123", log_dir=tmp_path / "logs")
        exec_output = "Task created: https://chatgpt.com/codex/tasks/task_e_6a8859c41f24832ab443a9db2294023d\n"
        push_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        submit_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=exec_output, stderr=""
        )
        with patch(
            "orchestune.dispatch_targets.subprocess.run",
            side_effect=[push_result, submit_result],
        ) as mock_run:
            handle = target.launch(_task(), "claude/issue-1-task-a", tmp_path / "wt")

        push_call, submit_call = mock_run.call_args_list
        assert push_call.args[0] == [
            "git",
            "push",
            "--set-upstream",
            "origin",
            "claude/issue-1-task-a",
        ]
        command = submit_call.args[0]
        assert command[:7] == [
            "codex",
            "cloud",
            "exec",
            "--env",
            "env_123",
            "--branch",
            "claude/issue-1-task-a",
        ]
        assert "#1" in command[-1]
        assert "非対話" in command[-1]
        assert handle.pid is None
        assert handle.external_id == "task_e_6a8859c41f24832ab443a9db2294023d"
        assert (
            handle.external_url
            == "https://chatgpt.com/codex/tasks/task_e_6a8859c41f24832ab443a9db2294023d"
        )
        assert handle.branch_name == "claude/issue-1-task-a"
        assert handle.issue_number == 1
        log_file = tmp_path / "logs" / "claude-issue-1-task-a.log"
        assert log_file.read_text(encoding="utf-8") == exec_output

    def test_launch_fallback_when_output_has_no_task_id(self, tmp_path):
        target = CodexCloudDispatchTarget("env_123", log_dir=tmp_path / "logs")
        push_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        submit_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Submitting task to cloud...\n", stderr=""
        )
        with patch(
            "orchestune.dispatch_targets.subprocess.run",
            side_effect=[push_result, submit_result],
        ):
            handle = target.launch(_task(), "claude/issue-1-task-a", tmp_path / "wt")

        assert handle.external_id == "codex-cloud:claude/issue-1-task-a"
        assert handle.external_url is None
        assert handle.branch_name == "claude/issue-1-task-a"
        assert handle.issue_number == 1

    def test_launch_force_pushes_after_rebase(self, tmp_path):
        """#384: 自動リベースによる再launchでは--force-with-leaseでpushする。"""
        target = CodexCloudDispatchTarget("env_123", log_dir=tmp_path / "logs")
        push_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        submit_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch(
            "orchestune.dispatch_targets.subprocess.run",
            side_effect=[push_result, submit_result],
        ) as mock_run:
            target.launch(
                _task(), "claude/issue-1-task-a", tmp_path / "wt", force_push=True
            )

        push_call = mock_run.call_args_list[0]
        assert push_call.args[0] == [
            "git",
            "push",
            "--force-with-lease",
            "--set-upstream",
            "origin",
            "claude/issue-1-task-a",
        ]

    def test_launch_propagates_codex_cloud_submission_failure(self, tmp_path):
        target = CodexCloudDispatchTarget("env_123", log_dir=tmp_path / "logs")
        push_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        submission_result = subprocess.CompletedProcess(
            args=["codex", "cloud", "exec"],
            returncode=1,
            stdout="error: unauthorized\n",
            stderr="fatal error\n",
        )
        with patch(
            "orchestune.dispatch_targets.subprocess.run",
            side_effect=[push_result, submission_result],
        ):
            with pytest.raises(subprocess.CalledProcessError) as error:
                target.launch(_task(), "claude/issue-1-task-a", tmp_path / "wt")

        assert error.value.returncode == 1
        log_file = tmp_path / "logs" / "claude-issue-1-task-a.log"
        assert log_file.exists()
        assert "error: unauthorized\nfatal error\n" in log_file.read_text(
            encoding="utf-8"
        )

    def test_fetch_codex_cloud_task_status(self):
        target = CodexCloudDispatchTarget("env_123")
        payload = json.dumps(
            {
                "items": [
                    {"id": "task_1", "status": "running"},
                    {"id": "task_2", "status": "READY"},
                    {"id": "task_3", "status": None},
                    "not_a_dict",
                ],
                "cursor": None,
            }
        )
        with patch(
            "orchestune.dispatch_targets.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=payload, stderr=""
            ),
        ):
            assert target._fetch_task_status("task_1") == "running"
            assert target._fetch_task_status("task_2") == "ready"
            assert target._fetch_task_status("task_3") is None
            assert target._fetch_task_status("task_nonexistent") is None

    def test_fetch_codex_cloud_task_status_pagination(self):
        target = CodexCloudDispatchTarget("env_123")
        page1 = json.dumps(
            {
                "items": [{"id": "task_1", "status": "ready"}],
                "cursor": "cursor_page2",
            }
        )
        page2 = json.dumps(
            {
                "items": [{"id": "task_target", "status": "failed"}],
                "cursor": None,
            }
        )
        with patch(
            "orchestune.dispatch_targets.subprocess.run",
            side_effect=[
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=page1, stderr=""
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=page2, stderr=""
                ),
            ],
        ) as mock_run:
            status = target._fetch_task_status("task_target")

        assert status == "failed"
        assert mock_run.call_count == 2
        first_call_cmd = mock_run.call_args_list[0].args[0]
        second_call_cmd = mock_run.call_args_list[1].args[0]
        assert "--cursor" not in first_call_cmd
        assert second_call_cmd[-2:] == ["--cursor", "cursor_page2"]

    def test_fetch_codex_cloud_task_status_handles_errors(self):
        target = CodexCloudDispatchTarget("env_123")
        with patch(
            "orchestune.dispatch_targets.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="CLI error"
            ),
        ):
            assert target._fetch_task_status("task_1") is None

        with patch(
            "orchestune.dispatch_targets.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="not json", stderr=""
            ),
        ):
            assert target._fetch_task_status("task_1") is None

        with patch(
            "orchestune.dispatch_targets.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=30),
        ):
            assert target._fetch_task_status("task_1") is None

    def test_launch_with_bare_task_id_in_output(self, tmp_path):
        target = CodexCloudDispatchTarget("env_123", log_dir=tmp_path / "logs")
        push_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        submit_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Created task_bare_999 successfully\n",
            stderr="",
        )
        with patch(
            "orchestune.dispatch_targets.subprocess.run",
            side_effect=[push_result, submit_result],
        ):
            handle = target.launch(_task(), "claude/issue-1-task-a", tmp_path / "wt")

        assert handle.external_id == "task_bare_999"
        assert handle.external_url is None
        assert handle.branch_name == "claude/issue-1-task-a"

    def test_completion_status_when_cloud_task_failed(self):
        target = CodexCloudDispatchTarget("env_123")
        handle = DispatchHandle(
            external_id="task_fail_1",
            branch_name="claude/issue-1-task-a",
            issue_number=1,
        )
        with (
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[]),
            patch.object(target, "_fetch_task_status", return_value="failed"),
        ):
            assert target.completion_status(handle) == "abandoned"
            assert target.is_complete(handle) is False

    def test_completion_status_when_cloud_task_cancelled(self):
        target = CodexCloudDispatchTarget("env_123")
        handle = DispatchHandle(
            external_id="task_cancel_1",
            branch_name="claude/issue-1-task-a",
            issue_number=1,
        )
        with (
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[]),
            patch.object(target, "_fetch_task_status", return_value="cancelled"),
        ):
            assert target.completion_status(handle) == "abandoned"
            assert target.is_complete(handle) is False

    def test_completion_status_when_cloud_task_running(self):
        target = CodexCloudDispatchTarget("env_123")
        handle = DispatchHandle(
            external_id="task_run_1",
            branch_name="claude/issue-1-task-a",
            issue_number=1,
        )
        with (
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[]),
            patch.object(target, "_fetch_task_status", return_value="running"),
        ):
            assert target.completion_status(handle) == "pending"
            assert target.is_complete(handle) is False

    def test_completion_status_when_forge_fails_returns_pending_and_does_not_abandon(
        self,
    ):
        target = CodexCloudDispatchTarget("env_123")
        handle = DispatchHandle(
            external_id="task_fail_1",
            branch_name="claude/issue-1-task-a",
            issue_number=1,
        )
        with (
            patch(
                "orchestune.forge.GitHubForge.list_prs",
                side_effect=RuntimeError("GitHub API outage"),
            ),
            patch.object(
                target, "_fetch_task_status", return_value="failed"
            ) as mock_fetch,
        ):
            assert target.completion_status(handle) == "pending"
            assert target.is_complete(handle) is False
            mock_fetch.assert_not_called()

    def test_completion_status_when_comments_lookup_fails_returns_pending_and_does_not_abandon(
        self,
    ):
        target = CodexCloudDispatchTarget("env_123")
        handle = DispatchHandle(
            external_id="task_fail_1",
            branch_name="claude/issue-1-task-a",
            issue_number=1,
        )
        with (
            patch(
                "orchestune.forge.GitHubForge.list_prs",
                return_value=[
                    PrRecord(
                        number=1,
                        head_ref="claude/issue-1-task-a",
                        changed_files=(),
                        state="OPEN",
                    )
                ],
            ),
            patch(
                "orchestune.forge.GitHubForge.list_comments",
                side_effect=RuntimeError("GitHub comments outage"),
            ),
            patch.object(
                target, "_fetch_task_status", return_value="failed"
            ) as mock_fetch,
        ):
            assert target.completion_status(handle) == "pending"
            assert target.is_complete(handle) is False
            mock_fetch.assert_not_called()

    def test_is_complete_when_pr_is_open_for_task_branch(self):
        target = CodexCloudDispatchTarget("env_123")
        outcome = OutcomeRecord(result="done", issue=1, pr=1)
        with (
            patch(
                "orchestune.forge.GitHubForge.list_prs",
                return_value=[
                    PrRecord(
                        number=1, head_ref="claude/issue-1-task-a", changed_files=()
                    )
                ],
            ),
            patch(
                "orchestune.forge.GitHubForge.list_comments",
                return_value=[
                    {"body": outcome.render(), "created_at": "2026-01-01T00:00:00Z"}
                ],
            ),
        ):
            assert (
                target.is_complete(DispatchHandle(branch_name="claude/issue-1-task-a"))
                is True
            )

    def test_is_complete_when_matching_issue_pr_was_already_merged(self):
        """#210: merged PRs remain completion signals for Codex Cloud."""
        target = CodexCloudDispatchTarget("env_123")
        merged_pr = PrRecord(
            number=210,
            head_ref="codex/unexpected-branch",
            changed_files=(),
            closes_issue_numbers=(210,),
            state="MERGED",
        )
        with patch(
            "orchestune.forge.GitHubForge.list_prs",
            return_value=[merged_pr],
        ) as mock_list_prs:
            handle = DispatchHandle(
                branch_name="codex/issue-210-task-a",
                issue_number=210,
            )
            assert target.is_complete(handle) is True
        mock_list_prs.assert_called_once_with(state="all")

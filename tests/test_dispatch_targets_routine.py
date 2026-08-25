import json
import subprocess
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from orchestune.dispatch.targets import (
    BranchReachabilityError,
    ClaudeCodeCloudRoutineDispatchTarget,
    DispatchHandle,
    _push_branch_and_verify,
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


class TestClaudeCodeCloudRoutineDispatchTarget:
    @pytest.fixture(autouse=True)
    def _inject_forge(self, fake_forge):
        self.forge = fake_forge

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
        with (
            patch("orchestune.dispatch.targets._push_branch_and_verify"),
            patch(
                "orchestune.dispatch.targets.urllib.request.urlopen",
                return_value=self._response(),
            ) as mock_urlopen,
        ):
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
        # #157: クラウドルーチンも非対話実行のため、承認待ちで停止しないよう明示する。
        assert "非対話" in body["text"]
        assert "承認待ちで停止せず" in body["text"]
        assert "model" not in body

    def test_launch_with_execution_selection_passes_model_in_api_payload(
        self, tmp_path
    ):
        from orchestune.dispatch.execution_profiles import ExecutionSelection

        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "sk-ant-oat01-xxx")
        selection = ExecutionSelection(
            profile="deep",
            model="claude-3-7-sonnet-20250219",
            reasoning_effort=None,
            reason="test",
        )
        with (
            patch("orchestune.dispatch.targets._push_branch_and_verify"),
            patch(
                "orchestune.dispatch.targets.urllib.request.urlopen",
                return_value=self._response(),
            ) as mock_urlopen,
        ):
            handle = target.launch(
                _task(),
                "claude/issue-1-task-a",
                tmp_path / "wt",
                execution_selection=selection,
            )

        assert handle.external_id == "session_1"
        request = mock_urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        assert body["model"] == "claude-3-7-sonnet-20250219"

    def test_launch_with_execution_selection_reasoning_effort_logs_warning_and_skips(
        self, tmp_path, caplog
    ):
        import logging

        from orchestune.dispatch.execution_profiles import ExecutionSelection

        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "sk-ant-oat01-xxx")
        selection = ExecutionSelection(
            profile="deep",
            model="claude-3-7-sonnet-20250219",
            reasoning_effort="high",
            reason="test",
        )
        with (
            caplog.at_level(logging.WARNING),
            patch("orchestune.dispatch.targets._push_branch_and_verify"),
            patch(
                "orchestune.dispatch.targets.urllib.request.urlopen",
                return_value=self._response(),
            ) as mock_urlopen,
        ):
            handle = target.launch(
                _task(),
                "claude/issue-1-task-a",
                tmp_path / "wt",
                execution_selection=selection,
            )

        assert handle.external_id == "session_1"
        assert "does not support reasoning_effort" in caplog.text
        request = mock_urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        assert body["model"] == "claude-3-7-sonnet-20250219"
        assert "reasoning_effort" not in body

    def test_launch_pushes_and_verifies_branch_before_fire(self, tmp_path):
        """Reproducer #244: stacked/parent base付きで作られたローカルbranchは、
        fireより前にoriginへpushされ、到達性が検証されなければならない。"""
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")
        events: list[object] = []

        def fake_run(args, **kwargs):
            events.append(tuple(args))
            if args[:2] == ["git", "push"]:
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout="", stderr=""
                )
            if args[:2] == ["git", "rev-parse"]:
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout="abc123\n", stderr=""
                )
            if args[:2] == ["git", "ls-remote"]:
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout="abc123\trefs/heads/claude/issue-1-task-a\n",
                    stderr="",
                )
            raise AssertionError(f"unexpected command: {args}")

        def fake_urlopen(request, timeout=None):
            events.append("fire")
            return self._response()

        with (
            patch(
                "orchestune.dispatch.targets.subprocess.run", side_effect=fake_run
            ) as mock_run,
            patch(
                "orchestune.dispatch.targets.urllib.request.urlopen",
                side_effect=fake_urlopen,
            ),
        ):
            handle = target.launch(_task(), "claude/issue-1-task-a", tmp_path / "wt")

        assert handle.external_id == "session_1"
        push_call = mock_run.call_args_list[0]
        assert push_call.args[0] == [
            "git",
            "push",
            "--set-upstream",
            "origin",
            "claude/issue-1-task-a",
        ]
        assert push_call.kwargs["cwd"] == tmp_path / "wt"
        push_event = (
            "git",
            "push",
            "--set-upstream",
            "origin",
            "claude/issue-1-task-a",
        )
        assert events.index(push_event) < events.index("fire")

    def test_launch_force_pushes_after_rebase(self, tmp_path):
        """#384: 自動リベースによる再launchでは、rebaseで書き換え済みの履歴を
        安全に再push（--force-with-lease）できなければならない。force無しの
        通常pushは常にnon-fast-forwardで拒否されていた。"""
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "push"]:
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout="", stderr=""
                )
            if args[:2] == ["git", "rev-parse"]:
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout="abc123\n", stderr=""
                )
            if args[:2] == ["git", "ls-remote"]:
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout="abc123\trefs/heads/claude/issue-1-task-a\n",
                    stderr="",
                )
            raise AssertionError(f"unexpected command: {args}")

        with (
            patch(
                "orchestune.dispatch.targets.subprocess.run", side_effect=fake_run
            ) as mock_run,
            patch(
                "orchestune.dispatch.targets.urllib.request.urlopen",
                return_value=self._response(),
            ),
        ):
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

    def test_launch_does_not_fire_when_push_fails(self, tmp_path):
        """#244: pushに失敗した場合はfireせず、例外を伝播させる（fail closed）。"""
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")
        with (
            patch(
                "orchestune.dispatch.targets.subprocess.run",
                side_effect=subprocess.CalledProcessError(
                    returncode=1, cmd="git push", stderr="remote: permission denied"
                ),
            ),
            patch("orchestune.dispatch.targets.urllib.request.urlopen") as mock_urlopen,
        ):
            with pytest.raises(subprocess.CalledProcessError):
                target.launch(_task(), "claude/issue-1-task-a", tmp_path / "wt")

        mock_urlopen.assert_not_called()

    def test_launch_does_not_fire_when_remote_verification_fails(self, tmp_path):
        """#244: push後のリモートSHAがローカルHEADと一致しない場合はfireしない。"""
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "rev-parse"]:
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout="abc123\n", stderr=""
                )
            if args[:2] == ["git", "ls-remote"]:
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout="", stderr=""
                )
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="", stderr=""
            )

        with (
            patch("orchestune.dispatch.targets.subprocess.run", side_effect=fake_run),
            patch("orchestune.dispatch.targets.urllib.request.urlopen") as mock_urlopen,
        ):
            with pytest.raises(BranchReachabilityError, match="到達性を検証できません"):
                target.launch(_task(), "claude/issue-1-task-a", tmp_path / "wt")

        mock_urlopen.assert_not_called()

    def test_build_text_instructs_checkout_of_pushed_branch(self):
        """#244: リモートセッションがdefault branch基点で同名branchを新規作成
        しないよう、push済みbranchのcheckoutを明示的に指示する。"""
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")

        text = target._build_text(_task(), "claude/issue-1-task-a")

        assert "push済み" in text
        assert "新規作成せず" in text
        assert "checkout" in text

    def test_fire_text_fires_arbitrary_prompt_and_returns_handle(self):
        # #186: 統合コーディネーターが同一ルーチンへ任意指示を投げる汎用fire。
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "sk-ant-oat01-xxx")
        with patch(
            "orchestune.dispatch.targets.urllib.request.urlopen",
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
            patch("orchestune.dispatch.targets._push_branch_and_verify"),
            patch(
                "orchestune.dispatch.targets.urllib.request.urlopen",
                side_effect=[transient, self._response()],
            ),
            patch("orchestune.dispatch.targets.time.sleep") as mock_sleep,
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
            patch("orchestune.dispatch.targets._push_branch_and_verify"),
            patch(
                "orchestune.dispatch.targets.urllib.request.urlopen",
                side_effect=[transient, transient, transient],
            ),
            patch("orchestune.dispatch.targets.time.sleep"),
        ):
            with pytest.raises(urllib.error.HTTPError):
                target.launch(_task(), "claude/issue-1-task-a", tmp_path / "wt")

    def test_does_not_retry_on_client_error(self, tmp_path):
        target = ClaudeCodeCloudRoutineDispatchTarget(
            "trig_1", "token", max_retries=3, initial_delay=0.01
        )
        auth_error = urllib.error.HTTPError("url", 401, "unauthorized", {}, None)
        with (
            patch("orchestune.dispatch.targets._push_branch_and_verify"),
            patch(
                "orchestune.dispatch.targets.urllib.request.urlopen",
                side_effect=[auth_error, self._response()],
            ) as mock_urlopen,
            patch("orchestune.dispatch.targets.time.sleep") as mock_sleep,
        ):
            with pytest.raises(urllib.error.HTTPError):
                target.launch(_task(), "claude/issue-1-task-a", tmp_path / "wt")

        mock_sleep.assert_not_called()
        assert mock_urlopen.call_count == 1

    def test_is_complete_true_when_pr_open_for_branch(self):
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")
        outcome = OutcomeRecord(result="done", issue=1, pr=1)
        with (
            patch.object(
                self.forge,
                "list_prs",
                return_value=[
                    PrRecord(
                        number=1, head_ref="claude/issue-1-task-a", changed_files=()
                    )
                ],
            ),
            patch.object(
                self.forge,
                "list_comments",
                return_value=[
                    {"body": outcome.render(), "created_at": "2026-01-01T00:00:00Z"}
                ],
            ),
        ):
            handle = DispatchHandle(
                external_id="session_1", branch_name="claude/issue-1-task-a"
            )
            assert target.is_complete(handle, forge=self.forge) is True

    def test_is_complete_false_when_no_matching_pr(self):
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")
        with patch.object(
            self.forge,
            "list_prs",
            return_value=[
                PrRecord(number=1, head_ref="other-branch", changed_files=())
            ],
        ):
            handle = DispatchHandle(
                external_id="session_1", branch_name="claude/issue-1-task-a"
            )
            assert target.is_complete(handle, forge=self.forge) is False

    def test_closed_pr_closed_before_launch_is_ignored_as_stale(self):
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")
        with patch.object(
            self.forge,
            "list_prs",
            return_value=[
                PrRecord(
                    number=1,
                    head_ref="claude/issue-1-task-a",
                    changed_files=(),
                    created_at="2026-01-01T00:00:00Z",
                    closed_at="2026-01-02T00:00:00Z",
                    state="CLOSED",
                )
            ],
        ):
            handle = DispatchHandle(
                external_id="session_1",
                branch_name="claude/issue-1-task-a",
                started_at=1_800_000_000.0,
            )
            assert target.completion_status(handle, forge=self.forge) == "pending"

    def test_pr_created_and_closed_after_launch_is_abandoned(self):
        """#246: session開始後に作成されたPRがCLOSEされた場合のみabandoned。"""
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")
        with patch.object(
            self.forge,
            "list_prs",
            return_value=[
                PrRecord(
                    number=1,
                    head_ref="claude/issue-1-task-a",
                    changed_files=(),
                    created_at="2029-01-01T00:00:00Z",
                    closed_at="2030-01-01T00:00:00Z",
                    state="CLOSED",
                )
            ],
        ):
            handle = DispatchHandle(
                external_id="session_1",
                branch_name="claude/issue-1-task-a",
                started_at=1_800_000_000.0,
            )
            assert target.completion_status(handle, forge=self.forge) == "abandoned"

    def test_pr_created_in_same_second_as_started_at_is_not_stale(self):
        """#246レビュー(#262 P1) Reproducer: GitHubのcreated_atは秒精度で
        切り捨てられるため、`started_at`が小数秒を含む場合、実際にはsession
        開始「後」に作成された正規PRでも`created_at < started_at`が真になり
        誤ってstale扱いされうる（例: created_at=X.000, started_at=X.900）。
        比較はGitHubの精度に合わせて秒単位に揃え、同じ秒に作成されたPRは
        staleとしない。"""
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")
        outcome = OutcomeRecord(result="done", issue=1, pr=1)
        with (
            patch.object(
                self.forge,
                "list_prs",
                return_value=[
                    PrRecord(
                        number=1,
                        head_ref="claude/issue-1-task-a",
                        changed_files=(),
                        created_at="2026-01-01T00:00:00Z",
                        state="OPEN",
                    )
                ],
            ),
            patch.object(
                self.forge,
                "list_comments",
                return_value=[
                    {"body": outcome.render(), "created_at": "2026-01-01T00:00:00Z"}
                ],
            ),
        ):
            handle = DispatchHandle(
                external_id="session_1",
                branch_name="claude/issue-1-task-a",
                started_at=1_767_225_600.9,
            )
            assert target.completion_status(handle, forge=self.forge) == "completed"

    def test_pr_created_one_full_second_before_started_at_is_still_stale(self):
        """秒単位に精度を揃えても、実際に1秒以上前に作成された古いPRの
        stale判定は維持される。"""
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")
        with patch.object(
            self.forge,
            "list_prs",
            return_value=[
                PrRecord(
                    number=1,
                    head_ref="claude/issue-1-task-a",
                    changed_files=(),
                    created_at="2025-12-31T23:59:59Z",
                    state="OPEN",
                )
            ],
        ):
            handle = DispatchHandle(
                external_id="session_1",
                branch_name="claude/issue-1-task-a",
                started_at=1_767_225_600.9,
            )
            assert target.completion_status(handle, forge=self.forge) == "pending"

    def test_merged_pr_created_before_launch_is_ignored_as_stale(self):
        """#246 Reproducer: 同名branchの古いMERGED PR（session開始前に作成）が
        あると、再キューした新sessionが次サイクルで即completed扱いされていた。
        起動前のPRは状態に関係なく現sessionの成果物ではないため除外する。"""
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")
        with patch.object(
            self.forge,
            "list_prs",
            return_value=[
                PrRecord(
                    number=1,
                    head_ref="claude/issue-1-task-a",
                    changed_files=(),
                    created_at="2026-01-01T00:00:00Z",
                    closed_at="2026-01-02T00:00:00Z",
                    state="MERGED",
                )
            ],
        ):
            handle = DispatchHandle(
                external_id="session_1",
                branch_name="claude/issue-1-task-a",
                started_at=1_800_000_000.0,
            )
            assert target.completion_status(handle, forge=self.forge) == "pending"

    def test_merged_pr_created_after_launch_is_completed(self):
        """#246/#210: session開始後に作成・マージされたPRは完了シグナルのまま。"""
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")
        with patch.object(
            self.forge,
            "list_prs",
            return_value=[
                PrRecord(
                    number=1,
                    head_ref="claude/issue-1-task-a",
                    changed_files=(),
                    created_at="2029-01-01T00:00:00Z",
                    closed_at="2029-01-02T00:00:00Z",
                    state="MERGED",
                )
            ],
        ):
            handle = DispatchHandle(
                external_id="session_1",
                branch_name="claude/issue-1-task-a",
                started_at=1_800_000_000.0,
            )
            assert target.completion_status(handle, forge=self.forge) == "completed"

    def test_open_pr_created_before_launch_is_ignored_as_stale(self):
        """#246: session開始前から存在するOPEN PRも現sessionの成果物ではない。"""
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")
        with patch.object(
            self.forge,
            "list_prs",
            return_value=[
                PrRecord(
                    number=1,
                    head_ref="claude/issue-1-task-a",
                    changed_files=(),
                    created_at="2026-01-01T00:00:00Z",
                    state="OPEN",
                )
            ],
        ):
            handle = DispatchHandle(
                external_id="session_1",
                branch_name="claude/issue-1-task-a",
                started_at=1_800_000_000.0,
            )
            assert target.completion_status(handle, forge=self.forge) == "pending"

    def test_pr_without_created_at_is_ignored_when_started_at_known(self):
        """#246: created_atを解釈できないPRは現世代の証拠にならない（fail closed）。"""
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")
        with patch.object(
            self.forge,
            "list_prs",
            return_value=[
                PrRecord(
                    number=1,
                    head_ref="claude/issue-1-task-a",
                    changed_files=(),
                    state="MERGED",
                )
            ],
        ):
            handle = DispatchHandle(
                external_id="session_1",
                branch_name="claude/issue-1-task-a",
                started_at=1_800_000_000.0,
            )
            assert target.completion_status(handle, forge=self.forge) == "pending"

    def test_pr_counts_when_handle_has_no_started_at(self):
        """started_atを持たないhandle（復元経路）では従来通りPRを完了扱いする。"""
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")
        with patch.object(
            self.forge,
            "list_prs",
            return_value=[
                PrRecord(
                    number=1,
                    head_ref="claude/issue-1-task-a",
                    changed_files=(),
                    created_at="2026-01-01T00:00:00Z",
                    state="MERGED",
                )
            ],
        ):
            handle = DispatchHandle(
                external_id="session_1",
                branch_name="claude/issue-1-task-a",
                started_at=None,
            )
            assert target.completion_status(handle, forge=self.forge) == "completed"

    def test_is_complete_false_without_branch_name(self):
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")
        assert (
            target.is_complete(
                DispatchHandle(external_id="session_1"), forge=self.forge
            )
            is False
        )

    def test_is_complete_true_via_closing_issue_reference_when_branch_mismatches(self):
        """#239: AIセッションがブランチ名指示に従わなかった場合でも、
        PRのclosingIssuesReferences経由で完了を検知できる。"""
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")
        outcome = OutcomeRecord(result="done", issue=218, pr=1)
        with (
            patch.object(
                self.forge,
                "list_prs",
                return_value=[
                    PrRecord(
                        number=1,
                        head_ref="claude/elegant-noether-5rli7u",
                        changed_files=(),
                        closes_issue_numbers=(218,),
                        state="OPEN",
                    )
                ],
            ),
            patch.object(
                self.forge,
                "list_comments",
                return_value=[
                    {"body": outcome.render(), "created_at": "2026-01-01T00:00:00Z"}
                ],
            ),
        ):
            handle = DispatchHandle(
                external_id="session_1",
                branch_name="claude/issue-218-review-history-backend-api",
                issue_number=218,
            )
            assert target.is_complete(handle, forge=self.forge) is True

    def test_is_complete_false_when_neither_branch_nor_issue_match(self):
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")
        with patch.object(
            self.forge,
            "list_prs",
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
            assert target.is_complete(handle, forge=self.forge) is False

    def test_closed_unmerged_pr_is_abandoned_not_complete(self):
        """#210 review: rejected PRs must not mark dependencies done."""
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")
        closed_pr = PrRecord(
            number=1,
            head_ref="claude/issue-210-task-a",
            changed_files=(),
            state="CLOSED",
        )
        with patch.object(
            self.forge,
            "list_prs",
            return_value=[closed_pr],
        ) as mock_list_prs:
            handle = DispatchHandle(
                external_id="session_1",
                branch_name="claude/issue-210-task-a",
                issue_number=210,
            )
            assert target.completion_status(handle, forge=self.forge) == "abandoned"
            assert target.is_complete(handle, forge=self.forge) is False
        assert mock_list_prs.call_count == 2
        mock_list_prs.assert_called_with(state="all")

    def test_open_pr_without_outcome_is_pending(self):
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")
        with (
            patch.object(
                self.forge,
                "list_prs",
                return_value=[
                    PrRecord(
                        number=1,
                        head_ref="claude/issue-1-task-a",
                        changed_files=(),
                        created_at="2026-01-01T00:00:00Z",
                        state="OPEN",
                    )
                ],
            ),
            patch.object(self.forge, "list_comments", return_value=[]),
        ):
            handle = DispatchHandle(
                external_id="session_1",
                branch_name="claude/issue-1-task-a",
                issue_number=1,
            )
            assert target.completion_status(handle, forge=self.forge) == "pending"
            assert target.is_complete(handle, forge=self.forge) is False

    def test_open_pr_with_outcome_done_is_completed(self):
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")
        outcome = OutcomeRecord(result="done", issue=1, pr=1)
        with (
            patch.object(
                self.forge,
                "list_prs",
                return_value=[
                    PrRecord(
                        number=1,
                        head_ref="claude/issue-1-task-a",
                        changed_files=(),
                        created_at="2026-01-01T00:00:00Z",
                        state="OPEN",
                    )
                ],
            ),
            patch.object(
                self.forge,
                "list_comments",
                return_value=[
                    {"body": outcome.render(), "created_at": "2026-01-01T01:00:00Z"}
                ],
            ),
        ):
            handle = DispatchHandle(
                external_id="session_1",
                branch_name="claude/issue-1-task-a",
                issue_number=1,
            )
            assert target.completion_status(handle, forge=self.forge) == "completed"
            assert target.is_complete(handle, forge=self.forge) is True

    def test_open_pr_with_outcome_not_needed_is_completed(self):
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")
        outcome = OutcomeRecord(result="not-needed", issue=1, pr=1)
        with (
            patch.object(
                self.forge,
                "list_prs",
                return_value=[
                    PrRecord(
                        number=1,
                        head_ref="claude/issue-1-task-a",
                        changed_files=(),
                        created_at="2026-01-01T00:00:00Z",
                        state="OPEN",
                    )
                ],
            ),
            patch.object(
                self.forge,
                "list_comments",
                return_value=[
                    {"body": outcome.render(), "created_at": "2026-01-01T01:00:00Z"}
                ],
            ),
        ):
            handle = DispatchHandle(
                external_id="session_1",
                branch_name="claude/issue-1-task-a",
                issue_number=1,
            )
            assert target.completion_status(handle, forge=self.forge) == "completed"
            assert target.is_complete(handle, forge=self.forge) is True

    def test_open_pr_with_forge_error_is_pending(self):
        target = ClaudeCodeCloudRoutineDispatchTarget("trig_1", "token")
        with (
            patch.object(
                self.forge,
                "list_prs",
                return_value=[
                    PrRecord(
                        number=1,
                        head_ref="claude/issue-1-task-a",
                        changed_files=(),
                        created_at="2026-01-01T00:00:00Z",
                        state="OPEN",
                    )
                ],
            ),
            patch.object(
                self.forge,
                "list_comments",
                side_effect=RuntimeError("forge connection timeout"),
            ),
        ):
            handle = DispatchHandle(
                external_id="session_1",
                branch_name="claude/issue-1-task-a",
                issue_number=1,
            )
            assert target.completion_status(handle, forge=self.forge) == "pending"
            assert target.is_complete(handle, forge=self.forge) is False


class TestPushBranchAndVerifyWithRealGit:
    """#244: stacked base（依存先branch）・parent base（親branch）の変更が、
    push後のリモートtask branchに実際に含まれることを実gitリポジトリで検証する。"""

    def _git(self, cwd, *args):
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    @pytest.mark.parametrize(
        "base_branch",
        ["claude/issue-9-dep-task", "parent/issue-100"],
        ids=["stacked_base", "parent_base"],
    )
    def test_pushed_branch_contains_base_changes(self, tmp_path, base_branch):
        origin = tmp_path / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", str(origin)], check=True, capture_output=True
        )
        clone = tmp_path / "clone"
        subprocess.run(
            ["git", "clone", str(origin), str(clone)], check=True, capture_output=True
        )
        self._git(clone, "config", "user.email", "test@example.com")
        self._git(clone, "config", "user.name", "test")

        (clone / "README.md").write_text("initial\n")
        self._git(clone, "add", "README.md")
        self._git(clone, "commit", "-m", "initial commit")
        self._git(clone, "push", "-u", "origin", "HEAD")

        # baseブランチ（依存先タスク/親Issueの成果）に変更を積む
        self._git(clone, "checkout", "-b", base_branch)
        (clone / "base_change.py").write_text("VALUE = 1\n")
        self._git(clone, "add", "base_change.py")
        self._git(clone, "commit", "-m", "base change")
        base_sha = self._git(clone, "rev-parse", "HEAD")

        # dispatcherと同様に、baseからtask branchのworktreeを作成する
        task_branch = "claude/issue-1-task-a"
        worktree_path = tmp_path / "wt"
        self._git(
            clone, "worktree", "add", "-b", task_branch, str(worktree_path), base_branch
        )

        _push_branch_and_verify(task_branch, worktree_path)

        remote_task_sha = self._git(origin, "rev-parse", f"refs/heads/{task_branch}")
        local_task_sha = self._git(worktree_path, "rev-parse", "HEAD")
        assert remote_task_sha == local_task_sha
        # リモートのtask branchがbaseの変更コミットを含む
        subprocess.run(
            [
                "git",
                "-C",
                str(origin),
                "merge-base",
                "--is-ancestor",
                base_sha,
                remote_task_sha,
            ],
            check=True,
        )

"""dispatcherのディスパッチループ統合テスト（run_dispatch_cycle経由）。

`tests/test_dispatcher.py`の肥大化解消のため分割している（#349）。
CLI引数・設定ファイル読み込み、およびpost-cycleのベストエフォート処理
（`_run_best_effort_phase`とその利用箇所）・`main`のテストは
`test_dispatcher_cli.py`へ分割し、本ファイルには`run_dispatch_cycle`
自体の挙動（イベントログ・リカバリ・ロック・クラッシュ安全性・重複起動
防止）を検証するテストを残している。
"""

import json
import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestune.consistency.models import RepairStatus
from orchestune.consistency.repairs.execution import COMMAND_REQUEUE
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle import (
    CycleReport,
    append_event_log,
    build_event_log_entry,
    run_dispatch_cycle,
)
from orchestune.dispatch.dispatcher import (
    _DispatcherRunResult,
    _emit_dispatcher_report,
)
from orchestune.dispatch.scoring import SchedulingDecision, ScoreComponents
from orchestune.dispatch.state import (
    ActiveWorktree,
    CompletedWorktree,
    RunState,
    load_run_state,
)
from orchestune.dispatch.summary import (
    REASON_EXTERNAL_LOCK,
    SUMMARY_PREFIX,
    SkipRecord,
)
from orchestune.models import PrRecord, Task
from orchestune.outcome_record import OutcomeRecord
from tests.dispatch_test_support import make_footprint_issue as _issue
from tests.dispatch_test_support import (
    save_locked_run_state as save_run_state,
)
from tests.dispatch_test_support import (
    stub_forge_check_auth,
    stub_label_actor_permission,
)

tmp_path = Path(tempfile.mkdtemp(prefix="orchestune-test-state-"))


@pytest.fixture(autouse=True)
def _stub_forge_check_auth_by_default(fake_forge):
    """`GitHubForge.check_auth()`が実際のgh認証エラーを投げないようスタブする。"""
    return stub_forge_check_auth(fake_forge)


@pytest.fixture(autouse=True)
def _stub_label_actor_permission_by_default(fake_forge):
    """#119のactor権限検証が実際の`gh api`を叩かないようスタブする。"""
    stub_label_actor_permission(fake_forge)


def _task(
    issue_number,
    priority="medium",
    risk=False,
    progress_partial=False,
    created_at="2023-01-01T00:00:00+00:00",
    footprint=("src/foo.py",),
    depends_on=(),
):
    return Task(
        issue_number=issue_number,
        subtask_id=f"task-{issue_number}",
        footprint=footprint,
        symbols=(),
        risk=risk,
        priority=priority,
        progress_partial=progress_partial,
        status_labels=("status:queued",),
        created_at=created_at,
        depends_on=depends_on,
    )


class TestAppendEventLog:
    def test_build_event_log_entry_includes_cycle_events(self):
        report = CycleReport(
            selected=[_task(1)],
            quota_slots_available=0,
            lock_changes={"to_lock": [], "to_unlock": []},
            deviation_events=[{"issue_number": 1, "action": "recomputed"}],
            completion_events=[{"issue_number": 2, "action": "completed"}],
            promotion_events=[{"issue_number": 3, "subtask_id": "task-c"}],
            applied=True,
        )
        entry = build_event_log_entry(report, now=1700000000.0)
        assert entry["timestamp"] == 1700000000.0
        assert entry["quota_slots_available"] == 0
        assert entry["selected"] == [{"issue_number": 1, "subtask_id": "task-1"}]
        assert entry["deviation_events"] == report.deviation_events
        assert entry["completion_events"] == report.completion_events
        assert entry["promotion_events"] == report.promotion_events

    def test_event_log_entry_carries_scheduling_decisions(self):
        """#660: 選定理由・rank・推定costがKPI集計用ログからも観測できること。"""
        decision = SchedulingDecision(
            issue_number=7,
            subtask_id="task-7",
            mode="critical-path",
            score=3.25,
            components=ScoreComponents(base_priority=2.0, critical_path=0.5),
            bottom_level=5400.0,
            unlocked_count=2,
            downstream_count=4,
            estimated_tokens=900,
            estimated_duration_seconds=1800.0,
            estimate_source="task-history",
            exact_bottom_level=True,
            exact_downstream=False,
            selected=False,
            reason="quota-exhausted",
        )
        report = CycleReport(
            selected=[],
            quota_slots_available=0,
            lock_changes={"to_lock": [], "to_unlock": []},
            deviation_events=[],
            completion_events=[],
            promotion_events=[],
            applied=True,
            scheduling_decisions=[decision],
        )

        entry = build_event_log_entry(report, now=1700000000.0)

        assert entry["scheduling_decisions"] == [
            {
                "issue_number": 7,
                "subtask_id": "task-7",
                "mode": "critical-path",
                "score": 3.25,
                "components": {
                    "base_priority": 2.0,
                    "aging": 0.0,
                    "critical_path": 0.5,
                    "unlock": 0.0,
                    "progress": 0.0,
                    "token_penalty": 0.0,
                    "rework_penalty": 0.0,
                },
                "bottom_level": 5400.0,
                "unlocked_count": 2,
                "downstream_count": 4,
                "estimated_tokens": 900,
                "estimated_duration_seconds": 1800.0,
                "estimate_source": "task-history",
                "exact_bottom_level": True,
                "exact_downstream": False,
                "selected": False,
                "reason": "quota-exhausted",
            }
        ]
        # JSON Linesとして書き出せる（＝dataclassが残っていない）こと。
        json.dumps(entry)

    def test_append_event_log_writes_jsonl(self, tmp_path):
        path = tmp_path / "events.jsonl"
        append_event_log({"timestamp": 1.0, "foo": "bar"}, path)
        append_event_log({"timestamp": 2.0, "foo": "baz"}, path)

        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"timestamp": 1.0, "foo": "bar"}
        assert json.loads(lines[1]) == {"timestamp": 2.0, "foo": "baz"}

    def test_append_event_log_creates_parent_directories(self, tmp_path):
        path = tmp_path / "nested" / "events.jsonl"
        append_event_log({"timestamp": 1.0}, path)
        assert path.exists()


class TestRecoveredActiveTask:
    def test_run_cycle_requeues_missing_execution_without_resource(
        self, tmp_path, fake_forge
    ):
        """Supervisor の typed repair が実行資源のないタスクを再キューする。"""
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
            task_timeout_seconds=60,
        )
        issue = _issue(1, labels=("status:in-progress",), subtask_id="task-a")

        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        mock_list = fake_forge.list_issues_by_label
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        fake_forge.get_issue_state.reset_mock(side_effect=True)
        fake_forge.get_issue_state.return_value = "OPEN"
        fake_forge.get_issue_labels.reset_mock(side_effect=True)
        fake_forge.get_issue_labels.return_value = ("status:queued",)
        fake_forge.add_label.reset_mock(side_effect=True)
        mock_add_label = fake_forge.add_label
        fake_forge.remove_label.reset_mock(side_effect=True)
        mock_remove_label = fake_forge.remove_label
        fake_forge.add_comment.reset_mock(side_effect=True)
        fake_forge.get_issue_labels.return_value = ("status:in-progress",)
        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches",
                autospec=True,
                return_value=[],
            ),
            patch(
                "orchestune.infra.git_cli.subprocess.run",
                return_value=MagicMock(stdout=""),
            ),
            patch(
                "orchestune.dispatch.rebase.check_footprint_deviation",
                autospec=True,
                return_value=[],
            ),
        ):
            mock_list.side_effect = lambda label, **_: (
                [issue] if label == "status:in-progress" else []
            )

            report = run_dispatch_cycle(config)

        assert "1" not in load_run_state(config.run_state_path).active_worktrees
        assert report.completion_events == []
        repair_results = tuple(
            result
            for repair_pass in report.consistency.repair_passes
            for result in repair_pass.results
        )
        assert any(
            result.command.code == COMMAND_REQUEUE
            and result.status is RepairStatus.APPLIED
            for result in repair_results
        ), tuple(
            (result.command.code, result.status, result.diagnostics)
            for result in repair_results
        )
        mock_remove_label.assert_called_once_with(1, "status:in-progress")
        mock_add_label.assert_called_once_with(1, "status:queued")

    def test_next_cycle_completes_recovered_task_when_closing_pr_appears(
        self, tmp_path, fake_forge
    ):
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
        )
        active = ActiveWorktree(
            issue_number=1,
            branch="claude/issue-1-task-a",
            worktree_path=str(tmp_path / "worktrees" / "missing-worktree"),
            pid=None,
            started_at=None,
            declared_footprint=(),
        )
        save_run_state(RunState(active_worktrees={"1": active}), config.run_state_path)
        issue = _issue(1, labels=("status:in-progress",), subtask_id="task-a")
        pr = PrRecord(
            number=101,
            head_ref="agent/issue-1-task-a",
            changed_files=("src/foo.py",),
            closes_issue_numbers=(1,),
        )

        outcome = OutcomeRecord(result="done", issue=1, pr=101)
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        mock_list = fake_forge.list_issues_by_label
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = [pr]
        fake_forge.list_prs.reset_mock(side_effect=True)
        fake_forge.list_prs.return_value = [pr]
        fake_forge.list_comments.reset_mock(side_effect=True)
        fake_forge.list_comments.return_value = [
            {"body": outcome.render(), "created_at": "2026-01-01T00:00:00Z"}
        ]
        fake_forge.add_label.reset_mock(side_effect=True)
        mock_add_label = fake_forge.add_label
        fake_forge.remove_label.reset_mock(side_effect=True)
        mock_remove_label = fake_forge.remove_label
        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches",
                autospec=True,
                return_value=[],
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                autospec=True,
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.remote_branch_commit_sha_if_ahead",
                autospec=True,
                return_value="recovered-commit",
            ),
            patch("orchestune.dispatch.gc.completion.remove_worktree", autospec=True),
        ):
            mock_list.side_effect = lambda label, **_: (
                [issue] if label == "status:in-progress" else []
            )

            report = run_dispatch_cycle(config)

        assert report.completion_events[0]["action"] == "completed"
        assert report.completion_events[0]["commit_sha"] == "recovered-commit"
        assert load_run_state(config.run_state_path).active_worktrees == {}
        mock_remove_label.assert_any_call(1, "status:in-progress")
        mock_add_label.assert_any_call(1, "status:done")


class TestDispatcherLocking:
    @pytest.mark.uses_real_file_lock
    def test_run_dispatch_cycle_raises_runtime_error_if_locked(
        self, tmp_path, fake_forge
    ):
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=False,
        )
        lock_path = Path(config.run_state_path).with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        from orchestune.dispatch.worktree import file_lock

        with file_lock(lock_path):
            with pytest.raises(RuntimeError) as exc_info:
                fake_forge.list_issues_by_label.reset_mock(side_effect=True)
                fake_forge.list_issues_by_label.return_value = []
                fake_forge.list_open_prs.reset_mock(side_effect=True)
                fake_forge.list_open_prs.return_value = []
                with (
                    patch(
                        "orchestune.dispatch.phase_rebase.list_remote_branches",
                        autospec=True,
                        return_value=[],
                    ),
                ):
                    run_dispatch_cycle(config)
            assert "another process is currently holding the lock" in str(
                exc_info.value
            )
            assert str(lock_path) in str(exc_info.value)


class TestLaunchOrderingCrashSafety:
    """run_stateへの登録とGitHubラベル更新の順序を入れ替え、クラッシュ時に
    「GitHub側は確定・run_state側は空」という検出不能な非対称が起きないようにする。"""

    def test_run_state_is_persisted_before_label_transition_and_survives_crash(
        self, tmp_path, fake_forge
    ):
        config = DispatcherConfig(
            parent_issue_number=100,
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        queued_issue = _issue(1)

        def remove_label_side_effect(issue_number, label):
            if label == "status:queued":
                raise RuntimeError("simulated crash during label transition")

        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        mock_list = fake_forge.list_issues_by_label
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        fake_forge.add_label.reset_mock(side_effect=True)
        mock_add_label = fake_forge.add_label
        fake_forge.remove_label.reset_mock(side_effect=True)
        fake_forge.remove_label.side_effect = remove_label_side_effect
        # #943: dispatchのlaunchはclaim_task経由になり、起動対象issueを
        # `forge.get_issue`で再取得・再検証する。
        fake_forge.get_issue.reset_mock(side_effect=True)
        fake_forge.get_issue.return_value = queued_issue
        with (
            patch(
                "orchestune.dispatch.worktree._branch_exists",
                autospec=True,
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches",
                autospec=True,
                return_value=[],
            ),
            patch("orchestune.dispatch.worktree.subprocess.run") as mock_subproc_run,
            patch("orchestune.dispatch.targets.subprocess.Popen") as mock_popen,
        ):
            mock_list.side_effect = lambda label, **_: (
                [queued_issue] if label == "status:queued" else []
            )
            mock_subproc_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            mock_popen.return_value.pid = 555

            # #943: dispatchの起動はclaim_task経由になった。claim_task自身が
            # ラベルAPI失敗を`ClaimFailure(LABEL_UPDATE_FAILED)`として捕捉し、
            # `stage=ACTIVE_SAVED`（予約・worktreeは既に永続化済み）で
            # 失敗outcomeを返す（生の例外は外へ伝播しない、#943の設計）。
            # dispatch側は、claimが既に部分的に触れたラベル状態の上へ
            # 独自の`status:blocked`遷移を重ねて矛盾を増やすことをせず、
            # 次サイクルの整合性回復に委ねて保留する（クラッシュしない）。
            run_dispatch_cycle(config)

        # #381: status:in-progressの付与はstatus:queuedの除去より先に行われる
        # ため（transition_status_label、claim_task内部でも同じ関数を使う）、
        # 除去で失敗してもstatus:in-progressは既に付与済みになっている。
        # Issueは常にどちらかのstatus:*ラベルを持ち続ける
        # （この場合は両方が一時的に併存する）。
        mock_add_label.assert_called_once_with(1, "status:in-progress")

        # しかし、run_state.json にはactive_worktreeエントリが既に永続化されている
        # （claim_taskが予約・worktree準備を先に確定し、ラベル遷移はその後の
        # 段階であるため、dispatch側の共有run_stateへも同期される）。
        assert (tmp_path / "run_state.json").exists()
        persisted = json.loads((tmp_path / "run_state.json").read_text())
        assert "1" in persisted["active_worktrees"]


class TestStaleActiveEntryReconciliation:
    """run_stateにエントリが残っているが、GitHub側のラベルが実際には
    status:in-progressになっていない（起動直後のクラッシュ等による）場合、
    run_state側を破棄してGitHubラベルを正とする（ゾンビGCの拡張）。"""

    def test_stale_entry_without_in_progress_label_is_discarded(
        self, tmp_path, fake_forge
    ):
        run_state_path = tmp_path / "run_state.json"
        save_run_state(
            RunState(
                active_worktrees={
                    "1": ActiveWorktree(
                        issue_number=1,
                        branch="claude/issue-1-task-a",
                        worktree_path=str(tmp_path / "w1"),
                        pid=111,
                        started_at=1_699_999_000.0,
                        declared_footprint=("src/foo.py",),
                    )
                },
                launch_history=[],
            ),
            run_state_path,
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            max_concurrent=0,
            max_launches_per_window=0,
            window_seconds=3600,
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        # 起動処理自体はcreate_worktree_and_launch成功後の何らかの時点で
        # クラッシュしており、GitHub側のラベルは "status:queued" のまま
        # （status:in-progressへの遷移は未完了）という状況を再現する。
        queued_issue = _issue(1, labels=("status:queued",), subtask_id="task-1")

        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        mock_list = fake_forge.list_issues_by_label
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        fake_forge.add_label.reset_mock(side_effect=True)
        mock_add_label = fake_forge.add_label
        fake_forge.remove_label.reset_mock(side_effect=True)
        mock_remove_label = fake_forge.remove_label
        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches",
                autospec=True,
                return_value=[],
            ),
        ):
            mock_list.side_effect = lambda label, **_: (
                [queued_issue] if label == "status:queued" else []
            )
            report = run_dispatch_cycle(config)

        # GC/完了検知としてはラベルに一切触らない（GitHub側は既にqueuedのまま
        # で正しいため、ここでラベル操作をしてはいけない）。
        mock_add_label.assert_not_called()
        mock_remove_label.assert_not_called()
        fake_forge.get_issue_state.assert_called_with(1)
        fake_forge.get_issue_labels.assert_called_with(1)

        assert any(
            event.get("action") == "stale_active_entry_discarded"
            for event in report.completion_events
        )

        loaded = load_run_state(run_state_path)
        assert "1" not in loaded.active_worktrees


class TestPreventDuplicateSessions:
    @patch(
        "orchestune.dispatch.phase_rebase.list_remote_branches",
        autospec=True,
        return_value=[],
    )
    @patch("orchestune.dispatch.worktree.subprocess.run")
    @patch("orchestune.dispatch.targets.subprocess.Popen")
    def test_run_dispatch_cycle_skips_launch_if_open_pr_exists(
        self,
        mock_popen,
        mock_subproc_run,
        mock_list_branches,
        tmp_path,
        fake_forge,
    ):
        mock_add_comment = fake_forge.add_comment
        mock_add_label = fake_forge.add_label
        mock_remove_label = fake_forge.remove_label
        mock_list_prs = fake_forge.list_open_prs
        mock_list_issues = fake_forge.list_issues_by_label
        config = DispatcherConfig(
            parent_issue_number=100,
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
            forge=fake_forge,
        )
        queued_issue = _issue(1, subtask_id="task-1")
        mock_list_issues.side_effect = lambda label, **_: (
            [queued_issue] if label == "status:queued" else []
        )

        # open PR with expected head_ref branch name exists
        mock_list_prs.return_value = [
            PrRecord(
                number=10,
                head_ref="claude/issue-1-task-1",
                changed_files=(),
                closes_issue_numbers=(),
                review_decision="",
                is_ci_passing=True,
                is_cross_repository=False,
            )
        ]

        report = run_dispatch_cycle(config)

        # 起動がスキップされていること
        assert len(report.selected) == 0
        assert mock_popen.call_count == 0

        # ラベル遷移とコメント追加が行われていること
        mock_remove_label.assert_any_call(1, "status:queued")
        mock_add_label.assert_any_call(1, "status:blocked-human-review")
        mock_add_comment.assert_called_once()
        assert "重複起動防止" in mock_add_comment.call_args[0][1]

    @patch(
        "orchestune.dispatch.phase_rebase.list_remote_branches",
        autospec=True,
        return_value=[],
    )
    @patch(
        "orchestune.dispatch.worktree._branch_exists",
        autospec=True,
        return_value=False,
    )
    @patch("orchestune.dispatch.worktree.subprocess.run")
    @patch("orchestune.dispatch.targets.subprocess.Popen")
    def test_run_dispatch_cycle_ignores_unrelated_closes_issue_pr(
        self,
        mock_popen,
        mock_subproc_run,
        mock_branch_exists,
        mock_list_branches,
        tmp_path,
        fake_forge,
    ):
        mock_add_comment = fake_forge.add_comment
        mock_add_label = fake_forge.add_label
        mock_remove_label = fake_forge.remove_label
        mock_list_prs = fake_forge.list_open_prs
        mock_list_issues = fake_forge.list_issues_by_label
        config = DispatcherConfig(
            parent_issue_number=100,
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
            forge=fake_forge,
        )
        queued_issue = _issue(1, subtask_id="task-1")
        mock_list_issues.side_effect = lambda label, **_: (
            [queued_issue] if label == "status:queued" else []
        )
        # #943: dispatchのlaunchはclaim_task経由になり、起動対象issueを
        # `forge.get_issue`で再取得・再検証する。
        fake_forge.get_issue.reset_mock(side_effect=True)
        fake_forge.get_issue.return_value = queued_issue

        # unrelated open PR closes issue 1, but head_ref does not follow Orchestune launch branch naming
        mock_list_prs.return_value = [
            PrRecord(
                number=10,
                head_ref="some-other-branch-name",
                changed_files=(),
                closes_issue_numbers=(1,),
                review_decision="",
                is_ci_passing=True,
            )
        ]
        mock_popen.return_value.pid = 12345
        mock_subproc_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        report = run_dispatch_cycle(config)

        # 無関係なPRは重複扱いせず、起動候補として残ること
        assert len(report.selected) == 1
        mock_popen.assert_called_once()
        mock_remove_label.assert_any_call(1, "status:queued")
        mock_add_label.assert_any_call(1, "status:in-progress")
        mock_add_comment.assert_not_called()

    def test_ls_remote_uses_existing_pr_head_ref_for_closes_issue_match(
        self, tmp_path, fake_forge
    ):
        config = DispatcherConfig(
            parent_issue_number=100,
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        queued_issue = _issue(1, subtask_id="task-1")

        recent_time = time.time() - 100.0
        save_run_state(
            RunState(
                completed_worktrees=[
                    CompletedWorktree(
                        issue_number=1,
                        subtask_id="task-1",
                        branch="claude/issue-1-task-1",
                        started_at=recent_time - 100.0,
                        completed_at=recent_time,
                        commit_sha="old-sha",
                    )
                ]
            ),
            config.run_state_path,
            now=recent_time,
        )

        def ls_remote_result(command, **_kwargs):
            stdout = (
                "updated-sha\trefs/heads/codex/issue-1-task-1\n"
                if command
                == [
                    "git",
                    "ls-remote",
                    "origin",
                    "refs/heads/codex/issue-1-task-1",
                ]
                else ""
            )
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        mock_list = fake_forge.list_issues_by_label
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = [
            PrRecord(
                number=101,
                head_ref="codex/issue-1-task-1",
                changed_files=(),
                review_decision="",
                is_ci_passing=False,
                closes_issue_numbers=(1,),
                is_cross_repository=False,
            )
        ]
        fake_forge.branch_exists.return_value = False
        fake_forge.add_label.reset_mock(side_effect=True)
        mock_add_label = fake_forge.add_label
        fake_forge.remove_label.reset_mock(side_effect=True)
        mock_remove_label = fake_forge.remove_label
        fake_forge.add_comment.reset_mock(side_effect=True)
        mock_add_comment = fake_forge.add_comment
        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches",
                autospec=True,
                return_value=[],
            ),
            patch(
                "orchestune.infra.git_cli.subprocess.run",
                side_effect=ls_remote_result,
            ) as mock_subprocess_run,
            patch("orchestune.dispatch.targets.subprocess.Popen") as mock_popen,
        ):
            mock_popen.return_value.pid = 12345
            mock_list.side_effect = lambda label, **_: (
                [queued_issue] if label == "status:queued" else []
            )
            report = run_dispatch_cycle(config)

        assert report.selected == []
        mock_popen.assert_not_called()
        mock_subprocess_run.assert_called_once_with(
            [
                "git",
                "ls-remote",
                "origin",
                "refs/heads/codex/issue-1-task-1",
            ],
            cwd=None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )

        mock_remove_label.assert_any_call(1, "status:queued")
        mock_add_label.assert_any_call(1, "status:blocked-human-review")
        mock_add_comment.assert_called_once()

    def test_ls_remote_failure_transitions_to_blocked_human_review(
        self, tmp_path, fake_forge
    ):
        """ls-remoteが例外等で失敗した場合は、安全のため重複とみなして起動をスキップする。"""
        config = DispatcherConfig(
            parent_issue_number=100,
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        # すでに過去の完了履歴があり、かつオープンなPRがあるタスク
        queued_issue = _issue(1, subtask_id="task-1")
        run_state = RunState(
            completed_worktrees=[
                CompletedWorktree(
                    issue_number=1,
                    subtask_id="task-1",
                    branch="claude/issue-1-task-1",
                    started_at=100.0,
                    completed_at=200.0,
                    commit_sha="old-sha",
                )
            ]
        )
        save_run_state(run_state, config.run_state_path)

        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        mock_list = fake_forge.list_issues_by_label
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = [
            PrRecord(
                number=101,
                head_ref="claude/issue-1-task-1",
                changed_files=(),
                review_decision="",
                is_ci_passing=False,
                closes_issue_numbers=(1,),
                is_cross_repository=False,
            )
        ]
        fake_forge.add_label.reset_mock(side_effect=True)
        mock_add_label = fake_forge.add_label
        fake_forge.remove_label.reset_mock(side_effect=True)
        mock_remove_label = fake_forge.remove_label
        fake_forge.add_comment.reset_mock(side_effect=True)
        mock_add_comment = fake_forge.add_comment
        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches",
                autospec=True,
                return_value=[],
            ),
            patch(
                "orchestune.dispatch.worktree.subprocess.run",
                side_effect=subprocess.CalledProcessError(
                    returncode=128, cmd="git ls-remote"
                ),
            ),
        ):
            mock_list.side_effect = lambda label, **_: (
                [queued_issue] if label == "status:queued" else []
            )
            report = run_dispatch_cycle(config)

        # 起動はスキップされ、status:blocked-human-review ラベルが付与されている
        assert report.selected == []
        mock_remove_label.assert_any_call(1, "status:queued")
        mock_add_label.assert_any_call(1, "status:blocked-human-review")
        mock_add_comment.assert_called_once()
        assert "重複起動防止" in mock_add_comment.call_args[0][1]


class TestEmitHumanSummary:
    """#787: stdoutのJSONとは別に、人間向けの要約をstderrへ出す。"""

    def _result(self, report):
        return _DispatcherRunResult(
            report=report, post_cycle_results=[], integrator_run_report=None
        )

    def _report(self, **overrides):
        defaults = dict(
            selected=[],
            quota_slots_available=1,
            lock_changes={"to_lock": [], "to_unlock": []},
            deviation_events=[],
            completion_events=[],
            promotion_events=[],
            applied=True,
        )
        defaults.update(overrides)
        return CycleReport(**defaults)

    def test_keeps_stdout_as_pure_json(self, capsys):
        report = self._report(
            skips=[
                SkipRecord(
                    issue_number=695,
                    subtask_id="task-695",
                    reason=REASON_EXTERNAL_LOCK,
                    detail="fix/issue-777 [tests/conftest.py]",
                )
            ]
        )

        _emit_dispatcher_report(self._result(report))

        captured = capsys.readouterr()
        json.loads(captured.out)
        assert SUMMARY_PREFIX in captured.err
        assert "#695" in captured.err
        assert "fix/issue-777 [tests/conftest.py]" in captured.err

    def test_stderr_summary_is_ascii_only(self, capsys):
        report = self._report(
            skips=[
                SkipRecord(
                    issue_number=1,
                    subtask_id="task-a",
                    reason=REASON_EXTERNAL_LOCK,
                    detail="feat/x [a.py]",
                )
            ],
            forge_warnings=[
                {"issue_number": 2, "operation": "list_prs", "error": "HTTPError: 504"}
            ],
        )

        _emit_dispatcher_report(self._result(report))

        capsys.readouterr().err.encode("ascii")

    def test_stays_silent_when_nothing_was_skipped(self, capsys):
        _emit_dispatcher_report(self._result(self._report()))
        assert capsys.readouterr().err == ""

    def test_a_rendering_failure_does_not_break_the_report(self, capsys):
        """要約はベストエフォート。整形に失敗してもサイクルの結果報告は落とさない。"""
        with patch(
            "orchestune.dispatch.dispatcher.render_skipped_text",
            autospec=True,
            side_effect=RuntimeError("boom"),
        ):
            _emit_dispatcher_report(self._result(self._report()))

        captured = capsys.readouterr()
        json.loads(captured.out)
        assert "Failed to render the cycle summary" in captured.err


class TestEventLogCarriesLockConflicts:
    """PR#789レビュー(Codex P2): events.jsonlを歴史的成果物として保持する運用でも
    衝突相手のブランチ/PRとファイルを特定できるようにする。"""

    def test_build_event_log_entry_includes_external_lock_conflicts(self):
        conflicts = {
            695: [
                {
                    "kind": "branch",
                    "source": "fix/issue-777-branch-naming",
                    "files": ["tests/conftest.py"],
                }
            ]
        }
        report = CycleReport(
            selected=[],
            quota_slots_available=0,
            lock_changes={"to_lock": [], "to_unlock": []},
            deviation_events=[],
            completion_events=[],
            promotion_events=[],
            applied=True,
            external_lock_conflicts=conflicts,
        )

        entry = build_event_log_entry(report, now=1700000000.0)

        assert entry["external_lock_conflicts"] == conflicts
        json.dumps(entry, ensure_ascii=False)

"""dispatch_cycleのコア機能（候補タスク決定・Issue取得・グルーピング・
起動確定・エンドツーエンドの基本フロー）テスト。

状態調整・整合性修復は`test_dispatch_cycle_reconciliation.py`、逸脱
フィルタリングは`test_dispatch_cycle_filters.py`、外部ロック同期は
`test_dispatch_cycle_locks.py`、アクティブworktree処理は
`test_dispatch_cycle_worktree.py`、完了・not-needed処理は
`test_dispatch_cycle_completion.py`へそれぞれ分割している（#343）。
"""

import json
import subprocess
import tempfile
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle import run_dispatch_cycle
from orchestune.dispatch.cycle_context import (
    IssuesByStatus,
    _fetch_issues,
    _group_by_status,
)
from orchestune.dispatch.locks import ExternalLockScanResult
from orchestune.dispatch.phase_scheduling import (
    _determine_candidate_tasks,
    _finalize_launch,
    run_scheduling_phase,
)
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import (
    ActiveWorktree,
    RunState,
    TaskReclaimRecord,
    load_run_state,
    save_run_state,
)
from orchestune.issue_parsing import PARENT_MARKER
from orchestune.models import IssueRecord, PrRecord
from tests.conftest import make_issue


@contextmanager
def _patch_gc_process_alive(*, return_value: bool):
    """Patch every consumer split from the former dispatch_gc dependency."""
    with ExitStack() as stack:
        for target in (
            "orchestune.dispatch.gc.is_process_alive",
            "orchestune.dispatch.gc.completion.is_process_alive",
            "orchestune.dispatch.gc.zombies.is_process_alive",
        ):
            stack.enter_context(patch(target, return_value=return_value))
        yield


tmp_path = Path(tempfile.mkdtemp(prefix="orchestune-test-state-"))


@pytest.fixture(autouse=True)
def _stub_label_actor_permission_by_default(fake_forge):
    """#119で追加したactor権限検証ステップが、既存の大半のテストで実際の
    `gh api`呼び出しを行わないよう、デフォルトで許可された actor/permission を
    返すようスタブする。検証ロジック自体のテストは
    tests/test_dispatch_actor_verification.py に集約する。"""
    fake_forge.get_label_actor.reset_mock(side_effect=True)
    fake_forge.get_label_actor.return_value = "trusted-actor"
    fake_forge.get_actor_permission.reset_mock(side_effect=True)
    fake_forge.get_actor_permission.return_value = "write"
    yield


def _task(**overrides):
    defaults = dict(
        issue_number=1,
        subtask_id="task-a",
        footprint=(),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=("status:in-progress",),
        created_at="2026-01-01T00:00:00+00:00",
        depends_on=(),
    )
    defaults.update(overrides)
    return Task(**defaults)


def _ctx(**overrides):
    defaults = dict(
        run_state=RunState(active_worktrees={}),
        tasks_by_issue={},
        issue_number_by_subtask_id={},
        done_subtask_ids=set(),
        ci_passed_pr_subtask_ids=set(),
        changes_requested_subtask_ids=set(),
        subtask_branch_map={},
        prs=[],
        pr_by_branch={},
        config=DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        ),
    )
    defaults.update(overrides)
    return CycleContext(**defaults)


def _issue(number, labels=(), state="OPEN"):
    return IssueRecord(
        number=number,
        title=f"Issue {number}",
        body="",
        labels=labels,
        created_at="2026-01-01T00:00:00+00:00",
        state=state,
    )


def _full_issue(
    number,
    labels=("status:queued",),
    footprint=("src/foo.py",),
    symbols=("foo.Foo",),
    subtask_id="task-a",
    depends_on=(),
    created_at="2026-01-01T00:00:00+00:00",
    parent_number=181,
):
    """`_issue()`より詳細なFootprint YAMLブロックを持つIssueRecordを作る。

    `run_dispatch_cycle`をエンドツーエンドで駆動する系のテスト（旧
    `test_dispatcher.py`の`TestRunDispatchCycle*`群）が要求するフィールド
    （footprint/symbols/subtask_id/depends_on/parent_number）を持つため、
    より単純な`_issue()`とは別名にし、`tests/conftest.py`の`make_issue`に
    委譲する薄いラッパーにしている。
    """
    parent = {"number": parent_number} if parent_number is not None else None
    return make_issue(
        number,
        title="t",
        labels=labels,
        footprint=footprint,
        symbols=symbols,
        subtask_id=subtask_id,
        depends_on=depends_on,
        created_at=created_at,
        parent=parent,
    )


def _sub_issue(number, labels=(), state="OPEN"):
    return IssueRecord(
        number=number,
        title=f"issue-{number}",
        body="",
        labels=labels,
        created_at="2026-01-01T00:00:00+00:00",
        state=state,
        parent={"number": 100},
    )


class TestConflictAwareSchedulingPhase:
    def test_excludes_candidate_conflicting_with_active_task(
        self, tmp_path, fake_forge
    ):
        active_task = _task(
            issue_number=1,
            subtask_id="active",
            footprint=("src/shared.py",),
        )
        conflicting = _task(
            issue_number=2,
            subtask_id="conflicting",
            footprint=("src/shared.py",),
            status_labels=("status:queued",),
        )
        independent = _task(
            issue_number=3,
            subtask_id="independent",
            footprint=("src/other.py",),
            status_labels=("status:queued",),
        )
        run_state = RunState(
            active_worktrees={
                "1": ActiveWorktree(
                    issue_number=1,
                    branch="task/active",
                    worktree_path=str(tmp_path / "active"),
                    pid=123,
                    started_at=1.0,
                    declared_footprint=active_task.footprint,
                )
            }
        )
        config = DispatcherConfig(
            apply=False,
            max_concurrent=3,
            max_launches_per_window=3,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "state.json",
            worktree_root=tmp_path / "worktrees",
        )
        ctx = _ctx(
            run_state=run_state,
            tasks_by_issue={
                1: active_task,
                2: conflicting,
                3: independent,
            },
            config=config,
        )
        issues = IssuesByStatus(
            queued=[
                _issue(2, labels=("status:queued",)),
                _issue(3, labels=("status:queued",)),
            ],
            locked=[],
            in_progress=[_issue(1, labels=("status:in-progress",))],
            blocked=[],
            done=[],
            not_needed=[],
        )

        scheduling = run_scheduling_phase(
            ctx,
            issues,
            ExternalLockScanResult(to_lock=[], to_unlock=[]),
            completed_subtask_ids=set(),
            any_forced_serial=False,
            deviation_events=[],
            now=2.0,
            config=config,
        )

        assert scheduling.quota_slots_available == 2
        assert [task.subtask_id for task in scheduling.selected] == ["independent"]

    def test_selects_deterministic_independent_set(self, tmp_path, fake_forge):
        tasks = [
            _task(
                issue_number=2,
                subtask_id="first",
                footprint=("src/shared.py",),
                status_labels=("status:queued",),
            ),
            _task(
                issue_number=3,
                subtask_id="second",
                footprint=("src/shared.py",),
                status_labels=("status:queued",),
            ),
            _task(
                issue_number=4,
                subtask_id="independent",
                footprint=("src/other.py",),
                status_labels=("status:queued",),
            ),
        ]
        config = DispatcherConfig(
            apply=False,
            max_concurrent=3,
            max_launches_per_window=3,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "state.json",
            worktree_root=tmp_path / "worktrees",
        )
        ctx = _ctx(
            tasks_by_issue={task.issue_number: task for task in tasks},
            config=config,
        )
        issues = IssuesByStatus(
            queued=[
                _issue(task.issue_number, labels=("status:queued",)) for task in tasks
            ],
            locked=[],
            in_progress=[],
            blocked=[],
            done=[],
            not_needed=[],
        )

        scheduling = run_scheduling_phase(
            ctx,
            issues,
            ExternalLockScanResult(to_lock=[], to_unlock=[]),
            completed_subtask_ids=set(),
            any_forced_serial=False,
            deviation_events=[],
            now=2.0,
            config=config,
        )

        assert [task.subtask_id for task in scheduling.selected] == [
            "first",
            "independent",
        ]

    def test_reports_a_decision_for_every_candidate(self, tmp_path, fake_forge):
        """#660: 起動されなかった候補も含め、選定理由がフェーズ結果に残ること。"""
        tasks = [
            _task(
                issue_number=2,
                subtask_id="first",
                footprint=("src/shared.py",),
                status_labels=("status:queued",),
            ),
            _task(
                issue_number=3,
                subtask_id="second",
                footprint=("src/shared.py",),
                status_labels=("status:queued",),
            ),
            _task(
                issue_number=4,
                subtask_id="independent",
                footprint=("src/other.py",),
                status_labels=("status:queued",),
            ),
            _task(
                issue_number=5,
                subtask_id="surplus",
                footprint=("src/fourth.py",),
                status_labels=("status:queued",),
            ),
        ]
        config = DispatcherConfig(
            apply=False,
            max_concurrent=2,
            max_launches_per_window=4,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "state.json",
            worktree_root=tmp_path / "worktrees",
        )
        ctx = _ctx(
            tasks_by_issue={task.issue_number: task for task in tasks},
            config=config,
        )
        issues = IssuesByStatus(
            queued=[
                _issue(task.issue_number, labels=("status:queued",)) for task in tasks
            ],
            locked=[],
            in_progress=[],
            blocked=[],
            done=[],
            not_needed=[],
        )

        scheduling = run_scheduling_phase(
            ctx,
            issues,
            ExternalLockScanResult(to_lock=[], to_unlock=[]),
            completed_subtask_ids=set(),
            any_forced_serial=False,
            deviation_events=[],
            now=2.0,
            config=config,
        )

        by_subtask = {d.subtask_id: d for d in scheduling.decisions}
        assert set(by_subtask) == {"first", "second", "independent", "surplus"}
        assert by_subtask["first"].reason == "selected"
        assert by_subtask["second"].reason == "conflict"
        assert by_subtask["independent"].reason == "selected"
        assert by_subtask["surplus"].reason == "quota-exhausted"
        assert all(d.mode == "critical-path" for d in scheduling.decisions)


class TestDetermineCandidateTasksExcludesDualStatus:
    """#254レビュー対応(#275 Codex P1): add(status:queued)成功後に
    remove(status:done)が失敗した中断状態のIssueを、dispatcherが誤って
    起動候補に含めないことを検証する。"""

    def test_excludes_early_death_retry_until_its_backoff_expires(self, fake_forge):
        task = _task(
            issue_number=1, subtask_id="task-a", status_labels=("status:queued",)
        )
        issues = IssuesByStatus(
            queued=[_issue(1, labels=("status:queued",))],
            locked=[],
            in_progress=[],
            blocked=[],
            done=[],
            not_needed=[],
        )
        ctx = _ctx(
            tasks_by_issue={1: task},
            run_state=RunState(
                task_reclaim_counts={
                    1: TaskReclaimRecord(
                        early_death_retry_count=1, early_death_retry_at=120.0
                    )
                }
            ),
        )
        lock_result = ExternalLockScanResult(to_lock=[], to_unlock=[])

        candidate_tasks, _ = _determine_candidate_tasks(
            ctx, issues, lock_result, set(), False, now=119.0
        )
        assert candidate_tasks == []

        candidate_tasks, _ = _determine_candidate_tasks(
            ctx, issues, lock_result, set(), False, now=120.0
        )
        assert candidate_tasks == [task]

    def test_excludes_queued_candidate_that_still_has_status_done(self, fake_forge):
        dual_status_task = _task(
            issue_number=1,
            subtask_id="task-a",
            status_labels=("status:done", "status:queued"),
        )
        normal_task = _task(
            issue_number=2,
            subtask_id="task-b",
            status_labels=("status:queued",),
        )
        issues = IssuesByStatus(
            queued=[
                _issue(1, labels=("status:done", "status:queued")),
                _issue(2, labels=("status:queued",)),
            ],
            locked=[],
            in_progress=[],
            blocked=[],
            done=[],
            not_needed=[],
        )
        ctx = _ctx(tasks_by_issue={1: dual_status_task, 2: normal_task})
        lock_result = ExternalLockScanResult(to_lock=[], to_unlock=[])

        fake_forge.get_label_actor.reset_mock(side_effect=True)
        fake_forge.get_label_actor.return_value = "some-user"
        fake_forge.get_actor_permission.reset_mock(side_effect=True)
        fake_forge.get_actor_permission.return_value = "write"
        candidate_tasks, _ = _determine_candidate_tasks(
            ctx,
            issues,
            lock_result,
            completed_subtask_ids=set(),
            any_forced_serial=False,
        )

        assert [t.issue_number for t in candidate_tasks] == [2]

    def test_excludes_queued_candidate_that_still_has_status_in_progress(
        self, fake_forge
    ):
        # #381レビュー対応(Codex P2): transition_status_labelはadd(status:
        # in-progress)をremove(status:queued)より先に行うため、removeが
        # 失敗/クラッシュするとIssueがstatus:queued/status:in-progressを
        # 同時に持つ中断状態のまま残りうる（launch成功時等）。これを通常の
        # 起動候補として扱うと、稼働中セッションが既に開いたPRを重複起動と
        # 誤認しstatus:blocked-human-reviewへ誤ってエスカレーションしうる。
        dual_status_task = _task(
            issue_number=1,
            subtask_id="task-a",
            status_labels=("status:in-progress", "status:queued"),
        )
        normal_task = _task(
            issue_number=2,
            subtask_id="task-b",
            status_labels=("status:queued",),
        )
        issues = IssuesByStatus(
            queued=[
                _issue(1, labels=("status:in-progress", "status:queued")),
                _issue(2, labels=("status:queued",)),
            ],
            locked=[],
            in_progress=[],
            blocked=[],
            done=[],
            not_needed=[],
        )
        ctx = _ctx(tasks_by_issue={1: dual_status_task, 2: normal_task})
        lock_result = ExternalLockScanResult(to_lock=[], to_unlock=[])

        fake_forge.get_label_actor.reset_mock(side_effect=True)
        fake_forge.get_label_actor.return_value = "some-user"
        fake_forge.get_actor_permission.reset_mock(side_effect=True)
        fake_forge.get_actor_permission.return_value = "write"
        candidate_tasks, _ = _determine_candidate_tasks(
            ctx,
            issues,
            lock_result,
            completed_subtask_ids=set(),
            any_forced_serial=False,
        )

        assert [t.issue_number for t in candidate_tasks] == [2]


class TestGroupByStatus:
    """#156: list_sub_issuesが返す親Issue配下の全Issueを、list_issues_by_label
    のstate引数（open/all）と同じ意味論でステータスラベル別に分類する。"""

    def test_groups_each_open_status_label(self):
        issues = [
            _sub_issue(1, labels=("status:queued",)),
            _sub_issue(2, labels=("status:external-lock",)),
            _sub_issue(3, labels=("status:in-progress",)),
            _sub_issue(4, labels=("status:blocked",)),
        ]
        result = _group_by_status(issues)
        assert [i.number for i in result.queued] == [1]
        assert [i.number for i in result.locked] == [2]
        assert [i.number for i in result.in_progress] == [3]
        assert [i.number for i in result.blocked] == [4]
        assert result.done == []
        assert result.not_needed == []

    def test_closed_open_only_labels_are_excluded(self):
        """status:queued/external-lock/in-progress/blockedはclosedなIssueを含めない
        （list_issues_by_labelの既定state="open"と同じ意味論）。"""
        issues = [_sub_issue(1, labels=("status:queued",), state="CLOSED")]
        result = _group_by_status(issues)
        assert result.queued == []

    def test_done_and_not_needed_include_closed(self):
        """status:done/not-neededはclosedでも含める
        （list_issues_by_labelのstate="all"呼び出しと同じ意味論）。"""
        issues = [
            _sub_issue(1, labels=("status:done",), state="CLOSED"),
            _sub_issue(2, labels=("status:not-needed",), state="CLOSED"),
        ]
        result = _group_by_status(issues)
        assert [i.number for i in result.done] == [1]
        assert [i.number for i in result.not_needed] == [2]

    def test_issue_with_multiple_status_labels_appears_in_each_bucket(self):
        issues = [_sub_issue(1, labels=("status:done", "status:not-needed"))]
        result = _group_by_status(issues)
        assert [i.number for i in result.done] == [1]
        assert [i.number for i in result.not_needed] == [1]


class TestFetchIssues:
    """#156: parent_issue_number指定時はlist_sub_issues経由のfast pathを、
    未指定時は従来通りlist_issues_by_labelを使う。"""

    def test_uses_list_sub_issues_when_parent_issue_number_is_set(
        self, tmp_path, fake_forge
    ):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            parent_issue_number=100,
        )
        fake_forge.list_sub_issues.reset_mock(side_effect=True)
        fake_forge.list_sub_issues.return_value = [
            _sub_issue(1, labels=("status:queued",))
        ]
        mock_sub_issues = fake_forge.list_sub_issues
        fake_forge.find_issues_by_parent_metadata.reset_mock(side_effect=True)
        fake_forge.find_issues_by_parent_metadata.return_value = []
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        fake_forge.list_issues_by_label.side_effect = AssertionError(
            "Should not scan the whole repository"
        )
        result = _fetch_issues(config)

        mock_sub_issues.assert_called_once_with(100)
        assert [i.number for i in result.queued] == [1]

    def test_uses_list_issues_by_label_when_parent_issue_number_is_none(
        self, tmp_path, fake_forge
    ):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        fake_forge.list_issues_by_label.return_value = []
        mock_list = fake_forge.list_issues_by_label
        fake_forge.list_sub_issues.reset_mock(side_effect=True)
        fake_forge.list_sub_issues.side_effect = AssertionError(
            "Should not use the parent fast path"
        )
        _fetch_issues(config)

        assert mock_list.call_count == 6


class TestFetchIssuesWithFakeForge:
    """#292: `mock.patch`によるグローバルなクラスメソッド差し替えではなく、
    `DispatcherConfig(forge=...)`への注入だけでテストが書けることを示す。"""

    def test_uses_injected_fake_forge_instead_of_patching(self, tmp_path):
        fake_forge = MagicMock()
        fake_forge.list_sub_issues.return_value = [
            _sub_issue(1, labels=("status:queued",))
        ]
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            parent_issue_number=100,
            forge=fake_forge,
        )

        result = _fetch_issues(config)

        fake_forge.list_sub_issues.assert_called_once_with(100)
        fake_forge.list_issues_by_label.assert_not_called()
        assert [i.number for i in result.queued] == [1]


class TestFinalizeLaunch:
    """#225レビュー対応: 起動確定時の永続化がconfig.window_secondsと現在のopen PR
    一覧(ctx.prs)を後続の起動処理まで正しく伝播することを検証する。"""

    def test_forwards_window_seconds_and_open_prs_to_launch_selected_tasks(
        self, tmp_path
    ):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            window_seconds=172800,
            apply=True,
        )
        pr = PrRecord(
            number=1,
            head_ref="claude/issue-1-task-a",
            changed_files=(),
            closes_issue_numbers=(1,),
        )
        ctx = _ctx(run_state=RunState(active_worktrees={}), prs=[pr], config=config)

        with patch(
            "orchestune.dispatch.phase_scheduling._launch_selected_tasks",
            return_value=[],
        ) as mock_launch:
            _finalize_launch([], {}, [], ctx, 1000.0, config)

        launch_ctx = mock_launch.call_args.args[0]
        assert launch_ctx.open_prs == [pr]


class TestRunDispatchCycle:
    def test_dry_run_makes_no_write_calls(self, tmp_path, fake_forge):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=False,
        )
        queued_issue = _full_issue(1)
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
                "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
            ),
            patch("orchestune.dispatch.worktree.subprocess.run") as mock_subproc_run,
            patch("orchestune.dispatch.targets.subprocess.Popen") as mock_popen,
        ):
            mock_list.side_effect = lambda label, **_: (
                [queued_issue] if label == "status:queued" else []
            )
            report = run_dispatch_cycle(config)

        mock_add_label.assert_not_called()
        mock_remove_label.assert_not_called()
        mock_subproc_run.assert_not_called()
        mock_popen.assert_not_called()
        assert report.applied is False
        assert len(report.selected) == 1
        assert not (tmp_path / "run_state.json").exists()

    def test_apply_launches_selected_task_and_persists_state(
        self, tmp_path, fake_forge
    ):
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        queued_issue = _full_issue(1)
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        mock_list = fake_forge.list_issues_by_label
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        fake_forge.add_label.reset_mock(side_effect=True)
        mock_add_label = fake_forge.add_label
        fake_forge.remove_label.reset_mock(side_effect=True)
        with (
            patch("orchestune.dispatch.worktree._branch_exists", return_value=False),
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
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
            report = run_dispatch_cycle(config)

        assert report.applied is True
        assert len(report.selected) == 1
        mock_add_label.assert_any_call(1, "status:in-progress")
        assert (tmp_path / "run_state.json").exists()
        persisted = json.loads((tmp_path / "run_state.json").read_text())
        assert "1" in persisted["active_worktrees"]

    def test_apply_updates_last_reconciled_at(self, tmp_path, fake_forge):
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        before = time.time()
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        fake_forge.list_issues_by_label.return_value = []
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
            ),
        ):
            run_dispatch_cycle(config)
        after = time.time()

        loaded = load_run_state(config.run_state_path)
        assert loaded.last_reconciled_at is not None
        assert before <= loaded.last_reconciled_at <= after

    def test_dry_run_does_not_update_last_reconciled_at(self, tmp_path, fake_forge):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=False,
        )
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        fake_forge.list_issues_by_label.return_value = []
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
            ),
        ):
            run_dispatch_cycle(config)

        assert not config.run_state_path.exists()

    def test_quota_exhausted_selects_nothing(self, tmp_path, fake_forge):
        run_state_path = tmp_path / "run_state.json"
        save_run_state(
            RunState(
                active_worktrees={
                    "9": ActiveWorktree(9, "b", "w", 1, 1_699_999_000.0, ()),
                    "8": ActiveWorktree(8, "b2", "w2", 2, 1_699_999_000.0, ()),
                },
                launch_history=[],
            ),
            run_state_path,
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            max_concurrent=2,
            max_launches_per_window=5,
            window_seconds=3600,
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=False,
        )
        queued_issue = _full_issue(1)
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        mock_list = fake_forge.list_issues_by_label
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
            ),
            _patch_gc_process_alive(return_value=True),
        ):
            mock_list.side_effect = lambda label, **_: (
                [queued_issue] if label == "status:queued" else []
            )
            report = run_dispatch_cycle(config)

        assert report.selected == []
        assert report.quota_slots_available == 0

    def test_run_dispatch_cycle_filters_by_parent_issue_number(
        self, tmp_path, fake_forge
    ):
        """#156: parent_issue_number指定時は、github.list_sub_issuesによる
        fast pathへ正しく配線され、その結果がそのまま使われることを確認する。
        『親を問わず返された候補から正しい親だけに絞る』という判定自体は、
        list_sub_issuesの実装（github.py）側の責務のためここでは検証しない。
        """
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            parent_issue_number=100,
            apply=False,
        )
        sub_issue_1 = _full_issue(
            1,
            labels=("status:queued",),
            subtask_id="task-a",
            parent_number=100,
        )

        fake_forge.list_sub_issues.reset_mock(side_effect=True)
        mock_list = fake_forge.list_sub_issues
        fake_forge.find_issues_by_parent_metadata.reset_mock(side_effect=True)
        fake_forge.find_issues_by_parent_metadata.return_value = []
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        fake_forge.get_issue.reset_mock(side_effect=True)
        fake_forge.get_issue.return_value = _full_issue(100, labels=(), subtask_id=None)
        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
            ),
        ):
            mock_list.return_value = [sub_issue_1]
            report = run_dispatch_cycle(config)

        mock_list.assert_called_once_with(100)
        assert [t.issue_number for t in report.selected] == [1]


class TestRunDispatchCycleParentIssueValidation:
    """#327: `--parent-issue`が本物のEPIC Issueかどうかを`ensure_parent_branch`
    呼び出し前に検証し、そうでなければ`RuntimeError`で拒否することを確認する。"""

    def _config(self, tmp_path, **overrides):
        defaults = dict(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            parent_issue_number=181,
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        defaults.update(overrides)
        return DispatcherConfig(**defaults)

    def _issue(self, *, title: str, body: str) -> IssueRecord:
        return IssueRecord(number=181, title=title, body=body, labels=(), created_at="")

    def test_raises_when_parent_issue_does_not_look_like_an_epic(
        self, tmp_path, fake_forge
    ):
        config = self._config(tmp_path)
        non_epic_issue = self._issue(title="[BUG] some bug", body="no marker here")
        fake_forge.get_issue.reset_mock(side_effect=True)
        fake_forge.get_issue.return_value = non_epic_issue
        with (
            patch(
                "orchestune.dispatch.phase_rebase.ensure_parent_branch"
            ) as mock_ensure,
        ):
            with pytest.raises(RuntimeError, match="181"):
                run_dispatch_cycle(config)

        mock_ensure.assert_not_called()

    def test_raises_when_parent_issue_does_not_exist(self, tmp_path, fake_forge):
        config = self._config(tmp_path)
        fake_forge.get_issue.reset_mock(side_effect=True)
        fake_forge.get_issue.return_value = None
        with (
            patch(
                "orchestune.dispatch.phase_rebase.ensure_parent_branch"
            ) as mock_ensure,
        ):
            with pytest.raises(RuntimeError, match="181"):
                run_dispatch_cycle(config)

        mock_ensure.assert_not_called()

    def test_raises_when_title_prefixed_but_marker_missing(self, tmp_path, fake_forge):
        config = self._config(tmp_path)
        issue = self._issue(title="[EPIC] Some plan", body="no marker here")
        fake_forge.get_issue.reset_mock(side_effect=True)
        fake_forge.get_issue.return_value = issue
        with (
            patch(
                "orchestune.dispatch.phase_rebase.ensure_parent_branch"
            ) as mock_ensure,
        ):
            with pytest.raises(RuntimeError):
                run_dispatch_cycle(config)

        mock_ensure.assert_not_called()

    def test_raises_when_marker_present_but_title_missing_prefix(
        self, tmp_path, fake_forge
    ):
        config = self._config(tmp_path)
        issue = self._issue(title="[BUG] some bug", body=f"...\n{PARENT_MARKER}")
        fake_forge.get_issue.reset_mock(side_effect=True)
        fake_forge.get_issue.return_value = issue
        with (
            patch(
                "orchestune.dispatch.phase_rebase.ensure_parent_branch"
            ) as mock_ensure,
        ):
            with pytest.raises(RuntimeError):
                run_dispatch_cycle(config)

        mock_ensure.assert_not_called()

    def test_calls_ensure_parent_branch_for_a_genuine_epic(self, tmp_path, fake_forge):
        config = self._config(tmp_path)
        epic_issue = self._issue(title="[EPIC] Some plan", body=f"...\n{PARENT_MARKER}")
        fake_forge.get_issue.reset_mock(side_effect=True)
        fake_forge.get_issue.return_value = epic_issue
        fake_forge.list_sub_issues.reset_mock(side_effect=True)
        fake_forge.list_sub_issues.return_value = []
        fake_forge.find_issues_by_parent_metadata.reset_mock(side_effect=True)
        fake_forge.find_issues_by_parent_metadata.return_value = []
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        fake_forge.list_issues_by_label.return_value = []
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        with (
            patch(
                "orchestune.dispatch.phase_rebase.ensure_parent_branch"
            ) as mock_ensure,
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
            ),
        ):
            run_dispatch_cycle(config)

        mock_ensure.assert_called_once_with(181)

    def test_does_not_check_when_apply_is_false(self, tmp_path, fake_forge):
        config = self._config(tmp_path, apply=False)
        parent = _full_issue(181, labels=(), subtask_id=None)
        fake_forge.get_issue.reset_mock(side_effect=True)
        fake_forge.get_issue.return_value = parent
        fake_forge.list_sub_issues.reset_mock(side_effect=True)
        fake_forge.list_sub_issues.return_value = []
        fake_forge.find_issues_by_parent_metadata.reset_mock(side_effect=True)
        fake_forge.find_issues_by_parent_metadata.return_value = []
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        with (
            patch(
                "orchestune.dispatch.phase_rebase.ensure_parent_branch"
            ) as mock_ensure,
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
            ),
        ):
            run_dispatch_cycle(config)

        # #519レビュー7巡目(P2)以降、dry-runでも親Issue本文の`launch_history`は
        # 読み取る（previewの正確さのため）ので`get_issue`自体は呼ばれうる。
        # このテストの対象は親ブランチの検証・作成が走らないことなので、
        # `ensure_parent_branch`が呼ばれないことで判定する。
        mock_ensure.assert_not_called()


class TestRunDispatchCycleActorVerification:
    """#119: status:queuedラベルを付与したactorの権限が不足している場合、
    run_dispatch_cycle経由でも起動をスキップしエスカレーションすることを確認する。"""

    def test_unauthorized_actor_skips_launch_and_escalates(self, tmp_path, fake_forge):
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        queued_issue = _full_issue(1, subtask_id="task-1")
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        mock_list = fake_forge.list_issues_by_label
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        fake_forge.add_label.reset_mock(side_effect=True)
        mock_add_label = fake_forge.add_label
        fake_forge.remove_label.reset_mock(side_effect=True)
        mock_remove_label = fake_forge.remove_label
        fake_forge.add_comment.reset_mock(side_effect=True)
        mock_add_comment = fake_forge.add_comment
        fake_forge.get_label_actor.reset_mock(side_effect=True)
        fake_forge.get_label_actor.return_value = "mallory"
        fake_forge.get_actor_permission.reset_mock(side_effect=True)
        fake_forge.get_actor_permission.return_value = "read"
        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
            ),
            patch("orchestune.dispatch.worktree.subprocess.run"),
            patch("orchestune.dispatch.targets.subprocess.Popen") as mock_popen,
        ):
            mock_list.side_effect = lambda label, **_: (
                [queued_issue] if label == "status:queued" else []
            )
            report = run_dispatch_cycle(config)

        assert report.selected == []
        mock_popen.assert_not_called()
        mock_remove_label.assert_any_call(1, "status:queued")
        mock_add_label.assert_any_call(1, "status:blocked-human-review")
        mock_add_comment.assert_called_once()
        assert "mallory" in mock_add_comment.call_args[0][1]

    def test_dry_run_excludes_unauthorized_actor_without_writes(
        self, tmp_path, fake_forge
    ):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=False,
        )
        queued_issue = _full_issue(1, subtask_id="task-1")
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        mock_list = fake_forge.list_issues_by_label
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        fake_forge.add_label.reset_mock(side_effect=True)
        mock_add_label = fake_forge.add_label
        fake_forge.remove_label.reset_mock(side_effect=True)
        mock_remove_label = fake_forge.remove_label
        fake_forge.get_label_actor.reset_mock(side_effect=True)
        fake_forge.get_label_actor.return_value = "mallory"
        fake_forge.get_actor_permission.reset_mock(side_effect=True)
        fake_forge.get_actor_permission.return_value = "none"
        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
            ),
        ):
            mock_list.side_effect = lambda label, **_: (
                [queued_issue] if label == "status:queued" else []
            )
            report = run_dispatch_cycle(config)

        assert report.selected == []
        mock_add_label.assert_not_called()
        mock_remove_label.assert_not_called()

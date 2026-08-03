import json
import subprocess
import tempfile
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest

from orchestune.dag import FootprintConflict
from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_cycle import (
    CycleContext,
    IssuesByStatus,
    _apply_external_lock_sync,
    _decide_external_lock_sync,
    _determine_candidate_tasks,
    _fetch_issues,
    _finalize_launch,
    _group_by_status,
    _process_active_worktrees,
    run_dispatch_cycle,
)
from orchestune.dispatch_filters import _filter_deviation_blocked_candidates
from orchestune.dispatch_locks import ExternalLockScanResult
from orchestune.dispatch_reconciliation import (
    _decide_blocked_promotions,
    _decide_dual_status_reconciliation,
    _reconcile_dual_status_tasks,
    _self_heal_run_state,
)
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import (
    ActiveWorktree,
    RunState,
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
            "orchestune.dispatch_gc.is_process_alive",
            "orchestune.dispatch_gc_completion.is_process_alive",
            "orchestune.dispatch_gc_zombies.is_process_alive",
        ):
            stack.enter_context(patch(target, return_value=return_value))
        yield


tmp_path = Path(tempfile.mkdtemp(prefix="orchestune-test-state-"))


@pytest.fixture(autouse=True)
def _stub_label_actor_permission_by_default():
    """#119で追加したactor権限検証ステップが、既存の大半のテストで実際の
    `gh api`呼び出しを行わないよう、デフォルトで許可された actor/permission を
    返すようスタブする。検証ロジック自体のテストは
    tests/test_dispatch_actor_verification.py に集約する。"""
    with (
        patch(
            "orchestune.forge.GitHubForge.get_label_actor",
            return_value="trusted-actor",
        ),
        patch(
            "orchestune.forge.GitHubForge.get_actor_permission",
            return_value="write",
        ),
    ):
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


def _active(**overrides):
    defaults = dict(
        issue_number=1,
        branch="claude/issue-1-task-a",
        worktree_path="worktrees/w1",
        pid=111,
        started_at=1_699_999_000.0,
        declared_footprint=(),
    )
    defaults.update(overrides)
    return ActiveWorktree(**defaults)


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


class TestDetermineCandidateTasksExcludesDualStatus:
    """#254レビュー対応(#275 Codex P1): add(status:queued)成功後に
    remove(status:done)が失敗した中断状態のIssueを、dispatcherが誤って
    起動候補に含めないことを検証する。"""

    def test_excludes_queued_candidate_that_still_has_status_done(self):
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

        with (
            patch(
                "orchestune.forge.GitHubForge.get_label_actor",
                return_value="some-user",
            ),
            patch(
                "orchestune.forge.GitHubForge.get_actor_permission",
                return_value="write",
            ),
        ):
            candidate_tasks, _ = _determine_candidate_tasks(
                ctx,
                issues,
                lock_result,
                completed_subtask_ids=set(),
                any_forced_serial=False,
            )

        assert [t.issue_number for t in candidate_tasks] == [2]


class TestDualStatusReconciliation:
    def test_detects_tasks_with_both_done_and_queued(self):
        dual_status_task = _task(
            issue_number=1,
            subtask_id="task-a",
            status_labels=("status:done", "status:queued"),
        )
        queued_only_task = _task(
            issue_number=2,
            subtask_id="task-b",
            status_labels=("status:queued",),
        )
        done_only_task = _task(
            issue_number=3,
            subtask_id="task-c",
            status_labels=("status:done",),
        )

        result = _decide_dual_status_reconciliation(
            {1: dual_status_task, 2: queued_only_task, 3: done_only_task}
        )

        assert [t.issue_number for t in result] == [1]

    def test_apply_removes_status_done_for_dual_status_tasks(self):
        dual_status_task = _task(
            issue_number=1,
            subtask_id="task-a",
            status_labels=("status:done", "status:queued"),
        )
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
        )

        with patch("orchestune.forge.GitHubForge.remove_label") as mock_remove:
            events = _reconcile_dual_status_tasks({1: dual_status_task}, config)

        mock_remove.assert_called_once_with(1, "status:done")
        assert events == [{"issue_number": 1, "subtask_id": "task-a"}]

    def test_dry_run_does_not_call_github(self):
        dual_status_task = _task(
            issue_number=1,
            subtask_id="task-a",
            status_labels=("status:done", "status:queued"),
        )
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=False,
        )

        with patch("orchestune.forge.GitHubForge.remove_label") as mock_remove:
            _reconcile_dual_status_tasks({1: dual_status_task}, config)

        mock_remove.assert_not_called()


class TestFilterDeviationBlockedCandidates:
    def test_excludes_candidates_blocked_by_recomputed_deviation(self):
        blocked = _task(issue_number=2, subtask_id="task-blocked")
        retained = _task(issue_number=3, subtask_id="task-retained")
        events = [
            {
                "action": "recomputed",
                "conflicts": [
                    {"blocked_subtask_id": "task-blocked"},
                    {"blocked_subtask_id": "unknown-task"},
                ],
            }
        ]

        result = _filter_deviation_blocked_candidates(
            [blocked, retained], events, {"task-blocked": 2}
        )

        assert result == [retained]

    def test_ignores_non_recomputed_and_missing_blocked_ids(self):
        candidates = [_task(issue_number=2, subtask_id="task-blocked")]
        events = [
            {
                "action": "blocked-human-review",
                "conflicts": [{"blocked_subtask_id": "task-blocked"}],
            },
            {"action": "recomputed", "conflicts": [{}]},
            {"action": "recomputed"},
        ]

        result = _filter_deviation_blocked_candidates(
            candidates, events, {"task-blocked": 2}
        )

        assert result is candidates

    def test_returns_original_candidates_when_there_are_no_events(self):
        candidates = [_task()]

        result = _filter_deviation_blocked_candidates(candidates, [], {})

        assert result is candidates


class TestDecideBlockedPromotions:
    """decide層: 依存解決済みタスクの判定のみを行い、githubラベルは変更しない。"""

    def test_no_depends_on_is_not_promotable(self):
        task = _task(depends_on=())
        promotable = _decide_blocked_promotions([], [], set(), {1: task})
        assert promotable == []

    def test_unresolved_dependency_is_not_promotable(self):
        task = _task(depends_on=("task-x",))
        issue = IssueRecord(
            number=1, title="t", body="", labels=(), created_at="2026-01-01T00:00:00Z"
        )
        promotable = _decide_blocked_promotions([issue], [], set(), {1: task})
        assert promotable == []

    def test_resolved_via_completed_subtask_ids_is_promotable(self):
        task = _task(depends_on=("task-x",))
        issue = IssueRecord(
            number=1, title="t", body="", labels=(), created_at="2026-01-01T00:00:00Z"
        )
        promotable = _decide_blocked_promotions([issue], [], {"task-x"}, {1: task})
        assert promotable == [task]


class TestDecideExternalLockSync:
    """decide層: githubからの読み取りとscan_external_locksの純粋計算のみを行い、
    ラベルの書き込みは行わない。"""

    def test_no_bare_branches_means_no_locks(self):
        run_state = RunState(active_worktrees={})
        with (
            patch(
                "orchestune.dispatch_cycle.list_remote_branches",
                return_value=[],
            ),
        ):
            result = _decide_external_lock_sync({}, [], run_state)
        assert result.to_lock == []
        assert result.to_unlock == []

    def test_ignores_invalid_remote_branch_name(self):
        run_state = RunState(active_worktrees={})
        task = _task(issue_number=1, footprint=("src/foo.py",))
        with (
            patch(
                "orchestune.dispatch_cycle.list_remote_branches",
                return_value=["origin/feature/foo@bar"],
            ),
        ):
            result = _decide_external_lock_sync({1: task}, [], run_state)
        assert result.to_lock == []
        assert result.to_unlock == []

    def test_diff_failure_keeps_existing_lock_and_locks_queued_tasks(self):
        """#245: 差分取得不能（branch_changed_files=None）はfail closed。
        既存のexternal-lockは解除されず、footprintを持つqueued taskはlockされる。"""
        run_state = RunState(active_worktrees={})
        locked_task = _task(
            issue_number=1,
            footprint=("src/foo.py",),
            status_labels=("status:external-lock",),
        )
        queued_task = _task(issue_number=2, footprint=("src/bar.py",))
        with (
            patch(
                "orchestune.dispatch_cycle.list_remote_branches",
                return_value=["origin/feat/x"],
            ),
            patch(
                "orchestune.dispatch_cycle.branch_changed_files",
                return_value=None,
            ),
        ):
            result = _decide_external_lock_sync(
                {1: locked_task, 2: queued_task}, [], run_state
            )
        assert result.to_unlock == []
        assert [t.issue_number for t in result.to_lock] == [2]


class TestApplyExternalLockSync:
    def test_unlocking_blocked_task_does_not_requeue_it(self):
        task = _task(status_labels=("status:blocked", "status:external-lock"))
        lock_result = ExternalLockScanResult(to_lock=[], to_unlock=[task])

        with (
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
        ):
            _apply_external_lock_sync(lock_result, DispatcherConfig(apply=True))

        mock_remove_label.assert_called_once_with(1, "status:external-lock")
        mock_add_label.assert_not_called()

    def test_unlocking_in_progress_task_does_not_requeue_it(self):
        task = _task(status_labels=("status:in-progress", "status:external-lock"))
        lock_result = ExternalLockScanResult(to_lock=[], to_unlock=[task])

        with (
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
        ):
            _apply_external_lock_sync(lock_result, DispatcherConfig(apply=True))

        mock_remove_label.assert_called_once_with(1, "status:external-lock")
        mock_add_label.assert_not_called()

    def test_unlocking_queued_task_requeues_it(self):
        task = _task(status_labels=("status:queued", "status:external-lock"))
        lock_result = ExternalLockScanResult(to_lock=[], to_unlock=[task])

        with (
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
        ):
            _apply_external_lock_sync(lock_result, DispatcherConfig(apply=True))

        mock_remove_label.assert_called_once_with(1, "status:external-lock")
        mock_add_label.assert_called_once_with(1, "status:queued")


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

    def test_uses_list_sub_issues_when_parent_issue_number_is_set(self):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            parent_issue_number=100,
        )
        with (
            patch(
                "orchestune.forge.GitHubForge.list_sub_issues",
                return_value=[_sub_issue(1, labels=("status:queued",))],
            ) as mock_sub_issues,
            patch(
                "orchestune.forge.GitHubForge.list_issues_by_label",
                side_effect=AssertionError("Should not scan the whole repository"),
            ),
        ):
            result = _fetch_issues(config)

        mock_sub_issues.assert_called_once_with(100)
        assert [i.number for i in result.queued] == [1]

    def test_uses_list_issues_by_label_when_parent_issue_number_is_none(self):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )
        with (
            patch(
                "orchestune.forge.GitHubForge.list_issues_by_label",
                return_value=[],
            ) as mock_list,
            patch(
                "orchestune.forge.GitHubForge.list_sub_issues",
                side_effect=AssertionError("Should not use the parent fast path"),
            ),
        ):
            _fetch_issues(config)

        assert mock_list.call_count == 6


class TestFetchIssuesWithFakeForge:
    """#292: `mock.patch`によるグローバルなクラスメソッド差し替えではなく、
    `DispatcherConfig(forge=...)`への注入だけでテストが書けることを示す。"""

    def test_uses_injected_fake_forge_instead_of_patching(self):
        fake_forge = MagicMock()
        fake_forge.list_sub_issues.return_value = [
            _sub_issue(1, labels=("status:queued",))
        ]
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            parent_issue_number=100,
            forge=fake_forge,
        )

        result = _fetch_issues(config)

        fake_forge.list_sub_issues.assert_called_once_with(100)
        fake_forge.list_issues_by_label.assert_not_called()
        assert [i.number for i in result.queued] == [1]


class TestSelfHealRunState:
    """#156: run_state.jsonは複数の親Issue（big rock）にまたがって共有されうる
    ため、parent_issue_number指定時のfast pathでスコープが絞られていても、
    自己修復は常にリポジトリ全体のstatus:in-progress Issueを読み直す。"""

    def test_noop_when_run_state_file_exists(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        run_state_path.write_text("{}")
        config = DispatcherConfig(
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            apply=True,
        )
        run_state = RunState(active_worktrees={})
        with patch(
            "orchestune.forge.GitHubForge.list_issues_by_label",
            side_effect=AssertionError("Should not fetch when file exists"),
        ):
            _self_heal_run_state(run_state, config)

    def test_noop_when_not_apply(self, tmp_path):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=False,
        )
        run_state = RunState(active_worktrees={})
        with patch(
            "orchestune.forge.GitHubForge.list_issues_by_label",
            side_effect=AssertionError("Should not fetch when apply=False"),
        ):
            _self_heal_run_state(run_state, config)

    def test_fetches_repo_wide_in_progress_issues_regardless_of_parent_scope(
        self, tmp_path
    ):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
            parent_issue_number=100,
        )
        run_state = RunState(active_worktrees={})
        with (
            patch(
                "orchestune.forge.GitHubForge.list_issues_by_label",
                return_value=[],
            ) as mock_list,
            patch(
                "orchestune.dispatch_reconciliation.recover_run_state",
                return_value=False,
            ) as mock_recover,
        ):
            _self_heal_run_state(run_state, config)

        mock_list.assert_called_once_with("status:in-progress")
        mock_recover.assert_called_once_with(run_state, [], config)


class TestFinalizeLaunch:
    """#225レビュー対応: 起動確定時の永続化がconfig.window_secondsと現在のopen PR
    一覧(ctx.prs)を後続の起動処理まで正しく伝播することを検証する。"""

    def test_forwards_window_seconds_and_open_prs_to_launch_selected_tasks(
        self, tmp_path
    ):
        from orchestune.models import PrRecord

        config = DispatcherConfig(
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
            "orchestune.dispatch_cycle._launch_selected_tasks", return_value=[]
        ) as mock_launch:
            _finalize_launch([], {}, [], ctx, 1000.0, config)

        assert mock_launch.call_args.kwargs.get("open_prs") == [pr]


class TestProcessActiveWorktrees:
    """_process_active_worktreesの結合テストケース。

    各ruleの中身(条件判定=decide/実処理=act)は対応するact側モジュール
    (dispatch_gc/dispatch_escalation/dispatch_rebase)に定義されているため、
    patch対象はそれらのモジュールを指す（#86のComposite化に伴う移設）。
    """

    def test_auto_rebase_not_needed_falls_through_to_footprint_deviation(self):
        active = _active(
            branch="feature",
            declared_footprint=("a.py",),
            worktree_path="worktrees/w1",
            recompute_count=0,
        )
        task = _task(
            issue_number=1,
            subtask_id="task-child",
            footprint=("a.py",),
            depends_on=("task-parent",),
        )
        run_state = RunState(active_worktrees={"1": active})
        ctx = _ctx(
            run_state=run_state,
            tasks_by_issue={1: task},
            ci_passed_pr_subtask_ids={"task-parent"},
            subtask_branch_map={"task-parent": "parent-branch"},
        )

        with (
            patch(
                "orchestune.dispatch_gc._is_worktree_complete",
                return_value=False,
            ),
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch_rebase._decide_rebase_needed",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation",
                return_value=["b.py"],
            ),
            patch(
                "orchestune.dispatch_rebase._handle_footprint_deviation",
                return_value={
                    "action": "recomputed",
                    "issue_number": 1,
                    "deviated_files": ["b.py"],
                },
            ),
        ):
            (
                completion_events,
                deviation_events,
                any_forced_serial,
                completed_subtask_ids,
            ) = _process_active_worktrees(ctx)

        assert completion_events == []
        assert len(deviation_events) == 1
        assert deviation_events[0]["action"] == "recomputed"
        assert deviation_events[0]["deviated_files"] == ["b.py"]
        assert any_forced_serial is False
        assert completed_subtask_ids == set()

    def test_dirty_worktree_skips_completion_and_does_not_fall_through(self):
        active = _active(
            branch="feature",
            declared_footprint=("a.py",),
            worktree_path="worktrees/w1",
        )
        task = _task(
            issue_number=1,
            subtask_id="task-child",
            footprint=("a.py",),
            depends_on=("task-parent",),
        )
        run_state = RunState(active_worktrees={"1": active})
        ctx = _ctx(
            run_state=run_state,
            tasks_by_issue={1: task},
            ci_passed_pr_subtask_ids={"task-parent"},
            subtask_branch_map={"task-parent": "parent-branch"},
        )

        with (
            patch(
                "orchestune.dispatch_gc._is_worktree_complete",
                return_value=True,
            ),
            # Completion now also consults the all-state PR list to rule out
            # an abandoned (closed-unmerged) PR before finalizing.
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[]),
            patch(
                "orchestune.dispatch_gc._finalize_completed_worktree",
                return_value={"action": "completion_skipped_dirty_worktree"},
            ),
            patch(
                "orchestune.dispatch_rebase._try_auto_rebase",
                side_effect=AssertionError("Should not call auto rebase"),
            ),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation",
                side_effect=AssertionError("Should not call check footprint deviation"),
            ),
        ):
            (
                completion_events,
                deviation_events,
                any_forced_serial,
                completed_subtask_ids,
            ) = _process_active_worktrees(ctx)

        assert len(completion_events) == 1
        assert completion_events[0]["action"] == "completion_skipped_dirty_worktree"
        assert deviation_events == []
        assert any_forced_serial is False
        assert completed_subtask_ids == set()

    def test_dirty_worktree_hold_survives_same_cycle_zombie_gc(self, tmp_path):
        """#212: 完了判定の保留を同一サイクルのゾンビGCが上書きしない。"""
        worktree_path = tmp_path / "w1"
        worktree_path.mkdir()
        active = _active(worktree_path=str(worktree_path), pid=None)
        task = _task()
        run_state = RunState(active_worktrees={"1": active})
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=False,
            zombie_gc=True,
        )
        ctx = _ctx(
            run_state=run_state,
            tasks_by_issue={1: task},
            config=config,
        )

        with (
            patch("orchestune.dispatch_cycle.load_run_state", return_value=run_state),
            patch(
                "orchestune.dispatch_cycle._fetch_issues",
                return_value=_group_by_status([]),
            ),
            patch("orchestune.dispatch_cycle._self_heal_run_state"),
            patch("orchestune.dispatch_cycle._build_cycle_context", return_value=ctx),
            patch("orchestune.dispatch_gc._is_worktree_complete", return_value=True),
            _patch_gc_process_alive(return_value=False),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=True,
            ),
            # Completion now also consults the all-state PR list to rule out
            # an abandoned (closed-unmerged) PR before finalizing.
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[]),
            patch("orchestune.dispatch_cycle._promote_blocked_tasks", return_value=[]),
            patch(
                "orchestune.dispatch_cycle._handle_blocked_recompute_recovery",
                return_value=[],
            ),
            patch(
                "orchestune.dispatch_cycle._sync_external_locks",
                return_value=ExternalLockScanResult(to_lock=[], to_unlock=[]),
            ),
            patch(
                "orchestune.dispatch_cycle._determine_candidate_tasks",
                return_value=([], {}),
            ),
            patch("orchestune.dispatch_cycle._finalize_launch", return_value=[]),
        ):
            report = run_dispatch_cycle(config)

        assert [event["action"] for event in report.completion_events] == [
            "completion_skipped_dirty_worktree"
        ]
        assert run_state.active_worktrees == {"1": active}

    def test_not_needed_label_takes_precedence_over_stale_entry(self):
        active = _active(issue_number=1)
        task = _task(
            issue_number=1,
            status_labels=("status:not-needed", "status:blocked"),
        )
        run_state = RunState(active_worktrees={"1": active})
        ctx = _ctx(
            run_state=run_state,
            tasks_by_issue={1: task},
            config=DispatcherConfig(
                run_state_path=Path("dummy.json"),
                worktree_root=Path("worktrees"),
                apply=True,
            ),
        )

        with (
            patch(
                "orchestune.dispatch_gc._finalize_not_needed_worktree",
                return_value={"action": "not_needed"},
            ),
            patch(
                "orchestune.dispatch_gc._decide_stale_active_entry",
                side_effect=AssertionError("Should not call decide stale active entry"),
            ),
        ):
            (
                completion_events,
                deviation_events,
                any_forced_serial,
                completed_subtask_ids,
            ) = _process_active_worktrees(ctx)

        assert len(completion_events) == 1
        assert completion_events[0]["action"] == "not_needed"
        assert deviation_events == []
        assert completed_subtask_ids == {task.subtask_id}
        assert "1" not in ctx.run_state.active_worktrees

    def test_auto_rebase_failure_discards_active_entry(self):
        active = _active(
            branch="feature",
            worktree_path="worktrees/w1",
            pid=123,
        )
        task = _task(
            issue_number=1,
            subtask_id="task-child",
            depends_on=("task-parent",),
        )
        run_state = RunState(active_worktrees={"1": active})
        ctx = _ctx(
            run_state=run_state,
            tasks_by_issue={1: task},
            ci_passed_pr_subtask_ids={"task-parent"},
            subtask_branch_map={"task-parent": "parent-branch"},
            config=DispatcherConfig(
                run_state_path=Path("dummy.json"),
                worktree_root=Path("worktrees"),
                apply=True,
            ),
        )

        def mock_subprocess_run(args, **kwargs):
            import subprocess

            if "rebase" in args:
                raise subprocess.CalledProcessError(1, args)
            # #213: rebase前のWIP退避チェック(`git status --porcelain`)がcleanと
            # 判定されるよう、空のstdoutを返す。
            return subprocess.CompletedProcess(args, 0, stdout="")

        with (
            patch(
                "orchestune.dispatch_gc._is_worktree_complete",
                return_value=False,
            ),
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch_rebase._decide_rebase_needed",
                return_value=True,
            ),
            patch(
                "orchestune.git_cli.subprocess.run",
                side_effect=mock_subprocess_run,
            ),
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove,
            patch("orchestune.forge.GitHubForge.add_label") as mock_add,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_comment,
        ):
            (
                completion_events,
                deviation_events,
                any_forced_serial,
                completed_subtask_ids,
            ) = _process_active_worktrees(ctx)

        assert completion_events == []
        assert deviation_events == []
        assert completed_subtask_ids == set()
        assert "1" not in ctx.run_state.active_worktrees
        mock_remove.assert_called_once_with(1, "status:in-progress")
        mock_add.assert_called_once_with(1, "status:manual-merge-required")
        mock_comment.assert_called_once_with(
            1,
            "自動リベース中にコンフリクトが発生しました。手動でマージを行ってください。\n対象の依存元ブランチ: parent-branch",
        )

    def test_forced_serial_persists_with_early_termination_rules(self):
        active = _active(
            issue_number=1,
            forced_serial=True,
        )
        task = _task(
            issue_number=1,
            status_labels=("status:not-needed",),
        )
        run_state = RunState(active_worktrees={"1": active})
        ctx = _ctx(
            run_state=run_state,
            tasks_by_issue={1: task},
            config=DispatcherConfig(
                run_state_path=Path("dummy.json"),
                worktree_root=Path("worktrees"),
                apply=True,
            ),
        )

        with (
            patch(
                "orchestune.dispatch_gc._finalize_not_needed_worktree",
                return_value={"action": "not_needed"},
            ),
        ):
            (
                completion_events,
                deviation_events,
                any_forced_serial,
                completed_subtask_ids,
            ) = _process_active_worktrees(ctx)

        assert len(completion_events) == 1
        assert completion_events[0]["action"] == "not_needed"
        assert deviation_events == []
        assert completed_subtask_ids == {task.subtask_id}
        assert any_forced_serial is False
        assert "1" not in ctx.run_state.active_worktrees

        # 追加検証：もう1つの worktree があって、そちらは早期終了せず forced_serial=True の場合
        active_early = _active(issue_number=1, forced_serial=True)
        active_keep = _active(issue_number=2, forced_serial=True)
        task_early = _task(issue_number=1, status_labels=("status:not-needed",))
        task_keep = _task(issue_number=2, status_labels=("status:in-progress",))

        run_state_two = RunState(active_worktrees={"1": active_early, "2": active_keep})
        ctx_two = _ctx(
            run_state=run_state_two,
            tasks_by_issue={1: task_early, 2: task_keep},
            config=DispatcherConfig(
                run_state_path=Path("dummy.json"),
                worktree_root=Path("worktrees"),
                apply=True,
            ),
        )

        with (
            patch(
                "orchestune.dispatch_gc._finalize_not_needed_worktree",
                return_value={"action": "not_needed"},
            ),
            patch(
                "orchestune.dispatch_gc._is_worktree_complete",
                return_value=False,
            ),
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch_rebase._decide_rebase_needed",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation",
                return_value=[],
            ),
        ):
            (
                completion_events,
                deviation_events,
                any_forced_serial,
                completed_subtask_ids,
            ) = _process_active_worktrees(ctx_two)

        assert any_forced_serial is True
        assert "1" not in ctx_two.run_state.active_worktrees


class TestDispatchCycleRecomputeExclusionAndRecovery:
    def test_same_cycle_recompute_exclusion(self, tmp_path):
        """同一サイクルで逸脱が発生した際、競合先タスクが candidate_tasks から除外されること"""
        from orchestune.dispatch_cycle import run_dispatch_cycle

        run_state_path = tmp_path / "run_state.json"
        run_state_path.write_text("{}")

        config = DispatcherConfig(
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            apply=True,
        )

        # 競合先タスクのTask定義
        blocked_task = _task(
            issue_number=2, subtask_id="task-blocked", status_labels=("status:queued",)
        )
        normal_task = _task(
            issue_number=3, subtask_id="task-normal", status_labels=("status:queued",)
        )

        blocked_body = "## Footprint\n" "```yaml\n" "subtask_id: task-blocked\n" "```\n"
        normal_body = "## Footprint\n" "```yaml\n" "subtask_id: task-normal\n" "```\n"
        blocked_issue = IssueRecord(
            2, "t", blocked_body, ("status:queued",), "2026-01-01T00:00:00Z"
        )
        normal_issue = IssueRecord(
            3, "t", normal_body, ("status:queued",), "2026-01-01T00:00:00Z"
        )

        class MockIssues:
            queued = [blocked_issue, normal_issue]
            locked = []
            in_progress = []
            blocked = []
            done = []
            not_needed = []

            def all(self):
                return [blocked_issue, normal_issue]

            def filtered_by_parent(self, parent):
                return self

        run_state = RunState(active_worktrees={})

        # _process_active_worktrees から返される deviation_events をシミュレート
        deviation_events = [
            {
                "action": "recomputed",
                "conflicts": [
                    {
                        "subtask_id": "task-active",
                        "other_subtask_id": "task-blocked",
                        "similarity": 0.8,
                        "blocked_subtask_id": "task-blocked",
                    }
                ],
            }
        ]

        with (
            patch("orchestune.dispatch_cycle.load_run_state", return_value=run_state),
            patch("orchestune.dispatch_cycle._fetch_issues", return_value=MockIssues()),
            patch(
                "orchestune.dispatch_cycle._process_active_worktrees",
                return_value=([], deviation_events, False, set()),
            ),
            patch("orchestune.dispatch_cycle._promote_blocked_tasks", return_value=[]),
            patch("orchestune.dispatch_cycle._sync_external_locks"),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch(
                "orchestune.forge.GitHubForge.get_label_actor",
                return_value="some-user",
            ),
            patch(
                "orchestune.forge.GitHubForge.get_actor_permission",
                return_value="write",
            ),
            patch("orchestune.dispatch_cycle.save_run_state"),
            patch(
                "orchestune.dispatch_cycle._determine_candidate_tasks",
                return_value=([blocked_task, normal_task], {}),
            ),
            patch("orchestune.dispatch_cycle.select_next_tasks") as mock_select,
        ):
            run_dispatch_cycle(config)

        called_candidates = mock_select.call_args[0][0]
        assert normal_task in called_candidates
        assert blocked_task not in called_candidates

    def test_recompute_resolved_automatic_recovery(self, tmp_path):
        """アクティブタスクが完了し競合が解消されたら、status:blocked-recomputeタスクが復帰すること"""
        from orchestune.dispatch_cycle import run_dispatch_cycle

        run_state_path = tmp_path / "run_state.json"
        run_state_path.write_text("{}")

        config = DispatcherConfig(
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            apply=True,
        )

        # アクティブワークツリーは空（競合なし）
        run_state = RunState(active_worktrees={})

        # status:blocked-recompute を持つ Issue
        body = (
            "## Footprint\n"
            "```yaml\n"
            "subtask_id: task-blocked\n"
            "footprint:\n"
            "  - src/bar.py\n"
            "```\n"
        )
        blocked_issue = IssueRecord(
            number=2,
            title="t",
            body=body,
            labels=("status:blocked", "status:blocked-recompute"),
            created_at="2026-01-01T00:00:00Z",
        )

        class MockIssues:
            queued = []
            locked = []
            in_progress = []
            blocked = [blocked_issue]
            done = []
            not_needed = []

            def all(self):
                return [blocked_issue]

            def filtered_by_parent(self, parent):
                return self

        with (
            patch("orchestune.dispatch_cycle.load_run_state", return_value=run_state),
            patch("orchestune.dispatch_cycle._fetch_issues", return_value=MockIssues()),
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch(
                "orchestune.forge.GitHubForge.get_label_actor",
                return_value="some-user",
            ),
            patch(
                "orchestune.forge.GitHubForge.get_actor_permission",
                return_value="write",
            ),
            patch("orchestune.dispatch_cycle.save_run_state"),
            patch(
                "orchestune.dispatch_cycle._determine_candidate_tasks",
                return_value=([], {}),
            ),
        ):
            run_dispatch_cycle(config)

        mock_remove_label.assert_any_call(2, "status:blocked-recompute")
        mock_remove_label.assert_any_call(2, "status:blocked")
        mock_add_label.assert_any_call(2, "status:queued")

    def test_recompute_recovery_error_fail_closed(self, tmp_path):
        """逸脱検知でエラー（None）が発生した際、自動復帰が抑止されること (fail-closed)"""
        from orchestune.dispatch_cycle import run_dispatch_cycle

        run_state_path = tmp_path / "run_state.json"
        run_state_path.write_text("{}")

        config = DispatcherConfig(
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            apply=True,
        )

        # アクティブワークツリーが存在する（逸脱検知を走らせるため）
        active = ActiveWorktree(
            issue_number=1,
            branch="branch-active",
            worktree_path="worktrees/w1",
            pid=None,
            started_at=1000.0,
            declared_footprint=("src/foo.py",),
            base_branch="origin/main",
        )
        run_state = RunState(active_worktrees={1: active})

        # status:blocked-recompute を持つ Issue
        body = (
            "## Footprint\n"
            "```yaml\n"
            "subtask_id: task-blocked\n"
            "footprint:\n"
            "  - src/bar.py\n"
            "```\n"
        )
        blocked_issue = IssueRecord(
            number=2,
            title="t",
            body=body,
            labels=("status:blocked", "status:blocked-recompute"),
            created_at="2026-01-01T00:00:00Z",
        )

        active_body = (
            "## Footprint\n"
            "```yaml\n"
            "subtask_id: task-active\n"
            "footprint:\n"
            "  - src/foo.py\n"
            "```\n"
        )
        active_issue = IssueRecord(
            number=1,
            title="active",
            body=active_body,
            labels=("status:in-progress",),
            created_at="2026-01-01T00:00:00Z",
        )

        class MockIssues:
            queued = []
            locked = []
            in_progress = [active_issue]
            blocked = [blocked_issue]
            done = []
            not_needed = []

            def all(self):
                return [active_issue, blocked_issue]

            def filtered_by_parent(self, parent):
                return self

        with (
            patch("orchestune.dispatch_cycle.load_run_state", return_value=run_state),
            patch("orchestune.dispatch_cycle._fetch_issues", return_value=MockIssues()),
            patch(
                "orchestune.dispatch_cycle._process_active_worktrees",
                return_value=([], [], False, set()),
            ),
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_label"),
            patch("orchestune.forge.GitHubForge.add_comment"),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch(
                "orchestune.forge.GitHubForge.get_label_actor",
                return_value="some-user",
            ),
            patch(
                "orchestune.forge.GitHubForge.get_actor_permission",
                return_value="write",
            ),
            patch("orchestune.dispatch_cycle.save_run_state"),
            patch(
                "orchestune.dispatch_cycle._determine_candidate_tasks",
                return_value=([], {}),
            ),
            patch(
                "orchestune.dispatch_locks.check_footprint_deviation", return_value=None
            ),  # エラー発生を模す
        ):
            run_dispatch_cycle(config)

        # remove_label が呼ばれない（fail-closed）ことを検証
        mock_remove_label.assert_not_called()

    def test_blocked_promotion_excludes_blocked_recompute(self):
        """status:blocked-recomputeを持つタスクは、通常のblocked昇格判定から除外されること"""
        from orchestune.dispatch_reconciliation import _decide_blocked_promotions

        task = _task(
            issue_number=2,
            subtask_id="task-blocked",
            status_labels=("status:blocked", "status:blocked-recompute"),
        )
        issue = IssueRecord(
            number=2,
            title="t",
            body="subtask_id: task-blocked\ndepends_on:\n  - task-dep",
            labels=("status:blocked", "status:blocked-recompute"),
            created_at="2026-01-01T00:00:00Z",
        )

        # 依存先が完了している（done_issuesに含まれる）が、status:blocked-recompute があるため昇格しないはず
        dep_issue = IssueRecord(
            3, "dep", "subtask_id: task-dep", ("status:done",), "2026-01-01T00:00:00Z"
        )
        dep_task = _task(
            issue_number=3, subtask_id="task-dep", status_labels=("status:done",)
        )

        promotable = _decide_blocked_promotions(
            [issue],
            [dep_issue],
            completed_subtask_ids=set(),
            tasks_by_issue={2: task, 3: dep_task},
        )

        assert task not in promotable

    def test_recompute_dag_error_fail_closed(self, tmp_path):
        """DAG再計算で例外が発生した際、自動復帰が抑止されること (fail-closed)"""
        from orchestune.dispatch_cycle import run_dispatch_cycle

        run_state_path = tmp_path / "run_state.json"
        run_state_path.write_text("{}")

        config = DispatcherConfig(
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            apply=True,
        )

        active = ActiveWorktree(
            issue_number=1,
            branch="branch-active",
            worktree_path="worktrees/w1",
            pid=None,
            started_at=1000.0,
            declared_footprint=("src/foo.py",),
            base_branch="origin/main",
        )
        run_state = RunState(active_worktrees={1: active})

        # status:blocked-recompute を持つ Issue
        body = (
            "## Footprint\n"
            "```yaml\n"
            "subtask_id: task-blocked\n"
            "footprint:\n"
            "  - src/bar.py\n"
            "```\n"
        )
        blocked_issue = IssueRecord(
            number=2,
            title="t",
            body=body,
            labels=("status:blocked", "status:blocked-recompute"),
            created_at="2026-01-01T00:00:00Z",
        )

        active_body = (
            "## Footprint\n"
            "```yaml\n"
            "subtask_id: task-active\n"
            "footprint:\n"
            "  - src/foo.py\n"
            "```\n"
        )
        active_issue = IssueRecord(
            number=1,
            title="active",
            body=active_body,
            labels=("status:in-progress",),
            created_at="2026-01-01T00:00:00Z",
        )

        class MockIssues:
            queued = []
            locked = []
            in_progress = [active_issue]
            blocked = [blocked_issue]
            done = []
            not_needed = []

            def all(self):
                return [active_issue, blocked_issue]

            def filtered_by_parent(self, parent):
                return self

        with (
            patch("orchestune.dispatch_cycle.load_run_state", return_value=run_state),
            patch("orchestune.dispatch_cycle._fetch_issues", return_value=MockIssues()),
            patch(
                "orchestune.dispatch_cycle._process_active_worktrees",
                return_value=([], [], False, set()),
            ),
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_label"),
            patch("orchestune.forge.GitHubForge.add_comment"),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch(
                "orchestune.forge.GitHubForge.get_label_actor",
                return_value="some-user",
            ),
            patch(
                "orchestune.forge.GitHubForge.get_actor_permission",
                return_value="write",
            ),
            patch("orchestune.dispatch_cycle.save_run_state"),
            patch(
                "orchestune.dispatch_cycle._determine_candidate_tasks",
                return_value=([], {}),
            ),
            patch(
                "orchestune.dispatch_locks.check_footprint_deviation",
                return_value=["src/unexpected.py"],
            ),  # 逸脱ありとする
            patch(
                "orchestune.dag.recompute_dag_for_footprint_change",
                side_effect=ValueError("DAG error"),
            ),  # DAG計算エラー
        ):
            run_dispatch_cycle(config)

        # remove_label が呼ばれない（fail-closed）ことを検証
        mock_remove_label.assert_not_called()


class TestRunDispatchCycle:
    def test_dry_run_makes_no_write_calls(self, tmp_path):
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=False,
        )
        queued_issue = _full_issue(1)
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_subproc_run,
            patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen,
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

    def test_apply_launches_selected_task_and_persists_state(self, tmp_path):
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
        with (
            patch("orchestune.dispatch_worktree._branch_exists", return_value=False),
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label"),
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_subproc_run,
            patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen,
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

    def test_apply_updates_last_reconciled_at(self, tmp_path):
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
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label", return_value=[]),
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
        ):
            run_dispatch_cycle(config)
        after = time.time()

        loaded = load_run_state(config.run_state_path)
        assert loaded.last_reconciled_at is not None
        assert before <= loaded.last_reconciled_at <= after

    def test_dry_run_does_not_update_last_reconciled_at(self, tmp_path):
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=False,
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label", return_value=[]),
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
        ):
            run_dispatch_cycle(config)

        assert not config.run_state_path.exists()

    def test_quota_exhausted_selects_nothing(self, tmp_path):
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
            max_concurrent=2,
            max_launches_per_window=5,
            window_seconds=3600,
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=False,
        )
        queued_issue = _full_issue(1)
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            _patch_gc_process_alive(return_value=True),
        ):
            mock_list.side_effect = lambda label, **_: (
                [queued_issue] if label == "status:queued" else []
            )
            report = run_dispatch_cycle(config)

        assert report.selected == []
        assert report.quota_slots_available == 0

    def test_run_dispatch_cycle_filters_by_parent_issue_number(self, tmp_path):
        """#156: parent_issue_number指定時は、github.list_sub_issuesによる
        fast pathへ正しく配線され、その結果がそのまま使われることを確認する。
        『親を問わず返された候補から正しい親だけに絞る』という判定自体は、
        list_sub_issuesの実装（github.py）側の責務のためここでは検証しない。
        """
        config = DispatcherConfig(
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

        with (
            patch("orchestune.forge.GitHubForge.list_sub_issues") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
        ):
            mock_list.return_value = [sub_issue_1]
            report = run_dispatch_cycle(config)

        mock_list.assert_called_once_with(100)
        assert [t.issue_number for t in report.selected] == [1]

    def test_run_dispatch_cycle_resolves_depends_on_from_blocked_by(self, tmp_path):
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
        )
        done_issue = _full_issue(1, labels=("status:done",), subtask_id="task-a")
        blocked_issue = _full_issue(
            2,
            labels=("status:blocked",),
            subtask_id="task-b",
            depends_on=(),
        )
        blocked_issue = IssueRecord(
            number=blocked_issue.number,
            title=blocked_issue.title,
            body=blocked_issue.body,
            labels=blocked_issue.labels,
            created_at=blocked_issue.created_at,
            blocked_by=(1,),
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
        ):

            def _list(label, **_):
                if label == "status:done":
                    return [done_issue]
                if label == "status:blocked":
                    return [blocked_issue]
                return []

            mock_list.side_effect = _list
            report = run_dispatch_cycle(config)

        # BがAの完了により昇格したことを確認
        mock_remove_label.assert_any_call(2, "status:blocked")
        mock_add_label.assert_any_call(2, "status:queued")
        assert report.promotion_events == [{"issue_number": 2, "subtask_id": "task-b"}]


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
            apply=True,
        )
        defaults.update(overrides)
        return DispatcherConfig(**defaults)

    def _issue(self, *, title: str, body: str) -> IssueRecord:
        return IssueRecord(number=181, title=title, body=body, labels=(), created_at="")

    def test_raises_when_parent_issue_does_not_look_like_an_epic(self, tmp_path):
        config = self._config(tmp_path)
        non_epic_issue = self._issue(title="[BUG] some bug", body="no marker here")
        with (
            patch(
                "orchestune.forge.GitHubForge.get_issue", return_value=non_epic_issue
            ),
            patch("orchestune.dispatch_cycle.ensure_parent_branch") as mock_ensure,
        ):
            with pytest.raises(RuntimeError, match="181"):
                run_dispatch_cycle(config)

        mock_ensure.assert_not_called()

    def test_raises_when_parent_issue_does_not_exist(self, tmp_path):
        config = self._config(tmp_path)
        with (
            patch("orchestune.forge.GitHubForge.get_issue", return_value=None),
            patch("orchestune.dispatch_cycle.ensure_parent_branch") as mock_ensure,
        ):
            with pytest.raises(RuntimeError, match="181"):
                run_dispatch_cycle(config)

        mock_ensure.assert_not_called()

    def test_raises_when_title_prefixed_but_marker_missing(self, tmp_path):
        config = self._config(tmp_path)
        issue = self._issue(title="[EPIC] Some plan", body="no marker here")
        with (
            patch("orchestune.forge.GitHubForge.get_issue", return_value=issue),
            patch("orchestune.dispatch_cycle.ensure_parent_branch") as mock_ensure,
        ):
            with pytest.raises(RuntimeError):
                run_dispatch_cycle(config)

        mock_ensure.assert_not_called()

    def test_raises_when_marker_present_but_title_missing_prefix(self, tmp_path):
        config = self._config(tmp_path)
        issue = self._issue(title="[BUG] some bug", body=f"...\n{PARENT_MARKER}")
        with (
            patch("orchestune.forge.GitHubForge.get_issue", return_value=issue),
            patch("orchestune.dispatch_cycle.ensure_parent_branch") as mock_ensure,
        ):
            with pytest.raises(RuntimeError):
                run_dispatch_cycle(config)

        mock_ensure.assert_not_called()

    def test_calls_ensure_parent_branch_for_a_genuine_epic(self, tmp_path):
        config = self._config(tmp_path)
        epic_issue = self._issue(title="[EPIC] Some plan", body=f"...\n{PARENT_MARKER}")
        with (
            patch("orchestune.forge.GitHubForge.get_issue", return_value=epic_issue),
            patch("orchestune.dispatch_cycle.ensure_parent_branch") as mock_ensure,
            patch("orchestune.forge.GitHubForge.list_sub_issues", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_issues_by_label", return_value=[]),
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
        ):
            run_dispatch_cycle(config)

        mock_ensure.assert_called_once_with(181)

    def test_does_not_check_when_apply_is_false(self, tmp_path):
        config = self._config(tmp_path, apply=False)
        with (
            patch("orchestune.forge.GitHubForge.get_issue") as mock_get_issue,
            patch("orchestune.dispatch_cycle.ensure_parent_branch") as mock_ensure,
            patch("orchestune.forge.GitHubForge.list_sub_issues", return_value=[]),
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
        ):
            run_dispatch_cycle(config)

        mock_get_issue.assert_not_called()
        mock_ensure.assert_not_called()


class TestRunDispatchCycleBranchNormalization:
    """#194: リモートブランチ名のorigin/プレフィックス正規化。"""

    def test_does_not_self_lock_own_active_branch(self, tmp_path):
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
                        declared_footprint=("src/shared.py",),
                    )
                },
                launch_history=[],
            ),
            run_state_path,
        )
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=False,
        )
        queued_issue = _full_issue(2, footprint=("src/shared.py",))
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch(
                "orchestune.dispatch_cycle.list_remote_branches",
                return_value=["origin/claude/issue-1-task-a"],
            ),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch(
                "orchestune.dispatch_cycle.branch_changed_files",
                return_value=["src/shared.py"],
            ),
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation", return_value=[]
            ),
        ):
            mock_list.side_effect = lambda label, **_: (
                [queued_issue] if label == "status:queued" else []
            )
            report = run_dispatch_cycle(config)

        assert report.lock_changes["to_lock"] == []

    def test_excludes_branch_with_open_pr_multisegment_headref(self, tmp_path):
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=False,
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label", return_value=[]),
            patch(
                "orchestune.dispatch_cycle.list_remote_branches",
                return_value=["origin/feature/foo"],
            ),
            patch(
                "orchestune.forge.GitHubForge.list_open_prs",
                return_value=[
                    PrRecord(
                        number=1, head_ref="feature/foo", changed_files=("src/x.py",)
                    )
                ],
            ),
            patch(
                "orchestune.dispatch_cycle.branch_changed_files"
            ) as mock_branch_files,
        ):
            run_dispatch_cycle(config)

        mock_branch_files.assert_not_called()

    def test_unrelated_external_branch_still_locks_overlapping_task(self, tmp_path):
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=False,
        )
        queued_issue = _full_issue(1, footprint=("src/shared.py",))
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch(
                "orchestune.dispatch_cycle.list_remote_branches",
                return_value=["origin/someone-elses-branch"],
            ),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch(
                "orchestune.dispatch_cycle.branch_changed_files",
                return_value=["src/shared.py"],
            ),
        ):
            mock_list.side_effect = lambda label, **_: (
                [queued_issue] if label == "status:queued" else []
            )
            report = run_dispatch_cycle(config)

        assert [t.issue_number for t in report.lock_changes["to_lock"]] == [1]


class TestRunDispatchCycleFootprintRecompute:
    """#192: footprint逸脱検知 → DAG再計算 → notify_recompute の配線。"""

    def _config(self, tmp_path, run_state_path, **overrides):
        defaults = dict(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
            parent_issue_number=181,
        )
        defaults.update(overrides)
        return DispatcherConfig(**defaults)

    def _epic_issue(self) -> IssueRecord:
        """#327: `parent_issue_number=181`は本物のEPICではない実在のバグIssue
        番号（このリポジトリの過去のIncidentに由来）であり、`ensure_parent_branch`
        呼び出し前の`is_epic_issue`検証が本物のGitHub `gh issue view 181`を
        呼ばないよう、EPIC形の`IssueRecord`を明示的にスタブする。"""
        return IssueRecord(
            number=181,
            title="[EPIC] Test plan",
            body=f"...\n{PARENT_MARKER}",
            labels=(),
            created_at="",
        )

    def test_significant_deviation_triggers_recompute_and_notify(self, tmp_path):
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
        config = self._config(tmp_path, run_state_path)
        in_progress_issue = _full_issue(
            1,
            labels=("status:in-progress",),
            footprint=("src/foo.py",),
            symbols=("foo.Foo",),
            subtask_id="task-a",
        )
        conflict = FootprintConflict(
            subtask_id="task-a",
            other_subtask_id="task-b",
            similarity=0.5,
            blocked_subtask_id="task-b",
        )
        with (
            patch("orchestune.forge.GitHubForge.list_sub_issues") as mock_list,
            patch(
                "orchestune.forge.GitHubForge.get_issue",
                return_value=self._epic_issue(),
            ),
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label"),
            patch("orchestune.dispatch_targets.subprocess.Popen"),
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation",
                return_value=["src/unexpected.py"],
            ) as mock_check_deviation,
            patch(
                "orchestune.dispatch_rebase.recompute_dag_for_footprint_change"
            ) as mock_recompute,
            patch(
                "orchestune.dispatch_rebase.notify_recompute", return_value=["body"]
            ) as mock_notify,
        ):
            mock_list.return_value = [in_progress_issue]
            mock_recompute.return_value = (MagicMock(), [conflict])

            report = run_dispatch_cycle(config)

        mock_add_label.assert_not_called()
        mock_check_deviation.assert_called_once()
        mock_recompute.assert_called_once()
        assert mock_recompute.call_args.args[1] == "task-a"
        mock_notify.assert_called_once()
        assert mock_notify.call_args.kwargs["apply"] is True
        assert len(report.deviation_events) == 1
        event = report.deviation_events[0]
        assert event["issue_number"] == 1
        assert event["action"] == "recomputed"
        assert event["deviated_files"] == ["src/unexpected.py"]

        persisted = json.loads(run_state_path.read_text())
        assert persisted["active_worktrees"]["1"]["recompute_count"] == 1

    def test_dry_run_recompute_does_not_persist_or_call_github(self, tmp_path):
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
        config = self._config(tmp_path, run_state_path, apply=False)
        in_progress_issue = _full_issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        conflict = FootprintConflict(
            subtask_id="task-a",
            other_subtask_id="task-b",
            similarity=0.5,
            blocked_subtask_id="task-b",
        )
        with (
            patch("orchestune.forge.GitHubForge.list_sub_issues") as mock_list,
            patch(
                "orchestune.forge.GitHubForge.get_issue",
                return_value=self._epic_issue(),
            ),
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation",
                return_value=["src/unexpected.py"],
            ),
            patch(
                "orchestune.dispatch_rebase.recompute_dag_for_footprint_change"
            ) as mock_recompute,
            patch(
                "orchestune.dispatch_rebase.notify_recompute", return_value=["dry body"]
            ) as mock_notify,
        ):
            mock_list.return_value = [in_progress_issue]
            mock_recompute.return_value = (MagicMock(), [conflict])

            run_dispatch_cycle(config)

        mock_add_label.assert_not_called()
        mock_add_comment.assert_not_called()
        assert mock_notify.call_args.kwargs["apply"] is False

        persisted = json.loads(run_state_path.read_text())
        assert persisted["active_worktrees"]["1"]["recompute_count"] == 0

    def test_retry_limit_exceeded_triggers_forced_serialization(self, tmp_path):
        """#200: リトライ上限超過時は再計算せず強制直列化にフォールバックする。"""
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
                        recompute_count=2,
                    )
                },
                launch_history=[],
            ),
            run_state_path,
        )
        config = self._config(
            tmp_path,
            run_state_path,
            max_recompute_retries=2,
            max_concurrent=2,
        )
        in_progress_issue = _full_issue(
            1,
            labels=("status:in-progress",),
            subtask_id="task-a",
            footprint=("src/foo.py",),
        )
        other_queued_issue = _full_issue(
            2,
            labels=("status:queued",),
            subtask_id="task-b",
            footprint=("src/bar.py",),
        )

        def _launch_stub(
            selected,
            _task_to_base_branch,
            _candidate_tasks,
            run_state,
            _now,
            config,
            open_prs=None,
        ):
            save_run_state(run_state, config.run_state_path)
            return selected

        with (
            patch("orchestune.forge.GitHubForge.list_sub_issues") as mock_list,
            patch(
                "orchestune.forge.GitHubForge.get_issue",
                return_value=self._epic_issue(),
            ),
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label"),
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch_cycle._launch_selected_tasks",
                side_effect=_launch_stub,
            ),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation",
                return_value=["src/unexpected.py"],
            ),
            patch(
                "orchestune.dispatch_rebase.recompute_dag_for_footprint_change"
            ) as mock_recompute,
        ):
            mock_list.return_value = [other_queued_issue, in_progress_issue]

            report = run_dispatch_cycle(config)

        mock_recompute.assert_not_called()
        mock_add_label.assert_any_call(1, "status:force-serial")
        mock_add_comment.assert_called_once()
        assert [task.issue_number for task in report.selected] == [2]
        assert report.quota_slots_available == 1
        assert report.deviation_events[0]["action"] == "forced_serial"

        persisted = json.loads(run_state_path.read_text())
        assert persisted["active_worktrees"]["1"]["forced_serial"] is True

    def test_forced_serial_filters_out_only_conflicting_candidates(self, tmp_path):
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
                        forced_serial=True,
                    )
                },
                launch_history=[],
            ),
            run_state_path,
        )
        config = self._config(tmp_path, run_state_path, max_concurrent=3)
        in_progress_issue = _full_issue(
            1,
            labels=("status:in-progress", "status:force-serial"),
            subtask_id="task-a",
            footprint=("src/foo.py",),
        )
        conflicting_issue = _full_issue(
            2,
            labels=("status:queued",),
            subtask_id="task-b",
            footprint=("src/foo.py",),
        )
        dependent_issue = _full_issue(
            3,
            labels=("status:queued",),
            subtask_id="task-c",
            footprint=("src/baz.py",),
            depends_on=("task-a",),
        )
        independent_issue = _full_issue(
            4,
            labels=("status:queued",),
            subtask_id="task-d",
            footprint=("src/qux.py",),
        )

        def _launch_stub(
            selected,
            _task_to_base_branch,
            _candidate_tasks,
            run_state,
            _now,
            config,
            open_prs=None,
        ):
            save_run_state(run_state, config.run_state_path)
            return selected

        with (
            patch("orchestune.forge.GitHubForge.list_sub_issues") as mock_list,
            patch(
                "orchestune.forge.GitHubForge.get_issue",
                return_value=self._epic_issue(),
            ),
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch_cycle._launch_selected_tasks",
                side_effect=_launch_stub,
            ),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation",
                return_value=[],
            ),
        ):
            mock_list.return_value = [
                conflicting_issue,
                dependent_issue,
                independent_issue,
                in_progress_issue,
            ]
            report = run_dispatch_cycle(config)

        assert report.quota_slots_available == 2
        assert [task.issue_number for task in report.selected] == [4]

    def test_already_forced_serial_does_not_recompute_again(self, tmp_path):
        """一度強制直列化された後は、再度の再計算・通知でチャーンさせない。"""
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
                        recompute_count=2,
                        forced_serial=True,
                    )
                },
                launch_history=[],
            ),
            run_state_path,
        )
        config = self._config(tmp_path, run_state_path, max_recompute_retries=2)
        in_progress_issue = _full_issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        with (
            patch("orchestune.forge.GitHubForge.list_sub_issues") as mock_list,
            patch(
                "orchestune.forge.GitHubForge.get_issue",
                return_value=self._epic_issue(),
            ),
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation",
                return_value=["src/unexpected.py"],
            ),
            patch(
                "orchestune.dispatch_rebase.recompute_dag_for_footprint_change"
            ) as mock_recompute,
        ):
            mock_list.return_value = [in_progress_issue]
            report = run_dispatch_cycle(config)

        mock_recompute.assert_not_called()
        mock_add_comment.assert_not_called()
        mock_add_label.assert_not_called()
        assert report.selected == []
        assert report.deviation_events[0]["action"] == "already_forced_serial"


class TestRunDispatchCycleCompletion:
    """#193: プロセス終了検知→worktree削除→クオータ解放→status:doneラベル遷移。"""

    def _config(self, tmp_path, run_state_path, **overrides):
        defaults = dict(
            max_concurrent=1,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        defaults.update(overrides)
        return DispatcherConfig(**defaults)

    def _seed_active(self, tmp_path, run_state_path, **overrides):
        defaults = dict(
            issue_number=1,
            branch="claude/issue-1-task-a",
            worktree_path=str(tmp_path / "w1"),
            pid=111,
            started_at=1_699_999_000.0,
            declared_footprint=("src/foo.py",),
        )
        defaults.update(overrides)
        save_run_state(
            RunState(
                active_worktrees={"1": ActiveWorktree(**defaults)}, launch_history=[]
            ),
            run_state_path,
        )

    def test_completed_clean_worktree_is_removed_and_labeled_done(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        self._seed_active(tmp_path, run_state_path)
        config = self._config(tmp_path, run_state_path)
        in_progress_issue = _full_issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            _patch_gc_process_alive(return_value=False),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_new_commits",
                return_value=True,
            ),
            patch(
                "orchestune.dispatch_gc_completion.remove_worktree"
            ) as mock_remove_worktree,
        ):
            mock_list.side_effect = lambda label, **_: (
                [in_progress_issue] if label == "status:in-progress" else []
            )
            report = run_dispatch_cycle(config)

        mock_remove_worktree.assert_called_once_with(str(tmp_path / "w1"))
        mock_remove_label.assert_any_call(1, "status:in-progress")
        mock_add_label.assert_any_call(1, "status:done")
        assert report.completion_events == [
            {
                "issue_number": 1,
                "worktree_path": str(tmp_path / "w1"),
                "action": "completed",
                "subtask_id": "task-a",
                "commit_sha": None,
            }
        ]

        persisted = json.loads(run_state_path.read_text())
        assert persisted["active_worktrees"] == {}
        assert len(persisted["completed_worktrees"]) == 1
        completed = persisted["completed_worktrees"][0]
        assert completed["issue_number"] == 1
        assert completed["subtask_id"] == "task-a"
        assert completed["branch"] == "claude/issue-1-task-a"
        assert completed["started_at"] == 1_699_999_000.0
        assert completed["completed_at"] >= completed["started_at"]

        events_lines = config.events_log_path.read_text(encoding="utf-8").splitlines()
        assert len(events_lines) == 1
        logged_entry = json.loads(events_lines[0])
        assert logged_entry["completion_events"] == report.completion_events

    def test_cloud_completion_uses_remote_branch_commits(self, tmp_path):
        """#177: クラウド実行の結果は、起動時のローカルworktreeではなく
        fetch済みのリモートブランチで検証する。"""
        run_state_path = tmp_path / "run_state.json"
        self._seed_active(tmp_path, run_state_path, external_id="session-1")
        dispatch_target = MagicMock()
        dispatch_target.completion_status.return_value = "completed"
        config = self._config(tmp_path, run_state_path, dispatch_target=dispatch_target)
        in_progress_issue = _full_issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label"),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_new_commits",
                return_value=False,
            ) as mock_local_commits,
            patch(
                "orchestune.dispatch_gc_completion.remote_branch_commit_sha_if_ahead",
                return_value="remote-commit",
            ) as mock_remote_commits,
            patch("orchestune.dispatch_gc_completion.remove_worktree"),
        ):
            mock_list.side_effect = lambda label, **_: (
                [in_progress_issue] if label == "status:in-progress" else []
            )
            report = run_dispatch_cycle(config)

        assert report.completion_events[0]["action"] == "completed"
        assert report.completion_events[0]["commit_sha"] == "remote-commit"
        mock_local_commits.assert_not_called()
        mock_remote_commits.assert_called_once_with(
            config.worktree_root.parent,
            "claude/issue-1-task-a",
            "origin/main",
        )
        mock_add_label.assert_any_call(1, "status:done")

    def test_cloud_completion_without_verified_sha_is_not_marked_done(self, tmp_path):
        """#177: SHAを取得できなければ、完了ラベルへ遷移しない。"""
        run_state_path = tmp_path / "run_state.json"
        self._seed_active(tmp_path, run_state_path, external_id="session-1")
        dispatch_target = MagicMock()
        dispatch_target.completion_status.return_value = "completed"
        config = self._config(tmp_path, run_state_path, dispatch_target=dispatch_target)
        in_progress_issue = _full_issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label"),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.remote_branch_commit_sha_if_ahead",
                return_value=None,
            ),
            patch(
                "orchestune.dispatch_gc_completion.apply_human_review_escalation"
            ) as mock_escalate,
            patch("orchestune.dispatch_gc_completion.remove_worktree"),
        ):
            mock_list.side_effect = lambda label, **_: (
                [in_progress_issue] if label == "status:in-progress" else []
            )
            report = run_dispatch_cycle(config)

        assert report.completion_events[0]["action"] == "completed_no_commits"
        mock_escalate.assert_called_once()
        assert all(
            call.args != (1, "status:done") for call in mock_add_label.call_args_list
        )

    def test_dirty_worktree_completion_is_skipped(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        self._seed_active(tmp_path, run_state_path)
        config = self._config(tmp_path, run_state_path)
        in_progress_issue = _full_issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            # Completion now also consults the all-state PR list to rule out an
            # abandoned (closed-unmerged) PR before finalizing.
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            _patch_gc_process_alive(return_value=False),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=True,
            ),
            patch(
                "orchestune.dispatch_gc_completion.remove_worktree"
            ) as mock_remove_worktree,
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation", return_value=[]
            ),
        ):
            mock_list.side_effect = lambda label, **_: (
                [in_progress_issue] if label == "status:in-progress" else []
            )
            report = run_dispatch_cycle(config)

        mock_remove_worktree.assert_not_called()
        mock_add_label.assert_not_called()
        mock_remove_label.assert_not_called()
        assert (
            report.completion_events[0]["action"] == "completion_skipped_dirty_worktree"
        )

        persisted = json.loads(run_state_path.read_text())
        assert "1" in persisted["active_worktrees"]

    def test_dry_run_completion_does_not_mutate_or_call_github(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        self._seed_active(tmp_path, run_state_path)
        config = self._config(tmp_path, run_state_path, apply=False)
        in_progress_issue = _full_issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            # Completion now also consults the all-state PR list to rule out an
            # abandoned (closed-unmerged) PR before finalizing as "completed".
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            _patch_gc_process_alive(return_value=False),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_new_commits",
                return_value=True,
            ),
            patch(
                "orchestune.dispatch_gc_completion.remove_worktree"
            ) as mock_remove_worktree,
        ):
            mock_list.side_effect = lambda label, **_: (
                [in_progress_issue] if label == "status:in-progress" else []
            )
            report = run_dispatch_cycle(config)

        mock_remove_worktree.assert_not_called()
        mock_add_label.assert_not_called()
        mock_remove_label.assert_not_called()
        assert report.completion_events[0]["action"] == "completed"
        assert not config.events_log_path.exists()

        persisted = json.loads(run_state_path.read_text())
        assert "1" in persisted["active_worktrees"]

    def test_no_commits_completion_frees_quota_without_promoting_dependents(
        self, tmp_path
    ):
        """#74: 空コミット完了はcompleted_subtask_idsに含めず依存先を昇格させないが、
        run_state側のクオータは解放する(worktree・ラベルはdispatch_gc側で片付け済みのため)。"""
        run_state_path = tmp_path / "run_state.json"
        self._seed_active(tmp_path, run_state_path)
        config = self._config(tmp_path, run_state_path)
        in_progress_issue = _full_issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        blocked_issue = _full_issue(
            2,
            labels=("status:blocked",),
            subtask_id="task-b",
            depends_on=("task-a",),
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            # Completion now also consults the all-state PR list to rule out an
            # abandoned (closed-unmerged) PR before finalizing.
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
            _patch_gc_process_alive(return_value=False),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_new_commits",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.remove_worktree"
            ) as mock_remove_worktree,
        ):
            mock_list.side_effect = lambda label, **_: (
                [in_progress_issue]
                if label == "status:in-progress"
                else ([blocked_issue] if label == "status:blocked" else [])
            )
            report = run_dispatch_cycle(config)

        assert report.completion_events[0]["action"] == "completed_no_commits"
        mock_remove_worktree.assert_called_once_with(str(tmp_path / "w1"))
        mock_remove_label.assert_any_call(1, "status:in-progress")
        mock_add_label.assert_any_call(1, "status:blocked-human-review")
        mock_add_comment.assert_called_once()
        # #74の核心: 依存先(#2)は誤って昇格させない
        assert report.promotion_events == []
        assert all(
            call.args != (2, "status:queued") for call in mock_add_label.call_args_list
        )

        persisted = json.loads(run_state_path.read_text())
        assert "1" not in persisted["active_worktrees"]
        assert persisted["completed_worktrees"] == []

    def test_freed_quota_allows_new_task_to_launch_same_cycle(self, tmp_path):
        """#193の核心: 完了検知でクオータが解放され、同一サイクル内で
        新規タスクが選出・起動されることを検証する（恒久停止バグの回帰テスト）。"""
        run_state_path = tmp_path / "run_state.json"
        self._seed_active(tmp_path, run_state_path)
        config = self._config(tmp_path, run_state_path)
        in_progress_issue = _full_issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        queued_issue = _full_issue(2, footprint=("src/bar.py",), subtask_id="task-b")
        with (
            patch("orchestune.dispatch_worktree._branch_exists", return_value=False),
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label"),
            _patch_gc_process_alive(return_value=False),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_new_commits",
                return_value=True,
            ),
            patch("orchestune.dispatch_gc_completion.remove_worktree"),
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_subproc_run,
            patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen,
        ):

            def _list(label, **_):
                if label == "status:in-progress":
                    return [in_progress_issue]
                if label == "status:queued":
                    return [queued_issue]
                return []

            mock_list.side_effect = _list
            mock_subproc_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            mock_popen.return_value.pid = 999
            report = run_dispatch_cycle(config)

        assert [t.issue_number for t in report.selected] == [2]
        mock_add_label.assert_any_call(2, "status:in-progress")

        persisted = json.loads(run_state_path.read_text())
        assert "1" not in persisted["active_worktrees"]
        assert "2" in persisted["active_worktrees"]


class TestRunDispatchCycleNotNeeded:
    """#280: status:not-neededラベル検知による完全自動クローズ・依存解決。"""

    def _config(self, tmp_path, run_state_path, **overrides):
        defaults = dict(
            max_concurrent=1,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        defaults.update(overrides)
        return DispatcherConfig(**defaults)

    def _seed_active(self, tmp_path, run_state_path, **overrides):
        defaults = dict(
            issue_number=1,
            branch="claude/issue-1-task-a",
            worktree_path=str(tmp_path / "w1"),
            pid=111,
            started_at=1_699_999_000.0,
            declared_footprint=("src/foo.py",),
        )
        defaults.update(overrides)
        save_run_state(
            RunState(
                active_worktrees={"1": ActiveWorktree(**defaults)}, launch_history=[]
            ),
            run_state_path,
        )

    def test_not_needed_label_closes_issue_regardless_of_pr_or_process_state(
        self, tmp_path
    ):
        """セッションがコミット・PRを一切作らない対応不要ケースでも、
        PID/PR存在に依存せずラベル検知だけで完了・クローズできることを検証する
        （#250で観測された、永遠にstatus:in-progressのままスタックする問題の回帰テスト）。"""
        run_state_path = tmp_path / "run_state.json"
        self._seed_active(tmp_path, run_state_path)
        config = self._config(tmp_path, run_state_path)
        not_needed_issue = _full_issue(
            1, labels=("status:not-needed",), subtask_id="task-a"
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.close_issue") as mock_close_issue,
            # プロセスは生きたまま・PRも存在しない、という「対応不要」の典型状態
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.remove_worktree"
            ) as mock_remove_worktree,
        ):
            mock_list.side_effect = lambda label, **_: (
                [not_needed_issue] if label == "status:not-needed" else []
            )
            report = run_dispatch_cycle(config)

        mock_remove_worktree.assert_called_once_with(str(tmp_path / "w1"))
        mock_remove_label.assert_any_call(1, "status:in-progress")
        mock_close_issue.assert_called_once()
        assert mock_close_issue.call_args.args[0] == 1
        assert mock_close_issue.call_args.args[1] == "not planned"
        assert report.completion_events == [
            {
                "issue_number": 1,
                "worktree_path": str(tmp_path / "w1"),
                "action": "not_needed",
                "subtask_id": "task-a",
            }
        ]

        persisted = json.loads(run_state_path.read_text())
        assert persisted["active_worktrees"] == {}

    def test_dry_run_not_needed_does_not_call_github_or_mutate(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        self._seed_active(tmp_path, run_state_path)
        config = self._config(tmp_path, run_state_path, apply=False)
        not_needed_issue = _full_issue(
            1, labels=("status:not-needed",), subtask_id="task-a"
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.close_issue") as mock_close_issue,
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.remove_worktree"
            ) as mock_remove_worktree,
        ):
            mock_list.side_effect = lambda label, **_: (
                [not_needed_issue] if label == "status:not-needed" else []
            )
            report = run_dispatch_cycle(config)

        mock_remove_worktree.assert_not_called()
        mock_remove_label.assert_not_called()
        mock_close_issue.assert_not_called()
        assert report.completion_events[0]["action"] == "not_needed"

        persisted = json.loads(run_state_path.read_text())
        assert "1" in persisted["active_worktrees"]

    def test_blocked_task_promotes_when_dependency_is_not_needed(self, tmp_path):
        """対応不要と判定された依存先も、status:done同様に依存解決済みとして
        扱われ、後続のstatus:blockedタスクがstatus:queuedへ昇格すること。"""
        run_state_path = tmp_path / "run_state.json"
        config = self._config(tmp_path, run_state_path, max_concurrent=2)
        not_needed_issue = _full_issue(
            1, labels=("status:not-needed",), subtask_id="task-a"
        )
        blocked_issue = _full_issue(
            2,
            labels=("status:blocked",),
            subtask_id="task-b",
            depends_on=("task-a",),
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
        ):

            def _list(label, **_):
                if label == "status:not-needed":
                    return [not_needed_issue]
                if label == "status:blocked":
                    return [blocked_issue]
                return []

            mock_list.side_effect = _list
            report = run_dispatch_cycle(config)

        mock_remove_label.assert_any_call(2, "status:blocked")
        mock_add_label.assert_any_call(2, "status:queued")
        assert report.promotion_events == [{"issue_number": 2, "subtask_id": "task-b"}]


class TestRunDispatchCycleBlockedPromotion:
    """#193: 依存解決によるstatus:blocked → status:queued昇格。"""

    def _config(self, tmp_path, **overrides):
        defaults = dict(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        defaults.update(overrides)
        return DispatcherConfig(**defaults)

    def test_promotes_blocked_task_when_dependency_already_done(self, tmp_path):
        config = self._config(tmp_path)
        done_issue = _full_issue(1, labels=("status:done",), subtask_id="task-a")
        blocked_issue = _full_issue(
            2,
            labels=("status:blocked",),
            subtask_id="task-b",
            depends_on=("task-a",),
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
        ):

            def _list(label, **_):
                if label == "status:done":
                    return [done_issue]
                if label == "status:blocked":
                    return [blocked_issue]
                return []

            mock_list.side_effect = _list
            report = run_dispatch_cycle(config)

        mock_remove_label.assert_any_call(2, "status:blocked")
        mock_add_label.assert_any_call(2, "status:queued")
        assert report.promotion_events == [{"issue_number": 2, "subtask_id": "task-b"}]

    def test_promotes_blocked_task_when_dependency_done_and_closed(self, tmp_path):
        """#236: 完了Issueが通常のGitHub運用でCloseされていても、
        status:done検索がstate="all"で呼ばれる限り依存解決できる。"""
        config = self._config(tmp_path)
        done_issue = _full_issue(1, labels=("status:done",), subtask_id="task-a")
        blocked_issue = _full_issue(
            2,
            labels=("status:blocked",),
            subtask_id="task-b",
            depends_on=("task-a",),
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
        ):

            def _list(label, state="open"):
                # closedなIssueもstatus:done検索に含まれるのはstate="all"の
                # 呼び出しのみ（実際のgh issue list --state open/allの挙動を模す）。
                if label == "status:done" and state == "all":
                    return [done_issue]
                if label == "status:blocked":
                    return [blocked_issue]
                return []

            mock_list.side_effect = _list
            report = run_dispatch_cycle(config)

        mock_remove_label.assert_any_call(2, "status:blocked")
        mock_add_label.assert_any_call(2, "status:queued")
        assert report.promotion_events == [{"issue_number": 2, "subtask_id": "task-b"}]

    def test_does_not_promote_when_dependency_unresolved(self, tmp_path):
        config = self._config(tmp_path)
        blocked_issue = _full_issue(
            2,
            labels=("status:blocked",),
            subtask_id="task-b",
            depends_on=("task-a",),
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
        ):
            mock_list.side_effect = lambda label, **_: (
                [blocked_issue] if label == "status:blocked" else []
            )
            report = run_dispatch_cycle(config)

        mock_add_label.assert_not_called()
        mock_remove_label.assert_not_called()
        assert report.promotion_events == []

    def test_promotes_when_dependency_completes_in_same_cycle(self, tmp_path):
        """依存先が同一サイクル内で完了検知された場合も即座に昇格させる。"""
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
        config = self._config(tmp_path, run_state_path=run_state_path)
        in_progress_issue = _full_issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        blocked_issue = _full_issue(
            2,
            labels=("status:blocked",),
            subtask_id="task-b",
            depends_on=("task-a",),
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            # Completion now also consults the all-state PR list to rule out an
            # abandoned (closed-unmerged) PR before finalizing as "completed".
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            _patch_gc_process_alive(return_value=False),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_new_commits",
                return_value=True,
            ),
            patch("orchestune.dispatch_gc_completion.remove_worktree"),
        ):

            def _list(label, **_):
                if label == "status:in-progress":
                    return [in_progress_issue]
                if label == "status:blocked":
                    return [blocked_issue]
                return []

            mock_list.side_effect = _list
            report = run_dispatch_cycle(config)

        mock_remove_label.assert_any_call(2, "status:blocked")
        mock_add_label.assert_any_call(2, "status:queued")
        assert {"issue_number": 2, "subtask_id": "task-b"} in report.promotion_events

    def test_dry_run_promotion_does_not_call_github(self, tmp_path):
        config = self._config(tmp_path, apply=False)
        done_issue = _full_issue(1, labels=("status:done",), subtask_id="task-a")
        blocked_issue = _full_issue(
            2,
            labels=("status:blocked",),
            subtask_id="task-b",
            depends_on=("task-a",),
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
        ):

            def _list(label, **_):
                if label == "status:done":
                    return [done_issue]
                if label == "status:blocked":
                    return [blocked_issue]
                return []

            mock_list.side_effect = _list
            report = run_dispatch_cycle(config)

        mock_add_label.assert_not_called()
        mock_remove_label.assert_not_called()
        assert report.promotion_events == [{"issue_number": 2, "subtask_id": "task-b"}]

    def test_yaml_error_transitions_to_blocked(self, tmp_path):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        body = (
            "## Footprint\n"
            "```yaml\n"
            "subtask_id: task-invalid\n"
            "footprint:\n"
            "  - [invalid-yaml-structure:\n"
            "```\n"
        )
        issue = IssueRecord(
            number=9,
            title="t",
            body=body,
            labels=("status:queued",),
            created_at="2026-01-01T00:00:00+00:00",
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
        ):
            mock_list.side_effect = lambda label, **_: (
                [issue] if label == "status:queued" else []
            )

            report = run_dispatch_cycle(config)

            assert report.selected == []
            mock_remove_label.assert_any_call(9, "status:queued")
            mock_add_label.assert_any_call(9, "status:blocked")
            mock_add_comment.assert_called_once_with(9, ANY)

    def test_worktree_launch_failure_transitions_to_blocked(self, tmp_path):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        issue = _full_issue(1)
        with (
            patch("orchestune.dispatch_worktree._branch_exists", return_value=False),
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
        ):
            mock_list.side_effect = lambda label, **_: (
                [issue] if label == "status:queued" else []
            )
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=128,
                cmd="git worktree add",
            )
            report = run_dispatch_cycle(config)

            assert report.selected == []
            mock_remove_label.assert_any_call(1, "status:queued")
            mock_add_label.assert_any_call(1, "status:blocked")
            mock_add_comment.assert_called_once_with(1, ANY)


class TestRunDispatchCycleActorVerification:
    """#119: status:queuedラベルを付与したactorの権限が不足している場合、
    run_dispatch_cycle経由でも起動をスキップしエスカレーションすることを確認する。"""

    def test_unauthorized_actor_skips_launch_and_escalates(self, tmp_path):
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
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
            patch(
                "orchestune.forge.GitHubForge.get_label_actor",
                return_value="mallory",
            ),
            patch(
                "orchestune.forge.GitHubForge.get_actor_permission",
                return_value="read",
            ),
            patch("orchestune.dispatch_worktree.subprocess.run"),
            patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen,
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

    def test_dry_run_excludes_unauthorized_actor_without_writes(self, tmp_path):
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=False,
        )
        queued_issue = _full_issue(1, subtask_id="task-1")
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch(
                "orchestune.forge.GitHubForge.get_label_actor",
                return_value="mallory",
            ),
            patch(
                "orchestune.forge.GitHubForge.get_actor_permission",
                return_value="none",
            ),
        ):
            mock_list.side_effect = lambda label, **_: (
                [queued_issue] if label == "status:queued" else []
            )
            report = run_dispatch_cycle(config)

        assert report.selected == []
        mock_add_label.assert_not_called()
        mock_remove_label.assert_not_called()

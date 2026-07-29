import subprocess
from unittest.mock import patch

import pytest

import orchestune.dispatch_locks
from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_cycle import CycleReport, _sync_external_locks
from orchestune.dispatch_locks import check_footprint_deviation, scan_external_locks
from orchestune.dispatch_report import write_github_step_summary
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import RunState
from orchestune.models import PrRecord


@pytest.fixture(autouse=True)
def mock_resolve_branch(monkeypatch):
    monkeypatch.setattr(
        orchestune.dispatch_locks,
        "resolve_local_or_remote_branch",
        lambda worktree_path, branch, prefer_remote=False: branch,
    )


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


class TestScanExternalLocks:
    def test_locks_task_overlapping_open_pr(self):
        queued = [_task(1, footprint=("src/shared.py",))]
        prs = [
            PrRecord(number=99, head_ref="feat/other", changed_files=("src/shared.py",))
        ]
        result = scan_external_locks(
            queued, remote_branches=[], prs=prs, active_branches=[]
        )
        assert [t.issue_number for t in result.to_lock] == [1]
        assert result.to_unlock == []

    def test_does_not_lock_disjoint_footprint(self):
        queued = [_task(1, footprint=("src/unique.py",))]
        prs = [
            PrRecord(number=99, head_ref="feat/other", changed_files=("src/shared.py",))
        ]
        result = scan_external_locks(
            queued, remote_branches=[], prs=prs, active_branches=[]
        )
        assert result.to_lock == []

    def test_excludes_dispatcher_managed_branches(self):
        queued = [_task(1, footprint=("src/shared.py",))]
        prs = [
            PrRecord(
                number=99, head_ref="claude/issue-5-x", changed_files=("src/shared.py",)
            )
        ]
        result = scan_external_locks(
            queued, remote_branches=[], prs=prs, active_branches=["claude/issue-5-x"]
        )
        assert result.to_lock == []

    def test_does_not_lock_task_against_its_own_closing_pr(self):
        """#239: AIセッションがブランチ名指示に従わず、run_stateのブランチ名と
        一致しないPRを作成した場合でも、そのPRが自タスク自身のIssueを
        closesしているなら「他人の変更」として誤ロックしない。"""
        queued = [_task(218, footprint=("src/routes/review_history.py",))]
        prs = [
            PrRecord(
                number=238,
                head_ref="claude/elegant-noether-5rli7u",
                changed_files=("src/routes/review_history.py",),
                closes_issue_numbers=(218,),
            )
        ]
        result = scan_external_locks(
            queued,
            remote_branches=[],
            prs=prs,
            active_branches=["claude/issue-218-review-history-backend-api"],
        )
        assert result.to_lock == []

    def test_still_locks_other_task_overlapping_unrelated_closing_pr(self):
        """自PRの除外は「そのタスク自身のIssueをclosesする場合」のみに限定され、
        他タスクに対しては引き続き外部衝突として扱われる。"""
        queued = [_task(1, footprint=("src/shared.py",))]
        prs = [
            PrRecord(
                number=238,
                head_ref="claude/elegant-noether-5rli7u",
                changed_files=("src/shared.py",),
                closes_issue_numbers=(218,),
            )
        ]
        result = scan_external_locks(
            queued, remote_branches=[], prs=prs, active_branches=[]
        )
        assert [t.issue_number for t in result.to_lock] == [1]

    def test_locks_task_overlapping_101st_changed_file(self):
        """#250: 101件目のchanged fileだけがtask footprintと重なる場合でもロックする。"""
        changed_files = tuple(f"file{i}.py" for i in range(1, 102))
        queued = [_task(1, footprint=("file101.py",))]
        prs = [PrRecord(number=99, head_ref="feat/large", changed_files=changed_files)]
        result = scan_external_locks(
            queued, remote_branches=[], prs=prs, active_branches=[]
        )
        assert [t.issue_number for t in result.to_lock] == [1]

    def test_locks_task_when_pr_files_truncated(self):
        """#250: changed filesが完全取得できずtruncated状態のPRが存在する場合、fail closedで外部ロック判定する。"""
        queued = [_task(1, footprint=("completely_unrelated.py",))]
        prs = [
            PrRecord(
                number=99,
                head_ref="feat/truncated",
                changed_files=("file1.py",),
                is_files_truncated=True,
            )
        ]
        result = scan_external_locks(
            queued, remote_branches=[], prs=prs, active_branches=[]
        )
        assert [t.issue_number for t in result.to_lock] == [1]

    def test_ignores_truncated_pr_for_hotspot_or_empty_footprint_task(self):
        """#250: truncated状態のPRが存在しても、taskのfootprintが空またはhotspotのみの場合は外部ロックしない。"""
        queued = [
            _task(1, footprint=("poetry.lock",)),
            _task(2, footprint=()),
        ]
        prs = [
            PrRecord(
                number=99,
                head_ref="feat/truncated",
                changed_files=("file1.py",),
                is_files_truncated=True,
            )
        ]
        result = scan_external_locks(
            queued, remote_branches=[], prs=prs, active_branches=[]
        )
        assert result.to_lock == []

    def test_unlocks_previously_locked_task_with_no_more_overlap(self):
        locked_task = Task(
            issue_number=1,
            subtask_id="task-1",
            footprint=("src/unique.py",),
            symbols=(),
            risk=False,
            priority="medium",
            progress_partial=False,
            status_labels=("status:external-lock",),
            created_at="2026-01-01T00:00:00+00:00",
        )
        result = scan_external_locks(
            [locked_task], remote_branches=[], prs=[], active_branches=[]
        )
        assert [t.issue_number for t in result.to_unlock] == [1]

    def test_done_task_is_never_locked(self):
        done_task = Task(
            issue_number=1,
            subtask_id="task-1",
            footprint=("src/shared.py",),
            symbols=(),
            risk=False,
            priority="medium",
            progress_partial=False,
            status_labels=("status:done",),
            created_at="2026-01-01T00:00:00+00:00",
        )
        prs = [
            PrRecord(number=99, head_ref="feat/other", changed_files=("src/shared.py",))
        ]
        result = scan_external_locks(
            [done_task], remote_branches=[], prs=prs, active_branches=[]
        )
        assert result.to_lock == []
        assert result.to_unlock == []

    def test_done_task_with_external_lock_label_is_unlocked(self):
        done_locked_task = Task(
            issue_number=1,
            subtask_id="task-1",
            footprint=("src/shared.py",),
            symbols=(),
            risk=False,
            priority="medium",
            progress_partial=False,
            status_labels=("status:done", "status:external-lock"),
            created_at="2026-01-01T00:00:00+00:00",
        )
        prs = [
            PrRecord(number=99, head_ref="feat/other", changed_files=("src/shared.py",))
        ]
        result = scan_external_locks(
            [done_locked_task], remote_branches=[], prs=prs, active_branches=[]
        )
        assert result.to_lock == []
        assert [t.issue_number for t in result.to_unlock] == [1]

    def test_not_needed_task_is_never_locked(self):
        """#261 Codexレビュー指摘(P2): status:not-neededタスクはstatus:done同様、
        既に対応不要と判定済みで再ディスパッチされないため、通常の重複判定でも
        lockの対象外とすべき。"""
        not_needed_task = Task(
            issue_number=1,
            subtask_id="task-1",
            footprint=("src/shared.py",),
            symbols=(),
            risk=False,
            priority="medium",
            progress_partial=False,
            status_labels=("status:not-needed",),
            created_at="2026-01-01T00:00:00+00:00",
        )
        prs = [
            PrRecord(number=99, head_ref="feat/other", changed_files=("src/shared.py",))
        ]
        result = scan_external_locks(
            [not_needed_task], remote_branches=[], prs=prs, active_branches=[]
        )
        assert result.to_lock == []
        assert result.to_unlock == []

    def test_not_needed_task_with_external_lock_label_is_unlocked(self):
        not_needed_locked_task = Task(
            issue_number=1,
            subtask_id="task-1",
            footprint=("src/shared.py",),
            symbols=(),
            risk=False,
            priority="medium",
            progress_partial=False,
            status_labels=("status:not-needed", "status:external-lock"),
            created_at="2026-01-01T00:00:00+00:00",
        )
        prs = [
            PrRecord(number=99, head_ref="feat/other", changed_files=("src/shared.py",))
        ]
        result = scan_external_locks(
            [not_needed_locked_task], remote_branches=[], prs=prs, active_branches=[]
        )
        assert result.to_lock == []
        assert [t.issue_number for t in result.to_unlock] == [1]

    def test_not_needed_task_is_excluded_from_fail_closed_locking(self):
        """#261 Codexレビュー指摘(P2) Reproducer: 差分取得不能なブランチが
        1件でもある場合のfail-closed判定(#245)は、status:doneのみを除外して
        いたため、既に解決済みのstatus:not-neededタスクにも
        status:external-lockが付与されうる状態だった。"""
        not_needed_task = Task(
            issue_number=1,
            subtask_id="task-1",
            footprint=("src/shared.py",),
            symbols=(),
            risk=False,
            priority="medium",
            progress_partial=False,
            status_labels=("status:not-needed",),
            created_at="2026-01-01T00:00:00+00:00",
        )
        result = scan_external_locks(
            [not_needed_task],
            remote_branches=[("feat/x", None)],
            prs=[],
            active_branches=[],
        )
        assert result.to_lock == []

    def test_does_not_lock_on_hotspot_file_overlap_only(self):
        """#209: poetry.lock等のホットスポットファイルだけが重複していても、
        実質的な直列化(外部ロック)を引き起こさない。"""
        queued = [_task(1, footprint=("poetry.lock",))]
        prs = [
            PrRecord(number=99, head_ref="feat/other", changed_files=("poetry.lock",))
        ]
        result = scan_external_locks(
            queued, remote_branches=[], prs=prs, active_branches=[]
        )
        assert result.to_lock == []

    def test_still_locks_when_non_hotspot_overlap_remains(self):
        """ホットスポット除外は重複ファイル集合の一部にのみ適用され、
        非ホットスポットな重複が残っていれば従来通りロックする。"""
        queued = [_task(1, footprint=("poetry.lock", "src/shared.py"))]
        prs = [
            PrRecord(
                number=99,
                head_ref="feat/other",
                changed_files=("poetry.lock", "src/shared.py"),
            )
        ]
        result = scan_external_locks(
            queued, remote_branches=[], prs=prs, active_branches=[]
        )
        assert [t.issue_number for t in result.to_lock] == [1]

    def test_unlocks_previously_locked_task_when_only_hotspot_overlap_remains(self):
        locked_task = Task(
            issue_number=1,
            subtask_id="task-1",
            footprint=("poetry.lock",),
            symbols=(),
            risk=False,
            priority="medium",
            progress_partial=False,
            status_labels=("status:external-lock",),
            created_at="2026-01-01T00:00:00+00:00",
        )
        prs = [
            PrRecord(number=99, head_ref="feat/other", changed_files=("poetry.lock",))
        ]
        result = scan_external_locks(
            [locked_task], remote_branches=[], prs=prs, active_branches=[]
        )
        assert result.to_lock == []
        assert [t.issue_number for t in result.to_unlock] == [1]


class TestScanExternalLocksWithUnknownFootprint:
    """#245: 差分取得不能（footprint不明）なブランチが1件でもある場合は
    fail closedとし、既存lockを維持し、新規taskも競合なしと判定しない。"""

    def _locked_task(self, footprint=("src/foo.py",)):
        return Task(
            issue_number=1,
            subtask_id="task-1",
            footprint=footprint,
            symbols=(),
            risk=False,
            priority="medium",
            progress_partial=False,
            status_labels=("status:external-lock",),
            created_at="2026-01-01T00:00:00+00:00",
        )

    def test_keeps_existing_lock_when_branch_footprint_is_unknown(self):
        """Reproducer: 従来は不明footprintが空集合に潰され、lockが解除されていた。"""
        result = scan_external_locks(
            [self._locked_task()],
            remote_branches=[("feat/x", None)],
            prs=[],
            active_branches=[],
        )
        assert result.to_unlock == []
        assert result.to_lock == []

    def test_keeps_existing_lock_when_remote_branches_is_a_single_use_iterator(self):
        """#261 Reproducer: `remote_branches`にgenerator等の単回走査イテレータが
        渡されると、2回走査する実装ではhas_unknown_branch_footprint判定が常に
        Falseとなり、fail-closed判定が無効化されて既存lockが解除されうる。"""

        def remote_branches_gen():
            yield ("feat/x", None)

        result = scan_external_locks(
            [self._locked_task()],
            remote_branches=remote_branches_gen(),
            prs=[],
            active_branches=[],
        )
        assert result.to_unlock == []
        assert result.to_lock == []

    def test_locks_queued_task_when_remote_branches_is_a_single_use_iterator(self):
        def remote_branches_gen():
            yield ("feat/x", None)

        queued = [_task(1, footprint=("src/foo.py",))]
        result = scan_external_locks(
            queued,
            remote_branches=remote_branches_gen(),
            prs=[],
            active_branches=[],
        )
        assert [t.issue_number for t in result.to_lock] == [1]

    def test_locks_queued_task_when_branch_footprint_is_unknown(self):
        queued = [_task(1, footprint=("src/foo.py",))]
        result = scan_external_locks(
            queued,
            remote_branches=[("feat/x", None)],
            prs=[],
            active_branches=[],
        )
        assert [t.issue_number for t in result.to_lock] == [1]

    def test_does_not_lock_task_with_empty_footprint(self):
        """footprint未宣言のtaskはどのブランチとも衝突し得ないため対象外。"""
        queued = [_task(1, footprint=())]
        result = scan_external_locks(
            queued,
            remote_branches=[("feat/x", None)],
            prs=[],
            active_branches=[],
        )
        assert result.to_lock == []

    def test_does_not_lock_task_with_hotspot_only_footprint(self):
        queued = [_task(1, footprint=("poetry.lock",))]
        result = scan_external_locks(
            queued,
            remote_branches=[("feat/x", None)],
            prs=[],
            active_branches=[],
        )
        assert result.to_lock == []

    def test_unknown_footprint_of_active_branch_is_ignored(self):
        """dispatcher自身が管理するアクティブブランチの差分不明は対象外。"""
        queued = [_task(1, footprint=("src/foo.py",))]
        result = scan_external_locks(
            queued,
            remote_branches=[("claude/issue-5-x", None)],
            prs=[],
            active_branches=["claude/issue-5-x"],
        )
        assert result.to_lock == []

    def test_done_task_with_lock_is_still_unlocked_despite_unknown(self):
        done_locked_task = Task(
            issue_number=1,
            subtask_id="task-1",
            footprint=("src/foo.py",),
            symbols=(),
            risk=False,
            priority="medium",
            progress_partial=False,
            status_labels=("status:done", "status:external-lock"),
            created_at="2026-01-01T00:00:00+00:00",
        )
        result = scan_external_locks(
            [done_locked_task],
            remote_branches=[("feat/x", None)],
            prs=[],
            active_branches=[],
        )
        assert [t.issue_number for t in result.to_unlock] == [1]

    def test_known_footprints_still_behave_normally_alongside_unknown(self):
        """不明ブランチがあっても、既知footprintとの重複判定は通常通り機能する。"""
        queued = [
            _task(1, footprint=("src/foo.py",)),
            _task(2, footprint=()),
        ]
        result = scan_external_locks(
            queued,
            remote_branches=[("feat/known", ("src/other.py",)), ("feat/x", None)],
            prs=[],
            active_branches=[],
        )
        assert [t.issue_number for t in result.to_lock] == [1]


class TestCheckFootprintDeviation:
    def test_returns_files_outside_declared_footprint(self):
        with patch("orchestune.dispatch_locks.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="1\t0\tsrc/foo.py\n20\t0\tsrc/unexpected.py\n",
                stderr="",
            )
            deviated = check_footprint_deviation(
                "worktrees/w1", declared_footprint=("src/foo.py",)
            )
        assert deviated == ["src/unexpected.py"]

    def test_no_deviation_returns_empty(self):
        with patch("orchestune.dispatch_locks.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="1\t0\tsrc/foo.py\n", stderr=""
            )
            deviated = check_footprint_deviation(
                "worktrees/w1", declared_footprint=("src/foo.py",)
            )
        assert deviated == []

    def test_small_deviation_within_buffer_is_ignored(self):
        """#200: 数行程度の微小な逸脱はライブロック防止のバッファとして無視する。"""
        with patch("orchestune.dispatch_locks.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="2\t1\tsrc/tiny_new_file.py\n",
                stderr="",
            )
            deviated = check_footprint_deviation(
                "worktrees/w1",
                declared_footprint=("src/foo.py",),
                min_changed_lines=5,
            )
        assert deviated == []

    def test_large_deviation_exceeding_buffer_is_reported(self):
        with patch("orchestune.dispatch_locks.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="10\t2\tsrc/large_new_file.py\n",
                stderr="",
            )
            deviated = check_footprint_deviation(
                "worktrees/w1",
                declared_footprint=("src/foo.py",),
                min_changed_lines=5,
            )
        assert deviated == ["src/large_new_file.py"]

    def test_binary_file_change_always_reported_regardless_of_buffer(self):
        with patch("orchestune.dispatch_locks.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="-\t-\tsrc/image.png\n", stderr=""
            )
            deviated = check_footprint_deviation(
                "worktrees/w1",
                declared_footprint=(),
                min_changed_lines=100,
            )
        assert deviated == ["src/image.png"]

    def test_hotspot_files_are_ignored_from_deviation(self):
        with patch("orchestune.dispatch_locks.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="10\t0\tpoetry.lock\n10\t0\tsrc/routes.py\n10\t0\tsrc/unexpected.py\n",
                stderr="",
            )
            deviated = check_footprint_deviation(
                "worktrees/w1",
                declared_footprint=(),
            )
        assert deviated == ["src/unexpected.py"]

    def test_respects_custom_base_branch(self):
        with patch("orchestune.dispatch_locks.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            check_footprint_deviation(
                "worktrees/w1",
                declared_footprint=(),
                base="parent/issue-12",
            )
            mock_run.assert_called_once()
            called_args = mock_run.call_args[0][0]
            assert called_args[-1] == "parent/issue-12...HEAD"
            assert mock_run.call_args.kwargs["cwd"] == "worktrees/w1"

    def test_deviation_error_returns_none(self):
        with patch(
            "orchestune.dispatch_locks.subprocess.run",
            side_effect=OSError("git command failed"),
        ):
            deviated = check_footprint_deviation(
                "worktrees/w1",
                declared_footprint=(),
            )
        assert deviated is None


class TestSyncExternalLocks:
    @patch("orchestune.dispatch_cycle.list_remote_branches")
    @patch("orchestune.forge.GitHubForge.remove_label")
    @patch("orchestune.forge.GitHubForge.add_label")
    def test_sync_external_locks_unlocks_without_requeue_for_done_tasks(
        self, mock_add_label, mock_remove_label, mock_list_branches
    ):
        mock_list_branches.return_value = []

        done_task = Task(
            issue_number=1,
            subtask_id="task-1",
            footprint=("src/shared.py",),
            symbols=(),
            risk=False,
            priority="medium",
            progress_partial=False,
            status_labels=("status:done", "status:external-lock"),
            created_at="2026-01-01T00:00:00+00:00",
        )

        run_state = RunState(active_worktrees={})
        config = DispatcherConfig(apply=True)

        res = _sync_external_locks(
            tasks_by_issue={1: done_task},
            prs=[],
            run_state=run_state,
            config=config,
        )

        assert res.to_lock == []
        assert [t.issue_number for t in res.to_unlock] == [1]

        mock_remove_label.assert_called_once_with(1, "status:external-lock")
        assert mock_add_label.call_count == 0

    def test_write_github_step_summary(self, tmp_path):
        summary_file = tmp_path / "step_summary.md"

        task_selected = Task(
            issue_number=10,
            subtask_id="task-launch-10",
            footprint=(),
            symbols=(),
            risk=False,
            priority="high",
            progress_partial=False,
            status_labels=(),
            created_at="2026-01-01T00:00:00+00:00",
        )
        task_lock = Task(
            issue_number=20,
            subtask_id="task-lock-20",
            footprint=(),
            symbols=(),
            risk=False,
            priority="medium",
            progress_partial=False,
            status_labels=(),
            created_at="2026-01-01T00:00:00+00:00",
        )

        cycle_report = CycleReport(
            selected=[task_selected],
            quota_slots_available=1,
            lock_changes={
                "to_lock": [task_lock],
                "to_unlock": [],
            },
            deviation_events=[],
            completion_events=[],
            promotion_events=[],
            applied=True,
        )

        integrator_report = {
            "status": "partial_success",
            "merged": ["task-merged-1"],
            "failed": ["task-failed-2"],
            "failed_reasons": {
                "task-failed-2": "CI verification failed\nDetailed error message here"
            },
        }

        write_github_step_summary(
            cycle_report=cycle_report,
            integrator_report=integrator_report,
            summary_path=str(summary_file),
        )

        assert summary_file.exists()
        content = summary_file.read_text(encoding="utf-8")
        assert "## 🤖 Orchestune Dispatch Summary" in content
        assert "### 🔍 仮マージ検証（Integrator）結果" in content
        assert "全体ステータス: **partial_success**" in content
        assert "| `task-merged-1` | ✅ 成功 |" in content
        assert "| `task-failed-2` | ❌ 失敗 | CI verification failed |" in content
        assert "### 🚀 新規起動タスク" in content
        assert "| `task-launch-10` | #10 | high |" in content
        assert "### 🔒 外部ロック（External Lock）変更" in content
        assert (
            "| `task-lock-20` | #20 | 🔒 ロック付与 (`status:external-lock`) |"
            in content
        )

    def test_write_github_step_summary_includes_integration_pr_link(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("GITHUB_REPOSITORY", "Saltmu/manuscriptune")
        summary_file = tmp_path / "step_summary.md"

        integrator_report = {
            "status": "success",
            "merged": ["task-merged-1"],
            "failed": [],
            "integration_pr_number": 315,
        }

        write_github_step_summary(
            cycle_report=None,
            integrator_report=integrator_report,
            summary_path=str(summary_file),
        )

        content = summary_file.read_text(encoding="utf-8")
        assert "統合PR #315" in content
        assert "https://github.com/Saltmu/manuscriptune/pull/315" in content

    def test_write_github_step_summary_without_repository_env_omits_link(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        summary_file = tmp_path / "step_summary.md"

        integrator_report = {
            "status": "success",
            "merged": ["task-merged-1"],
            "failed": [],
            "integration_pr_number": 315,
        }

        write_github_step_summary(
            cycle_report=None,
            integrator_report=integrator_report,
            summary_path=str(summary_file),
        )

        content = summary_file.read_text(encoding="utf-8")
        assert "統合PR #315" in content
        assert "https://github.com/" not in content

    def test_write_github_step_summary_no_pr_number_omits_pr_line(self, tmp_path):
        summary_file = tmp_path / "step_summary.md"

        integrator_report = {
            "status": "success",
            "merged": ["task-merged-1"],
            "failed": [],
            "integration_pr_number": None,
        }

        write_github_step_summary(
            cycle_report=None,
            integrator_report=integrator_report,
            summary_path=str(summary_file),
        )

        content = summary_file.read_text(encoding="utf-8")
        assert "統合PR" not in content

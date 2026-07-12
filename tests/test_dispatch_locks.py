import subprocess
from unittest.mock import patch

import pytest

import orchestune.dispatch_locks
from orchestune.dispatch_locks import check_footprint_deviation, scan_external_locks
from orchestune.dispatch_scoring import Task
from orchestune.github import PrRecord


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
            assert called_args[5] == "parent/issue-12...HEAD"

import json

from orchestune.dispatch_state import (
    ActiveWorktree,
    CompletedWorktree,
    RunState,
    load_run_state,
    save_run_state,
)


class TestRunState:
    def test_load_missing_file_returns_empty_state(self, tmp_path):
        state = load_run_state(tmp_path / "run_state.json")
        assert state.active_worktrees == {}
        assert state.launch_history == []

    def test_save_and_load_roundtrip(self, tmp_path):
        path = tmp_path / "run_state.json"
        now = 1700000000.0
        state = RunState(
            active_worktrees={
                "10": ActiveWorktree(
                    issue_number=10,
                    branch="claude/issue-10-x",
                    worktree_path="worktrees/claude-issue-10-x",
                    pid=12345,
                    started_at=1700000000.0,
                    declared_footprint=("src/foo.py",),
                )
            },
            launch_history=[1700000000.0],
        )
        save_run_state(state, path, now=now)
        loaded = load_run_state(path)
        assert loaded.active_worktrees["10"].branch == "claude/issue-10-x"
        assert loaded.launch_history == [1700000000.0]

    def test_save_and_load_roundtrip_with_unknown_active_start_time(self, tmp_path):
        path = tmp_path / "run_state.json"
        state = RunState(
            active_worktrees={
                "10": ActiveWorktree(
                    issue_number=10,
                    branch="claude/issue-10-x",
                    worktree_path="worktrees/claude-issue-10-x",
                    pid=None,
                    started_at=None,
                    declared_footprint=("src/foo.py",),
                )
            }
        )

        save_run_state(state, path)

        assert load_run_state(path).active_worktrees["10"].started_at is None

    def test_save_and_load_roundtrip_with_completed_worktrees(self, tmp_path):
        path = tmp_path / "run_state.json"
        now = 1700003600.0
        state = RunState(
            active_worktrees={},
            launch_history=[],
            completed_worktrees=[
                CompletedWorktree(
                    issue_number=11,
                    subtask_id="task-b",
                    branch="claude/issue-11-task-b",
                    started_at=1700000000.0,
                    completed_at=1700003600.0,
                    recompute_count=1,
                    forced_serial=False,
                )
            ],
        )
        save_run_state(state, path, now=now)
        loaded = load_run_state(path)
        assert loaded.completed_worktrees == state.completed_worktrees

    def test_completed_worktree_preserves_unknown_start_time(self, tmp_path):
        path = tmp_path / "run_state.json"
        now = 1700003600.0
        state = RunState(
            completed_worktrees=[
                CompletedWorktree(
                    issue_number=11,
                    subtask_id="task-b",
                    branch="claude/issue-11-task-b",
                    started_at=None,
                    completed_at=1700003600.0,
                )
            ]
        )

        save_run_state(state, path, now=now)

        assert load_run_state(path).completed_worktrees[0].started_at is None

    def test_load_missing_completed_worktrees_key_defaults_to_empty(self, tmp_path):
        path = tmp_path / "run_state.json"
        path.write_text(json.dumps({"active_worktrees": {}, "launch_history": []}))
        loaded = load_run_state(path)
        assert loaded.completed_worktrees == []

    def test_prune_run_state(self):
        from orchestune.dispatch_state import prune_run_state

        now = 5000000.0
        # launch_window = 86400 -> min_launch_time = 4913600
        # completed_retention = 30 * 86400 (2592000) -> min_completed_time = 2408000
        state = RunState(
            launch_history=[
                4000000.0,
                4920000.0,
                4990000.0,
            ],  # 4000000.0 is older than 24h
            completed_worktrees=[
                CompletedWorktree(
                    issue_number=1,
                    subtask_id="old",
                    branch="b1",
                    started_at=1000000.0,
                    completed_at=1000000.0,  # 1000000 < 2408000 (very old > 30 days)
                ),
                CompletedWorktree(
                    issue_number=2,
                    subtask_id="recent",
                    branch="b2",
                    started_at=4900000.0,
                    completed_at=4950000.0,  # 4950000 > 2408000 (recent)
                ),
            ],
        )

        pruned = prune_run_state(
            state,
            now=now,
            launch_window_seconds=86400.0,
            completed_retention_seconds=2592000.0,
        )

        assert pruned.launch_history == [4920000.0, 4990000.0]
        # open PR のない古い Issue 1 のレコードは削除され、直近30日以内の Issue 2 のみが残る
        assert len(pruned.completed_worktrees) == 1
        assert pruned.completed_worktrees[0].subtask_id == "recent"

    def test_prune_run_state_bounded_when_many_old_issues(self):
        from orchestune.dispatch_state import prune_run_state

        now = 5000000.0
        # 30日以上前(500.0)の CompletedWorktree が 1000 個ある
        many_old_worktrees = [
            CompletedWorktree(
                issue_number=i,
                subtask_id=f"t{i}",
                branch=f"b{i}",
                started_at=100.0,
                completed_at=500.0,
            )
            for i in range(1, 1001)
        ]
        state = RunState(completed_worktrees=many_old_worktrees)

        # open PR なし、上限500
        pruned = prune_run_state(
            state,
            now=now,
            launch_window_seconds=86400.0,
            completed_retention_seconds=2592000.0,
            max_completed_worktrees=500,
        )

        # 古い履歴はすべて削除され 0 件（有界かつ無駄に保持されない）
        assert len(pruned.completed_worktrees) == 0

    def test_prune_run_state_preserves_open_pr_latest_completed_worktree(self):
        from orchestune.dispatch_state import prune_run_state
        from orchestune.github import PrRecord

        now = 5000000.0  # min_completed_time = 2408000
        state = RunState(
            completed_worktrees=[
                # Issue 10: 30日以上前だが、現在 Open PR #101 (closes #10) が存在する
                CompletedWorktree(
                    issue_number=10,
                    subtask_id="t1",
                    branch="b10",
                    started_at=100.0,
                    completed_at=500.0,
                ),
                # Issue 20: 30日以上前で、Open PR なし (closed/merged 済み)
                CompletedWorktree(
                    issue_number=20,
                    subtask_id="t2",
                    branch="b20",
                    started_at=100.0,
                    completed_at=500.0,
                ),
                # Issue 30: 30日以内の最新 (Open PR なしでも保持)
                CompletedWorktree(
                    issue_number=30,
                    subtask_id="t3",
                    branch="b30",
                    started_at=4900000.0,
                    completed_at=4950000.0,
                ),
            ],
        )

        open_prs = [
            PrRecord(
                number=101,
                head_ref="b10",
                changed_files=(),
                review_decision="",
                is_ci_passing=True,
                closes_issue_numbers=(10,),
            )
        ]

        pruned = prune_run_state(
            state,
            now=now,
            launch_window_seconds=86400.0,
            completed_retention_seconds=2592000.0,
            open_prs=open_prs,
        )

        # Issue 10 (open PRありで保護) と Issue 30 (30日以内) が残り、Issue 20 は削除される
        assert len(pruned.completed_worktrees) == 2
        issues_in_pruned = {cw.issue_number for cw in pruned.completed_worktrees}
        assert issues_in_pruned == {10, 30}

    def test_prune_run_state_preserves_old_protected_record_over_new_unprotected_history(
        self,
    ):
        from orchestune.dispatch_state import prune_run_state
        from orchestune.github import PrRecord

        now = 5000000.0  # min_completed_time = 2408000
        # 古い保護レコード (30日以上前の open PR 用)
        old_protected = CompletedWorktree(
            issue_number=10,
            subtask_id="t10",
            branch="b10",
            started_at=100.0,
            completed_at=500.0,
        )
        # 30日以内の新しい非保護レコード 500 件
        new_unprotected = [
            CompletedWorktree(
                issue_number=1000 + i,
                subtask_id=f"t{1000 + i}",
                branch=f"b{1000 + i}",
                started_at=4900000.0 + i,
                completed_at=4900000.0 + i,
            )
            for i in range(500)
        ]
        state = RunState(completed_worktrees=[old_protected] + new_unprotected)

        open_prs = [
            PrRecord(
                number=101,
                head_ref="b10",
                changed_files=(),
                review_decision="",
                is_ci_passing=True,
                closes_issue_numbers=(10,),
            )
        ]

        pruned = prune_run_state(
            state,
            now=now,
            launch_window_seconds=86400.0,
            completed_retention_seconds=2592000.0,
            open_prs=open_prs,
            max_completed_worktrees=500,
        )

        assert len(pruned.completed_worktrees) == 500
        issues = {cw.issue_number for cw in pruned.completed_worktrees}
        # 古い保護対象の Issue 10 が削られずに確実に残っていること
        assert 10 in issues

    def test_save_run_state_prunes_automatically(self, tmp_path):
        path = tmp_path / "run_state.json"
        now = 5000000.0
        state = RunState(
            launch_history=[4000000.0, 4950000.0],  # 4000000.0 is too old for 24h
            completed_worktrees=[
                CompletedWorktree(
                    issue_number=1,
                    subtask_id="old_closed",
                    branch="b1",
                    started_at=1000.0,
                    completed_at=1000.0,  # 古い
                ),
            ],
        )

        save_run_state(state, path, now=now)
        loaded = load_run_state(path)
        assert loaded.launch_history == [4950000.0]
        assert loaded.completed_worktrees == []

    def test_last_reconciled_at_defaults_to_none(self, tmp_path):
        state = load_run_state(tmp_path / "run_state.json")
        assert state.last_reconciled_at is None

    def test_save_and_load_roundtrip_with_last_reconciled_at(self, tmp_path):
        path = tmp_path / "run_state.json"
        state = RunState(
            active_worktrees={}, launch_history=[], last_reconciled_at=1700003600.0
        )
        save_run_state(state, path)
        loaded = load_run_state(path)
        assert loaded.last_reconciled_at == 1700003600.0

    def test_load_missing_last_reconciled_at_key_defaults_to_none(self, tmp_path):
        path = tmp_path / "run_state.json"
        path.write_text(json.dumps({"active_worktrees": {}, "launch_history": []}))
        loaded = load_run_state(path)
        assert loaded.last_reconciled_at is None

    def test_load_backwards_compatibility_for_base_branch(self, tmp_path):
        path = tmp_path / "run_state.json"
        old_data = {
            "active_worktrees": {
                "10": {
                    "issue_number": 10,
                    "branch": "claude/issue-10-x",
                    "worktree_path": "worktrees/claude-issue-10-x",
                    "pid": 12345,
                    "started_at": 1700000000.0,
                    "declared_footprint": ["src/foo.py"],
                }
            },
            "launch_history": [],
            "completed_worktrees": [
                {
                    "issue_number": 11,
                    "subtask_id": "task-b",
                    "branch": "claude/issue-11-task-b",
                    "started_at": 1700000000.0,
                    "completed_at": 1700003600.0,
                }
            ],
        }
        path.write_text(json.dumps(old_data))
        loaded = load_run_state(path)
        assert loaded.active_worktrees["10"].base_branch == "origin/main"
        assert loaded.completed_worktrees[0].base_branch == "origin/main"

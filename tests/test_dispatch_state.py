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
        # issue_number=1 の唯一のレコード "old" も最新1件として保護される
        assert len(pruned.completed_worktrees) == 2

    def test_prune_run_state_preserves_latest_completed_worktree_per_issue(self):
        from orchestune.dispatch_state import prune_run_state

        now = 5000000.0  # min_completed_time = 2408000
        state = RunState(
            completed_worktrees=[
                CompletedWorktree(
                    issue_number=10,
                    subtask_id="t1",
                    branch="b10",
                    started_at=100.0,
                    completed_at=500.0,  # 古い（2件目以降）
                ),
                CompletedWorktree(
                    issue_number=10,
                    subtask_id="t1",
                    branch="b10",
                    started_at=1000.0,
                    completed_at=1500.0,  # Issue 10 の最新（30日超だが最新1件のため保護される）
                ),
                CompletedWorktree(
                    issue_number=20,
                    subtask_id="t2",
                    branch="b20",
                    started_at=4900000.0,
                    completed_at=4950000.0,  # 30日以内の最新
                ),
            ],
        )

        pruned = prune_run_state(
            state,
            now=now,
            launch_window_seconds=86400.0,
            completed_retention_seconds=2592000.0,
        )

        # issue 10 の completed_at=500.0 (古い方) は削られ、completed_at=1500.0 (最新) は保護される
        assert len(pruned.completed_worktrees) == 2
        issue_10_cw = [cw for cw in pruned.completed_worktrees if cw.issue_number == 10]
        assert len(issue_10_cw) == 1
        assert issue_10_cw[0].completed_at == 1500.0

    def test_prune_run_state_respects_custom_launch_window(self):
        from orchestune.dispatch_state import prune_run_state

        now = 100000.0
        # 48時間 = 172800秒 -> min_launch_time = 100000 - 172800 = -72800
        state = RunState(
            launch_history=[10000.0, 50000.0],
        )

        pruned = prune_run_state(
            state,
            now=now,
            launch_window_seconds=172800.0,
        )

        assert pruned.launch_history == [10000.0, 50000.0]

    def test_save_run_state_prunes_automatically(self, tmp_path):
        path = tmp_path / "run_state.json"
        now = 5000000.0
        state = RunState(
            launch_history=[4000000.0, 4950000.0],  # 4000000.0 is too old for 24h
            completed_worktrees=[
                CompletedWorktree(
                    issue_number=1,
                    subtask_id="old_first",
                    branch="b1",
                    started_at=1000.0,
                    completed_at=1000.0,  # 古い
                ),
                CompletedWorktree(
                    issue_number=1,
                    subtask_id="old_latest",
                    branch="b1",
                    started_at=2000.0,
                    completed_at=2000.0,  # 最新1件のため保護される
                ),
            ],
        )

        save_run_state(state, path, now=now)
        loaded = load_run_state(path)
        assert loaded.launch_history == [4950000.0]
        assert len(loaded.completed_worktrees) == 1
        assert loaded.completed_worktrees[0].subtask_id == "old_latest"

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

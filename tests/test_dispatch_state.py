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
        save_run_state(state, path)
        loaded = load_run_state(path)
        assert loaded.active_worktrees["10"].branch == "claude/issue-10-x"
        assert loaded.launch_history == [1700000000.0]

    def test_save_and_load_roundtrip_with_completed_worktrees(self, tmp_path):
        path = tmp_path / "run_state.json"
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
        save_run_state(state, path)
        loaded = load_run_state(path)
        assert loaded.completed_worktrees == state.completed_worktrees

    def test_load_missing_completed_worktrees_key_defaults_to_empty(self, tmp_path):
        path = tmp_path / "run_state.json"
        path.write_text(json.dumps({"active_worktrees": {}, "launch_history": []}))
        loaded = load_run_state(path)
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

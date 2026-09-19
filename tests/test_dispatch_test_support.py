"""`tests/dispatch_test_support.py`の共通ファクトリが持つ既定値の特性テスト（#916）。

各テストの期待値は、共通化前に各テストファイルへ散在していたヘルパー
（`_task` / `_active` / `_ctx` / `_full_issue` / `_issue`）の既定値を
リテラルで書き下したものである。ファクトリ側の既定値が動いた時点で
移行済みテストの前提が静かに変わるため、ここで固定する。
"""

from __future__ import annotations

from pathlib import Path

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.state import ActiveWorktree, RunState
from orchestune.models import IssueRecord, Task
from tests.conftest import make_issue
from tests.dispatch_test_support import (
    DEFAULT_CREATED_AT,
    GC_PROCESS_ALIVE_TARGETS,
    make_footprint_issue,
    make_plain_issue,
    make_state_root,
    make_test_active_worktree,
    make_test_cycle_context,
    make_test_dispatcher_config,
    make_test_task,
    patch_gc_process_alive,
    stub_forge_check_auth,
    stub_label_actor_permission,
)


class TestMakeTestTask:
    def test_defaults_match_the_pre_consolidation_cycle_helper(self):
        assert make_test_task() == Task(
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

    def test_issue_number_is_positional_and_every_field_is_overridable(self):
        task = make_test_task(
            280,
            subtask_id="task-b",
            footprint=("src/foo.py",),
            status_labels=("status:not-needed",),
            created_at="2026-01-01T00:00:00Z",
            issue_state="CLOSED",
        )
        assert task.issue_number == 280
        assert task.subtask_id == "task-b"
        assert task.footprint == ("src/foo.py",)
        assert task.status_labels == ("status:not-needed",)
        assert task.created_at == "2026-01-01T00:00:00Z"
        assert task.issue_state == "CLOSED"


class TestMakeTestActiveWorktree:
    def test_defaults_describe_a_live_worktree(self):
        assert make_test_active_worktree() == ActiveWorktree(
            issue_number=1,
            branch="claude/issue-1-task-a",
            worktree_path="worktrees/w1",
            pid=111,
            started_at=1_699_999_000.0,
            declared_footprint=(),
        )

    def test_branch_follows_the_issue_number_unless_overridden(self):
        assert make_test_active_worktree(280).branch == "claude/issue-280-task-a"
        assert make_test_active_worktree(280, branch="claude/x").branch == "claude/x"

    def test_reclaim_shaped_worktree_is_expressed_by_overrides(self):
        active = make_test_active_worktree(
            280, worktree_path="worktrees/missing-280", pid=None, started_at=1_000.0
        )
        assert (active.pid, active.started_at) == (None, 1_000.0)
        assert active.worktree_path == "worktrees/missing-280"


class TestMakeTestDispatcherConfig:
    def test_state_paths_are_placed_under_the_given_root(self, tmp_path):
        config = make_test_dispatcher_config(tmp_path)
        assert config.events_log_path == tmp_path / "events.jsonl"
        assert config.run_state_path == tmp_path / "run_state.json"
        assert config.worktree_root == tmp_path / "worktrees"

    def test_a_shared_root_is_used_when_none_is_given(self):
        config = make_test_dispatcher_config()
        assert config.events_log_path.name == "events.jsonl"
        assert config.events_log_path.parent.is_dir()

    def test_make_state_root_returns_distinct_existing_directories(self):
        first = make_state_root()
        second = make_state_root()
        assert first != second
        assert first.is_dir() and second.is_dir()
        assert isinstance(first, Path)

    def test_overrides_reach_the_dispatcher_config(self, tmp_path):
        config = make_test_dispatcher_config(tmp_path, apply=True, max_concurrent=3)
        assert isinstance(config, DispatcherConfig)
        assert config.apply is True
        assert config.max_concurrent == 3


class TestMakeTestCycleContext:
    def test_observation_containers_default_to_empty(self, tmp_path):
        ctx = make_test_cycle_context(state_root=tmp_path)
        assert ctx.tasks() == ()
        assert ctx.pull_requests() == ()
        assert ctx.issue_records() == ()
        assert ctx.config.run_state_path == tmp_path / "run_state.json"

    def test_action_port_is_unbound_unless_action_now_is_given(self, tmp_path):
        ctx = make_test_cycle_context(state_root=tmp_path)
        try:
            ctx.process_active_worktrees()
        except ValueError as exc:
            assert "no bound action adapter" in str(exc)
        else:  # pragma: no cover - defensive
            raise AssertionError("expected an unbound action port")

    def test_action_now_binds_a_cycle_action_adapter(self, tmp_path):
        ctx = make_test_cycle_context(state_root=tmp_path, action_now=2.0)
        assert ctx.process_active_worktrees() is not None

    def test_dependencies_are_resolved_only_when_requested(self, tmp_path):
        tasks = {1: make_test_task(1, subtask_id="task-1")}
        unresolved = make_test_cycle_context(state_root=tmp_path, tasks_by_issue=tasks)
        assert unresolved.dependencies_of(1) is None
        resolved = make_test_cycle_context(
            state_root=tmp_path, tasks_by_issue=tasks, resolve_dependencies=True
        )
        assert resolved.dependencies_of(1) is not None

    def test_explicit_dependency_resolution_wins_over_auto_resolution(self, tmp_path):
        tasks = {1: make_test_task(1, subtask_id="task-1")}
        ctx = make_test_cycle_context(
            state_root=tmp_path,
            tasks_by_issue=tasks,
            dependency_resolution={},
            resolve_dependencies=True,
        )
        assert ctx.dependencies_of(1) is None

    def test_run_state_override_is_visible_through_the_context(self, tmp_path):
        active = make_test_active_worktree(7)
        ctx = make_test_cycle_context(
            state_root=tmp_path,
            run_state=RunState(active_worktrees={"7": active}),
            tasks_by_issue={7: make_test_task(7)},
        )
        fact = ctx.launch_fact(7)
        assert fact is not None
        assert (fact.branch, fact.worktree_path, fact.pid) == (
            "claude/issue-7-task-a",
            "worktrees/w1",
            111,
        )


class TestMakeFootprintIssue:
    def test_it_matches_make_issue_with_the_legacy_title_and_parent(self):
        assert make_footprint_issue(1) == make_issue(
            1,
            title="t",
            labels=("status:queued",),
            footprint=("src/foo.py",),
            symbols=("foo.Foo",),
            subtask_id="task-a",
            depends_on=(),
            created_at=DEFAULT_CREATED_AT,
            parent={"number": 181},
        )

    def test_a_none_parent_number_drops_the_parent_relation(self):
        assert make_footprint_issue(1, parent_number=None).parent is None

    def test_footprint_yaml_carries_the_given_fields(self):
        issue = make_footprint_issue(
            2, footprint=("src/bar.py",), symbols=(), depends_on=("task-a",)
        )
        assert "subtask_id: task-a" in issue.body
        assert "  - src/bar.py" in issue.body
        assert "symbols:\n  []" in issue.body
        assert "depends_on:\n  - task-a" in issue.body


class TestMakePlainIssue:
    def test_it_has_no_body_so_no_footprint_is_parsed(self):
        assert make_plain_issue(3) == IssueRecord(
            number=3,
            title="Issue 3",
            body="",
            labels=(),
            created_at=DEFAULT_CREATED_AT,
            state="OPEN",
        )

    def test_labels_and_state_are_overridable(self):
        issue = make_plain_issue(4, labels=("status:done",), state="CLOSED")
        assert issue.labels == ("status:done",)
        assert issue.state == "CLOSED"


class TestPatchGcProcessAlive:
    def test_every_split_consumer_of_is_process_alive_is_patched(self):
        assert GC_PROCESS_ALIVE_TARGETS == (
            "orchestune.dispatch.execution_repair.is_process_alive",
            "orchestune.dispatch.gc.is_process_alive",
            "orchestune.dispatch.gc.completion.is_process_alive",
            "orchestune.dispatch.gc.zombies.is_process_alive",
        )

    def test_the_patched_value_is_visible_from_each_module(self):
        from orchestune.dispatch import execution_repair, gc
        from orchestune.dispatch.gc import completion, zombies

        modules = (execution_repair, gc, completion, zombies)
        with patch_gc_process_alive(return_value=False):
            assert [module.is_process_alive(1) for module in modules] == [False] * 4
        with patch_gc_process_alive(return_value=True):
            assert [module.is_process_alive(1) for module in modules] == [True] * 4


class TestForgeStubs:
    def test_actor_permission_stub_returns_an_allowed_actor(self, fake_forge):
        fake_forge.get_label_actor.side_effect = RuntimeError("gh api called")
        stub_label_actor_permission(fake_forge)
        assert fake_forge.get_label_actor(1, "status:queued") == "trusted-actor"
        assert fake_forge.get_actor_permission("trusted-actor") == "write"

    def test_actor_and_permission_are_overridable(self, fake_forge):
        stub_label_actor_permission(fake_forge, actor="bot", permission="read")
        assert fake_forge.get_label_actor(1, "status:queued") == "bot"
        assert fake_forge.get_actor_permission("bot") == "read"

    def test_check_auth_stub_clears_the_side_effect_and_returns_the_mock(
        self, fake_forge
    ):
        fake_forge.check_auth.side_effect = RuntimeError("not authenticated")
        mock_check = stub_forge_check_auth(fake_forge)
        assert mock_check is fake_forge.check_auth
        assert fake_forge.check_auth() is not None or True

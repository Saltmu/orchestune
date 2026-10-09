"""#1270: apply a claude-cli session-limit exit without spending the reclaim budget."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_events import UsageLimitCompletion
from orchestune.dispatch.gc.usage_limit import handle_usage_limit_exit
from orchestune.infra.process_utils import run_state_lock
from orchestune.ledger.run_state import (
    RunState,
    TaskReclaimRecord,
    load_run_state,
    save_run_state,
)
from tests.conftest import FakeForge, make_issue, make_task
from tests.dispatch_test_support import make_test_active_worktree

TOKYO = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 10, 10, 9, 0, tzinfo=TOKYO).timestamp()
RESET = datetime(2026, 10, 10, 13, 0, tzinfo=TOKYO).timestamp()
GRACE = 30
LIMIT_LINE = "You've hit your session limit · resets 1pm"
MODULE = "orchestune.dispatch.gc.usage_limit"


def _lock(config):
    return run_state_lock(config.run_state_path.with_suffix(".lock"))


@pytest.fixture
def world(tmp_path):
    forge = FakeForge()
    forge.issues[1] = make_issue(1, labels=("status:in-progress",))
    forge.comments[1] = []
    worktree = tmp_path / "worktrees" / "claude-issue-1-task-1"
    worktree.mkdir(parents=True)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_path = log_dir / "claude-issue-1-task-1.log"
    previous = "earlier run output\n"
    log_path.write_text(previous, encoding="utf-8")
    config = DispatcherConfig(
        parent_issue_number=100,
        forge=forge,
        apply=True,
        run_state_path=tmp_path / "run_state.json",
        worktree_root=tmp_path / "worktrees",
        log_dir=log_dir,
        events_log_path=tmp_path / "events.jsonl",
        usage_limit_timezone="Asia/Tokyo",
        max_usage_limit_retries=2,
        usage_limit_backoff_seconds=900,
        usage_limit_reset_grace_seconds=GRACE,
    )
    active = make_test_active_worktree(
        1,
        branch="claude/issue-1-task-1",
        worktree_path=str(worktree),
        pid=4242,
        started_at=NOW - 3600,
        claim_id="claim-1",
        launch_target="claude-cli",
        launch_log_path=str(log_path),
        launch_log_offset=len(previous.encode()),
    )
    state = RunState(active_worktrees={"1": active})
    with _lock(config):
        save_run_state(state, config.run_state_path, now=NOW)
    task = make_task(1, status_labels=("status:in-progress",))
    return SimpleNamespace(
        forge=forge,
        config=config,
        active=active,
        state=state,
        task=task,
        log_path=log_path,
        worktree=worktree,
    )


@pytest.fixture
def effects():
    """Dead process; WIP backup and worktree removal are observable and harmless."""
    with (
        patch(f"{MODULE}.is_process_alive", return_value=False) as alive,
        patch(f"{MODULE}.backup_wip_commit", return_value=None) as backup,
        patch(f"{MODULE}.remove_worktree") as remove,
    ):
        yield SimpleNamespace(alive=alive, backup=backup, remove=remove)


def _write_run(world, text: str) -> None:
    with open(world.log_path, "a", encoding="utf-8") as fh:
        fh.write(text)


def _handle(world, *, now: float = NOW, state=None, active=None):
    state = world.state if state is None else state
    active = world.active if active is None else active
    with _lock(world.config):
        return handle_usage_limit_exit(
            state, "1", active, world.task, world.config, now=now
        )


def _labels(world):
    return set(world.forge.issues[1].labels)


class TestSessionLimitRequeue:
    def test_known_reset_waits_until_reset_plus_grace_without_spending_reclaims(
        self, world, effects
    ):
        _write_run(world, f"working\n{LIMIT_LINE}\n")

        event = _handle(world)

        assert isinstance(event, UsageLimitCompletion)
        assert event.action == "usage_limit_requeued"
        assert (event.reset_known, event.reset_at, event.timezone) == (
            True,
            RESET,
            "Asia/Tokyo",
        )
        assert event.retry_at == RESET + GRACE
        assert event.retries_remaining == 1
        record = world.state.task_reclaim_counts[1]
        assert (
            record.usage_limit_retry_count,
            record.usage_limit_retry_at,
            record.usage_limit_retry_pending,
        ) == (1, RESET + GRACE, False)
        # The general reclaim, early-death and review-timeout budgets stay untouched.
        assert (record.count, record.early_death_retry_count) == (0, 0)
        assert record.review_timeout_retry_count == 0
        assert world.state.usage_limit_cooldowns == {"claude-cli": RESET + GRACE}
        assert "1" not in world.state.active_worktrees
        assert "status:queued" in _labels(world)
        assert "status:in-progress" not in _labels(world)
        effects.backup.assert_called_once()
        effects.remove.assert_called_once_with(str(world.worktree))

    def test_state_is_persisted_for_the_next_run(self, world, effects):
        _write_run(world, f"{LIMIT_LINE}\n")

        _handle(world)

        loaded = load_run_state(world.config.run_state_path)
        assert loaded.usage_limit_cooldowns == {"claude-cli": RESET + GRACE}
        assert loaded.task_reclaim_counts[1].usage_limit_retry_count == 1
        assert "1" not in loaded.active_worktrees

    def test_unknown_reset_uses_the_finite_backoff(self, world, effects):
        _write_run(world, "You've hit your session limit\n")

        event = _handle(world)

        assert event.reset_known is False
        assert event.reset_at is None
        assert event.retry_at == NOW + 900
        assert world.state.usage_limit_cooldowns == {"claude-cli": NOW + 900}

    def test_wall_clock_reset_without_a_timezone_is_unknown_not_utc(
        self, world, effects
    ):
        world.config.usage_limit_timezone = None
        _write_run(world, f"{LIMIT_LINE}\n")

        event = _handle(world)

        assert event.reset_known is False
        assert event.retry_at == NOW + 900

    def test_one_issue_comment_announces_the_wait_once(self, world, effects):
        _write_run(world, f"{LIMIT_LINE}\n")

        _handle(world)

        comments = [c["body"] for c in world.forge.comments[1]]
        assert len(comments) == 1
        assert "usage" in comments[0].lower() or "使用上限" in comments[0]
        # The log itself is never echoed into the Issue.
        assert "earlier run output" not in comments[0]

    def test_comment_failure_does_not_undo_the_requeue(self, world, effects):
        _write_run(world, f"{LIMIT_LINE}\n")
        with patch.object(world.forge, "add_comment", side_effect=OSError("boom")):
            event = _handle(world)

        assert event.action == "usage_limit_requeued"
        assert "status:queued" in _labels(world)
        assert world.state.task_reclaim_counts[1].usage_limit_retry_pending is False

    def test_cooldown_never_moves_earlier(self, world, effects):
        world.state.usage_limit_cooldowns["claude-cli"] = RESET + 10_000
        _write_run(world, f"{LIMIT_LINE}\n")

        _handle(world)

        assert world.state.usage_limit_cooldowns["claude-cli"] == RESET + 10_000

    def test_unrelated_records_keep_their_other_retry_state(self, world, effects):
        world.state.task_reclaim_counts[1] = TaskReclaimRecord(
            count=2, early_death_retry_count=1, review_timeout_retry_count=1
        )
        _write_run(world, f"{LIMIT_LINE}\n")

        _handle(world)

        record = world.state.task_reclaim_counts[1]
        assert (record.count, record.early_death_retry_count) == (2, 1)
        assert record.review_timeout_retry_count == 1
        assert record.usage_limit_retry_count == 1


class TestNotAUsageLimitExit:
    @pytest.mark.parametrize(
        "text",
        [
            "all done\n",
            "429 Too Many Requests\n",
            f'agent quoted: "{LIMIT_LINE}"\n',
            f"{LIMIT_LINE}\n" + "\n".join(f"line {i}" for i in range(20)) + "\n",
        ],
    )
    def test_unclassified_exits_are_left_to_the_normal_path(self, world, effects, text):
        _write_run(world, text)

        assert _handle(world) is None
        assert world.state.task_reclaim_counts == {}
        assert world.state.usage_limit_cooldowns == {}
        assert "1" in world.state.active_worktrees
        effects.remove.assert_not_called()

    def test_a_limit_message_from_an_earlier_run_is_ignored(self, world, effects):
        # The previous run's limit message sits before this run's start offset.
        previous = f"{LIMIT_LINE}\n"
        world.log_path.write_text(previous, encoding="utf-8")
        active = replace(
            world.active,
            launch=replace(
                world.active.launch, launch_log_offset=len(previous.encode())
            ),
        )
        world.state.active_worktrees["1"] = active
        _write_run(world, "normal run output, then clean exit\n")

        assert _handle(world, active=active) is None

    def test_a_live_process_is_never_classified(self, world, effects):
        effects.alive.return_value = True
        _write_run(world, f"{LIMIT_LINE}\n")

        assert _handle(world) is None

    @pytest.mark.parametrize(
        "launch_changes",
        [
            {"launch_target": "codex-cli"},
            {"launch_target": None},
            {"launch_log_path": None},
            {"launch_log_offset": None},
            {"external_id": "cloud-1"},
            {"pid": None},
        ],
    )
    def test_runs_without_claude_cli_attribution_are_not_classified(
        self, world, effects, launch_changes
    ):
        _write_run(world, f"{LIMIT_LINE}\n")
        active = replace(
            world.active, launch=replace(world.active.launch, **launch_changes)
        )

        assert _handle(world, active=active) is None

    def test_interactive_claims_are_excluded(self, world, effects):
        _write_run(world, f"{LIMIT_LINE}\n")
        active = replace(
            world.active, claim=replace(world.active.claim, owner_kind="interactive")
        )

        assert _handle(world, active=active) is None

    @pytest.mark.parametrize("problem", ["missing", "truncated", "directory"])
    def test_unreadable_or_replaced_logs_are_unknown(self, world, effects, problem):
        _write_run(world, f"{LIMIT_LINE}\n")
        if problem == "missing":
            world.log_path.unlink()
        elif problem == "truncated":
            world.log_path.write_text("x", encoding="utf-8")
        else:
            world.log_path.unlink()
            world.log_path.mkdir()

        assert _handle(world) is None
        assert world.state.usage_limit_cooldowns == {}

    def test_only_a_bounded_tail_of_a_huge_log_is_read(self, world, effects):
        from orchestune.dispatch.usage_limit import (
            LOG_TAIL_MAX_BYTES,
            sanitize_log_tail,
        )

        _write_run(world, "x" * 3_000_000 + f"\n{LIMIT_LINE}\n")
        seen: list[int] = []

        def spy(raw, **kwargs):
            seen.append(len(raw))
            return sanitize_log_tail(raw, **kwargs)

        with patch(f"{MODULE}.sanitize_log_tail", side_effect=spy):
            event = _handle(world)

        assert event is not None
        assert seen and max(seen) <= LOG_TAIL_MAX_BYTES


class TestRetryBudget:
    def test_exhausted_budget_escalates_as_a_usage_limit_stop(self, world, effects):
        world.config.max_usage_limit_retries = 2
        world.state.task_reclaim_counts[1] = TaskReclaimRecord(
            usage_limit_retry_count=2
        )
        _write_run(world, f"{LIMIT_LINE}\n")

        event = _handle(world)

        assert event.action == "usage_limit_escalated"
        assert event.retries_remaining == 0
        assert "status:blocked-human-review" in _labels(world)
        assert "status:queued" not in _labels(world)
        # The cause is stated and the work is kept for a human.
        comment = world.forge.comments[1][-1]["body"]
        assert "使用上限" in comment or "usage limit" in comment.lower()
        effects.remove.assert_not_called()
        assert world.state.task_reclaim_counts[1].count == 0
        assert "1" not in world.state.active_worktrees
        # Other claude-cli tasks still wait for the limit to lift.
        assert world.state.usage_limit_cooldowns["claude-cli"] > NOW

    def test_zero_retries_never_relaunches_automatically(self, world, effects):
        world.config.max_usage_limit_retries = 0
        _write_run(world, f"{LIMIT_LINE}\n")

        event = _handle(world)

        assert event.action == "usage_limit_escalated"
        assert "status:queued" not in _labels(world)

    def test_two_retries_allow_exactly_two_additional_launches(self, world, effects):
        actions = []
        for attempt in range(3):
            active = replace(
                world.active,
                launch=replace(world.active.launch, started_at=NOW - 3600 + attempt),
            )
            world.state.active_worktrees["1"] = active
            world.forge.issues[1] = make_issue(1, labels=("status:in-progress",))
            _write_run(world, f"{LIMIT_LINE}\n")
            world.active = active
            event = _handle(world, now=NOW + attempt)
            actions.append(event.action)
            # Advance the run's log window like a fresh launch would.
            world.active = replace(
                active,
                launch=replace(
                    active.launch, launch_log_offset=world.log_path.stat().st_size
                ),
            )

        assert actions == [
            "usage_limit_requeued",
            "usage_limit_requeued",
            "usage_limit_escalated",
        ]


class TestPersistenceAndRecovery:
    def test_a_failed_ledger_save_changes_nothing(self, world, effects):
        _write_run(world, f"{LIMIT_LINE}\n")

        def failing_save(*args, **kwargs):
            raise OSError("disk full")

        with patch(f"{MODULE}.save_run_state", side_effect=failing_save):
            event = _handle(world)

        assert event.action == "usage_limit_held"
        assert world.state.task_reclaim_counts == {}
        assert world.state.usage_limit_cooldowns == {}
        assert "status:in-progress" in _labels(world)
        effects.backup.assert_not_called()
        effects.remove.assert_not_called()
        assert "1" in world.state.active_worktrees

    def test_label_failure_keeps_the_reservation_and_resume_does_not_double_count(
        self, world, effects
    ):
        _write_run(world, f"{LIMIT_LINE}\n")
        with patch.object(world.forge, "add_label", side_effect=OSError("api down")):
            held = _handle(world)

        assert held.action == "usage_limit_held"
        record = world.state.task_reclaim_counts[1]
        assert (record.usage_limit_retry_count, record.usage_limit_retry_pending) == (
            1,
            True,
        )
        # Persisted before the label change: a restart sees it.
        saved = load_run_state(world.config.run_state_path)
        assert saved.task_reclaim_counts[1].usage_limit_retry_pending is True
        assert saved.usage_limit_cooldowns

        resumed = _handle(world, now=NOW + 60)

        assert resumed.action == "usage_limit_requeued"
        record = world.state.task_reclaim_counts[1]
        assert (record.usage_limit_retry_count, record.usage_limit_retry_pending) == (
            1,
            False,
        )

    def test_a_restart_between_reservation_and_label_resumes_from_the_ledger(
        self, world, effects
    ):
        _write_run(world, f"{LIMIT_LINE}\n")
        with patch.object(world.forge, "add_label", side_effect=OSError("api down")):
            _handle(world)

        restarted = load_run_state(world.config.run_state_path)
        resumed = _handle(world, now=NOW + 60, state=restarted)

        assert resumed.action == "usage_limit_requeued"
        assert restarted.task_reclaim_counts[1].usage_limit_retry_count == 1

    def test_a_pending_reservation_of_another_run_does_not_hide_a_new_failure(
        self, world, effects
    ):
        world.state.task_reclaim_counts[1] = TaskReclaimRecord(
            usage_limit_retry_count=1,
            usage_limit_retry_at=NOW - 10,
            usage_limit_retry_pending=True,
            usage_limit_retry_run="claim-0:1:1.0",
        )
        _write_run(world, f"{LIMIT_LINE}\n")

        event = _handle(world)

        assert event.action == "usage_limit_requeued"
        assert world.state.task_reclaim_counts[1].usage_limit_retry_count == 2

    def test_wip_backup_failure_keeps_the_worktree_and_the_reservation(
        self, world, effects
    ):
        effects.backup.return_value = "git add failed"
        _write_run(world, f"{LIMIT_LINE}\n")

        event = _handle(world)

        assert event.action == "usage_limit_held"
        effects.remove.assert_not_called()
        assert "status:in-progress" in _labels(world)
        assert world.state.task_reclaim_counts[1].usage_limit_retry_pending is True
        assert world.worktree.exists()

    def test_missing_worktree_directory_still_requeues(self, world, effects):
        world.worktree.rmdir()
        _write_run(world, f"{LIMIT_LINE}\n")

        event = _handle(world)

        assert event.action == "usage_limit_requeued"
        effects.backup.assert_not_called()


class TestDryRun:
    def test_dry_run_reports_without_touching_ledger_forge_or_worktree(
        self, world, effects
    ):
        world.config.apply = False
        _write_run(world, f"{LIMIT_LINE}\n")
        before = world.config.run_state_path.read_text()

        event = _handle(world)

        assert event.action == "usage_limit_requeued"
        assert event.retry_at == RESET + GRACE
        assert world.state.task_reclaim_counts == {}
        assert world.state.usage_limit_cooldowns == {}
        assert "1" in world.state.active_worktrees
        assert world.config.run_state_path.read_text() == before
        assert "status:in-progress" in _labels(world)
        assert world.forge.comments[1] == []
        effects.backup.assert_not_called()
        effects.remove.assert_not_called()


class TestResumeWithoutTheLog:
    def test_a_saved_reservation_of_the_same_run_resumes_after_log_loss(
        self, world, effects
    ):
        _write_run(world, f"{LIMIT_LINE}\n")
        with patch.object(world.forge, "add_label", side_effect=OSError("api down")):
            _handle(world)
        world.log_path.unlink()

        resumed = _handle(world, now=NOW + 60)

        assert resumed.action == "usage_limit_requeued"
        assert world.state.task_reclaim_counts[1].usage_limit_retry_count == 1

    def test_without_a_saved_reservation_a_missing_log_stays_unknown(
        self, world, effects
    ):
        world.log_path.unlink()

        assert _handle(world) is None


class TestTypedReclaimDiversion:
    @staticmethod
    def _command():
        from orchestune.consistency.invariants.execution import LOCAL_PROCESS_DEAD
        from orchestune.consistency.models import ConsistencyScope, RepairCommand
        from orchestune.consistency.repairs.execution import COMMAND_RECLAIM

        return RepairCommand(
            code=COMMAND_RECLAIM,
            scope=ConsistencyScope.TASK,
            subject_id="1",
            idempotency_key="execution:1:reclaim",
            parameters=(("finding_codes", (LOCAL_PROCESS_DEAD,)),),
        )

    def _run(self, world, now=NOW):
        from orchestune.dispatch.gc.usage_limit import handle_usage_limit_reclaim

        events: list = []
        with _lock(world.config):
            result = handle_usage_limit_reclaim(
                self._command(),
                world.state,
                {1: world.task},
                world.config,
                events,
                (),
                frozenset(),
                now,
            )
        return result, events

    def test_a_dead_process_that_hit_the_limit_is_not_a_crash_reclaim(
        self, world, effects
    ):
        from orchestune.consistency.models import RepairStatus

        _write_run(world, f"{LIMIT_LINE}\n")

        result, events = self._run(world)

        assert result.status is RepairStatus.APPLIED
        assert [e.action for e in events] == ["usage_limit_requeued"]
        assert world.state.task_reclaim_counts[1].count == 0
        assert "status:queued" in _labels(world)

    def test_a_crash_without_a_limit_message_falls_through(self, world, effects):
        _write_run(world, "Segmentation fault\n")

        result, events = self._run(world)

        assert result is None
        assert events == []
        assert world.state.task_reclaim_counts == {}

    def test_other_finding_codes_are_not_diverted(self, world, effects):
        from orchestune.consistency.models import RepairCommand

        _write_run(world, f"{LIMIT_LINE}\n")
        command = replace(self._command(), parameters=(("finding_codes", ()),))
        assert isinstance(command, RepairCommand)
        from orchestune.dispatch.gc.usage_limit import handle_usage_limit_reclaim

        with _lock(world.config):
            result = handle_usage_limit_reclaim(
                command,
                world.state,
                {1: world.task},
                world.config,
                [],
                (),
                frozenset(),
                NOW,
            )
        assert result is None

    def test_held_worktrees_are_left_alone(self, world, effects):
        from orchestune.dispatch.gc.usage_limit import handle_usage_limit_reclaim

        _write_run(world, f"{LIMIT_LINE}\n")
        with _lock(world.config):
            result = handle_usage_limit_reclaim(
                self._command(),
                world.state,
                {1: world.task},
                world.config,
                [],
                (),
                frozenset({world.active.core.worktree_path}),
                NOW,
            )
        assert result is None
        assert world.state.task_reclaim_counts == {}


class TestThroughTheDispatchCycle:
    """The whole cycle: classification happens before dirty / no-outcome handling."""

    @staticmethod
    def _seed(tmp_path, run_state_path, *, dirty_commits: bool):
        from orchestune.ledger.run_state import RunState
        from tests.dispatch_test_support import flat_active_worktree
        from tests.dispatch_test_support import save_locked_run_state as save_state

        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        log_path = log_dir / "claude-issue-1-task-a.log"
        previous = b"previous run\n"
        log_path.write_bytes(previous + b"work\n" + f"{LIMIT_LINE}\n".encode())
        worktree = tmp_path / "w1"
        worktree.mkdir()
        save_state(
            RunState(
                active_worktrees={
                    "1": flat_active_worktree(
                        issue_number=1,
                        branch="claude/issue-1-task-a",
                        worktree_path=str(worktree),
                        pid=111,
                        started_at=time.time() - 7200,
                        declared_footprint=("src/foo.py",),
                        launch_target="claude-cli",
                        launch_log_path=str(log_path),
                        launch_log_offset=len(previous),
                    )
                },
                task_reclaim_counts={1: TaskReclaimRecord(count=2)},
            ),
            run_state_path,
        )
        return log_dir

    @pytest.mark.parametrize("dirty_commits", [False, True])
    def test_limit_exit_is_requeued_even_for_dirty_worktrees_with_commits(
        self, tmp_path, fake_forge, dirty_commits
    ):
        from orchestune.dispatch.cycle import run_dispatch_cycle
        from tests.dispatch_test_support import (
            make_footprint_issue,
            patch_gc_process_alive,
            stub_label_actor_permission,
        )

        stub_label_actor_permission(fake_forge)
        run_state_path = tmp_path / "run_state.json"
        log_dir = self._seed(tmp_path, run_state_path, dirty_commits=dirty_commits)
        config = DispatcherConfig(
            parent_issue_number=100,
            max_concurrent=1,
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            log_dir=log_dir,
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
            max_task_reclaims=3,
            usage_limit_timezone="Asia/Tokyo",
            forge=fake_forge,
        )
        issue = make_footprint_issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        fake_forge.list_issues_by_label.side_effect = lambda label, **_: (
            [issue] if label == "status:in-progress" else []
        )
        fake_forge.list_open_prs.return_value = []
        fake_forge.list_prs.return_value = []
        fake_forge.list_comments.return_value = []

        with (
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches",
                autospec=True,
                return_value=[],
            ),
            patch_gc_process_alive(return_value=False),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                autospec=True,
                return_value=dirty_commits,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                autospec=True,
                return_value=dirty_commits,
            ),
            patch(f"{MODULE}.backup_wip_commit", return_value=None),
            patch(f"{MODULE}.remove_worktree"),
        ):
            report = run_dispatch_cycle(config)

        actions = [event.to_dict()["action"] for event in report.completion_events]
        assert "usage_limit_requeued" in actions
        assert "completed_without_outcome" not in actions
        assert "completion_skipped_dirty_worktree" not in actions
        saved = load_run_state(run_state_path)
        record = saved.task_reclaim_counts[1]
        assert record.count == 2  # the general reclaim budget is untouched
        assert record.usage_limit_retry_count == 1
        assert "claude-cli" in saved.usage_limit_cooldowns
        assert "1" not in saved.active_worktrees
        fake_forge.add_label.assert_any_call(1, "status:queued")

"""Tests for pre-scheduling integration boundary and Dispatcher 4-stage execution (#1377)."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from orchestune.dispatch import dispatcher
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle import CycleReport
from orchestune.dispatch.cycle_execution import CycleStage, execute_pipeline
from orchestune.dispatch.result import PhaseResult, PhaseStatus
from orchestune.infra.process_utils import is_run_state_lock_held, run_state_lock
from tests.conftest import FakeForge


def _join_all(threads: list[threading.Thread]) -> None:
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)


def test_dispatcher_executes_four_stages_in_order(tmp_path, monkeypatch):
    """Dispatcher must execute: pre_integration cycle -> integration boundary -> scheduling cycle -> post-cycle."""
    config = DispatcherConfig(
        parent_issue_number=1,
        forge=FakeForge(),
        run_state_path=tmp_path / "run_state.json",
        events_log_path=tmp_path / "events.jsonl",
        apply=True,
    )

    call_order: list[str] = []

    def fake_pre_integration_cycle(cfg):
        call_order.append("pre_integration_cycle")
        return SimpleNamespace(
            skips=[],
            scheduling_decisions=[],
            forge_warnings=[],
            applied=True,
            quota_slots_available=1,
            selected=[],
            lock_changes={"to_lock": [], "to_unlock": []},
            deviation_events=[],
            completion_events=[],
            promotion_events=[],
            external_lock_conflicts={},
            execution_selections={},
            consistency=MagicMock(mode="off"),
        )

    def fake_scheduling_cycle(cfg):
        call_order.append("scheduling_cycle")
        return SimpleNamespace(
            skips=[],
            scheduling_decisions=[],
            forge_warnings=[],
            applied=True,
            quota_slots_available=1,
            selected=[],
            lock_changes={"to_lock": [], "to_unlock": []},
            deviation_events=[],
            completion_events=[],
            promotion_events=[],
            external_lock_conflicts={},
            execution_selections={},
            consistency=MagicMock(mode="off"),
        )

    def probe_integration_step(name: str) -> PhaseResult:
        call_order.append(name)
        return PhaseResult(name, PhaseStatus.SUCCESS)

    def probe_post_cycle_step(name: str) -> PhaseResult:
        call_order.append(name)
        return PhaseResult(name, PhaseStatus.SUCCESS)

    # Monkeypatch the dispatcher and integration steps
    from orchestune.dispatch import cycle

    monkeypatch.setattr(
        cycle, "run_pre_integration_cycle", fake_pre_integration_cycle, raising=False
    )
    monkeypatch.setattr(
        dispatcher,
        "run_pre_integration_cycle",
        fake_pre_integration_cycle,
        raising=False,
    )
    monkeypatch.setattr(dispatcher, "run_dispatch_cycle", fake_scheduling_cycle)

    monkeypatch.setattr(
        dispatcher,
        "_pre_scheduling_integration_steps",
        lambda *a, **k: [
            (
                "poll_pending_not_needed_reviews",
                lambda: probe_integration_step("poll_pending_not_needed_reviews"),
                True,
            ),
            (
                "advance_pending_semantic_reviews",
                lambda: probe_integration_step("advance_pending_semantic_reviews"),
                True,
            ),
            (
                "run_semantic_integrator",
                lambda: probe_integration_step("run_semantic_integrator"),
                True,
            ),
        ],
        raising=False,
    )
    monkeypatch.setattr(
        dispatcher,
        "_post_cycle_steps",
        lambda *a, **k: [
            (
                "process_parent_completion",
                lambda: probe_post_cycle_step("process_parent_completion"),
                True,
            ),
            (
                "post_event_log_comment",
                lambda: probe_post_cycle_step("post_event_log_comment"),
                True,
            ),
            (
                "post_finding_notices",
                lambda: probe_post_cycle_step("post_finding_notices"),
                True,
            ),
        ],
    )

    dispatcher._run_dispatcher(config)
    assert call_order == [
        "pre_integration_cycle",
        "poll_pending_not_needed_reviews",
        "advance_pending_semantic_reviews",
        "run_semantic_integrator",
        "scheduling_cycle",
        "process_parent_completion",
        "post_event_log_comment",
        "post_finding_notices",
    ]


def test_run_state_lock_is_not_held_during_integration_boundary(tmp_path, monkeypatch):
    """The run_state lock must be released between pre_integration cycle and scheduling cycle."""
    lock = tmp_path / "run_state.lock"
    config = DispatcherConfig(
        parent_issue_number=1,
        forge=FakeForge(),
        run_state_path=tmp_path / "run_state.json",
        events_log_path=tmp_path / "events.jsonl",
        apply=True,
    )
    observed: dict[str, object] = {}

    def fake_cycle(cfg):
        with run_state_lock(lock):
            pass
        return SimpleNamespace(
            skips=[],
            scheduling_decisions=[],
            forge_warnings=[],
            applied=True,
            quota_slots_available=1,
            selected=[],
            lock_changes={"to_lock": [], "to_unlock": []},
            deviation_events=[],
            completion_events=[],
            promotion_events=[],
            external_lock_conflicts={},
            execution_selections={},
            consistency=MagicMock(mode="off"),
        )

    def probe_integration() -> PhaseResult:
        observed["held"] = is_run_state_lock_held(lock)
        outcome: list[bool] = []

        def other_thread() -> None:
            try:
                with run_state_lock(lock, timeout=0):
                    outcome.append(True)
            except RuntimeError:
                outcome.append(False)

        _join_all([threading.Thread(target=other_thread)])
        observed["other_thread_acquired"] = outcome
        return PhaseResult("run_semantic_integrator", PhaseStatus.SUCCESS)

    from orchestune.dispatch import cycle

    monkeypatch.setattr(cycle, "run_pre_integration_cycle", fake_cycle, raising=False)
    monkeypatch.setattr(
        dispatcher, "run_pre_integration_cycle", fake_cycle, raising=False
    )
    monkeypatch.setattr(dispatcher, "run_dispatch_cycle", fake_cycle)

    monkeypatch.setattr(
        dispatcher,
        "_pre_scheduling_integration_steps",
        lambda *a, **k: [("run_semantic_integrator", probe_integration, True)],
        raising=False,
    )
    monkeypatch.setattr(dispatcher, "_post_cycle_steps", lambda *a, **k: [])

    dispatcher._run_dispatcher(config)
    assert observed == {"held": False, "other_thread_acquired": [True]}


def test_execute_pipeline_pre_integration_skips_scheduling():
    """`execute_pipeline` with CycleStage.PRE_INTEGRATION must not call reconcile_before_scheduling or schedule."""
    dependencies = MagicMock()
    ctx = MagicMock()
    issues = MagicMock()
    run_state = MagicMock()
    config = MagicMock()
    config.progress = MagicMock()
    repair_cycle = MagicMock()

    active_mock = MagicMock()
    active_mock.completion_events = ["comp-1"]
    active_mock.deviation_events = ["dev-1"]
    ctx.process_active_worktrees.return_value = active_mock
    dependencies.run_gc.return_value = ["comp-1", "comp-gc"]

    execute_pipeline(
        dependencies=dependencies,
        ctx=ctx,
        issues=issues,
        run_state=run_state,
        config=config,
        now=100.0,
        repair_cycle=repair_cycle,
        prior_events=(),
        stage=CycleStage.PRE_INTEGRATION,
    )

    # reconcile_before_scheduling and schedule must NOT be called
    dependencies.reconcile_before_scheduling.assert_not_called()
    dependencies.schedule.assert_not_called()
    # build_report must be called
    dependencies.build_report.assert_called_once()


def test_scheduling_cycle_lock_contended_defers_scheduling_and_exits_code_2(
    tmp_path, monkeypatch
):
    """When scheduling cycle encounters state lock contention, it defers scheduling, runs post-cycle, and exits 2."""
    config = DispatcherConfig(
        parent_issue_number=1,
        forge=FakeForge(),
        run_state_path=tmp_path / "run_state.json",
        events_log_path=tmp_path / "events.jsonl",
        report_path=tmp_path / "result.json",
        apply=True,
    )
    post_cycle_called = []

    pre_report = SimpleNamespace(
        stage="pre_integration",
        skips=[],
        scheduling_decisions=[],
        forge_warnings=[],
        applied=True,
        quota_slots_available=1,
        selected=[],
        lock_changes={"to_lock": [], "to_unlock": []},
        deviation_events=[],
        completion_events=[],
        promotion_events=[],
        external_lock_conflicts={},
        execution_selections={},
        consistency=MagicMock(mode="off"),
    )

    monkeypatch.setattr(dispatcher, "run_pre_integration_cycle", lambda cfg: pre_report)

    def lock_contended_cycle(cfg):
        raise RuntimeError("Another process is currently holding the lock")

    monkeypatch.setattr(dispatcher, "run_dispatch_cycle", lock_contended_cycle)
    monkeypatch.setattr(
        dispatcher,
        "_pre_scheduling_integration_steps",
        lambda *a, **k: [
            (
                "run_semantic_integrator",
                lambda: PhaseResult("run_semantic_integrator", PhaseStatus.SUCCESS),
                True,
            )
        ],
    )
    monkeypatch.setattr(
        dispatcher,
        "_post_cycle_steps",
        lambda *a, **k: [
            (
                "post_event_log_comment",
                lambda: post_cycle_called.append(True)
                or PhaseResult("post_event_log_comment", PhaseStatus.SUCCESS),
                True,
            )
        ],
    )

    run_result = dispatcher._run_dispatcher(config)
    assert run_result.lock_contended is True
    assert run_result.scheduling_deferred_reason == "state_lock_contended"
    assert post_cycle_called == [True]
    code = dispatcher._post_cycle_exit_code(
        run_result.post_cycle_results, lock_contended=run_result.lock_contended
    )
    assert code == 2


def test_post_event_log_combines_pre_integration_and_scheduling_events(
    tmp_path, fake_forge
):
    """Event log comment merges completion and deviation events from pre-integration and scheduling cycles."""
    from orchestune.dispatch.cycle_events import RecomputedDeviation, WorktreeCompletion
    from orchestune.dispatch.postcycle import _post_event_log_comment

    comp1 = WorktreeCompletion(
        issue_number=1,
        subtask_id="t1",
        worktree_path="wt1",
        action="reclaimed_worktree",
    )
    comp2 = WorktreeCompletion(
        issue_number=2,
        subtask_id="t2",
        worktree_path="wt2",
        action="reclaimed_worktree",
    )
    dev1 = RecomputedDeviation(issue_number=1, deviated_files=("a.py",), conflicts=())

    pre_report = CycleReport(
        stage="pre_integration",
        selected=[],
        deviation_events=[dev1],
        completion_events=[comp1],
        promotion_events=[],
        skips=[],
        forge_warnings=[],
        scheduling_decisions=[],
        applied=True,
        quota_slots_available=1,
        lock_changes={"to_lock": [], "to_unlock": []},
    )

    sched_report = CycleReport(
        stage="scheduling",
        selected=[],
        deviation_events=[],
        completion_events=[comp2],
        promotion_events=[],
        skips=[],
        forge_warnings=[],
        scheduling_decisions=[],
        applied=True,
        quota_slots_available=1,
        lock_changes={"to_lock": [], "to_unlock": []},
    )

    config = DispatcherConfig(
        parent_issue_number=100,
        forge=fake_forge,
        events_log_path=tmp_path / "events.jsonl",
        run_state_path=tmp_path / "run_state.json",
        worktree_root=tmp_path / "worktrees",
    )

    result = _post_event_log_comment(
        config=config,
        report=sched_report,
        pre_integration_report=pre_report,
    )
    assert result.status == PhaseStatus.SUCCESS
    assert fake_forge.add_comment.call_count == 1
    comment_body = fake_forge.add_comment.call_args.args[1]
    # Check that both completions are present in the comment
    assert "#1" in comment_body
    assert "#2" in comment_body

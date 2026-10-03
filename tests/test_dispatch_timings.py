"""Cycle timings use existing notifications and never change business outcomes."""

import json
import subprocess
from unittest.mock import Mock, patch

import pytest

from orchestune.dispatch.progress import StdoutProgress
from orchestune.dispatch.timings import (
    CycleTimingCollector,
    TimingProgress,
    timing_snapshot,
)


def collector_at(values):
    ticks = iter(values)
    return CycleTimingCollector(clock=lambda: next(ticks))


@pytest.mark.uses_real_forge
def test_cycle_gh_counts_are_isolated_and_freeze_before_event_io(tmp_path, fake_forge):
    from orchestune.dispatch import cycle
    from orchestune.dispatch.config import DispatcherConfig
    from orchestune.forge import GitHubForge

    original_fetch = cycle._fetch_issues
    original_append = cycle.append_event_log
    config = DispatcherConfig(
        parent_issue_number=1164,
        forge=fake_forge,
        apply=True,
        run_state_path=tmp_path / "state.json",
        events_log_path=tmp_path / "events.jsonl",
    )

    def fetch(config):
        GitHubForge()._run(["gh", "api", "test"])
        return original_fetch(config)

    def append(entry, path):
        GitHubForge()._run(["gh", "api", "excluded-event-io"])
        original_append(entry, path)

    with (
        patch("orchestune.dispatch.cycle.ensure_parent_branch_ready"),
        patch("orchestune.dispatch.cycle._fetch_issues", side_effect=fetch),
        patch("orchestune.dispatch.cycle.append_event_log", side_effect=append),
        patch(
            "orchestune.forge.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, ""),
        ),
        patch("orchestune.infra.command_metrics._clock", side_effect=[10, 12] * 4),
    ):
        cycle.run_dispatch_cycle(config)
        GitHubForge()._run(["gh", "api", "post-cycle"])
        cycle.run_dispatch_cycle(config)
    entries = [
        json.loads(line) for line in config.events_log_path.read_text().splitlines()
    ]
    assert len(entries) == 2
    assert [entry["timings"]["gh"] for entry in entries] == [
        {"calls": 1, "seconds": 2.0},
        {"calls": 1, "seconds": 2.0},
    ]


@pytest.mark.parametrize(
    "status,expected", [("success", 0), ("retryable_failure", 2), ("fatal_failure", 1)]
)
def test_metrics_failure_preserves_cli_exit_and_saved_json(
    tmp_path, fake_forge, status, expected
):
    from orchestune.dispatch import dispatcher
    from orchestune.dispatch.config import DispatcherConfig
    from orchestune.dispatch.report import _report_to_dict
    from orchestune.dispatch.result import PhaseResult, PhaseStatus

    config = DispatcherConfig(
        parent_issue_number=1164,
        forge=fake_forge,
        apply=True,
        run_state_path=tmp_path / "state.json",
        events_log_path=tmp_path / "events.jsonl",
        report_path=tmp_path / "result.json",
    )
    phase = PhaseResult("post", PhaseStatus(status))
    with (
        patch(
            "orchestune.dispatch.dispatcher.load_and_resolve_config",
            return_value=config,
        ),
        patch("orchestune.dispatch.cycle.ensure_parent_branch_ready"),
        patch(
            "orchestune.dispatch.cycle_execution.CycleTimingCollector",
            side_effect=RuntimeError("metrics"),
        ),
        patch(
            "orchestune.dispatch.dispatcher._decide_semantic_review_enabled",
            return_value=False,
        ),
        patch(
            "orchestune.dispatch.dispatcher._post_cycle_steps",
            return_value=[("post", lambda: phase, True)],
        ),
        patch("orchestune.dispatch.dispatcher._emit_dispatcher_report") as emit,
    ):
        assert dispatcher.main([]) == expected
    saved = json.loads(config.report_path.read_text())
    result = emit.call_args.args[0]
    assert saved == {
        **_report_to_dict(result.report),
        "post_cycle_results": [phase.to_dict()],
    }
    assert (
        json.loads(config.events_log_path.read_text())["timings"]["collection_status"]
        == "unavailable"
    )


def test_nested_phases_and_repeated_launches_are_inclusive_and_frozen():
    collector = collector_at([0, 1, 2, 5, 6, 8, 9, 10])
    collector.observe("cycle", "started")
    collector.observe("scheduling", "started")
    collector.observe("task_launch", "started", task_issue=1)
    collector.observe("task_launch", "launched", task_issue=1)
    collector.observe("task_launch", "started", task_issue=2)
    collector.observe("task_launch", "held", task_issue=2)
    collector.observe("scheduling", "completed")
    collector.observe("events_record", "started")
    snapshot = collector.snapshot()
    assert snapshot["elapsed_seconds"] == 10
    assert snapshot["phases"] == {
        "scheduling": {"seconds": 8, "count": 1},
        "task_launch": {"seconds": 5, "count": 2},
    }
    assert snapshot["collection_status"] == "ok"
    collector.observe("events_record", "completed")
    collector.observe("cycle", "completed")
    collector.gh_started()
    collector.gh_finished(100)
    assert collector.snapshot() == snapshot


@pytest.mark.parametrize(
    "terminal", ["completed", "failed", "held", "unknown", "launched", "warning"]
)
def test_existing_terminal_events_close_started_intervals(terminal):
    collector = collector_at([0, 1, 4, 5])
    collector.observe("cycle", "started")
    collector.observe("task_launch", "started", task_issue=42)
    collector.observe("task_launch", terminal, task_issue=42)
    collector.observe("events_record", "started")
    assert collector.snapshot()["phases"]["task_launch"] == {"seconds": 3, "count": 1}


def test_unstarted_planned_skipped_and_terminal_notifications_are_not_durations():
    collector = collector_at([0, 5])
    collector.observe("cycle", "started")
    collector.observe("task_launch", "planned", task_issue=1)
    collector.observe("task_launch", "skipped", task_issue=1)
    collector.observe("unstarted", "completed")
    collector.observe("events_record", "started")
    assert collector.snapshot()["phases"] == {}


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1])
def test_invalid_clock_and_incomplete_intervals_are_missing_not_zero(bad):
    collector = collector_at([0, 1, bad, 5])
    collector.observe("cycle", "started")
    collector.observe("issues_fetch", "started")
    collector.observe("issues_fetch", "completed")
    collector.observe("events_record", "started")
    result = collector.snapshot()
    assert result["collection_status"] == "partial"
    assert "issues_fetch" not in result["phases"]
    json.dumps(result, allow_nan=False)


def test_open_interval_at_cutoff_is_partial():
    collector = collector_at([0, 1, 5])
    collector.observe("cycle", "started")
    collector.observe("task_launch", "started", task_issue=42)
    collector.observe("events_record", "started")
    assert collector.snapshot()["collection_status"] == "partial"
    assert collector.snapshot()["phases"] == {}


def test_timing_progress_forwards_once_and_survives_collector_failure():
    collector = collector_at([0, 2])
    sink = Mock()
    progress = TimingProgress(sink, collector)
    with patch.object(collector, "observe", side_effect=RuntimeError("metrics")):
        progress.emit("cycle", "started", task_issue=42, reason="existing")
    sink.emit.assert_called_once_with(
        "cycle", "started", task_issue=42, reason="existing"
    )
    assert timing_snapshot(collector)["collection_status"] != "ok"


def test_snapshot_exception_or_unserializable_value_has_safe_fallback():
    collector = collector_at([])
    for result in [RuntimeError("snapshot"), {"bad": object()}]:
        with patch.object(
            collector,
            "snapshot",
            **(
                {"side_effect": result}
                if isinstance(result, Exception)
                else {"return_value": result}
            ),
        ):
            snapshot = timing_snapshot(collector)
        assert snapshot["collection_status"] == "unavailable"
        assert snapshot["elapsed_seconds"] is None
        assert snapshot["gh"] == {"calls": None, "seconds": None}
        json.dumps(snapshot, allow_nan=False)


def test_broken_stdout_does_not_disable_collection(capsys):
    stream = Mock()
    stream.write.side_effect = BrokenPipeError("closed")
    collector = collector_at([0, 1, 2, 3])
    sink = TimingProgress(StdoutProgress("run", 1164, True, stream=stream), collector)
    sink.emit("cycle", "started")
    sink.emit("state_load", "started")
    sink.emit("state_load", "completed")
    sink.emit("events_record", "started")
    assert collector.snapshot()["phases"]["state_load"]["seconds"] == 1
    assert capsys.readouterr().err.count("progress unavailable") == 1


def test_cycle_integration_preserves_report_progress_and_old_event_fields(
    tmp_path, fake_forge
):
    from orchestune.dispatch.config import DispatcherConfig
    from orchestune.dispatch.cycle import run_dispatch_cycle
    from orchestune.dispatch.cycle_report import build_event_log_entry
    from orchestune.dispatch.report import _report_to_dict

    progress = Mock()
    config = DispatcherConfig(
        parent_issue_number=1164,
        forge=fake_forge,
        progress=progress,
        run_state_path=tmp_path / "state.json",
        events_log_path=tmp_path / "events.jsonl",
        apply=True,
        worktree_root=tmp_path / "worktrees",
    )
    with patch("orchestune.dispatch.cycle.ensure_parent_branch_ready"):
        report = run_dispatch_cycle(config)
    entry = json.loads(config.events_log_path.read_text())
    timings = entry.pop("timings")
    assert entry == build_event_log_entry(report, entry["timestamp"])
    assert timings["scope"] == "cycle_before_events_record"
    assert timings["collection_status"] == "ok"
    assert timings["gh"] == {"calls": 0, "seconds": 0.0}
    assert {
        "state_lock",
        "state_load",
        "scheduling",
        "consistency_postprocessing",
    } <= timings["phases"].keys()
    assert not {"cycle", "events_record"} & timings["phases"].keys()
    assert config.progress is progress
    assert "timings" not in _report_to_dict(report)
    assert progress.emit.call_args_list[0].args == ("cycle", "started")
    assert progress.emit.call_args_list[-1].args == ("cycle", "completed")


@pytest.mark.parametrize("failure", ["setup", "observe", "snapshot"])
def test_metrics_failure_keeps_cycle_result_and_executes_once(
    tmp_path, fake_forge, failure
):
    from orchestune.dispatch.config import DispatcherConfig
    from orchestune.dispatch.cycle import run_dispatch_cycle

    config = DispatcherConfig(
        parent_issue_number=1164,
        forge=fake_forge,
        apply=True,
        run_state_path=tmp_path / "state.json",
        events_log_path=tmp_path / "events.jsonl",
    )
    target = {
        "setup": "orchestune.dispatch.cycle_execution.CycleTimingCollector",
        "observe": "orchestune.dispatch.timings.CycleTimingCollector.observe",
        "snapshot": "orchestune.dispatch.timings.CycleTimingCollector.snapshot",
    }[failure]
    with (
        patch("orchestune.dispatch.cycle.ensure_parent_branch_ready"),
        patch(target, side_effect=RuntimeError("metrics")),
        patch(
            "orchestune.dispatch.cycle.load_run_state",
            wraps=__import__(
                "orchestune.ledger.run_state", fromlist=["load_run_state"]
            ).load_run_state,
        ) as load,
    ):
        report = run_dispatch_cycle(config)
    assert report.applied
    assert load.call_count == 1
    entry = json.loads(config.events_log_path.read_text())
    assert entry["timings"]["collection_status"] in {"partial", "unavailable"}


def test_dry_run_and_business_exception_do_not_create_event_records(
    tmp_path, fake_forge
):
    from orchestune.dispatch.config import DispatcherConfig
    from orchestune.dispatch.cycle import run_dispatch_cycle

    config = DispatcherConfig(
        parent_issue_number=1164,
        forge=fake_forge,
        run_state_path=tmp_path / "state.json",
        events_log_path=tmp_path / "events.jsonl",
    )
    with patch("orchestune.dispatch.cycle.ensure_parent_branch_ready"):
        run_dispatch_cycle(config)
    assert not config.events_log_path.exists()
    config.apply = True
    failure = ValueError("business")
    with (
        patch("orchestune.dispatch.cycle.load_run_state", side_effect=failure),
        pytest.raises(ValueError) as caught,
    ):
        run_dispatch_cycle(config)
    assert caught.value is failure
    assert not config.events_log_path.exists()

from io import StringIO

import pytest

from orchestune.dispatch.progress import StdoutProgress, progress_phase


def test_progress_flushes_and_escapes_newlines():
    class Stream(StringIO):
        flushed = 0

        def flush(self):
            self.flushed += 1

    stream = Stream()
    sink = StdoutProgress("run", 1070, False, stream=stream)
    sink.emit("launch", "held", task_issue=42, reason="claim\nheld")
    assert stream.flushed == 1
    assert len(stream.getvalue().splitlines()) == 1
    assert "dry-run" in stream.getvalue()
    assert "42" in stream.getvalue()


def test_broken_stream_stops_sink_once(capsys):
    class Broken:
        calls = 0

        def write(self, value):
            self.calls += 1
            raise BrokenPipeError("closed")

    stream = Broken()
    sink = StdoutProgress("run", 1070, True, stream=stream)
    sink.emit("cycle", "started")
    sink.emit("cycle", "completed")
    assert stream.calls == 1
    assert capsys.readouterr().err.count("progress unavailable") == 1


def test_closed_python_stream_is_best_effort(capsys):
    stream = StringIO()
    stream.close()
    sink = StdoutProgress("run", 1070, True, stream=stream)
    sink.emit("cycle", "started")
    sink.emit("cycle", "completed")
    assert sink.disabled
    assert capsys.readouterr().err.count("progress unavailable") == 1


def test_phase_failure_does_not_emit_completed():
    events = []

    class Sink:
        def emit(self, phase, event, **kwargs):
            events.append((phase, event))

    with pytest.raises(ValueError), progress_phase(Sink(), "recovery"):
        raise ValueError("failure")
    assert events == [("recovery", "started"), ("recovery", "failed")]


def test_closed_stdout_pipe_preserves_process_exit_code():
    import os
    import subprocess
    import sys

    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from orchestune.dispatch.progress import StdoutProgress; s = StdoutProgress('run', 1070, True); s.emit('cycle', 'started'); s.emit('cycle', 'completed')",
            ],
            stdout=write_fd,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    finally:
        os.close(write_fd)
    assert result.returncode == 0, result.stderr


def test_cycle_failure_identifies_boundary_and_no_success(tmp_path, fake_forge):
    from unittest.mock import Mock, patch

    from orchestune.dispatch.config import DispatcherConfig
    from orchestune.dispatch.cycle import run_dispatch_cycle

    sink = Mock()
    config = DispatcherConfig(
        parent_issue_number=1070,
        forge=fake_forge,
        events_log_path=tmp_path / "events.jsonl",
        run_state_path=tmp_path / "state.json",
        progress=sink,
    )
    with (
        patch(
            "orchestune.dispatch.cycle.load_run_state",
            side_effect=ValueError("broken state"),
        ),
        pytest.raises(ValueError),
    ):
        run_dispatch_cycle(config)
    pairs = [(call.args[0], call.args[1]) for call in sink.emit.call_args_list]
    assert ("state_load", "failed") in pairs
    assert ("state_load", "completed") not in pairs
    assert ("scheduling", "started") not in pairs


@pytest.mark.parametrize(
    "outcome", ["held", "unknown", "failed", "launched", "reservation"]
)
def test_launch_progress_matches_actual_result(tmp_path, fake_forge, outcome):
    from contextlib import nullcontext
    from unittest.mock import Mock, patch

    from orchestune.dispatch.config import DispatcherConfig
    from orchestune.dispatch.launch import TaskLaunchPlan, _apply_single_task_launch
    from orchestune.ledger.run_state import RunState

    task = Mock(issue_number=42)
    target = Mock(target_name="local")
    sink = Mock()
    config = DispatcherConfig(
        parent_issue_number=100,
        forge=fake_forge,
        events_log_path=tmp_path / "events.jsonl",
        run_state_path=tmp_path / "state.json",
        dispatch_target=target,
        progress=sink,
    )
    plan = TaskLaunchPlan(task, "branch", "base", "base", None)
    launch = (
        None
        if outcome == "unknown"
        else Mock(
            held=outcome == "held",
            launched=outcome == "launched",
            error_message="existing reason",
        )
    )
    commit = None if outcome == "reservation" else Mock()
    state = RunState(active_worktrees={})

    def record(*args):
        assert not any(c.args[1] == "launched" for c in sink.emit.call_args_list)

    with (
        patch(
            "orchestune.dispatch.launch._launch_reservation",
            return_value=nullcontext(commit),
        ),
        patch(
            "orchestune.dispatch.launch.prepare_journaled_target", return_value=target
        ),
        patch(
            "orchestune.dispatch.launch._try_planned_launch", return_value=launch
        ) as provider,
        patch(
            "orchestune.dispatch.launch._record_successful_launch", side_effect=record
        ) as saved,
        patch("orchestune.dispatch.launch._record_failed_launch_phase"),
        patch("orchestune.dispatch.launch._handle_launch_failure"),
    ):
        result = _apply_single_task_launch(
            plan, state, 10.0, config, Mock(), None, None
        )
    events = [c.args[1] for c in sink.emit.call_args_list]
    assert events == ["started", "held" if outcome == "reservation" else outcome]
    assert result is task if outcome == "launched" else result is None
    assert saved.call_count == (outcome == "launched")
    assert provider.call_count == (outcome != "reservation")
    assert state.launch_history == ([10.0] if outcome == "unknown" else [])

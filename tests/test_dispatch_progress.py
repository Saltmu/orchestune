from io import StringIO
from unittest.mock import patch

import pytest

from orchestune.dispatch.progress import (
    DispatchProgressSink,
    StdoutProgress,
    progress_phase,
)
from orchestune.dispatch.progress_output import (
    RichDispatchProgress,
    select_progress_sink,
)


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


SHOW_CURSOR = "\x1b[?25h"
HIDE_CURSOR = "\x1b[?25l"


class TtyStream(StringIO):
    """An in-memory stream that reports itself as a UTF-8 terminal."""

    def __init__(self, *, tty: bool = True) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"


def rich_sink(stream=None, *, apply=False):
    sink = RichDispatchProgress(
        "run-1", 1070, apply, stream=stream or TtyStream(), env={}, width=100
    )
    return sink, sink.stream


def emit_typical_run(sink) -> None:
    sink.emit("configuration", "started")
    sink.emit("configuration", "completed")
    sink.emit("cycle", "started")
    sink.emit("task_launch", "started", task_issue=42)
    sink.emit("task_launch", "held", task_issue=42, reason="claim\nheld")
    sink.emit("cycle", "completed")
    sink.emit("post", "skipped", reason="dry_run")
    sink.emit("report", "completed", reason="report saved: /r/result.json")
    sink.emit("execution", "completed", reason="exit_code=0")


@pytest.mark.parametrize(
    "env",
    [
        {"CI": "true"},
        {"TERM": "dumb"},
        {"ORCHESTUNE_CLI_PLAIN": "1"},
    ],
)
def test_plain_environments_on_a_tty_keep_stdout_progress(env):
    sink = select_progress_sink("run", stream=TtyStream(), env=env)
    assert type(sink) is StdoutProgress


def test_tty_selects_the_rich_adapter():
    sink = select_progress_sink("run", stream=TtyStream(), env={})
    assert isinstance(sink, RichDispatchProgress)
    assert sink.owns_stream is False


def test_non_tty_output_is_byte_identical_to_stdout_progress():
    expected_stream = TtyStream(tty=False)
    expected = StdoutProgress("run", None, None, stream=expected_stream)
    selected_stream = TtyStream(tty=False)
    selected = select_progress_sink("run", stream=selected_stream, env={})
    for sink in (expected, selected):
        sink.parent_issue = 1070
        sink.apply = False
        emit_typical_run(sink)
    assert type(selected) is StdoutProgress
    assert selected_stream.getvalue() == expected_stream.getvalue()


def test_default_stream_is_read_from_stdout_at_call_time(monkeypatch):
    monkeypatch.setattr("sys.stdout", TtyStream(tty=False))
    owned = StringIO()
    with patch(
        "orchestune.dispatch.progress._stdout_stream", return_value=(owned, True)
    ):
        sink = select_progress_sink("run", env={})
    assert type(sink) is StdoutProgress
    assert sink.stream is owned and sink.owns_stream


def test_both_sinks_satisfy_the_dispatch_contract():
    sinks: list[DispatchProgressSink] = [
        StdoutProgress("run-1", None, None, stream=StringIO()),
        rich_sink()[0],
    ]
    for sink in sinks:
        sink.parent_issue = 7
        sink.apply = True
        assert sink.run_id == "run-1"
        sink.close()


def test_rich_adapter_shows_run_mode_results_and_report_path():
    sink, stream = rich_sink()
    emit_typical_run(sink)
    sink.close()
    text = stream.getvalue()
    assert "dispatch run run-1" in text
    assert "#1070" in text and "dry-run" in text
    assert "[DRY-RUN]" in text
    assert "[OK]" in text and "cycle completed" in text
    assert "[WARN]" in text and "task_launch #42 held: claim held" in text
    assert "[SKIPPED]" in text and "post skipped: dry_run" in text
    assert "report saved: /r/result.json" in text
    assert "execution completed: exit_code=0" in text
    assert "%" not in text and "ETA" not in text
    assert text.count(HIDE_CURSOR) == text.count(SHOW_CURSOR) >= 1


def test_live_runs_only_while_a_phase_is_open():
    sink, stream = rich_sink()
    sink.emit("cycle", "started")
    assert sink.live_active
    sink.emit("task_launch", "started", task_issue=1)
    sink.emit("cycle", "failed")
    # The enclosing phase ended: anything opened inside it is closed with it.
    assert not sink.live_active
    assert stream.getvalue().count(HIDE_CURSOR) == stream.getvalue().count(SHOW_CURSOR)
    sink.close()


def test_apply_mode_is_not_labelled_dry_run():
    sink, stream = rich_sink(apply=True)
    sink.emit("configuration", "completed")
    sink.close()
    assert "[DRY-RUN]" not in stream.getvalue()
    assert "apply" in stream.getvalue()


def test_reason_markup_and_control_sequences_are_literal():
    sink, stream = rich_sink()
    sink.emit("report", "planned", reason="[red]x[/red] \x1b]0;title\x07done")
    sink.close()
    text = stream.getvalue()
    assert "[red]x[/red] done" in text
    assert "\x1b]0;" not in text


def test_close_restores_cursor_when_a_phase_is_left_open():
    sink, stream = rich_sink()
    sink.emit("cycle", "started")
    sink.close()
    sink.close()
    text = stream.getvalue()
    assert text.count(HIDE_CURSOR) == 1
    assert text.count(SHOW_CURSOR) == 1
    assert not sink.live_active


def test_rich_broken_stream_disables_once(capsys):
    class Broken(TtyStream):
        calls = 0

        def write(self, value):
            self.calls += 1
            raise BrokenPipeError("closed")

    stream = Broken()
    sink, _ = rich_sink(stream)
    sink.emit("cycle", "started")
    calls = stream.calls
    sink.emit("cycle", "completed")
    sink.close()
    assert sink.disabled
    assert stream.calls == calls
    assert capsys.readouterr().err.count("progress unavailable") == 1


def test_rich_closed_stream_is_best_effort(capsys):
    stream = TtyStream()
    stream.close()
    sink, _ = rich_sink(stream)
    sink.emit("cycle", "started")
    sink.emit("cycle", "completed")
    assert sink.disabled
    assert capsys.readouterr().err.count("progress unavailable") == 1


def test_rich_rendering_error_stops_live_and_disables(capsys, monkeypatch):
    sink, stream = rich_sink()
    sink.emit("cycle", "started")

    def boom(*args, **kwargs):
        raise RuntimeError("render failed")

    monkeypatch.setattr(sink._output, "message", boom)
    sink.emit("cycle", "completed")
    assert sink.disabled and not sink.live_active
    assert stream.getvalue().count(SHOW_CURSOR) == 1
    err = capsys.readouterr().err
    assert err.count("progress unavailable") == 1
    assert "completed" not in stream.getvalue().split(SHOW_CURSOR)[-1]


def test_rich_close_closes_only_the_owned_stream():
    injected = TtyStream()
    sink, _ = rich_sink(injected)
    sink.close()
    assert not injected.closed

    owned = TtyStream()
    with patch(
        "orchestune.dispatch.progress_output._stdout_stream",
        return_value=(owned, True),
    ):
        sink = RichDispatchProgress("run", None, None, env={})
    sink.close()
    assert owned.closed

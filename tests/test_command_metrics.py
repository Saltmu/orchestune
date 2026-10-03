"""Forge metrics are scoped to a cycle and preserve subprocess behavior."""

import contextvars
import subprocess
from threading import Thread
from unittest.mock import Mock, patch

import pytest

from orchestune.dispatch.timings import CycleTimingCollector
from orchestune.forge import GitHubForge
from orchestune.infra import command_metrics
from orchestune.infra.command_metrics import command_observer_scope, measure_gh_call

pytestmark = pytest.mark.uses_real_forge


@pytest.mark.parametrize("stdin", [None, "本文\r\n"])
@pytest.mark.parametrize("outcome", ["ok", "error", "timeout", "missing"])
def test_forge_counts_each_attempt_once_with_unchanged_errors(stdin, outcome):
    collector = CycleTimingCollector()
    failure = {
        "ok": None,
        "error": subprocess.CalledProcessError(1, ["gh"]),
        "timeout": subprocess.TimeoutExpired(["gh"], 3),
        "missing": FileNotFoundError("gh"),
    }[outcome]
    process_result = subprocess.CompletedProcess(
        ["gh"], 0, stdout="result" if stdin is None else b"result", stderr=b""
    )
    with (
        patch(
            "orchestune.forge.subprocess.run",
            return_value=process_result,
            side_effect=failure,
        ) as process,
        patch.object(command_metrics.time, "monotonic", side_effect=[10, 12]),
        command_observer_scope(collector),
    ):
        if failure is None:
            assert (
                GitHubForge(timeout_seconds=3)._run(["gh", "api", "test"], stdin)
                == "result"
            )
        else:
            with pytest.raises(type(failure)) as caught:
                GitHubForge(timeout_seconds=3)._run(["gh", "api", "test"], stdin)
            assert caught.value is failure
    assert collector.snapshot()["gh"] == {"calls": 1, "seconds": 2.0}
    assert process.call_count == 1
    assert process.call_args.kwargs["timeout"] == 3
    if stdin is not None:
        assert process.call_args.kwargs["input"] == "本文\n".encode()


def test_no_observer_or_non_gh_does_not_read_clock():
    with patch.object(
        command_metrics.time,
        "monotonic",
        side_effect=AssertionError("unexpected clock"),
    ):
        with measure_gh_call():
            pass
        with (
            command_observer_scope(CycleTimingCollector()),
            measure_gh_call(enabled=False),
        ):
            pass


def test_nested_contexts_threads_and_sequential_scopes_are_isolated():
    outer, inner, next_cycle = (CycleTimingCollector() for _ in range(3))
    with command_observer_scope(outer):
        with measure_gh_call():
            pass
        with command_observer_scope(inner), measure_gh_call():
            pass
        thread = Thread(target=lambda: contextvars.Context().run(lambda: _attempt()))
        thread.start()
        thread.join()
        with measure_gh_call():
            pass
    with command_observer_scope(next_cycle), measure_gh_call():
        pass
    with measure_gh_call():
        pass
    assert outer.snapshot()["gh"]["calls"] == 2
    assert inner.snapshot()["gh"]["calls"] == 1
    assert next_cycle.snapshot()["gh"]["calls"] == 1


def _attempt():
    with measure_gh_call():
        pass


@pytest.mark.parametrize("failure_method", ["gh_started", "gh_finished"])
def test_observer_failure_does_not_replace_business_exception(failure_method):
    collector = CycleTimingCollector()
    original = ValueError("business")
    with (
        patch.object(collector, failure_method, side_effect=RuntimeError("metrics")),
        command_observer_scope(collector),
        pytest.raises(ValueError) as caught,
        measure_gh_call(),
    ):
        raise original
    assert caught.value is original
    assert collector.snapshot()["gh"] == {"calls": None, "seconds": None}


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
def test_business_control_exceptions_are_preserved_and_scope_restored(exception):
    collector = CycleTimingCollector()
    original = exception()
    with (
        pytest.raises(exception) as caught,
        command_observer_scope(collector),
        measure_gh_call(),
    ):
        raise original
    assert caught.value is original
    assert collector.snapshot()["gh"]["calls"] == 1
    with measure_gh_call():
        pass
    assert collector.snapshot()["gh"]["calls"] == 1


@pytest.mark.parametrize(
    "ticks",
    [[RuntimeError("clock"), 2], [1, RuntimeError("clock")], [1, float("nan")], [5, 2]],
)
def test_clock_failure_reports_unknown_duration_and_preserves_success(ticks):
    collector = CycleTimingCollector()
    with (
        patch.object(command_metrics.time, "monotonic", side_effect=ticks),
        command_observer_scope(collector),
        measure_gh_call(),
    ):
        result = "business succeeded"
    assert result == "business succeeded"
    assert collector.snapshot()["gh"] == {"calls": 1, "seconds": None}
    assert collector.snapshot()["collection_status"] == "partial"


def test_scope_setup_failure_still_executes_body_and_marks_missing():
    collector = CycleTimingCollector()
    broken_var = Mock()
    broken_var.set.side_effect = RuntimeError("context")
    with (
        patch.object(command_metrics, "_observer", broken_var),
        command_observer_scope(collector),
    ):
        result = "executed"
    assert result == "executed"
    assert collector.snapshot()["gh"] == {"calls": None, "seconds": None}

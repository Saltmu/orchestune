"""Tests for Quint scenario run cache and stored trace cache support (#1367)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.quint_replay import ModelRun, ReplayFormatError, ToolError, Trace
from tests.quint_scenario_cache_support import (
    ScenarioRunCache,
    StoredTraceCache,
)


def _fake_ok_runner(out_dir: Path, scenario: str) -> ModelRun:
    out_dir.mkdir(parents=True, exist_ok=True)
    itf = out_dir / "trace_0.itf.json"
    itf.write_text('{"#meta": {"status": "ok"}, "states": []}', encoding="utf-8")
    return ModelRun(
        command=("quint", "run", f"--init=init_{scenario}"),
        returncode=0,
        stdout="fake stdout",
        stderr="",
        outcome="ok",
        violated=(),
        seconds=1.23,
        traces=(itf,),
    )


def _fake_violation_runner(out_dir: Path, scenario: str) -> ModelRun:
    out_dir.mkdir(parents=True, exist_ok=True)
    itf = out_dir / "trace_0.itf.json"
    itf.write_text('{"#meta": {"status": "violation"}, "states": []}', encoding="utf-8")
    return ModelRun(
        command=("quint", "run", f"--init=init_{scenario}"),
        returncode=1,
        stdout="fake violation stdout",
        stderr="",
        outcome="violation",
        violated=("safety",),
        seconds=0.45,
        traces=(itf,),
    )


@pytest.fixture
def mock_project_dir(tmp_path: Path) -> Path:
    (tmp_path / ".gitignore").write_text(".orchestune/tmp/\n", encoding="utf-8")
    return tmp_path


# ---- ScenarioRunCache tests --------------------------------------------------


def test_scenario_run_cache_cold_and_hit(mock_project_dir: Path) -> None:
    calls: list[str] = []

    def runner(scenario: str, out_dir: Path | None = None) -> ModelRun:
        assert out_dir is not None
        calls.append(scenario)
        return _fake_ok_runner(out_dir, scenario)

    cache = ScenarioRunCache(
        known_scenarios=("final_apply", "final_dry"),
        runner=runner,
        project_dir=mock_project_dir,
    )
    try:
        # First call (cold)
        run1 = cache.get("final_apply")
        assert len(calls) == 1
        assert run1.outcome == "ok"
        assert run1.seconds == 1.23
        assert run1.traces[0].is_file()

        # Second call (hit)
        run2 = cache.get("final_apply")
        assert len(calls) == 1  # Runner not invoked again
        assert run2.outcome == "ok"
        assert run2.seconds == 1.23
        assert run2.traces[0] == run1.traces[0]
        # Defensively copied dataclass instance
        assert run2 is not run1
        assert run2 == run1

        # Callable interface cache(scenario)
        run3 = cache("final_apply")
        assert len(calls) == 1
        assert run3 is not run1
        assert run3 == run1

        # Different scenario
        run_dry = cache.get("final_dry")
        assert len(calls) == 2
        assert run_dry.outcome == "ok"
    finally:
        cache.close()


def test_scenario_run_cache_unknown_scenario_rejected(mock_project_dir: Path) -> None:
    cache = ScenarioRunCache(
        known_scenarios=("final_apply",),
        runner=lambda s, out_dir=None: _fake_ok_runner(out_dir or mock_project_dir, s),
        project_dir=mock_project_dir,
    )
    try:
        with pytest.raises(KeyError, match="unknown scenario"):
            cache.get("unknown_foo")
        with pytest.raises(KeyError, match="unknown scenario"):
            cache.get("../traversal")
    finally:
        cache.close()


def test_scenario_run_cache_error_not_cached(mock_project_dir: Path) -> None:
    calls: list[str] = []

    def failing_runner(scenario: str, out_dir: Path | None = None) -> ModelRun:
        calls.append(scenario)
        if len(calls) == 1:
            assert out_dir is not None
            out_dir.mkdir(parents=True, exist_ok=True)
            return ModelRun(
                command=("quint", "run"),
                returncode=2,
                stdout="error text",
                stderr="",
                outcome="error",
                violated=(),
                seconds=0.1,
                traces=(),
            )
        assert out_dir is not None
        return _fake_ok_runner(out_dir, scenario)

    cache = ScenarioRunCache(
        known_scenarios=("final_apply",),
        runner=failing_runner,
        project_dir=mock_project_dir,
    )
    try:
        with pytest.raises(ToolError):
            cache.get("final_apply")
        assert len(calls) == 1

        # Next call should retry and not serve a broken cache entry
        run = cache.get("final_apply")
        assert len(calls) == 2
        assert run.outcome == "ok"
    finally:
        cache.close()


def test_scenario_run_cache_missing_itf_not_cached(mock_project_dir: Path) -> None:
    calls: list[str] = []

    def missing_itf_runner(scenario: str, out_dir: Path | None = None) -> ModelRun:
        calls.append(scenario)
        assert out_dir is not None
        out_dir.mkdir(parents=True, exist_ok=True)
        # Return traces pointing to non-existent file
        return ModelRun(
            command=("quint", "run"),
            returncode=0,
            stdout="",
            stderr="",
            outcome="ok",
            violated=(),
            seconds=0.1,
            traces=(out_dir / "missing.itf.json",),
        )

    cache = ScenarioRunCache(
        known_scenarios=("final_apply",),
        runner=missing_itf_runner,
        project_dir=mock_project_dir,
    )
    try:
        with pytest.raises(ReplayFormatError):
            cache.get("final_apply")
        assert len(calls) == 1
    finally:
        cache.close()


def test_scenario_run_cache_empty_itf_not_cached(mock_project_dir: Path) -> None:
    calls: list[str] = []

    def empty_itf_runner(scenario: str, out_dir: Path | None = None) -> ModelRun:
        calls.append(scenario)
        assert out_dir is not None
        out_dir.mkdir(parents=True, exist_ok=True)
        empty_itf = out_dir / "trace_0.itf.json"
        empty_itf.write_text("", encoding="utf-8")
        return ModelRun(
            command=("quint", "run"),
            returncode=0,
            stdout="",
            stderr="",
            outcome="ok",
            violated=(),
            seconds=0.1,
            traces=(empty_itf,),
        )

    cache = ScenarioRunCache(
        known_scenarios=("final_apply",),
        runner=empty_itf_runner,
        project_dir=mock_project_dir,
    )
    try:
        with pytest.raises(ReplayFormatError, match="empty"):
            cache.get("final_apply")
        assert len(calls) == 1
    finally:
        cache.close()


def test_scenario_run_cache_violation_cached(mock_project_dir: Path) -> None:
    calls: list[str] = []

    def runner(scenario: str, out_dir: Path | None = None) -> ModelRun:
        assert out_dir is not None
        calls.append(scenario)
        return _fake_violation_runner(out_dir, scenario)

    cache = ScenarioRunCache(
        known_scenarios=("fault_suppress",),
        runner=runner,
        project_dir=mock_project_dir,
    )
    try:
        run1 = cache.get("fault_suppress")
        assert len(calls) == 1
        assert run1.outcome == "violation"
        assert run1.violated == ("safety",)

        run2 = cache.get("fault_suppress")
        assert len(calls) == 1
        assert run2.outcome == "violation"
        assert run2 is not run1
    finally:
        cache.close()


def test_scenario_run_cache_cleanup_removes_dir(mock_project_dir: Path) -> None:
    cache = ScenarioRunCache(
        known_scenarios=("final_apply",),
        runner=lambda s, out_dir=None: _fake_ok_runner(out_dir or mock_project_dir, s),
        project_dir=mock_project_dir,
    )
    session_dir = cache.session_dir
    assert session_dir.is_dir()
    cache.get("final_apply")
    assert (session_dir / "final_apply" / "trace_0.itf.json").is_file()

    cache.close()
    assert not session_dir.exists()


def test_scenario_run_cache_explicit_session_dir_and_context_manager(
    tmp_path: Path,
) -> None:
    custom_dir = tmp_path / "custom_session"
    calls: list[str] = []

    def runner(scenario: str, out_dir: Path | None = None) -> ModelRun:
        assert out_dir is not None
        calls.append(scenario)
        return _fake_ok_runner(out_dir, scenario)

    with ScenarioRunCache(
        known_scenarios=("final_apply",),
        runner=runner,
        session_dir=custom_dir,
    ) as cache:
        assert cache.session_dir == custom_dir.resolve()
        run = cache("final_apply")
        assert run.outcome == "ok"
        assert len(calls) == 1

    # Caller-supplied session directory is preserved on exit
    assert custom_dir.exists()


def test_scenario_run_cache_unexpected_outcome(tmp_path: Path) -> None:
    def weird_runner(scenario: str, out_dir: Path | None = None) -> ModelRun:
        assert out_dir is not None
        out_dir.mkdir(parents=True, exist_ok=True)
        return ModelRun(
            command=("quint", "run"),
            returncode=0,
            stdout="",
            stderr="",
            outcome="weird_status",
            violated=(),
            seconds=0.1,
            traces=(),
        )

    with ScenarioRunCache(
        known_scenarios=("final_apply",),
        runner=weird_runner,
        session_dir=tmp_path / "session",
    ) as cache:
        with pytest.raises(ToolError, match="unexpected model outcome"):
            cache.get("final_apply")


def test_scenario_run_cache_unexpected_trace_count(tmp_path: Path) -> None:
    def multi_trace_runner(scenario: str, out_dir: Path | None = None) -> ModelRun:
        assert out_dir is not None
        out_dir.mkdir(parents=True, exist_ok=True)
        itf1 = out_dir / "trace_0.itf.json"
        itf2 = out_dir / "trace_1.itf.json"
        itf1.write_text("{}", encoding="utf-8")
        itf2.write_text("{}", encoding="utf-8")
        return ModelRun(
            command=("quint", "run"),
            returncode=0,
            stdout="",
            stderr="",
            outcome="ok",
            violated=(),
            seconds=0.1,
            traces=(itf1, itf2),
        )

    with ScenarioRunCache(
        known_scenarios=("final_apply",),
        runner=multi_trace_runner,
        session_dir=tmp_path / "session",
    ) as cache:
        with pytest.raises(ReplayFormatError, match="expected 1 ITF trace"):
            cache.get("final_apply")


# ---- StoredTraceCache tests --------------------------------------------------


def test_stored_trace_cache_cold_and_hit() -> None:
    from tests.quint_scenarios import TRACES_FIXTURE, load_fixture

    stored_entries = {
        entry["id"]: entry for entry in load_fixture(TRACES_FIXTURE)["traces"]
    }
    fake_traces = {"final_apply": stored_entries["final_apply"]}
    parse_calls: list[str] = []

    def mock_parse(itf: dict[str, Any]) -> Trace:
        parse_calls.append("called")
        from tests.quint_replay import parse_trace

        return parse_trace(itf)

    cache = StoredTraceCache(stored=fake_traces, parser=mock_parse)

    # First call (cold)
    t1 = cache.get("final_apply")
    assert len(parse_calls) == 1
    assert len(t1.steps) > 0

    # Second call (hit)
    t2 = cache.get("final_apply")
    assert len(parse_calls) == 1  # parser not called again
    assert len(t2.steps) == len(t1.steps)
    assert t2 is not t1

    # Deepcopy isolation test: mutate t1 inner state
    assert isinstance(t1.steps[0].state, dict)
    t1.steps[0].state["mutated_key"] = "leak"
    assert "mutated_key" not in t2.steps[0].state

    # Callable interface
    t3 = cache("final_apply")
    assert len(parse_calls) == 1
    assert "mutated_key" not in t3.steps[0].state


def test_stored_trace_cache_unknown_scenario() -> None:
    cache = StoredTraceCache(stored={})
    with pytest.raises(KeyError, match="unknown stored scenario"):
        cache.get("non_existent")

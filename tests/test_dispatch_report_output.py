import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

import pytest

from orchestune.dispatch.config.model import DispatcherConfig
from orchestune.dispatch.reporting.report_output import reserve_report


def config(tmp_path, path=None):
    return DispatcherConfig(
        forge=Mock(),
        parent_issue_number=1070,
        report_dir=tmp_path / "reports",
        report_path=path,
        run_state_path=tmp_path / "state.json",
        events_log_path=tmp_path / "events.jsonl",
        not_needed_review_state_path=tmp_path / "review.json",
    )


def test_auto_results_unique_and_atomic(tmp_path):
    paths = []
    for _ in range(2):
        with reserve_report(config(tmp_path), "run") as output:
            paths.append(output.path)
            assert not output.path.exists()
            output.save({"selected": [], "post_cycle_results": []})
            assert json.loads(output.path.read_text())["selected"] == []
    assert paths[0] != paths[1]


def test_existing_output_never_overwritten(tmp_path):
    path = tmp_path / "result.json"
    path.write_text("old")
    with pytest.raises((ValueError, RuntimeError)):
        with reserve_report(config(tmp_path, path), "run"):
            pytest.fail("existing output entered reservation")
    assert path.read_text() == "old"


@pytest.mark.parametrize(
    "name",
    [
        "state.json",
        "state.lock",
        "events.jsonl",
        "review.json",
        "state.status-intents.json",
    ],
)
def test_business_paths_rejected(tmp_path, name):
    with pytest.raises(ValueError):
        with reserve_report(config(tmp_path, tmp_path / name), "run"):
            pytest.fail("business path accepted")


def test_symlink_rejected(tmp_path):
    path = tmp_path / "result.json"
    try:
        path.symlink_to(tmp_path / "missing")
    except OSError:
        pytest.skip("Symlink creation not permitted on this system")
    with pytest.raises(ValueError):
        with reserve_report(config(tmp_path, path), "run"):
            pytest.fail("symlink accepted")


def test_same_explicit_path_excludes_other_execution(tmp_path):
    path = tmp_path / "result.json"
    entered, release = Event(), Event()

    def hold():
        with reserve_report(config(tmp_path, path), "one"):
            entered.set()
            assert release.wait(5)

    with ThreadPoolExecutor() as pool:
        future = pool.submit(hold)
        assert entered.wait(5)
        try:
            with pytest.raises(RuntimeError):
                with reserve_report(config(tmp_path, path), "two"):
                    pytest.fail("concurrent execution accepted")
        finally:
            release.set()
        future.result()


def test_save_rechecks_file_and_preserves_external_result(tmp_path):
    with reserve_report(config(tmp_path, tmp_path / "result.json"), "run") as output:
        output.path.write_text("external")
        with pytest.raises(ValueError):
            output.save({})
        assert output.path.read_text() == "external"


def _report():
    from orchestune.dispatch.cycle.report import CycleReport

    return CycleReport(
        selected=[],
        quota_slots_available=0,
        lock_changes={"to_lock": [], "to_unlock": []},
        deviation_events=[],
        completion_events=[],
        promotion_events=[],
        applied=False,
    )


_OBSERVED_AT = datetime(2026, 11, 14, 7, 0, tzinfo=UTC)


def _parent_integration(cfg, *, apply: bool, report=None):
    from orchestune.dispatch.dispatcher import build_parent_integration
    from orchestune.status_snapshot import observe_repository_id

    return build_parent_integration(
        repository_id=observe_repository_id(Path.cwd(), cfg.resolved_forge),
        parent_issue=cfg.parent_issue_number,
        observed_at="2026-11-14T07:00:00Z",
        apply=apply,
        integrator_report=report,
    )


@pytest.mark.parametrize("apply", [False, True])
def test_cli_saves_exact_old_schema_and_no_stdout_json(tmp_path, capsys, apply):
    from orchestune.dispatch import dispatcher as dispatcher_module
    from orchestune.dispatch.dispatcher import main
    from orchestune.dispatch.reporting.report import _report_to_dict
    from orchestune.dispatch.result import PhaseResult, PhaseStatus

    cfg = config(tmp_path, tmp_path / "result.json")
    cfg.apply = apply
    report = _report()
    phase = PhaseResult("run_semantic_integrator", PhaseStatus.SUCCESS)
    with (
        patch(
            "orchestune.dispatch.dispatcher.load_and_resolve_config", return_value=cfg
        ),
        patch("orchestune.dispatch.dispatcher.run_dispatch_cycle", return_value=report),
        patch(
            "orchestune.dispatch.dispatcher.run_pre_integration_cycle",
            return_value=report,
        ),
        patch(
            "orchestune.dispatch.dispatcher._decide_semantic_review_enabled",
            return_value=False,
        ),
        patch(
            "orchestune.dispatch.pre_scheduling_integration._advance_pending_semantic_reviews",
            return_value=phase,
        ),
        patch(
            "orchestune.dispatch.pre_scheduling_integration._run_semantic_integrator",
            return_value=phase,
        ),
        patch(
            "orchestune.dispatch.dispatcher._process_parent_completion",
            return_value=phase,
        ),
        patch(
            "orchestune.dispatch.dispatcher._post_event_log_comment", return_value=phase
        ),
        patch(
            "orchestune.dispatch.dispatcher._post_finding_notices", return_value=phase
        ),
        patch.object(dispatcher_module, "_utc_now", return_value=_OBSERVED_AT),
    ):
        assert main([]) == 0
    expected = _report_to_dict(report)
    expected["post_cycle_results"] = [phase.to_dict()] * 5 if apply else []
    if apply:
        expected["pre_integration_cycle"] = _report_to_dict(report)
    expected["parent_integration"] = _parent_integration(cfg, apply=apply)
    assert json.loads(cfg.report_path.read_text()) == expected
    stdout = capsys.readouterr().out
    assert '"selected"' not in stdout
    assert f"report saved: {cfg.report_path}" in stdout


def test_postcycle_exception_keeps_completed_results_and_skips_rest(tmp_path, capsys):
    from orchestune.dispatch.dispatcher import main
    from orchestune.dispatch.result import PhaseResult, PhaseStatus

    cfg = config(tmp_path, tmp_path / "result.json")
    cfg.apply = True
    with (
        patch(
            "orchestune.dispatch.dispatcher.load_and_resolve_config", return_value=cfg
        ),
        patch(
            "orchestune.dispatch.dispatcher.run_dispatch_cycle", return_value=_report()
        ),
        patch(
            "orchestune.dispatch.dispatcher.run_pre_integration_cycle",
            return_value=_report(),
        ),
        patch(
            "orchestune.dispatch.dispatcher._decide_semantic_review_enabled",
            return_value=True,
        ),
        patch(
            "orchestune.dispatch.pre_scheduling_integration._poll_pending_not_needed_reviews",
            return_value=PhaseResult(
                "poll_pending_not_needed_reviews", PhaseStatus.SUCCESS
            ),
        ),
        patch(
            "orchestune.dispatch.pre_scheduling_integration._advance_pending_semantic_reviews",
            return_value=PhaseResult(
                "advance_pending_semantic_reviews", PhaseStatus.SUCCESS
            ),
        ),
        patch(
            "orchestune.dispatch.pre_scheduling_integration._run_semantic_integrator",
            side_effect=ValueError("unexpected"),
        ),
        patch("orchestune.dispatch.dispatcher._process_parent_completion") as later,
    ):
        assert main([]) == 1
    results = json.loads(cfg.report_path.read_text())["post_cycle_results"]
    assert [r["status"] for r in results] == ["success", "success", "fatal_failure"]
    assert results[2]["error_message"] == "unexpected"
    later.assert_not_called()
    assert "prior_failure" in capsys.readouterr().out


def test_cycle_exception_creates_no_report(tmp_path, capsys):
    from orchestune.dispatch.dispatcher import main

    cfg = config(tmp_path, tmp_path / "result.json")
    with (
        patch(
            "orchestune.dispatch.dispatcher.load_and_resolve_config", return_value=cfg
        ),
        patch(
            "orchestune.dispatch.dispatcher.run_dispatch_cycle",
            side_effect=ValueError("cycle broken"),
        ),
    ):
        assert main([]) == 1
    assert not cfg.report_path.exists()
    assert "report not created" in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["mkdir", "probe", "atomic", "convert"])
def test_output_failures_never_claim_saved(tmp_path, capsys, failure):
    from contextlib import ExitStack

    from orchestune.dispatch.dispatcher import main

    cfg = config(tmp_path, tmp_path / "result.json")
    targets = {
        "mkdir": "pathlib.Path.mkdir",
        "probe": "orchestune.dispatch.reporting.report_output.tempfile.mkstemp",
        "atomic": "orchestune.dispatch.reporting.report_output.write_json_atomic",
        "convert": "orchestune.dispatch.dispatcher._report_to_dict",
    }
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "orchestune.dispatch.dispatcher.load_and_resolve_config",
                return_value=cfg,
            )
        )
        cycle = stack.enter_context(
            patch(
                "orchestune.dispatch.dispatcher.run_dispatch_cycle",
                return_value=_report(),
            )
        )
        stack.enter_context(
            patch(targets[failure], side_effect=OSError("disk failure"))
        )
        assert main([]) == 1
    if failure in {"mkdir", "probe"}:
        cycle.assert_not_called()
    assert not cfg.report_path.exists()
    captured = capsys.readouterr()
    assert "report saved" not in captured.out
    assert "disk failure" in captured.err


def test_save_failure_overrides_retryable_and_reports_both(tmp_path, capsys):
    from orchestune.dispatch.dispatcher import main
    from orchestune.dispatch.result import PhaseResult, PhaseStatus

    cfg = config(tmp_path, tmp_path / "result.json")
    cfg.apply = True
    retry = PhaseResult(
        "run_semantic_integrator",
        PhaseStatus.RETRYABLE_FAILURE,
        error_message="retry later",
        retryable=True,
    )
    success = PhaseResult("later", PhaseStatus.SUCCESS)
    with (
        patch(
            "orchestune.dispatch.dispatcher.load_and_resolve_config", return_value=cfg
        ),
        patch(
            "orchestune.dispatch.dispatcher.run_dispatch_cycle", return_value=_report()
        ),
        patch(
            "orchestune.dispatch.dispatcher.run_pre_integration_cycle",
            return_value=_report(),
        ),
        patch(
            "orchestune.dispatch.dispatcher._decide_semantic_review_enabled",
            return_value=False,
        ),
        patch(
            "orchestune.dispatch.pre_scheduling_integration._run_semantic_integrator",
            return_value=retry,
        ),
        patch(
            "orchestune.dispatch.dispatcher._process_parent_completion",
            return_value=success,
        ),
        patch(
            "orchestune.dispatch.dispatcher._post_event_log_comment",
            return_value=success,
        ),
        patch(
            "orchestune.dispatch.dispatcher._post_finding_notices", return_value=success
        ),
        patch(
            "orchestune.dispatch.reporting.report_output.write_json_atomic",
            side_effect=OSError("full"),
        ),
    ):
        assert main([]) == 1
    err = capsys.readouterr().err
    assert "retry later" in err and "report save failed" in err


def test_stdout_failure_does_not_stop_cli_save(tmp_path, monkeypatch):
    from orchestune.dispatch.dispatcher import main

    class Broken:
        def write(self, value):
            raise BrokenPipeError("closed")

    cfg = config(tmp_path, tmp_path / "result.json")
    monkeypatch.setattr("sys.stdout", Broken())
    with (
        patch(
            "orchestune.dispatch.dispatcher.load_and_resolve_config", return_value=cfg
        ),
        patch(
            "orchestune.dispatch.dispatcher.run_dispatch_cycle", return_value=_report()
        ),
        patch(
            "orchestune.dispatch.dispatcher.run_pre_integration_cycle",
            return_value=_report(),
        ),
    ):
        assert main([]) == 0
    assert cfg.report_path.exists()


@pytest.mark.parametrize("config_error", [False, True])
def test_cli_closes_owned_progress_stream_even_on_config_error(tmp_path, config_error):
    from io import StringIO

    from orchestune.config_contracts import ConfigError
    from orchestune.dispatch.dispatcher import main

    stream = StringIO()
    cfg = config(tmp_path, tmp_path / "result.json")
    with (
        patch(
            "orchestune.dispatch.reporting.progress._stdout_stream",
            return_value=(stream, True),
        ),
        patch(
            "orchestune.dispatch.dispatcher.load_and_resolve_config",
            side_effect=ConfigError("invalid") if config_error else None,
            return_value=cfg,
        ),
        patch(
            "orchestune.dispatch.dispatcher.run_dispatch_cycle", return_value=_report()
        ),
        patch(
            "orchestune.dispatch.dispatcher.run_pre_integration_cycle",
            return_value=_report(),
        ),
    ):
        if config_error:
            with pytest.raises(SystemExit) as exc:
                main([])
            assert exc.value.code == 2
        else:
            assert main([]) == 0
    assert stream.closed


class _TtyStream(StringIO):
    def isatty(self):
        return True

    @property
    def encoding(self):
        return "utf-8"


def _run_main_with_tty_sink(tmp_path, stream, cycle):
    from orchestune.dispatch import dispatcher as dispatcher_module
    from orchestune.dispatch.dispatcher import main
    from orchestune.dispatch.reporting.progress_output import RichDispatchProgress

    cfg = config(tmp_path, tmp_path / "result.json")
    sink = RichDispatchProgress("run-tty", None, None, stream=stream, env={})
    with (
        patch("orchestune.dispatch.dispatcher.select_progress_sink", return_value=sink),
        patch(
            "orchestune.dispatch.dispatcher.load_and_resolve_config", return_value=cfg
        ),
        patch("orchestune.dispatch.dispatcher.run_dispatch_cycle", side_effect=cycle),
        patch.object(dispatcher_module, "_utc_now", return_value=_OBSERVED_AT),
    ):
        return cfg, sink, main([])


HIDE_CURSOR, SHOW_CURSOR = "\x1b[?25l", "\x1b[?25h"


def _phase_then(outcome):
    def cycle(cfg):
        cfg.progress.emit("cycle", "started")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return cycle


def test_tty_progress_saves_report_and_never_prints_json(tmp_path, capsys):
    from orchestune.dispatch.reporting.report import _report_to_dict

    report = _report()
    stream = _TtyStream()
    cfg, sink, code = _run_main_with_tty_sink(tmp_path, stream, _phase_then(report))
    assert code == 0
    saved = json.loads(cfg.report_path.read_text())
    assert saved == {
        **_report_to_dict(report),
        "post_cycle_results": [],
        "parent_integration": _parent_integration(cfg, apply=False),
    }
    text = stream.getvalue()
    assert f"report saved: {cfg.report_path}" in text
    assert "run-tty" in text and "dry-run" in text and "#1070" in text
    assert text.count(HIDE_CURSOR) == text.count(SHOW_CURSOR) >= 1
    assert sink.disabled
    assert '"selected"' not in text + capsys.readouterr().out


def test_broken_tty_progress_still_saves_report(tmp_path, capsys):
    class Broken(_TtyStream):
        def write(self, value):
            raise BrokenPipeError("closed")

    cfg, _sink, code = _run_main_with_tty_sink(
        tmp_path, Broken(), _phase_then(_report())
    )
    assert code == 0
    assert cfg.report_path.exists()
    assert capsys.readouterr().err.count("progress unavailable") == 1


def test_parent_integration_selects_one_parent_and_drops_tails(tmp_path):
    from orchestune.dispatch.dispatcher import build_parent_integration

    sha = "a" * 40
    report = {
        "status": "composite_failure",
        "details": {
            "issue_1": {
                "status": "success",
                "execution_failures": [{"detail": "other"}],
            },
            "issue_2": {
                "status": "execution_timed_out",
                "execution_failures": [
                    {
                        "cause": "ci_timeout",
                        "stage": "ci",
                        "issue_number": 9,
                        "subtask_id": "task-9",
                        "source_sha": sha,
                        "attempt": 2,
                        "max_attempts": 3,
                        "next_retry_at": "2026-11-14T08:00:00Z",
                        "stop_confirmed": True,
                        "rollback_confirmed": False,
                        "side_effect_state": "unknown",
                        "output_tail": "tail",
                        "detail": "/home/micro/secret",
                    }
                ],
                "child_review_gate_failures": [
                    {"reason": "stale_read_gate: stale", "issue_number": 9}
                ],
            },
        },
    }
    saved = build_parent_integration(
        repository_id="Saltmu/orchestune",
        parent_issue=2,
        observed_at="2026-11-14T07:00:00Z",
        apply=True,
        integrator_report=report,
    )
    assert saved["executed"] is True
    assert saved["status"] == "execution_timed_out"
    assert saved["failures"] == [
        {
            "kind": "execution",
            "cause": "ci_timeout",
            "stage": "ci",
            "attempt": 2,
            "max_attempts": 3,
            "next_retry_at": "2026-11-14T08:00:00Z",
            "stop_confirmed": True,
            "rollback_confirmed": False,
            "side_effect_state": "unknown",
            "target": {"issue_number": 9, "subtask_id": "task-9", "source_sha": sha},
        }
    ]
    assert "other" not in json.dumps(saved)
    missing = build_parent_integration(
        repository_id=None,
        parent_issue=3,
        observed_at="2026-11-14T07:00:00Z",
        apply=True,
        integrator_report=report,
    )
    assert missing["executed"] is False
    assert missing["status"] is None
    idle = build_parent_integration(
        repository_id="Saltmu/orchestune",
        parent_issue=2,
        observed_at="2026-11-14T07:00:00Z",
        apply=False,
        integrator_report=None,
    )
    assert idle["mode"] == "preview"
    assert idle["status"] is None
    assert idle["executed"] is False


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (RuntimeError("boom"), "report not created"),
        (KeyboardInterrupt(), "interrupted; report not created"),
    ],
)
def test_tty_progress_restores_cursor_on_failure(tmp_path, failure, message):
    stream = _TtyStream()
    if isinstance(failure, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            _run_main_with_tty_sink(tmp_path, stream, _phase_then(failure))
    else:
        cfg, _sink, code = _run_main_with_tty_sink(
            tmp_path, stream, _phase_then(failure)
        )
        assert code == 1
        assert not cfg.report_path.exists()
    text = stream.getvalue()
    assert message in text
    assert text.count(HIDE_CURSOR) == text.count(SHOW_CURSOR) >= 1
    assert text.rfind(SHOW_CURSOR) > text.rfind(HIDE_CURSOR)

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock, patch

import pytest

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.report_output import reserve_report


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
    from orchestune.dispatch.cycle_report import CycleReport

    return CycleReport(
        selected=[],
        quota_slots_available=0,
        lock_changes={"to_lock": [], "to_unlock": []},
        deviation_events=[],
        completion_events=[],
        promotion_events=[],
        applied=False,
    )


@pytest.mark.parametrize("apply", [False, True])
def test_cli_saves_exact_old_schema_and_no_stdout_json(tmp_path, capsys, apply):
    from orchestune.dispatch.dispatcher import main
    from orchestune.dispatch.report import _report_to_dict
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
            "orchestune.dispatch.dispatcher._decide_semantic_review_enabled",
            return_value=False,
        ),
        patch(
            "orchestune.dispatch.dispatcher._run_semantic_integrator",
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
    ):
        assert main([]) == 0
    expected = _report_to_dict(report)
    expected["post_cycle_results"] = [phase.to_dict()] * 4 if apply else []
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
            "orchestune.dispatch.dispatcher._decide_semantic_review_enabled",
            return_value=True,
        ),
        patch(
            "orchestune.dispatch.dispatcher._poll_pending_not_needed_reviews",
            return_value=PhaseResult(
                "poll_pending_not_needed_reviews", PhaseStatus.SUCCESS
            ),
        ),
        patch(
            "orchestune.dispatch.dispatcher._run_semantic_integrator",
            side_effect=ValueError("unexpected"),
        ),
        patch("orchestune.dispatch.dispatcher._process_parent_completion") as later,
    ):
        assert main([]) == 1
    results = json.loads(cfg.report_path.read_text())["post_cycle_results"]
    assert [r["status"] for r in results] == ["success", "fatal_failure"]
    assert results[1]["error_message"] == "unexpected"
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
        "probe": "orchestune.dispatch.report_output.tempfile.mkstemp",
        "atomic": "orchestune.dispatch.report_output.write_json_atomic",
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
            "orchestune.dispatch.dispatcher._decide_semantic_review_enabled",
            return_value=False,
        ),
        patch(
            "orchestune.dispatch.dispatcher._run_semantic_integrator",
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
            "orchestune.dispatch.report_output.write_json_atomic",
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
    ):
        assert main([]) == 0
    assert cfg.report_path.exists()


@pytest.mark.parametrize("config_error", [False, True])
def test_cli_closes_owned_progress_stream_even_on_config_error(tmp_path, config_error):
    from io import StringIO

    from orchestune.dispatch.config_loader import ConfigError
    from orchestune.dispatch.dispatcher import main

    stream = StringIO()
    cfg = config(tmp_path, tmp_path / "result.json")
    with (
        patch(
            "orchestune.dispatch.progress._stdout_stream", return_value=(stream, True)
        ),
        patch(
            "orchestune.dispatch.dispatcher.load_and_resolve_config",
            side_effect=ConfigError("invalid") if config_error else None,
            return_value=cfg,
        ),
        patch(
            "orchestune.dispatch.dispatcher.run_dispatch_cycle", return_value=_report()
        ),
    ):
        if config_error:
            with pytest.raises(SystemExit) as exc:
                main([])
            assert exc.value.code == 2
        else:
            assert main([]) == 0
    assert stream.closed

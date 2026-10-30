"""Observational progress preserves provision operations and terminal diagnostics."""

import io
import sys
from unittest.mock import patch

import pytest

from orchestune.infra import cli_progress
from orchestune.infra.cli_output import make_output
from orchestune.provisioning import cli, flow
from orchestune.provisioning.retry import ProvisionRetryForge
from tests.test_provisioning_support import FakeForge


class Tty(io.StringIO):
    def isatty(self):
        return True

    @property
    def encoding(self):
        return "utf-8"


class RecordingForge(FakeForge):
    def __init__(self):
        super().__init__()
        self.calls = []

    def __getattribute__(self, name):
        value = super().__getattribute__(name)
        if not name.startswith("_") and callable(value):

            def observed(*args, **kwargs):
                self.calls.append((name, args, kwargs.copy()))
                return value(*args, **kwargs)

            return observed
        return value


def test_flow_progress_is_observational(plan_path, template_path):
    original = plan_path.read_text()
    forge = RecordingForge()
    expected = flow.provision_issues(plan_path, forge, template_path=template_path)
    expected_plan = plan_path.read_text()
    plan_path.write_text(original)
    observed = RecordingForge()
    events = []
    result = flow.provision_issues(
        plan_path, observed, template_path=template_path, on_progress=events.append
    )
    assert result == expected
    assert observed.__dict__ == forge.__dict__
    assert plan_path.read_text() == expected_plan
    assert [event.step for event in events] == [
        "parent",
        "index",
        "subtask",
        "subtask",
        "sync",
    ]
    assert [
        (e.subtask_id, e.index, e.total) for e in events if e.step == "subtask"
    ] == [("task-a", 1, 2), ("task-b", 2, 2)]


def test_dry_run_has_no_progress(plan_path, template_path):
    events = []
    flow.provision_issues(
        plan_path,
        FakeForge(),
        apply=False,
        template_path=template_path,
        on_progress=events.append,
    )
    assert events == []


def test_stopped_precedes_diagnostic(plan_path, template_path, monkeypatch):
    events = []
    monkeypatch.setattr(
        flow, "print", lambda *a, **k: events.append("diagnostic"), raising=False
    )
    with patch.object(
        flow, "_provision_and_link_one_subtask", side_effect=ValueError("bad")
    ):
        with pytest.raises(ValueError, match="bad"):
            flow.provision_issues(
                plan_path,
                FakeForge(),
                template_path=template_path,
                on_progress=lambda event: events.append(event.step),
            )
    assert events[-2:] == ["stopped", "diagnostic"]


def test_retry_notifications_precede_failure_and_probe_sleeps():
    events = []

    class LostForge(FakeForge):
        def create_issue(self, *args, **kwargs):
            raise ConnectionError("lost")

    retry = ProvisionRetryForge(
        LostForge(),
        min_interval=0,
        clock=lambda: 0,
        sleep=lambda seconds: events.append(("sleep", seconds)),
        on_wait=lambda seconds: events.append(("wait", seconds)),
    )
    with pytest.raises(RuntimeError, match="uncertain"):
        retry.create_issue("[EPIC] unknown", "<!-- orchestune:parent -->")
    assert events == [
        (kind, seconds) for seconds in (1.0, 2.0, 4.0) for kind in ("wait", "sleep")
    ]


def test_pacing_does_not_notify():
    events = []
    retry = ProvisionRetryForge(
        FakeForge(),
        clock=lambda: 0,
        sleep=lambda delay: events.append(("sleep", delay)),
        on_wait=lambda delay: events.append(("wait", delay)),
    )
    retry.get_issue(1)
    retry.get_issue(1)
    assert events == [("sleep", 0.2)]


def test_flow_maps_retry_wait():
    events = []

    class FlakyForge(FakeForge):
        failed = False

        def get_issue(self, number):
            if not self.failed:
                self.failed = True
                raise ConnectionError("retry")
            return None

    with patch.object(
        flow,
        "ProvisionRetryForge",
        wraps=lambda *a, **k: ProvisionRetryForge(*a, sleep=lambda _: None, **k),
    ):
        forge = flow._resolve_provision_forge(FlakyForge(), events.append)
        forge.get_issue(1)
    assert [(e.step, e.wait_seconds) for e in events] == [("retry-wait", 1.0)]


@pytest.mark.parametrize("entry", ["cli", "package"])
def test_facade_forwards_progress(entry, monkeypatch):
    from orchestune import provisioning

    seen = []
    hook = seen.append
    monkeypatch.setattr(cli, "_provision_issues", lambda *a, **kw: kw["on_progress"])
    function = cli.provision_issues if entry == "cli" else provisioning.provision_issues
    assert function("plan", on_progress=hook) is hook


def test_display_updates_and_stops_before_diagnostic(monkeypatch):
    stream = Tty()
    output = make_output(stream, env={}, width=80)
    updates = []
    original = cli_progress.ProgressHandle.update

    def update(handle, content):
        updates.append(content.plain)
        return original(handle, content)

    monkeypatch.setattr(cli_progress.ProgressHandle, "update", update)
    with cli.ProvisionProgressDisplay(output) as hook:
        assert hook is not None
        hook(flow.ProvisionProgress("parent"))
        hook(flow.ProvisionProgress("index"))
        hook(flow.ProvisionProgress("subtask", "task-a", 1, 2))
        hook(flow.ProvisionProgress("retry-wait", wait_seconds=4))
        hook(flow.ProvisionProgress("sync"))
        hook(flow.ProvisionProgress("stopped"))
        before = stream.getvalue()
        hook(flow.ProvisionProgress("parent"))
        assert stream.getvalue() == before
        stream.write("Provision stopped: bad\n")
    assert "[1/2] task-a (retrying in 4s)" in updates[-2]
    assert "syncing plan" in updates[-1]
    assert stream.getvalue().endswith("Provision stopped: bad\n")
    assert "\x1b[?25h" in stream.getvalue()


def test_long_display_does_not_erase_stderr_warning(monkeypatch):
    stream = Tty()
    monkeypatch.setattr(sys, "stderr", stream)
    with cli.ProvisionProgressDisplay(make_output(stream, env={}, width=24)) as hook:
        hook(flow.ProvisionProgress("subtask", "危険\x1b[31m[red]" * 30, 1, 2))
        print("Warning: retained", file=sys.stderr)
        marker = len(stream.getvalue())
        hook(flow.ProvisionProgress("sync"))
        # Refresh must erase only its current line, leaving the warning above it.
        assert "\x1b[1A" not in stream.getvalue()[marker:]
    # Live.stop emits a newline then moves up once to erase its own transient
    # line; an extra move would erase the warning.
    assert "\x1b[2A" not in stream.getvalue()[marker:]
    assert "\x1b[31m" not in stream.getvalue()


@pytest.mark.parametrize("failure", [RuntimeError("boom"), KeyboardInterrupt()])
def test_display_restores_cursor_on_body_failure(failure):
    stream = Tty()
    with pytest.raises(type(failure)):
        with cli.ProvisionProgressDisplay(make_output(stream, env={})):
            raise failure
    assert "\x1b[?25h" in stream.getvalue()


def test_render_failure_disables_once_and_keeps_work_running(monkeypatch, capsys):
    stream = Tty()
    with cli.ProvisionProgressDisplay(make_output(stream, env={})) as hook:
        monkeypatch.setattr(
            cli_progress.ProgressHandle,
            "update",
            lambda *a: (_ for _ in ()).throw(OSError("broken")),
        )
        hook(flow.ProvisionProgress("parent"))
        stopped = stream.getvalue()
        hook(flow.ProvisionProgress("sync"))
        assert stream.getvalue() == stopped
    assert capsys.readouterr().err.count("Warning:") == 1
    assert "\x1b[?25h" in stream.getvalue()


@pytest.mark.parametrize(
    "env", [{"CI": "1"}, {"TERM": "dumb"}, {"ORCHESTUNE_CLI_PLAIN": "1"}]
)
def test_plain_display_is_silent(env):
    stream = Tty()
    with cli.ProvisionProgressDisplay(make_output(stream, env=env)) as hook:
        assert hook is None
    assert stream.getvalue() == ""


@pytest.mark.parametrize("failure", [OSError("render"), KeyboardInterrupt()])
def test_initial_update_failure_cleans_up(failure, monkeypatch, capsys):
    stream = Tty()

    def fail(*args):
        raise failure

    monkeypatch.setattr(cli_progress.ProgressHandle, "update", fail)
    if isinstance(failure, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            with cli.ProvisionProgressDisplay(make_output(stream, env={})):
                pytest.fail("must not enter")
    else:
        with cli.ProvisionProgressDisplay(make_output(stream, env={})) as hook:
            hook(flow.ProvisionProgress("parent"))
        assert capsys.readouterr().err.count("Warning:") == 1
    assert "\x1b[?25h" in stream.getvalue()


@pytest.mark.parametrize("decorated", [False, True])
def test_main_passes_hook_only_for_decorated_output(decorated, monkeypatch, plan_path):
    stream = Tty() if decorated else io.StringIO()
    output = make_output(stream, env={})
    monkeypatch.setattr(cli, "make_output", lambda *a: output)
    monkeypatch.setattr(cli, "GitHubForge", FakeForge)
    seen = []

    def provision(*args, on_progress=None, **kwargs):
        seen.append(on_progress)
        return flow.ProvisionResult(100, True, {}, {})

    monkeypatch.setattr(cli, "_provision_issues", provision)
    with pytest.raises(SystemExit) as result:
        cli.main(["--plan", str(plan_path)])
    assert result.value.code == 0
    assert (seen[0] is not None) == decorated

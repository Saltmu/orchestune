from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

from orchestune.dispatch import doctor_cli
from orchestune.dispatch.doctor import DoctorInputError
from orchestune.dispatch.doctor_models import (
    OWNERSHIP_NOTICE,
    Diagnostic,
    DoctorReport,
)
from orchestune.infra.cli_output import CliOutput, make_output

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class FakeStream(io.StringIO):
    def __init__(self, *, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"


def _output(*, tty: bool) -> tuple[CliOutput, FakeStream]:
    stream = FakeStream(tty=tty)
    return make_output(stream, env={}, width=140), stream


def _report(*diagnostics: Diagnostic) -> DoctorReport:
    return DoctorReport("actions", diagnostics)


_OK = Diagnostic("dispatch.config.readable", "ok", "config is readable")
_ERROR = Diagnostic(
    "dispatch.workflow.readable",
    "error",
    "workflow cannot be read",
    evidence=("path: .github/workflows/w.yml", "cause: missing"),
    remediation="create the workflow file",
)
_WARNING = Diagnostic(
    "dispatch.actions.group", "warning", "group differs", evidence=("group: x",)
)
_UNCHECKED = Diagnostic(
    "dispatch.external_ownership", "not_checked", "ownership not checked"
)


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(doctor_cli, "resolve_repository_root", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _workflow(repo: Path, text: str = "on: push\n") -> None:
    path = repo / ".github" / "workflows" / "w.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_exit_0_text(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert doctor_cli.main(["--execution-mode", "local"]) == 0
    out = capsys.readouterr().out
    assert "mode: local" in out
    assert "ownership_status: unverified" in out
    assert OWNERSHIP_NOTICE in out


def test_exit_1_still_prints_everything(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["--execution-mode", "actions", "--workflow", ".github/workflows/w.yml"]
    assert doctor_cli.main(argv) == 1
    out = capsys.readouterr().out
    assert "dispatch.workflow.readable" in out
    assert "dispatch.external_ownership" in out


def test_json_output(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _workflow(
        repo,
        "on: push\n"
        "concurrency:\n"
        "  group: orchestune-control-${{ github.repository }}\n"
        "  cancel-in-progress: false\n"
        "jobs:\n"
        "  ctl:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: orchestune dispatch --dispatch-target cloud-routine\n"
        "        env:\n"
        "          ORCHESTUNE_ROUTINE_ID: id\n"
        "          ORCHESTUNE_ROUTINE_TOKEN: token\n",
    )
    argv = ["--execution-mode", "actions", "--workflow", ".github/workflows/w.yml"]
    assert doctor_cli.main([*argv, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["schema_version"] == 1
    assert data["notice"] == OWNERSHIP_NOTICE
    assert data["ownership_status"] == "unverified"


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--execution-mode", "actions"],
        ["--execution-mode", "local", "--workflow", "w.yml"],
        ["--execution-mode", "local", "--offline"],
    ],
)
def test_argument_errors_exit_2(repo: Path, argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        doctor_cli.main(argv)
    assert exc.value.code == 2


def test_workflow_outside_repo_exits_2(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["--execution-mode", "actions", "--workflow", "../outside.yml"]
    assert doctor_cli.main(argv) == 2
    assert "outside the repository" in capsys.readouterr().err


def test_not_a_git_repository_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail() -> Path:
        raise DoctorInputError("not a git repository: x")

    monkeypatch.setattr(doctor_cli, "resolve_repository_root", fail)
    assert doctor_cli.main(["--execution-mode", "local"]) == 2
    assert "not a git repository" in capsys.readouterr().err


def test_help_has_no_offline_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        doctor_cli.main(["--help"])
    assert exc.value.code == 0
    assert "--offline" not in capsys.readouterr().out


class TestPlainText:
    def test_outcomes_use_text_labels_not_icons(self) -> None:
        text = doctor_cli.render_text(_report(_OK, _WARNING, _ERROR, _UNCHECKED))
        assert "[OK] dispatch.config.readable: config is readable" in text
        assert "[WARN] dispatch.actions.group: group differs" in text
        assert "[FAIL] dispatch.workflow.readable: workflow cannot be read" in text
        assert "[SKIPPED] dispatch.external_ownership: ownership not checked" in text
        assert not any(icon in text for icon in "✓⚠✗○")

    def test_evidence_and_remediation_are_listed_for_non_ok(self) -> None:
        text = doctor_cli.render_text(_report(_OK, _ERROR))
        assert "    - path: .github/workflows/w.yml" in text
        assert "    - cause: missing" in text
        assert "    → create the workflow file" in text

    def test_configuration_success_is_not_shown_as_ownership_verified(self) -> None:
        lines = doctor_cli.render_text(_report(_OK)).splitlines()
        assert "[OK] configuration_status: valid" in lines
        assert "[UNVERIFIED] ownership_status: unverified" in lines
        assert OWNERSHIP_NOTICE in lines
        assert not any("ownership" in line and "[OK]" in line for line in lines)

    def test_invalid_configuration_is_a_failure_label(self) -> None:
        lines = doctor_cli.render_text(_report(_ERROR)).splitlines()
        assert "[FAIL] configuration_status: invalid" in lines

    def test_unverified_configuration_label(self) -> None:
        lines = doctor_cli.render_text(_report(_OK, _WARNING)).splitlines()
        assert "[OK] configuration_status: valid" in lines
        report = _report(Diagnostic("dispatch.actions.group", "not_checked", "n"))
        assert "[UNVERIFIED] configuration_status: unverified" in (
            doctor_cli.render_text(report).splitlines()
        )


class TestRichDisplay:
    def test_table_and_details_keep_every_fact(self) -> None:
        out, stream = _output(tty=True)
        report = _report(_OK, _WARNING, _ERROR, _UNCHECKED)

        doctor_cli.render_report(out, report)

        text = ANSI.sub("", stream.getvalue())
        for expected in (
            "Orchestune Dispatch Doctor (mode: actions)",
            "dispatch.config.readable",
            "config is readable",
            "dispatch.workflow.readable",
            "workflow cannot be read",
            "[FAIL]",
            "[WARN]",
            "path: .github/workflows/w.yml",
            "cause: missing",
            "create the workflow file",
            "group: x",
            "configuration_status: invalid",
            "[UNVERIFIED] ownership_status: unverified",
            OWNERSHIP_NOTICE,
        ):
            assert expected in text, expected

    def test_configuration_success_still_shows_unverified_ownership(self) -> None:
        out, stream = _output(tty=True)

        doctor_cli.render_report(out, _report(_OK))

        text = ANSI.sub("", stream.getvalue())
        assert "configuration_status: valid" in text
        assert "ownership_status: unverified" in text
        assert "[UNVERIFIED]" in text
        assert OWNERSHIP_NOTICE in text

    def test_diagnostic_text_cannot_inject_terminal_sequences(self) -> None:
        out, stream = _output(tty=True)
        evil = Diagnostic(
            "dispatch.x",
            "error",
            "msg\x1b[2J",
            evidence=("ev\x1b]0;t\x07",),
            remediation="fix\x1b[31m",
        )

        doctor_cli.render_report(out, _report(evil))

        raw = stream.getvalue()
        assert "\x1b[2J" not in raw and "\x1b]0;" not in raw
        assert "\x1b[31m" not in raw

    def test_plain_stream_matches_render_text(self) -> None:
        out, stream = _output(tty=False)
        report = _report(_OK, _ERROR)

        doctor_cli.render_report(out, report)

        assert stream.getvalue() == doctor_cli.render_text(report) + "\n"

    def test_main_uses_the_injected_output_and_keeps_exit_codes(
        self, repo: Path
    ) -> None:
        out, stream = _output(tty=True)
        assert doctor_cli.main(["--execution-mode", "local"], output=out) == 0
        text = ANSI.sub("", stream.getvalue())
        assert "ownership_status: unverified" in text
        assert OWNERSHIP_NOTICE in text

        out, stream = _output(tty=True)
        argv = ["--execution-mode", "actions", "--workflow", ".github/workflows/w.yml"]
        assert doctor_cli.main(argv, output=out) == 1
        assert "dispatch.workflow.readable" in ANSI.sub("", stream.getvalue())


class TestJsonIsNotDecorated:
    def test_json_on_a_tty_is_a_single_undecorated_document(self, repo: Path) -> None:
        out, stream = _output(tty=True)
        argv = ["--execution-mode", "local", "--json"]

        assert doctor_cli.main(argv, output=out) == 0

        raw = stream.getvalue()
        assert "\x1b" not in raw
        data = json.loads(raw)
        assert set(data) == {
            "schema_version",
            "mode",
            "configuration_status",
            "ownership_status",
            "notice",
            "diagnostics",
        }
        assert raw == json.dumps(data, ensure_ascii=False, indent=2) + "\n"

    def test_json_goes_to_stdout_by_default(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert doctor_cli.main(["--execution-mode", "local", "--json"]) == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out)["mode"] == "local"
        assert captured.err == ""

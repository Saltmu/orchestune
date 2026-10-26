import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_remediation_preview import FakeForge
from test_remediation_spec import write_spec

from orchestune.remediation.cli import main


def test_cli_prints_scope_and_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--spec", str(write_spec(tmp_path))], forge=FakeForge()) == 0
    output = capsys.readouterr().out
    for text in (
        "Works",
        "#10",
        "Required to finish",
        "src/a.py",
        "depends_on",
        "remediate-preview-v1:sha256:",
    ):
        assert text in output


def test_apply_requires_confirmation() -> None:
    with pytest.raises(SystemExit) as error:
        main(["--spec", "x", "--apply"])
    assert error.value.code == 2


def test_cli_applies_confirmed_preview(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from test_remediation_apply import ApplyForge, token

    forge = ApplyForge()
    path = write_spec(tmp_path)
    assert (
        main(
            ["--spec", str(path), "--apply", "--confirm-preview", token(path, forge)],
            forge=forge,
        )
        == 0
    )
    assert "orchestune dispatch -p 1" in capsys.readouterr().out


def test_confirmation_without_apply_is_rejected() -> None:
    with pytest.raises(SystemExit):
        main(["--spec", "x", "--confirm-preview", "token"])


def test_cli_input_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--spec", str(tmp_path / "missing")], forge=FakeForge()) == 2
    path = write_spec(tmp_path)
    assert main(["--spec", str(path), "--parent-issue", "2"], forge=FakeForge()) == 2
    assert "conflicts" in capsys.readouterr().err


def test_cli_reports_every_problem(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_spec(tmp_path, parent_acceptance=["Unknown"], origins=["#99"])
    assert (
        main(["--spec", str(path), "--template", "custom.md"], forge=FakeForge()) == 7
    )
    text = capsys.readouterr().out
    assert "unmatched parent_acceptance" in text
    assert "invalid origin" in text
    assert "replan" in text


class _TtyStream(io.StringIO):
    """A stand-in for an interactive UTF-8 terminal stream."""

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"

    def isatty(self) -> bool:
        return True


def _use_tty(monkeypatch: pytest.MonkeyPatch, columns: int) -> _TtyStream:
    stream = _TtyStream()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)
    for name in ("CI", "ORCHESTUNE_CLI_PLAIN", "NO_COLOR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLUMNS", str(columns))
    return stream


_TOKEN = "tok-" + "0123456789abcdef" * 4
_PREVIEW_TEXT = (
    "Remediation preview for parent Issue #693\n"
    "Spec revision: rev-1\n"
    "create: rem-1 (Issue new)\n"
    "  kind: test\n"
    "  parent_acceptance: AC-1\n"
    "  matched sources: ['AC-1']\n"
    "  origins: ['ci']\n"
    "  reason: missing\n"
    "  footprint: ['orchestune/a.py']\n"
    "  depends_on: []\n"
    f"Preview token: {_TOKEN}\n"
)


def _stub_preview(monkeypatch: pytest.MonkeyPatch, *, problems=(), exit_code=0):
    task = SimpleNamespace(
        subtask=SimpleNamespace(
            id="rem-1", footprint=["orchestune/a.py"], depends_on=[]
        ),
        kind="test",
        parent_acceptance="AC-1",
        origins=["ci"],
        reason="missing",
    )
    item = SimpleNamespace(
        action="create",
        task=task,
        issue_number=None,
        acceptance_sources=["AC-1"],
        footprint_diff=[],
        completed_steps=[],
        remaining_steps=[],
    )
    preview = SimpleNamespace(
        spec_revision="rev-1",
        tasks=(item,),
        problems=tuple(problems),
        preview_token=_TOKEN,
        exit_code=exit_code,
    )
    monkeypatch.setattr(
        "orchestune.remediation.cli.load_remediation_spec",
        lambda path: SimpleNamespace(parent_issue_number=None),
    )
    monkeypatch.setattr(
        "orchestune.remediation.cli.collect_remediation_snapshot",
        lambda forge, parent, spec: None,
    )
    monkeypatch.setattr(
        "orchestune.remediation.cli.build_remediation_preview",
        lambda spec, snapshot, template=None: preview,
    )
    monkeypatch.setattr("orchestune.remediation.cli.GitHubForge", lambda: None)


def test_plain_preview_stdout_is_unchanged(monkeypatch, capsys) -> None:
    _stub_preview(monkeypatch)
    assert main(["--spec", "spec.md", "--parent-issue", "693"]) == 0
    captured = capsys.readouterr()
    assert captured.out == _PREVIEW_TEXT
    assert captured.err == ""


def test_plain_problems_are_listed_and_exit_code_passes_through(
    monkeypatch, capsys
) -> None:
    problem = SimpleNamespace(category="conflict", message="two tasks claim a file")
    _stub_preview(monkeypatch, problems=[problem], exit_code=3)
    assert main(["--spec", "spec.md", "--parent-issue", "693"]) == 3
    assert capsys.readouterr().out == _PREVIEW_TEXT.replace(
        f"Preview token: {_TOKEN}\n",
        f"Error [conflict]: two tasks claim a file\nPreview token: {_TOKEN}\n",
    )


def test_plain_apply_prints_the_service_message(monkeypatch, capsys) -> None:
    result = SimpleNamespace(message="Applied remediation: 1 task", exit_code=0)
    monkeypatch.setattr(
        "orchestune.remediation.cli.apply_remediation",
        lambda *args, **kwargs: result,
    )
    argv = ["--spec", "spec.md", "--parent-issue", "693", "--apply"]
    assert main([*argv, "--confirm-preview", _TOKEN]) == 0
    assert capsys.readouterr().out == "Applied remediation: 1 task\n"


def test_tty_preview_keeps_token_and_footprint_intact_on_narrow_width(
    monkeypatch,
) -> None:
    _stub_preview(monkeypatch)
    stream = _use_tty(monkeypatch, 30)
    assert main(["--spec", "spec.md", "--parent-issue", "693"]) == 0
    assert stream.getvalue() == _PREVIEW_TEXT

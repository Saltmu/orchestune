from pathlib import Path

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

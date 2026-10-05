from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestune.dispatch import doctor_cli
from orchestune.dispatch.doctor import DoctorInputError
from orchestune.dispatch.doctor_models import OWNERSHIP_NOTICE


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

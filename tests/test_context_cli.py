"""`orchestune context` rebuilds and exports without a preview/apply switch."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from orchestune.context import cli as context_cli
from orchestune.context.cli import main
from orchestune.models import IssueRecord
from orchestune.outcome_record import OutcomeRecord
from tests.fake_forge_support import FakeForge


def _forge() -> FakeForge:
    forge = FakeForge()
    forge.issues[7] = IssueRecord(
        number=7,
        title="索引検索",
        body="説明\n",
        labels=(),
        created_at="2026-01-01T00:00:00Z",
    )
    forge.create_issue_comment(
        7,
        OutcomeRecord(result="done", issue=7, completion_id="completion-7").render(),
    )
    return forge


def test_command_is_registered_for_lazy_import() -> None:
    from orchestune.cli import _COMMAND_SPECS

    spec = next(item for item in _COMMAND_SPECS if item.name == "context")
    assert spec.module == "orchestune.context.cli"
    assert spec.function == "main"
    assert spec.exit_with_return is True


@pytest.mark.parametrize(
    "argv", [["--help"], ["rebuild", "--help"], ["export", "--help"]]
)
def test_help_has_no_preview_apply_switch(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as caught:
        main(argv)

    assert caught.value.code == 0
    text = capsys.readouterr().out
    assert "rebuild" in text
    assert "export" in text
    for flag in ("--apply", "--no-apply", "--dry-run"):
        assert flag not in text


def test_rebuild_and_export_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    forge = _forge()
    monkeypatch.setattr(context_cli, "GitHubForge", lambda: forge)
    database = tmp_path / "context-index.sqlite3"

    status = main(
        [
            "rebuild",
            "--repository",
            "Saltmu/orchestune",
            "--issue",
            "7",
            "--db",
            str(database),
            "--indexed-at",
            "2026-01-05T00:00:00Z",
        ]
    )
    rebuilt = json.loads(capsys.readouterr().out)
    exported_status = main(["export", "--db", str(database)])
    exported = json.loads(capsys.readouterr().out)

    assert status == 0
    assert rebuilt["published"] is True
    assert exported_status == 0
    assert exported["status"] == "ready"
    assert exported["artifacts"][0]["title"] == "索引検索"
    assert exported["artifacts"][0]["completion_id"] == "completion-7"


def test_export_of_a_missing_index_is_unavailable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = main(["export", "--db", str(tmp_path / "missing.sqlite3")])
    payload = json.loads(capsys.readouterr().out)

    assert status == 0
    assert payload["status"] == "unavailable"
    assert payload["artifacts"] == []


def test_rebuild_exits_when_fetch_raises_subprocess_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(forge: object, request: object) -> object:
        raise subprocess.CalledProcessError(1, ["gh", "api"])

    monkeypatch.setattr(context_cli, "fetch_context", fail)

    status = main(
        [
            "rebuild",
            "--repository",
            "Saltmu/orchestune",
            "--issue",
            "7",
            "--db",
            str(tmp_path / "context-index.sqlite3"),
        ]
    )

    assert status == 1
    assert "non-zero" in capsys.readouterr().err


def test_rebuild_requires_a_selector(capsys: pytest.CaptureFixture[str]) -> None:
    status = main(["rebuild", "--repository", "Saltmu/orchestune"])

    assert status == 2
    assert "specify" in capsys.readouterr().err

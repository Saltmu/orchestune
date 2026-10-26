from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestune.replan.apply import apply_replan
from orchestune.replan.cli import main
from tests.test_replan_apply import PARENT, TEMPLATE, FakeReplanForge, preview_token


@pytest.fixture
def replan_files(tmp_path: Path) -> tuple[Path, Path]:
    plan = tmp_path / "decomposition_plan.md"
    plan.write_text(
        """---
title: New plan
parent_issue_number: 693
parent_issue_source: adopted
subtasks:
  - id: task-a
    description: Prepare A
    footprint: [orchestune/a.py]
  - id: task-b
    description: Prepare B
    footprint: [orchestune/b.py]
    depends_on: [task-a]
---
""",
        encoding="utf-8",
    )
    template = tmp_path / "issue_template.md"
    template.write_text(TEMPLATE, encoding="utf-8")
    return plan, template


def test_preview_is_read_only_and_prints_token_and_target_issues(
    replan_files: tuple[Path, Path], capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    plan, _ = replan_files
    forge = FakeReplanForge()

    monkeypatch.setattr("orchestune.replan.cli.GitHubForge", lambda: forge)
    assert main(["--plan", str(plan), "--parent-issue", str(PARENT)]) == 0

    output = capsys.readouterr().out
    assert "Preview token:" in output
    assert "create: task-a" in output
    assert "retire: old-a (#10)" in output
    assert forge.mutations == []


def test_unsafe_preview_returns_distinct_conflict_exit_code(
    replan_files: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch,
) -> None:
    plan, _ = replan_files
    forge = FakeReplanForge(old_status="status:in-progress")

    monkeypatch.setattr("orchestune.replan.cli.GitHubForge", lambda: forge)
    assert main(["--plan", str(plan), "--parent-issue", str(PARENT)]) == 6

    assert "manual-review: old-a (#10)" in capsys.readouterr().out
    assert forge.mutations == []


def test_apply_requires_current_confirmation_token_without_mutation(
    replan_files: tuple[Path, Path], capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    plan, _ = replan_files
    forge = FakeReplanForge()

    monkeypatch.setattr("orchestune.replan.cli.GitHubForge", lambda: forge)
    assert main(["--plan", str(plan), "--parent-issue", str(PARENT), "--apply"]) == 3

    assert "--confirm-preview" in capsys.readouterr().err
    assert forge.mutations == []


def test_apply_unsafe_preview_returns_conflict_exit_code(
    replan_files: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch,
) -> None:
    plan, _ = replan_files
    forge = FakeReplanForge(old_status="status:in-progress")
    token = preview_token(forge, plan)

    monkeypatch.setattr("orchestune.replan.cli.GitHubForge", lambda: forge)
    assert (
        main(
            [
                "--plan",
                str(plan),
                "--parent-issue",
                str(PARENT),
                "--apply",
                "--confirm-preview",
                token,
            ]
        )
        == 6
    )

    assert "manual review" in capsys.readouterr().err
    assert forge.mutations == []


def test_apply_reports_partial_failure_with_distinct_exit_code(
    replan_files: tuple[Path, Path], capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    plan, template = replan_files
    forge = FakeReplanForge()
    forge.fail_after = "create:100"
    token = preview_token(forge, plan)

    monkeypatch.setattr("orchestune.replan.cli.GitHubForge", lambda: forge)
    assert (
        main(
            [
                "--plan",
                str(plan),
                "--template",
                str(template),
                "--parent-issue",
                str(PARENT),
                "--apply",
                "--confirm-preview",
                token,
            ]
        )
        == 4
    )

    assert "did not complete" in capsys.readouterr().err


def test_apply_reports_noop_with_distinct_exit_code(
    replan_files: tuple[Path, Path], monkeypatch
) -> None:
    plan, template = replan_files
    forge = FakeReplanForge()
    apply_replan(plan, preview_token(forge, plan), forge=forge, template_path=template)
    token = preview_token(forge, plan)

    monkeypatch.setattr("orchestune.replan.cli.GitHubForge", lambda: forge)
    assert (
        main(
            [
                "--plan",
                str(plan),
                "--template",
                str(template),
                "--apply",
                "--confirm-preview",
                token,
            ]
        )
        == 5
    )


def test_invalid_plan_is_a_configuration_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = tmp_path / "bad.md"
    plan.write_text("---\nsubtasks: []\n---\n", encoding="utf-8")

    assert main(["--plan", str(plan), "--parent-issue", str(PARENT)]) == 2
    assert "Error:" in capsys.readouterr().err


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


_PREVIEW_TEXT = (
    "Replan preview for parent Issue #693\n"
    "Plan revision: rev-1\n"
    "create: task-a — new\n"
    "retire: old-a (#10) — superseded\n"
    "Preview token: tok-1\n"
)


def _decision(action: str, subject: str, issue_number, reason: str):
    return SimpleNamespace(
        action=action, subject=subject, issue_number=issue_number, reason=reason
    )


def _stub_preview(monkeypatch: pytest.MonkeyPatch, decisions, token: str = "tok-1"):
    preview = SimpleNamespace(
        plan_revision="rev-1", decisions=tuple(decisions), preview_token=token
    )
    monkeypatch.setattr("orchestune.replan.cli.GitHubForge", lambda: None)
    monkeypatch.setattr(
        "orchestune.replan.cli.load_replan_plan",
        lambda path: SimpleNamespace(parent_issue_number=693),
    )
    monkeypatch.setattr(
        "orchestune.replan.cli.collect_replan_snapshot", lambda forge, parent: None
    )
    monkeypatch.setattr(
        "orchestune.replan.cli.build_replan_preview",
        lambda plan, snapshot: preview,
    )


_SAFE_DECISIONS = (
    _decision("create", "task-a", None, "new"),
    _decision("retire", "old-a", 10, "superseded"),
)


def test_plain_preview_stdout_is_unchanged(monkeypatch, capsys) -> None:
    _stub_preview(monkeypatch, _SAFE_DECISIONS)
    assert main(["--parent-issue", "693"]) == 0
    captured = capsys.readouterr()
    assert captured.out == _PREVIEW_TEXT
    assert captured.err == ""


def test_plain_unsafe_preview_reports_conflict_on_stderr(monkeypatch, capsys) -> None:
    _stub_preview(monkeypatch, [_decision("manual-review", "old-a", 10, "in use")])
    assert main(["--parent-issue", "693"]) == 6
    captured = capsys.readouterr()
    assert captured.out == (
        "Replan preview for parent Issue #693\n"
        "Plan revision: rev-1\n"
        "manual-review: old-a (#10) — in use\n"
        "Preview token: tok-1\n"
    )
    assert captured.err == "Error: preview contains conflicts requiring manual review\n"


def test_plain_apply_without_confirmation_keeps_preview_and_error(
    monkeypatch, capsys
) -> None:
    _stub_preview(monkeypatch, _SAFE_DECISIONS)
    assert main(["--parent-issue", "693", "--apply"]) == 3
    captured = capsys.readouterr()
    assert captured.out == _PREVIEW_TEXT
    assert captured.err == "Error: --apply requires --confirm-preview TOKEN\n"


def test_plain_apply_success_and_noop_messages_are_unchanged(
    monkeypatch, capsys
) -> None:
    _stub_preview(monkeypatch, _SAFE_DECISIONS)
    applied = SimpleNamespace(
        degraded=False,
        created_issue_numbers=(11,),
        reused_issue_numbers=(),
        retired_issue_numbers=(10,),
    )
    monkeypatch.setattr("orchestune.replan.cli.apply_replan", lambda *a, **k: applied)
    argv = ["--parent-issue", "693", "--apply", "--confirm-preview", "tok-1"]
    assert main(argv) == 0
    assert capsys.readouterr().out == (
        _PREVIEW_TEXT + "Applied replan: created=1, reused=0, retired=1\n"
    )

    noop = SimpleNamespace(
        degraded=False,
        created_issue_numbers=(),
        reused_issue_numbers=(),
        retired_issue_numbers=(),
    )
    monkeypatch.setattr("orchestune.replan.cli.apply_replan", lambda *a, **k: noop)
    assert main(argv) == 5
    assert capsys.readouterr().out == (
        _PREVIEW_TEXT + "No-op: this generation is already active.\n"
    )


def test_plain_apply_failure_and_config_error_stay_on_stderr(
    monkeypatch, capsys
) -> None:
    _stub_preview(monkeypatch, _SAFE_DECISIONS)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("orchestune.replan.cli.apply_replan", boom)
    argv = ["--parent-issue", "693", "--apply", "--confirm-preview", "tok-1"]
    assert main(argv) == 4
    assert capsys.readouterr().err == (
        "Error: replan did not complete; run a new preview before retrying: boom\n"
    )

    def bad_plan(path):
        raise ValueError("bad plan")

    monkeypatch.setattr("orchestune.replan.cli.load_replan_plan", bad_plan)
    assert main(["--parent-issue", "693"]) == 2
    assert capsys.readouterr().err == "Error: bad plan\n"


def test_tty_preview_keeps_long_token_intact_on_narrow_width(monkeypatch) -> None:
    long_token = "tok-" + "0123456789abcdef" * 5
    _stub_preview(monkeypatch, _SAFE_DECISIONS, token=long_token)
    stream = _use_tty(monkeypatch, 30)
    assert main(["--parent-issue", "693"]) == 0
    assert stream.getvalue() == _PREVIEW_TEXT.replace("tok-1", long_token)

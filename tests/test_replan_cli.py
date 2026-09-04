from __future__ import annotations

from pathlib import Path

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


def test_apply_requires_current_confirmation_token_without_mutation(
    replan_files: tuple[Path, Path], capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    plan, _ = replan_files
    forge = FakeReplanForge()

    monkeypatch.setattr("orchestune.replan.cli.GitHubForge", lambda: forge)
    assert main(["--plan", str(plan), "--parent-issue", str(PARENT), "--apply"]) == 3

    assert "--confirm-preview" in capsys.readouterr().err
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

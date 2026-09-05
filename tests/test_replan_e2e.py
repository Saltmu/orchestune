"""FakeForge end-to-end coverage for a stale decomposition replacement."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestune.replan.cli import main
from tests.test_replan_apply import (
    PARENT,
    PLAN_TEXT,
    TEMPLATE,
    FakeReplanForge,
    preview_token,
)

pytestmark = pytest.mark.e2e


def test_stale_footprint_generation_replaces_one_old_issue_with_two_new_issues(
    tmp_path: Path, monkeypatch
) -> None:
    """A coarse, stale Issue is retired only after two new footprint Issues exist."""

    plan = tmp_path / "decomposition_plan.md"
    template = tmp_path / "issue_template.md"
    plan.write_text(PLAN_TEXT, encoding="utf-8")
    template.write_text(TEMPLATE, encoding="utf-8")
    forge = FakeReplanForge()
    token = preview_token(forge, plan)

    monkeypatch.setattr("orchestune.replan.cli.GitHubForge", lambda: forge)
    assert main(["--plan", str(plan), "--parent-issue", str(PARENT)]) == 0
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
        == 0
    )

    assert forge.issues[10]["state"] == "CLOSED"
    assert {100, 101}.issubset(forge.issues)

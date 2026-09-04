from __future__ import annotations

from pathlib import Path

import pytest

from orchestune.replan.apply import apply_replan
from orchestune.replan.audit import replan_audit_marker, retirement_marker
from orchestune.replan.plan import compute_plan_revision, load_replan_plan
from tests.test_replan_apply import (
    PARENT,
    PLAN_TEXT,
    TEMPLATE,
    FakeReplanForge,
    preview_token,
)


@pytest.fixture
def recovery_files(tmp_path: Path) -> tuple[Path, Path]:
    plan = tmp_path / "decomposition_plan.md"
    plan.write_text(PLAN_TEXT, encoding="utf-8")
    template = tmp_path / "issue_template.md"
    template.write_text(TEMPLATE, encoding="utf-8")
    return plan, template


@pytest.mark.parametrize(
    "failed_operation",
    [
        "create:100",
        "parent:100",
        "blocked:101:100",
        "comment:retirement:10",
        "label+:10:status:not-needed",
        "label-:10:status:queued",
        "close:10",
        "detach:10",
        "parent-body",
        f"comment:audit:{PARENT}",
    ],
)
def test_repreview_and_retry_resumes_each_partial_failure_without_duplicates(
    recovery_files: tuple[Path, Path], failed_operation: str
) -> None:
    plan, template = recovery_files
    forge = FakeReplanForge()
    original_token = preview_token(forge, plan)
    forge.fail_after = failed_operation

    with pytest.raises(RuntimeError, match="simulated crash"):
        apply_replan(
            plan,
            original_token,
            forge=forge,
            template_path=template,
            repo_root=plan.parent,
        )

    fresh_token = preview_token(forge, plan)
    assert fresh_token != original_token
    with pytest.raises(ValueError, match="preview token"):
        apply_replan(
            plan,
            original_token,
            forge=forge,
            template_path=template,
            repo_root=plan.parent,
        )

    result = apply_replan(
        plan,
        fresh_token,
        forge=forge,
        template_path=template,
        repo_root=plan.parent,
    )

    assert result.plan_revision == compute_plan_revision(load_replan_plan(plan))
    assert len([number for number in forge.issues if 100 <= number < PARENT]) == 2
    assert len(forge.comments[10]) == 1
    assert retirement_marker(result.plan_revision) in forge.comments[10][0]
    assert len(forge.comments[PARENT]) == 1
    assert replan_audit_marker(result.plan_revision) in forge.comments[PARENT][0]
    assert forge.parents == {100: PARENT, 101: PARENT}
    assert forge.blockers == {101: {100}}


def test_retry_reuses_a_created_generation_even_if_the_local_writeback_failed(
    recovery_files: tuple[Path, Path],
) -> None:
    plan, template = recovery_files
    forge = FakeReplanForge()
    forge.fail_after = "create:100"

    with pytest.raises(RuntimeError):
        apply_replan(
            plan,
            preview_token(forge, plan),
            forge=forge,
            template_path=template,
            repo_root=plan.parent,
        )

    assert "issue_number: 100" not in plan.read_text(encoding="utf-8")
    result = apply_replan(
        plan,
        preview_token(forge, plan),
        forge=forge,
        template_path=template,
        repo_root=plan.parent,
    )
    assert result.reused_issue_numbers == (100,)
    assert result.created_issue_numbers == (101,)
    assert sorted(number for number in forge.issues if 100 <= number < PARENT) == [
        100,
        101,
    ]

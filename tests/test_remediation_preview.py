import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from test_remediation_spec import write_spec

from orchestune.models import IssueRecord, PrRecord
from orchestune.remediation.preview import build_remediation_preview, expected_footprint
from orchestune.remediation.snapshot import collect_remediation_snapshot
from orchestune.remediation.spec import load_remediation_spec


def issue(number: int, body: str = "", **kwargs: Any) -> IssueRecord:
    return IssueRecord(
        number, "[EPIC] Parent" if number == 1 else "Task", body, (), "", **kwargs
    )


class FakeForge:
    def __init__(self) -> None:
        self.parent = issue(
            1,
            "<!-- orchestune:decomposition-plan-parent -->\n- [ ] Works\n<!-- orchestune:decomposition-plan -->\n```yaml\nsubtasks:\n- id: old\n  issue_number: 10\n  acceptance_criteria: [Old works]\n```\n",
        )
        self.children = [issue(10)]
        self.existing: dict[int, IssueRecord] = {}
        self.prs: list[PrRecord] = []
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self.writes: list[str] = []
        self.merged = False

    def get_issue(self, number: int) -> IssueRecord | None:
        return {
            1: self.parent,
            **{i.number: i for i in self.children},
            **self.existing,
        }.get(number)

    def list_sub_issues(self, number: int) -> list[IssueRecord]:
        return self.children

    def find_issues_by_parent_metadata(self, number: int) -> list[IssueRecord]:
        return []

    def list_prs(self, state: str = "open", **kwargs: Any) -> list[PrRecord]:
        return self.prs

    def get_pull_request(self, number: int) -> PrRecord:
        return next(p for p in self.prs if p.number == number)

    def list_comments(self, number: int) -> list[dict[str, Any]]:
        return self.comments.get(number, [])

    def list_all_issue_comments(self, number: int) -> list[dict[str, Any]]:
        return self.comments.get(number, [])

    def list_pull_request_review_comments(self, number: int) -> list[dict[str, Any]]:
        return self.comments.get(number, [])

    def list_pull_request_reviews(self, number: int) -> list[dict[str, Any]]:
        return self.comments.get(number, [])

    def get_repository_slug(self) -> str:
        return "owner/repo"

    def get_merged_pr_timestamp(self, head: str, base: str) -> str | None:
        return "2026-01-01" if self.merged else None

    def get_issue_last_reopened_at(self, number: int) -> None:
        return None

    def is_current_branch_tip_merged_into(self, head: str, base: str) -> bool:
        return self.merged

    def __getattr__(self, name: str) -> Any:
        if name.startswith(("create", "update", "add", "remove", "set", "close")):

            def mutation(*args: Any, **kwargs: Any) -> None:
                self.writes.append(name)
                raise AssertionError("preview wrote to forge")

            return mutation
        raise AttributeError(name)


def preview(tmp_path: Path, forge: FakeForge, **changes: object) -> Any:
    spec = load_remediation_spec(write_spec(tmp_path, **changes))
    return build_remediation_preview(spec, collect_remediation_snapshot(forge, 1, spec))


def test_preview_is_read_only_and_deterministic(tmp_path: Path) -> None:
    forge = FakeForge()
    path = write_spec(tmp_path)
    before = path.read_bytes()
    result = preview(tmp_path, forge)
    assert result.exit_code == 0
    assert result.tasks[0].action == "new"
    assert result.tasks[0].acceptance_sources
    assert result.preview_token.startswith("remediate-preview-v1:sha256:")
    assert result.preview_token == preview(tmp_path, forge).preview_token
    assert forge.writes == []
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "changes",
    [
        {"parent_acceptance": ["New requirement"]},
        {"origins": ["#999"]},
        {"origins": ["https://github.com/other/repo/issues/10"]},
        {"kind": "feature"},
    ],
)
def test_scope_guard(tmp_path: Path, changes: dict[str, object]) -> None:
    assert preview(tmp_path, FakeForge(), **changes).exit_code == 7


@pytest.mark.parametrize(
    "state,merged,expected",
    [("OPEN", False, 0), ("CLOSED", False, 6), ("OPEN", True, 6)],
)
def test_parent_state(tmp_path: Path, state: str, merged: bool, expected: int) -> None:
    forge = FakeForge()
    forge.parent = replace(forge.parent, state=state)
    forge.merged = merged
    assert preview(tmp_path, forge).exit_code == expected


def test_existing_issue_diff_and_auxiliary_labels(tmp_path: Path) -> None:
    forge = FakeForge()
    forge.existing[42] = replace(
        issue(42, "```yaml\ndepends_on: [old]\n```"),
        labels=(
            "status:force-serial",
            "status:blocked-recompute",
            "ci:base-branch-red",
        ),
    )
    result = preview(tmp_path, forge, issue_number=42, depends_on=["old"])
    assert result.exit_code == 0
    assert result.tasks[0].footprint_diff
    forge.existing[42] = replace(forge.existing[42], labels=("status:in-progress",))
    assert preview(tmp_path, forge, issue_number=42).exit_code == 6


def test_all_problems_and_exit_precedence(tmp_path: Path) -> None:
    forge = FakeForge()
    forge.parent = replace(forge.parent, state="CLOSED")
    forge.existing[42] = issue(42, state="CLOSED")
    result = preview(tmp_path, forge, issue_number=42, parent_acceptance=["Unknown"])
    assert result.exit_code == 6
    assert len(result.problems) >= 3


def test_unknown_dependency_is_input_error(tmp_path: Path) -> None:
    assert preview(tmp_path, FakeForge(), depends_on=["missing"]).exit_code == 2


@pytest.mark.parametrize(
    "labels",
    [
        ("status:done",),
        ("status:queued", "status:blocked"),
        ("status:blocked-human-review",),
        ("status:not-needed",),
    ],
)
def test_rejects_protected_or_conflicting_lifecycle(
    tmp_path: Path, labels: tuple[str, ...]
) -> None:
    forge = FakeForge()
    forge.existing[42] = replace(issue(42), labels=labels)
    assert preview(tmp_path, forge, issue_number=42).exit_code == 6


@pytest.mark.parametrize(
    "body",
    [
        "```yaml\nother: value\n```",
        "```yaml\ndescription: unrelated example\n```",
        "```yaml\n42: unrelated example\n```",
        "```yaml\n[broken\n```",
        "```yaml\nsubtask_id: other\n```",
        "```yaml\n- value\n```",
    ],
)
def test_rejects_unrelated_or_malformed_footprint(tmp_path: Path, body: str) -> None:
    forge = FakeForge()
    forge.existing[42] = issue(42, body)
    assert preview(tmp_path, forge, issue_number=42).exit_code == 6


def test_native_parent_and_dependencies(tmp_path: Path) -> None:
    forge = FakeForge()
    forge.existing[42] = issue(42, parent={"number": 99})
    assert preview(tmp_path, forge, issue_number=42).exit_code == 6
    forge.existing[42] = issue(42, blocked_by=(10,))
    assert preview(tmp_path, forge, issue_number=42).exit_code == 6
    assert preview(tmp_path, forge, issue_number=42, depends_on=["old"]).exit_code == 0


@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com/owner/repo/issues/10#issuecomment-123",
        "https://github.com/owner/repo/pull/20#discussion_r123",
        "https://github.com/owner/repo/pull/20#pullrequestreview-123",
        "#20",
    ],
)
def test_origin_comments_and_parent_pr(tmp_path: Path, origin: str) -> None:
    forge = FakeForge()
    forge.prs = [PrRecord(20, "fix", (), base_ref="parent/issue-1")]
    forge.comments = {10: [{"id": 123}], 20: [{"id": 123}]}
    assert preview(tmp_path, forge, origins=[origin]).exit_code == 0
    if "#" in origin and not origin.startswith("#"):
        forge.comments = {}
        assert preview(tmp_path, forge, origins=[origin]).exit_code == 7


def test_unrelated_pr_and_bad_urls(tmp_path: Path) -> None:
    forge = FakeForge()
    forge.prs = [PrRecord(20, "other", (), base_ref="main")]
    for origin in (
        "#20",
        "https://github.com/owner/repo/pull/20",
        "https://evil.example/owner/repo/issues/10",
        "https://github.com/owner/repo/issues/10?bad=1",
        "https://github.com/owner/repo/issues/10#unknown",
    ):
        assert preview(tmp_path, forge, origins=[origin]).exit_code == 7


def test_issue_cannot_reference_a_pr_review_comment(tmp_path: Path) -> None:
    forge = FakeForge()
    forge.comments[10] = [{"id": 123}]
    assert (
        preview(
            tmp_path,
            forge,
            origins=["https://github.com/owner/repo/issues/10#discussion_r123"],
        ).exit_code
        == 7
    )


def test_parent_acceptance_normalization_and_plan_exclusion(tmp_path: Path) -> None:
    forge = FakeForge()
    assert preview(tmp_path, forge, parent_acceptance=[" Old   works "]).exit_code == 0
    forge.parent = replace(
        forge.parent,
        body=forge.parent.body.replace(
            "acceptance_criteria: [Old works]",
            "description: |\n    - [ ] Fake requirement",
        ),
    )
    assert (
        preview(tmp_path, forge, parent_acceptance=["Fake requirement"]).exit_code == 7
    )


def test_non_epic_and_missing_plan(tmp_path: Path) -> None:
    forge = FakeForge()
    forge.parent = replace(forge.parent, title="ordinary")
    assert preview(tmp_path, forge).exit_code == 2
    forge.parent = issue(1, "<!-- orchestune:decomposition-plan-parent -->")
    assert preview(tmp_path, forge).exit_code == 2


def registration(tmp_path: Path, forge: FakeForge) -> str:
    spec = load_remediation_spec(write_spec(tmp_path))
    task = spec.tasks[0]
    record = {
        "parent_issue_number": 1,
        "subtask_id": "fix",
        "spec_revision": spec.spec_revision,
    }
    body = (
        "```yaml\n"
        + yaml.safe_dump(expected_footprint(task, 1))
        + "```\n<!-- orchestune:remediation-v1 -->\n```json\n"
        + json.dumps(record)
        + "\n```\n"
    )
    forge.children.append(
        replace(issue(42, body, parent={"number": 1}), labels=("status:queued",))
    )
    return spec.spec_revision


def test_resume_registered_and_revision_mismatch(tmp_path: Path) -> None:
    forge = FakeForge()
    revision = registration(tmp_path, forge)
    result = preview(tmp_path, forge)
    assert result.exit_code == 0
    assert result.tasks[0].action == "resume"
    assert result.tasks[0].issue_number == 42
    assert "plan" in result.tasks[0].remaining_steps
    entry = {
        "id": "fix",
        "issue_number": 42,
        "remediation": {"spec_revision": revision},
    }
    from orchestune.issue_parsing import (
        decomposition_plan_from_parent_body,
        embed_decomposition_plan_in_parent_body,
    )

    plan = decomposition_plan_from_parent_body(forge.parent.body)
    assert plan is not None
    plan["subtasks"].append(entry)
    forge.parent = replace(
        forge.parent,
        body=embed_decomposition_plan_in_parent_body(forge.parent.body, plan),
    )
    forge.comments[1] = [
        {"body": f"<!-- orchestune:remediation-audit spec_revision={revision} -->"}
    ]
    assert preview(tmp_path, forge).exit_code == 5
    assert preview(tmp_path, forge).tasks[0].action == "registered"
    assert preview(tmp_path, forge, reason="Changed").exit_code == 6


def test_bad_remediation_record_and_duplicate_child(tmp_path: Path) -> None:
    forge = FakeForge()
    registration(tmp_path, forge)
    forge.children[-1] = replace(
        forge.children[-1],
        body=forge.children[-1].body.replace("```json\n{", "```json\n["),
    )
    assert preview(tmp_path, forge).exit_code == 6
    forge.children.append(replace(forge.children[-1], number=43))
    assert preview(tmp_path, forge).exit_code == 6


def test_resume_rejects_conflicting_lifecycle(tmp_path: Path) -> None:
    forge = FakeForge()
    registration(tmp_path, forge)
    forge.children[-1] = replace(
        forge.children[-1], labels=("status:queued", "status:blocked")
    )
    assert preview(tmp_path, forge).exit_code == 6


def test_final_pr_stage_uses_pending_warning_not_table(tmp_path: Path) -> None:
    forge = FakeForge()
    registration(tmp_path, forge)
    forge.prs = [
        PrRecord(
            20,
            "parent/issue-1",
            (),
            base_ref="main",
            body="⚠️ #420 pending\n\n| #42 | task |",
        )
    ]
    assert "final-pr" in preview(tmp_path, forge).tasks[0].remaining_steps
    forge.prs[0] = replace(
        forge.prs[0], body="Parent issue: #1\n\n⚠️ #42 pending\n\n| #42 | task |"
    )
    assert "final-pr" in preview(tmp_path, forge).tasks[0].completed_steps


def test_token_changes_with_remote_state(tmp_path: Path) -> None:
    forge = FakeForge()
    original = preview(tmp_path, forge).preview_token
    forge.children[0] = replace(forge.children[0], labels=("status:done",))
    assert original != preview(tmp_path, forge).preview_token
    original = preview(tmp_path, forge).preview_token
    forge.prs = [PrRecord(20, "parent/issue-1", (), base_ref="main")]
    assert original != preview(tmp_path, forge).preview_token


def test_self_cycle_and_missing_issue(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cycle"):
        preview(tmp_path, FakeForge(), depends_on=["fix"])
    assert preview(tmp_path, FakeForge(), issue_number=999).exit_code == 6

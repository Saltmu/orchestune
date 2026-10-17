from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
from test_remediation_preview import FakeForge, issue
from test_remediation_spec import write_spec

from orchestune.issue_parsing import (
    decomposition_plan_from_parent_body,
    parse_task_from_issue,
)
from orchestune.models import IssueRecord, PrRecord
from orchestune.remediation.apply import apply_remediation
from orchestune.remediation.preview import build_remediation_preview
from orchestune.remediation.snapshot import collect_remediation_snapshot
from orchestune.remediation.spec import load_remediation_spec


class ApplyForge(FakeForge):
    children: list[IssueRecord]
    prs: list[PrRecord]

    def get_issue(self, number: int) -> IssueRecord | None:
        return cast(IssueRecord | None, super().get_issue(number))

    def list_open_prs(self, **kwargs: Any) -> list[PrRecord]:
        return [p for p in self.prs if p.state.upper() == "OPEN"]

    def update_pull_request(self, number: int, *, title: str, body: str) -> None:
        self.writes.append("pr")
        self.prs = [
            replace(p, body=body, title=title) if p.number == number else p
            for p in self.prs
        ]

    def update_issue_body(self, number: int, body: str) -> None:
        self.writes.append("body")
        current = self.get_issue(number)
        assert current is not None
        updated = replace(current, body=body)
        if number == 1:
            self.parent = updated
        else:
            self.existing[number] = updated
            self.children = [
                updated if i.number == number else i for i in self.children
            ]

    def create_issue(self, title: str, body: str, labels: tuple[str, ...] = ()) -> int:
        number = 42 + len(self.existing)
        self.writes.append("create")
        self.existing[number] = replace(
            issue(number, body),
            title=title,
            labels=labels,
            created_at="2026-01-01T00:00:00Z",
        )
        return number

    def find_issues_by_parent_metadata(self, number: int) -> list[Any]:
        return [
            i
            for i in self.existing.values()
            if parse_task_from_issue(i).parent_number == number
        ]

    def add_sub_issue(self, parent: int, child: int) -> None:
        self.writes.append("parent")
        current = self.get_issue(child)
        assert current
        self.existing[child] = replace(current, parent={"number": parent})

    def set_blocked_by(self, number: int, dependency: int) -> None:
        self.writes.append("dependency")
        current = self.get_issue(number)
        assert current
        self.existing[number] = replace(
            current, blocked_by=tuple(set(current.blocked_by) | {dependency})
        )

    def add_label(self, number: int, label: str) -> None:
        self.writes.append("label")
        current = self.get_issue(number)
        assert current
        self.existing[number] = replace(current, labels=(*current.labels, label))

    def get_issue_labels(self, number: int) -> tuple[str, ...]:
        current = self.get_issue(number)
        assert current
        return current.labels

    def add_comment(self, number: int, body: str) -> None:
        self.writes.append("comment")
        self.comments.setdefault(number, []).append({"body": body})


def token(path: Path, forge: ApplyForge) -> str:
    spec = load_remediation_spec(path)
    return build_remediation_preview(
        spec, collect_remediation_snapshot(forge, 1, spec)
    ).preview_token


def test_new_task_preserves_parent_and_is_idempotent(tmp_path: Path) -> None:
    forge = ApplyForge()
    forge.parent = replace(
        forge.parent, body=forge.parent.body + "\n## Human Notes\r\nKeep me exactly\r\n"
    )
    path = write_spec(tmp_path, depends_on=["old"], kind="review-followup")
    result = apply_remediation(path, token(path, forge), forge=forge)
    assert result.exit_code == 0
    task = parse_task_from_issue(forge.existing[42])
    assert task.subtask_id == "fix" and task.depends_on == ("old",)
    assert task.native_depends_on == (10,)
    assert task.footprint == ("src/a.py",)
    assert task.parent_number == 1
    assert task.status_labels == ("status:blocked", "priority:medium")
    assert forge.parent.body.endswith("\n## Human Notes\nKeep me exactly\n")
    plan = decomposition_plan_from_parent_body(forge.parent.body)
    assert plan and [entry["id"] for entry in plan["subtasks"]] == ["old", "fix"]
    assert plan["subtasks"][-1]["remediation"]["reason"] == "Required to finish"
    assert load_remediation_spec(path).tasks[0].subtask.issue_number == 42
    assert apply_remediation(path, token(path, forge), forge=forge).exit_code == 5
    assert forge.writes.count("create") == 1
    assert forge.writes.count("comment") == 1


@pytest.mark.parametrize(
    "body",
    [
        "original\n```yaml\ndepends_on: [old]\n```\nNotes",
        "original\nblocked-by #10\nNotes",
    ],
)
def test_register_existing_issue(tmp_path: Path, body: str) -> None:
    forge = ApplyForge()
    forge.existing[50] = issue(50, body)
    path = write_spec(tmp_path, issue_number=50, depends_on=["old"])
    assert apply_remediation(path, token(path, forge), forge=forge).exit_code == 0
    updated = forge.existing[50]
    assert (
        updated.title == "Task"
        and "original" in updated.body
        and "Notes" in updated.body
    )
    assert parse_task_from_issue(updated).depends_on == ("old",)
    assert updated.labels == ("status:blocked",)
    assert "create" not in forge.writes


def test_stale_token_does_not_write(tmp_path: Path) -> None:
    forge = ApplyForge()
    path = write_spec(tmp_path)
    confirmed = token(path, forge)
    forge.parent = replace(forge.parent, state="CLOSED")
    before = path.read_bytes()
    assert apply_remediation(path, confirmed, forge=forge).exit_code == 3
    assert forge.writes == [] and path.read_bytes() == before


def test_resume_after_create_before_writeback(tmp_path: Path) -> None:
    forge = ApplyForge()
    path = write_spec(tmp_path)
    with patch(
        "orchestune.remediation.operations.write_issue_numbers",
        side_effect=OSError("disk"),
    ):
        assert apply_remediation(path, token(path, forge), forge=forge).exit_code == 4
    assert apply_remediation(path, token(path, forge), forge=forge).exit_code == 0
    assert forge.writes.count("create") == 1


def test_concurrent_parent_plan_change_is_preserved(tmp_path: Path) -> None:
    forge = ApplyForge()
    path = write_spec(tmp_path)
    original = forge.add_sub_issue

    def concurrent(parent: int, child: int) -> None:
        original(parent, child)
        forge.parent = replace(
            forge.parent, body=forge.parent.body.replace("id: old", "id: concurrent")
        )

    forge.add_sub_issue = concurrent  # type: ignore[method-assign]
    assert apply_remediation(path, token(path, forge), forge=forge).exit_code == 4
    assert "id: concurrent" in forge.parent.body
    assert "id: fix" not in forge.parent.body


@pytest.mark.parametrize(
    "failure",
    ["child-size", "parent-size", "dependency-number", "number-alias", "template"],
)
def test_preflight_fails_without_writes(tmp_path: Path, failure: str) -> None:
    forge = ApplyForge()
    changes: dict[str, object] = {}
    if failure == "child-size":
        forge.existing[50] = issue(50, "x" * 66000)
        changes["issue_number"] = 50
    elif failure == "parent-size":
        forge.parent = replace(forge.parent, body=forge.parent.body + "x" * 66000)
    elif failure == "dependency-number":
        forge.parent = replace(
            forge.parent, body=forge.parent.body.replace("  issue_number: 10\n", "")
        )
        changes["depends_on"] = ["old"]
    elif failure == "number-alias":
        changes["issue_number"] = 10
    path = write_spec(tmp_path, **changes)
    kwargs: dict[str, Any] = (
        {"template_path": tmp_path / "missing"} if failure == "template" else {}
    )
    before = path.read_bytes()
    assert (
        apply_remediation(path, token(path, forge), forge=forge, **kwargs).exit_code
        == 2
    )
    assert forge.writes == [] and path.read_bytes() == before


def test_audit_failure_resumes_without_duplicate_children_or_entries(
    tmp_path: Path,
) -> None:
    forge = ApplyForge()
    path = write_spec(tmp_path)
    with patch.object(forge, "add_comment", side_effect=RuntimeError("network")):
        assert apply_remediation(path, token(path, forge), forge=forge).exit_code == 4
    assert apply_remediation(path, token(path, forge), forge=forge).exit_code == 0
    plan = decomposition_plan_from_parent_body(forge.parent.body)
    assert plan and len(plan["subtasks"]) == 2
    assert forge.writes.count("create") == 1


def test_open_final_pr_is_pending_and_failed_update_resumes(tmp_path: Path) -> None:
    forge = ApplyForge()
    forge.prs = [
        PrRecord(
            20,
            "parent/issue-1",
            (),
            base_ref="main",
            body="Parent issue: #1",
            is_cross_repository=False,
        )
    ]
    path = write_spec(tmp_path)
    with patch.object(
        forge, "update_pull_request", side_effect=RuntimeError("network")
    ):
        assert apply_remediation(path, token(path, forge), forge=forge).exit_code == 4
    assert apply_remediation(path, token(path, forge), forge=forge).exit_code == 0
    assert "⚠️" in forge.prs[0].body and "#42" in forge.prs[0].body
    assert forge.writes.count("create") == 1


def test_partial_failure_during_labels_resumes_existing_registration(
    tmp_path: Path,
) -> None:
    forge = ApplyForge()
    forge.existing[50] = issue(50, "Original report")
    path = write_spec(tmp_path, issue_number=50)
    with patch.object(forge, "add_label", side_effect=RuntimeError("network")):
        assert apply_remediation(path, token(path, forge), forge=forge).exit_code == 4
    assert apply_remediation(path, token(path, forge), forge=forge).exit_code == 0
    assert forge.existing[50].labels == ("status:queued",)


def test_native_relationship_unavailable_uses_body_fallback(tmp_path: Path) -> None:
    from orchestune.forge import RelationshipUnavailableError

    forge = ApplyForge()
    path = write_spec(tmp_path, depends_on=["old"])
    with (
        patch.object(
            forge,
            "add_sub_issue",
            side_effect=RelationshipUnavailableError("unsupported"),
        ),
        patch.object(forge, "set_blocked_by", side_effect=NotImplementedError),
    ):
        assert apply_remediation(path, token(path, forge), forge=forge).exit_code == 0
    assert parse_task_from_issue(forge.existing[42]).parent_number == 1
    assert parse_task_from_issue(forge.existing[42]).depends_on == ("old",)
    assert apply_remediation(path, token(path, forge), forge=forge).exit_code == 5


def test_completed_dependencies_start_queued(tmp_path: Path) -> None:
    forge = ApplyForge()
    forge.children[0] = replace(forge.children[0], labels=("status:done",))
    path = write_spec(tmp_path, depends_on=["old"])
    assert apply_remediation(path, token(path, forge), forge=forge).exit_code == 0
    assert "status:queued" in forge.existing[42].labels


def test_normalized_subtask_id_round_trips(tmp_path: Path) -> None:
    forge = ApplyForge()
    path = write_spec(tmp_path, id=" fix ")
    assert apply_remediation(path, token(path, forge), forge=forge).exit_code == 0
    assert parse_task_from_issue(forge.existing[42]).subtask_id == "fix"


def test_no_discovery_backend_is_partial_failure(tmp_path: Path) -> None:
    forge = ApplyForge()
    path = write_spec(tmp_path)
    with (
        patch.object(forge, "add_sub_issue", side_effect=NotImplementedError),
        patch.object(forge, "find_issues_by_parent_metadata", return_value=[]),
    ):
        assert apply_remediation(path, token(path, forge), forge=forge).exit_code == 4
    assert 42 in forge.existing
    assert not forge.comments.get(1)


def test_dependency_link_failure_resumes(tmp_path: Path) -> None:
    forge = ApplyForge()
    path = write_spec(tmp_path, depends_on=["old"])
    with patch.object(forge, "set_blocked_by", side_effect=RuntimeError("network")):
        assert apply_remediation(path, token(path, forge), forge=forge).exit_code == 4
    assert apply_remediation(path, token(path, forge), forge=forge).exit_code == 0
    assert forge.writes.count("create") == 1


def test_resume_preserves_claim_metadata_and_running_status(tmp_path: Path) -> None:
    from orchestune.claim.issue_metadata import update_issue_claim_metadata

    forge = ApplyForge()
    path = write_spec(tmp_path)
    with patch.object(forge, "add_comment", side_effect=RuntimeError("network")):
        assert apply_remediation(path, token(path, forge), forge=forge).exit_code == 4
    current = forge.existing[42]
    body = update_issue_claim_metadata(
        current.body, "interactive", "claim-example", "footprint"
    )
    forge.existing[42] = replace(current, body=body, labels=("status:in-progress",))
    assert apply_remediation(path, token(path, forge), forge=forge).exit_code == 0
    assert forge.existing[42].body == body
    assert forge.existing[42].labels == ("status:in-progress",)
    assert "Works" in forge.comments[1][0]["body"]
    assert "#10" in forge.comments[1][0]["body"]


def test_registered_child_enters_cycle_dependency_and_conflict_evaluation(
    tmp_path: Path,
    fake_forge: Any,
) -> None:
    from orchestune.dependencies.assessment import DependencyState
    from orchestune.dispatch.cycle_context import _build_task_mappings
    from orchestune.dispatch.locks import ExternalLockScanResult
    from orchestune.dispatch.phase_scheduling import run_scheduling_phase
    from orchestune.ledger.run_state import RunState
    from orchestune.task_metadata import TaskMetadata
    from tests.dispatch_test_support import (
        flat_active_worktree,
        make_test_cycle_context,
        make_test_dispatcher_config,
        make_test_task,
        stub_label_actor_permission,
    )

    forge = ApplyForge()
    forge.children[0] = replace(
        issue(10, "```yaml\nsubtask_id: old\nfootprint: [src/old.py]\n```"),
        labels=("status:queued",),
    )
    path = write_spec(tmp_path, depends_on=["old"])
    assert apply_remediation(path, token(path, forge), forge=forge).exit_code == 0
    tasks, _, _ = _build_task_mappings([*forge.children, forge.existing[42]])
    ctx = make_test_cycle_context(
        state_root=tmp_path, resolve_dependencies=True, tasks_by_issue=tasks
    )
    assert isinstance(ctx.task(42), TaskMetadata)
    dependencies = ctx.assess_dependencies(42)
    assert dependencies and dependencies.resolved[0].issue_number == 10
    assert dependencies.resolved[0].state == DependencyState.WAITING
    tasks[10] = replace(tasks[10], status_labels=("status:done",))
    # The next cycle's consistency repair promotes blocked tasks whose dependencies completed.
    tasks[42] = replace(tasks[42], status_labels=("status:queued",))
    active = make_test_task(99, subtask_id="active", footprint=("src/a.py",))
    tasks[99] = active
    state = RunState(
        active_worktrees={
            "99": flat_active_worktree(
                issue_number=99,
                branch="task/active",
                worktree_path=str(tmp_path / "active"),
                pid=123,
                started_at=1.0,
                declared_footprint=active.footprint,
            )
        }
    )
    ctx = make_test_cycle_context(
        state_root=tmp_path,
        resolve_dependencies=True,
        tasks_by_issue=tasks,
        run_state=state,
        action_now=2.0,
        config=make_test_dispatcher_config(
            tmp_path, forge=fake_forge, apply=False, max_concurrent=3
        ),
    )
    stub_label_actor_permission(fake_forge)
    scheduling = run_scheduling_phase(
        ctx=ctx, lock_result=ExternalLockScanResult([], []), deviation_events=[]
    )
    assert 42 not in {t.issue_number for t in scheduling.selected}
    assert any(d.issue_number == 42 for d in scheduling.decisions)

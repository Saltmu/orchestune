"""#1162: per-scan current-tip evidence for completed direct dependencies."""

from dataclasses import replace
from unittest.mock import patch

import pytest

from orchestune.branch_naming import build_task_branch_name
from orchestune.dependencies.assessment import (
    AssessedDependency,
    DependencyAssessment,
    DependencyState,
)
from orchestune.dependencies.resolution import UnresolvedDependency
from orchestune.dispatch.locks import scan_external_locks
from orchestune.dispatch.phase_rebase import _decide_external_lock_sync
from orchestune.ledger.run_state import RunState
from orchestune.lock_contracts import CompletedDependencyBranchEvidence
from orchestune.models import PrRecord
from tests.dispatch_lock_test_support import LockDependencyTestView
from tests.test_dispatch_lock_assessment_policy import _FakeLockDependencyView
from tests.test_dispatch_locks_dependency_exclusion import _task

SHA = "a" * 40
BASE = "parent/issue-100"
BRANCH = build_task_branch_name(2, "dep")
EVIDENCE = CompletedDependencyBranchEvidence(BRANCH, BASE, SHA)


def _fixture(status="status:queued", **overrides):
    dep = _task(2, subtask_id="dep", status_labels=("status:done",), footprint=())
    task = _task(
        1,
        depends_on=("dep",),
        status_labels=(status, "status:external-lock"),
        **overrides,
    )
    view = LockDependencyTestView.from_tasks([dep, task])
    return dep, task, view


def _scan(task, view, evidence=EVIDENCE, branches=None, prs=(), key=(1, 2)):
    return scan_external_locks(
        [task],
        [(BRANCH, ("src/foo.py",))] if branches is None else branches,
        list(prs),
        [],
        view,
        completed_dependency_evidence={} if evidence is None else {key: evidence},
    )


@pytest.mark.parametrize("status", ["status:blocked", "status:queued"])
@pytest.mark.parametrize("files", [("src/foo.py",), None])
def test_included_tip_unlocks_even_when_branch_diff_is_unknown(status, files):
    _, task, view = _fixture(status)
    result = _scan(task, view, branches=[(BRANCH, files)])
    assert result.to_unlock == [task]
    assert result.conflicts == {}


@pytest.mark.parametrize(
    "evidence",
    [
        None,
        replace(EVIDENCE, branch_name="feature/other"),
        replace(EVIDENCE, base_ref="parent/issue-999"),
        replace(EVIDENCE, head_sha="bad"),
        replace(EVIDENCE, head_sha="g" * 40),
        replace(EVIDENCE, head_sha="a" * 39),
    ],
)
def test_invalid_evidence_retains_lock(evidence):
    _, task, view = _fixture()
    result = _scan(task, view, evidence)
    assert result.to_unlock == []
    assert 1 in result.conflicts


@pytest.mark.parametrize("key", [(9, 2), (1, 9)])
def test_evidence_does_not_apply_to_other_task_or_dependency(key):
    _, task, view = _fixture()
    assert 1 in _scan(task, view, key=key).conflicts


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_sha", "b" * 40),
        ("head_sha", ""),
        ("base_ref", "parent/issue-999"),
        ("is_cross_repository", True),
        ("is_cross_repository", None),
        ("head_ref", "feature/other"),
        ("state", "CLOSED"),
    ],
)
@pytest.mark.parametrize("truncated", [False, True])
def test_pr_with_different_provenance_retains_lock(field, value, truncated):
    _, task, view = _fixture()
    pr = PrRecord(
        90,
        BRANCH,
        ("src/foo.py",),
        base_ref=BASE,
        head_sha=SHA,
        is_cross_repository=False,
        is_files_truncated=truncated,
    )
    pr = replace(pr, **{field: value})
    result = _scan(task, view, branches=[], prs=[pr])
    assert result.to_unlock == []
    assert 1 in result.conflicts


@pytest.mark.parametrize("state", ["OPEN", "MERGED"])
def test_exact_pr_is_excluded_including_truncated_files(state):
    _, task, view = _fixture()
    pr = PrRecord(
        90,
        BRANCH,
        (),
        state=state,
        base_ref=BASE,
        head_sha=SHA,
        is_cross_repository=False,
        is_files_truncated=True,
    )
    assert _scan(task, view, branches=[], prs=[pr]).to_unlock == [task]


@pytest.mark.parametrize("files", [("src/foo.py",), None])
def test_other_conflict_keeps_lock(files):
    _, task, view = _fixture()
    result = _scan(task, view, branches=[(BRANCH, None), ("feature/other", files)])
    assert result.to_unlock == []
    assert [c.source for c in result.conflicts[1]] == ["feature/other"]


@pytest.mark.parametrize(
    "case",
    [
        "no-assessment",
        "unresolved",
        "waiting",
        "missing-task",
        "missing-branch",
        "wrong-branch",
        "empty-identity",
        "different-parent",
        "unknown-parent",
        "task-unknown-parent",
        "not-eligible",
        "not-direct",
    ],
)
def test_consumer_rechecks_dependency_identity_and_assessment(case):
    dep, task, _ = _fixture()
    assessment = DependencyAssessment(
        resolved=(AssessedDependency(2, DependencyState.COMPLETED),)
    )
    view = _FakeLockDependencyView(
        tasks={2: dep}, assessments={1: assessment}, branches={2: BRANCH}
    )
    if case == "no-assessment":
        view.assessments[1] = None
    elif case == "unresolved":
        view.assessments[1] = replace(
            assessment, unresolved=(UnresolvedDependency("missing", "missing"),)
        )
    elif case == "waiting":
        view.assessments[1] = DependencyAssessment(
            resolved=(AssessedDependency(2, DependencyState.WAITING),)
        )
    elif case == "missing-task":
        view.tasks.clear()
    elif case in {"missing-branch", "wrong-branch"}:
        view.branches[2] = None if case == "missing-branch" else "feature/other"
    elif case == "empty-identity":
        view.tasks[2] = replace(dep, subtask_id="")
    elif case in {"different-parent", "unknown-parent"}:
        view.tasks[2] = replace(
            dep, parent_number=999 if case == "different-parent" else None
        )
    elif case == "task-unknown-parent":
        task = replace(task, parent_number=None)
    elif case == "not-eligible":
        task = replace(
            task, status_labels=("status:in-progress", "status:external-lock")
        )
    elif case == "not-direct":
        view.assessments[1] = DependencyAssessment()
    assert 1 in _scan(task, view).conflicts


def _decide(
    tasks, view, forge, remote_branches=("origin/" + BRANCH,), prs=(), parent=100
):
    with (
        patch(
            "orchestune.dispatch.phase_rebase.list_remote_branches",
            return_value=list(remote_branches),
        ),
        patch(
            "orchestune.dispatch.phase_rebase.branch_changed_files",
            return_value=["src/foo.py"],
        ),
    ):
        return _decide_external_lock_sync(
            {task.issue_number: task for task in tasks},
            list(prs),
            RunState(),
            view=view,
            forge=forge,
            parent_issue_number=parent,
        )


@pytest.mark.parametrize("tip", [SHA, None, "bad", "g" * 40])
def test_collector_caches_result_per_scan_and_refreshes_next_scan(fake_forge, tip):
    dep, task, _ = _fixture()
    other = replace(task, issue_number=3)
    view = LockDependencyTestView.from_tasks([dep, task, other])
    fake_forge.get_current_branch_tip_sha_if_merged_into.return_value = tip
    result = _decide([task, other], view, fake_forge)
    assert result.to_unlock == ([task, other] if tip == SHA else [])
    fake_forge.get_current_branch_tip_sha_if_merged_into.assert_called_once_with(
        BRANCH, BASE
    )
    _decide([task, other], view, fake_forge)
    assert fake_forge.get_current_branch_tip_sha_if_merged_into.call_count == 2
    fake_forge.is_branch_merged_into.assert_not_called()


def test_api_error_is_cached_but_next_scan_can_succeed(fake_forge):
    dep, task, _ = _fixture()
    other = replace(task, issue_number=3)
    view = LockDependencyTestView.from_tasks([dep, task, other])
    fake_forge.get_current_branch_tip_sha_if_merged_into.side_effect = RuntimeError(
        "404/base missing"
    )
    assert _decide([task, other], view, fake_forge).to_unlock == []
    fake_forge.get_current_branch_tip_sha_if_merged_into.assert_called_once()
    fake_forge.get_current_branch_tip_sha_if_merged_into.side_effect = None
    fake_forge.get_current_branch_tip_sha_if_merged_into.return_value = SHA
    assert _decide([task, other], view, fake_forge).to_unlock == [task, other]


@pytest.mark.parametrize(
    "case",
    [
        "no-forge",
        "no-parent",
        "different-config-parent",
        "not-observed",
        "unresolved",
        "other-parent",
        "unknown-parent",
        "empty-identity",
        "not-completed",
        "not-eligible",
        "fallback",
    ],
)
def test_collector_only_verifies_observed_identified_completed_dependencies(
    fake_forge, case
):
    dep, task, _ = _fixture()
    parent, forge, branches = (
        100,
        fake_forge,
        ["origin/" + BRANCH, "origin/feature/unrelated"],
    )
    if case == "no-forge":
        forge = None
    elif case == "no-parent":
        parent = None
    elif case == "different-config-parent":
        parent = 999
    elif case == "not-observed":
        branches = ["origin/feature/unrelated"]
    elif case == "unresolved":
        task = replace(task, depends_on=("dep", "missing"))
    elif case in {"other-parent", "unknown-parent"}:
        dep = replace(dep, parent_number=999 if case == "other-parent" else None)
    elif case == "empty-identity":
        dep = replace(dep, subtask_id="")
    elif case == "not-completed":
        dep = replace(dep, status_labels=("status:queued",))
    elif case == "not-eligible":
        task = replace(task, status_labels=("status:in-progress",))
    view = LockDependencyTestView.from_tasks([dep, task])
    if case == "fallback":
        view = _FakeLockDependencyView(
            tasks={2: dep},
            assessments={
                1: DependencyAssessment(
                    resolved=(AssessedDependency(2, DependencyState.COMPLETED),)
                )
            },
            branches={2: "feature/unrelated"},
        )
    _decide([task], view, forge, branches, parent=parent)
    fake_forge.get_current_branch_tip_sha_if_merged_into.assert_not_called()


def test_pr_head_is_observed_without_remote_branch(fake_forge):
    _, task, view = _fixture()
    fake_forge.get_current_branch_tip_sha_if_merged_into.return_value = SHA
    pr = PrRecord(
        90,
        BRANCH,
        ("src/foo.py",),
        base_ref=BASE,
        head_sha=SHA,
        is_cross_repository=False,
    )
    assert _decide([task], view, fake_forge, [], [pr]).to_unlock == [task]
    fake_forge.get_current_branch_tip_sha_if_merged_into.assert_called_once_with(
        BRANCH, BASE
    )


def test_old_merged_pr_cannot_hide_reused_branch_with_new_tip(fake_forge):
    _, task, view = _fixture()
    fake_forge.is_branch_merged_into.return_value = True
    fake_forge.get_current_branch_tip_sha_if_merged_into.return_value = None
    pr = PrRecord(
        90,
        BRANCH,
        ("src/foo.py",),
        state="MERGED",
        base_ref=BASE,
        head_sha=SHA,
        is_cross_repository=False,
    )
    assert _decide([task], view, fake_forge, prs=[pr]).to_unlock == []
    fake_forge.is_branch_merged_into.assert_not_called()

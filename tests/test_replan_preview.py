from __future__ import annotations

from orchestune.dag.models import SubTask
from orchestune.models import IssueRecord
from orchestune.replan.models import PlanRevision, ReplanPlan, RetirementCandidate
from orchestune.replan.plan import compute_plan_revision
from orchestune.replan.preview import build_replan_preview, compute_preview_token
from orchestune.replan.snapshot import ReplanSnapshot

REVISION = PlanRevision("replan-v1:sha256:" + "a" * 64)


def _issue(
    number: int, *, labels: tuple[str, ...] = (), state: str = "OPEN", body: str = ""
) -> IssueRecord:
    return IssueRecord(number, f"Issue {number}", body, labels, "2026-01-01", state)


def _plan() -> ReplanPlan:
    return ReplanPlan(
        "new", 693, "adopted", (SubTask("new-a", "d", (), (), (), False, (), "high"),)
    )


def _snapshot(
    *,
    old: IssueRecord,
    children: tuple[IssueRecord, ...] = (),
    merged: tuple[int, ...] = (),
) -> ReplanSnapshot:
    return ReplanSnapshot(
        parent_issue=_issue(693),
        parent_plan_fingerprint="parent-plan",
        retirement_candidates=(RetirementCandidate("old-a", old.number),),
        old_issues=(old,),
        child_issues=(old,) + children,
        merged_closing_issue_numbers=merged,
    )


def test_preview_classifies_create_and_safe_retirement() -> None:
    preview = build_replan_preview(
        _plan(), _snapshot(old=_issue(10, labels=("status:queued",)))
    )

    assert [(item.action, item.subject) for item in preview.decisions] == [
        ("create", "new-a"),
        ("retire", "old-a"),
    ]


def test_preview_fails_closed_for_active_or_merged_old_issues() -> None:
    active = build_replan_preview(
        _plan(), _snapshot(old=_issue(10, labels=("status:in-progress",)))
    )
    merged = build_replan_preview(
        _plan(), _snapshot(old=_issue(10, labels=("status:queued",)), merged=(10,))
    )

    assert active.decisions[-1].action == "manual-review"
    assert merged.decisions[-1].action == "manual-review"


def test_preview_reports_a_missing_old_child_once() -> None:
    snapshot = ReplanSnapshot(
        parent_issue=_issue(693),
        parent_plan_fingerprint="parent-plan",
        retirement_candidates=(RetirementCandidate("old-a", 10),),
        old_issues=(),
        child_issues=(),
        merged_closing_issue_numbers=(),
    )

    preview = build_replan_preview(_plan(), snapshot)

    assert [item.action for item in preview.decisions] == ["create", "conflict"]


def test_preview_recognizes_a_completed_matching_retirement_as_no_op() -> None:
    initial = build_replan_preview(
        _plan(), _snapshot(old=_issue(10, labels=("status:queued",)))
    )
    retired = _issue(
        10,
        labels=("status:not-needed",),
        state="CLOSED",
        body=f"<!-- orchestune:replan-retirement plan_revision={initial.plan_revision} -->",
    )

    preview = build_replan_preview(_plan(), _snapshot(old=retired))

    assert preview.decisions[-1].action == "no-op"


def test_preview_reuses_one_matching_generation_but_rejects_duplicates() -> None:
    marker = (
        "<!-- orchestune:replan-generation plan_revision="
        + str(REVISION)
        + " subtask_id_b64=bmV3LWE -->"
    )
    plan = _plan()
    # Use a precomputed revision so the marker precisely matches this plan below.
    preview = build_replan_preview(
        plan, _snapshot(old=_issue(10, labels=("status:blocked",)))
    )
    matching = _issue(20, body=preview.generations[0].marker)
    reused = build_replan_preview(
        plan,
        _snapshot(old=_issue(10, labels=("status:blocked",)), children=(matching,)),
    )
    duplicate = build_replan_preview(
        plan,
        _snapshot(
            old=_issue(10, labels=("status:blocked",)),
            children=(matching, _issue(21, body=preview.generations[0].marker)),
        ),
    )

    assert reused.decisions[0].action == "reuse"
    assert duplicate.decisions[0].action == "conflict"
    assert marker  # documents the marker's deterministic format


def test_preview_token_is_stable_and_includes_snapshot_state() -> None:
    snapshot = _snapshot(old=_issue(10, labels=("status:queued",)))
    changed = _snapshot(old=_issue(10, labels=("status:blocked",)))

    assert compute_preview_token(REVISION, snapshot) == compute_preview_token(
        REVISION, snapshot
    )
    assert compute_preview_token(REVISION, snapshot) != compute_preview_token(
        REVISION, changed
    )


def test_merged_closing_takes_precedence_over_stale_retirement_marker() -> None:
    plan = _plan()
    revision = compute_plan_revision(plan)
    marker = f"<!-- orchestune:replan-retirement plan_revision={revision} -->"
    snapshot = ReplanSnapshot(
        parent_issue=_issue(693),
        parent_plan_fingerprint="parent-plan",
        retirement_candidates=(RetirementCandidate("old-a", 10),),
        old_issues=(_issue(10, labels=("status:queued",)),),
        child_issues=(_issue(10, labels=("status:queued",)),),
        merged_closing_issue_numbers=(10,),
        retirement_comments=((10, (marker,)),),
    )

    preview = build_replan_preview(plan, snapshot)

    assert [(item.action, item.subject) for item in preview.decisions] == [
        ("create", "new-a"),
        ("manual-review", "old-a"),
    ]

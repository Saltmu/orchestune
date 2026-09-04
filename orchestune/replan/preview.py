"""Pure, fail-closed classification of a replan generation replacement."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from orchestune import STATUS_LABEL_PREFIX, StatusLabel
from orchestune.models import IssueRecord
from orchestune.replan.audit import retirement_marker
from orchestune.replan.models import PlanGeneration, PlanRevision, ReplanPlan
from orchestune.replan.plan import compute_plan_revision
from orchestune.replan.snapshot import ReplanSnapshot


@dataclass(frozen=True)
class PreviewDecision:
    """One stable action recommendation; preview never performs the action."""

    action: str
    subject: str
    reason: str
    issue_number: int | None = None


@dataclass(frozen=True)
class ReplanPreview:
    plan_revision: PlanRevision
    generations: tuple[PlanGeneration, ...]
    decisions: tuple[PreviewDecision, ...]
    preview_token: str


def compute_preview_token(
    plan_revision: PlanRevision | str, snapshot: ReplanSnapshot
) -> str:
    """Hash every observed input so changed GitHub state invalidates confirmation."""

    revision = PlanRevision(str(plan_revision))
    payload = {"plan_revision": str(revision), "snapshot": snapshot.state_fingerprint()}
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "replan-preview-v1:sha256:" + hashlib.sha256(encoded).hexdigest()


def _new_generation_decision(
    generation: PlanGeneration, children: tuple[IssueRecord, ...]
) -> PreviewDecision:
    matches = [issue for issue in children if generation.matches_body(issue.body)]
    if not matches:
        return PreviewDecision(
            "create", generation.subtask_id, "no matching new-generation Issue exists"
        )
    if len(matches) == 1:
        return PreviewDecision(
            "reuse",
            generation.subtask_id,
            "exact generation marker matches one child Issue",
            matches[0].number,
        )
    return PreviewDecision(
        "conflict",
        generation.subtask_id,
        "multiple child Issues carry the same generation marker",
    )


def _generations(
    new_plan: ReplanPlan, revision: PlanRevision
) -> tuple[PlanGeneration, ...]:
    return tuple(PlanGeneration(revision, subtask.id) for subtask in new_plan.subtasks)


def _closed_retirement_decision(
    issue: IssueRecord,
    status: str,
    subtask_id: str,
    revision: PlanRevision,
    comments: tuple[str, ...],
) -> PreviewDecision:
    marker = retirement_marker(revision)
    if status == StatusLabel.NOT_NEEDED and (
        marker in issue.body or any(marker in comment for comment in comments)
    ):
        return PreviewDecision(
            "no-op",
            subtask_id,
            "old Issue is already retired by this replacement",
            issue.number,
        )
    return PreviewDecision(
        "manual-review",
        subtask_id,
        "old Issue is closed without this retirement marker",
        issue.number,
    )


def _open_retirement_decision(
    issue: IssueRecord, status: str, subtask_id: str
) -> PreviewDecision:
    if status in {StatusLabel.QUEUED, StatusLabel.BLOCKED}:
        return PreviewDecision(
            "retire",
            subtask_id,
            "old Issue is open, unstarted, and has no merged result",
            issue.number,
        )
    return PreviewDecision(
        "manual-review",
        subtask_id,
        f"old Issue status {status} cannot be retired automatically",
        issue.number,
    )


def _is_recoverable_retirement(
    snapshot: ReplanSnapshot,
    issue: IssueRecord,
    issue_number: int,
    revision: PlanRevision,
    labels: set[str],
) -> bool:
    recoverable_labels = {
        StatusLabel.QUEUED,
        StatusLabel.BLOCKED,
        StatusLabel.NOT_NEEDED,
    }
    return bool(
        labels
        and issue.state.upper() == "OPEN"
        and labels <= recoverable_labels
        and any(
            retirement_marker(revision) in comment
            for comment in snapshot.comments_for(issue_number)
        )
    )


def _old_generation_decision(
    snapshot: ReplanSnapshot,
    issue: IssueRecord | None,
    subtask_id: str,
    issue_number: int,
    revision: PlanRevision,
) -> PreviewDecision:
    if issue is None:
        return PreviewDecision(
            "conflict",
            subtask_id,
            "old Issue is absent from the parent children",
            issue_number,
        )
    labels = {label for label in issue.labels if label.startswith(STATUS_LABEL_PREFIX)}
    comments = snapshot.comments_for(issue_number)
    if _is_recoverable_retirement(snapshot, issue, issue_number, revision, labels):
        return PreviewDecision(
            "retire",
            subtask_id,
            "matching retirement marker authorizes partial-failure recovery",
            issue.number,
        )
    if len(labels) != 1:
        return PreviewDecision(
            "conflict",
            subtask_id,
            "old Issue has missing or conflicting status labels",
            issue_number,
        )
    status = next(iter(labels))
    if issue_number in snapshot.merged_closing_issue_numbers:
        return PreviewDecision(
            "manual-review",
            subtask_id,
            "a merged PR closes the old Issue",
            issue_number,
        )
    if issue.state.upper() != "OPEN":
        return _closed_retirement_decision(
            issue, status, subtask_id, revision, comments
        )
    return _open_retirement_decision(issue, status, subtask_id)


def build_replan_preview(
    new_plan: ReplanPlan, snapshot: ReplanSnapshot
) -> ReplanPreview:
    """Classify only generation creation/reuse and old-generation retirement."""

    revision = compute_plan_revision(new_plan)
    generations = _generations(new_plan, revision)
    old_by_number = {issue.number: issue for issue in snapshot.old_issues}
    decisions = [
        _new_generation_decision(generation, snapshot.child_issues)
        for generation in generations
    ]
    decisions.extend(
        _old_generation_decision(
            snapshot,
            old_by_number.get(candidate.issue_number),
            candidate.subtask_id,
            candidate.issue_number,
            revision,
        )
        for candidate in snapshot.retirement_candidates
    )
    decisions.extend(
        PreviewDecision("conflict", "snapshot", reason) for reason in snapshot.conflicts
    )
    return ReplanPreview(
        revision,
        generations,
        tuple(decisions),
        compute_preview_token(revision, snapshot),
    )


__all__ = [
    "PreviewDecision",
    "ReplanPreview",
    "build_replan_preview",
    "compute_preview_token",
]

"""Read-only observation of the state required for a replan preview."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from orchestune.issue_parsing import (
    _parse_footprint_block,
    decomposition_plan_from_parent_body,
    find_children_by_parent,
)
from orchestune.models import IssueRecord, PrRecord
from orchestune.replan.models import RetirementCandidate


class ReplanSnapshotForge(Protocol):
    """The deliberately read-only Forge surface used by preview collection."""

    def get_issue(self, issue_number: int) -> IssueRecord | None: ...

    def list_sub_issues(self, parent_issue_number: int) -> list[IssueRecord]: ...

    def list_prs(self, state: str = "open", **kwargs: object) -> list[PrRecord]: ...


def _stable_fingerprint(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _old_candidates(parent: IssueRecord) -> tuple[RetirementCandidate, ...]:
    plan = decomposition_plan_from_parent_body(parent.body)
    if plan is None or not isinstance(plan.get("subtasks"), list):
        raise ValueError("parent Issue does not contain a valid decomposition plan")
    candidates: list[RetirementCandidate] = []
    for entry in plan["subtasks"]:
        if not isinstance(entry, dict) or "issue_number" not in entry:
            continue
        try:
            subtask_id = entry["id"]
            if not isinstance(subtask_id, str):
                raise ValueError("subtask ID must be a string")
            candidates.append(RetirementCandidate(subtask_id, entry["issue_number"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "parent decomposition plan has an invalid old subtask"
            ) from exc
    if len({item.issue_number for item in candidates}) != len(candidates):
        raise ValueError("parent decomposition plan duplicates an old Issue number")
    return tuple(sorted(candidates, key=lambda item: item.subtask_id))


def _body_subtask_id(issue: IssueRecord) -> str | None:
    """Read a Footprint subtask ID without treating malformed text as a match."""

    value = _parse_footprint_block(issue.body)
    raw_id = value.get("subtask_id") if isinstance(value, dict) else None
    return raw_id if isinstance(raw_id, str) and raw_id.strip() else None


@dataclass(frozen=True)
class ReplanSnapshot:
    """Immutable observation; it contains no mutation capability."""

    parent_issue: IssueRecord
    parent_plan_fingerprint: str
    retirement_candidates: tuple[RetirementCandidate, ...]
    old_issues: tuple[IssueRecord, ...]
    child_issues: tuple[IssueRecord, ...]
    merged_closing_issue_numbers: tuple[int, ...]
    conflicts: tuple[str, ...] = ()
    retirement_comments: tuple[tuple[int, tuple[str, ...]], ...] = ()
    parent_comments: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "retirement_candidates",
            tuple(sorted(self.retirement_candidates, key=lambda item: item.subtask_id)),
        )
        object.__setattr__(
            self,
            "old_issues",
            tuple(sorted(self.old_issues, key=lambda item: item.number)),
        )
        object.__setattr__(
            self,
            "child_issues",
            tuple(sorted(self.child_issues, key=lambda item: item.number)),
        )
        object.__setattr__(
            self,
            "merged_closing_issue_numbers",
            tuple(sorted(set(self.merged_closing_issue_numbers))),
        )
        object.__setattr__(self, "conflicts", tuple(sorted(set(self.conflicts))))
        object.__setattr__(
            self,
            "retirement_comments",
            tuple(
                sorted(
                    (
                        number,
                        tuple(sorted(set(comments))),
                    )
                    for number, comments in self.retirement_comments
                )
            ),
        )
        object.__setattr__(
            self, "parent_comments", tuple(sorted(set(self.parent_comments)))
        )

    def comments_for(self, issue_number: int) -> tuple[str, ...]:
        return dict(self.retirement_comments).get(issue_number, ())

    def state_fingerprint(self) -> dict[str, object]:
        """Return the complete normalized state that guards preview confirmation."""

        return {
            "parent_plan": self.parent_plan_fingerprint,
            "old": [
                {
                    "number": issue.number,
                    "body": issue.body,
                    "labels": sorted(issue.labels),
                    "state": issue.state,
                    "parent": (
                        issue.parent.get("number") if issue.parent is not None else None
                    ),
                    "blocked_by": sorted(issue.blocked_by),
                }
                for issue in self.old_issues
            ],
            "children": [
                {
                    "number": issue.number,
                    "body": issue.body,
                    "labels": sorted(issue.labels),
                    "state": issue.state,
                    "parent": (
                        issue.parent.get("number") if issue.parent is not None else None
                    ),
                    "blocked_by": sorted(issue.blocked_by),
                }
                for issue in self.child_issues
            ],
            "merged_closing": self.merged_closing_issue_numbers,
            "conflicts": self.conflicts,
            "retirement_comments": self.retirement_comments,
            "parent_comments": tuple(
                comment
                for comment in self.parent_comments
                if "<!-- orchestune:replan" in comment
            ),
        }


def _comment_bodies(forge: ReplanSnapshotForge, issue_number: int) -> tuple[str, ...]:
    list_comments = getattr(forge, "list_comments", None)
    if not callable(list_comments):
        return ()
    try:
        comments = list_comments(issue_number)
    except NotImplementedError:
        return ()
    return tuple(
        str(comment.get("body", ""))
        for comment in comments
        if isinstance(comment, dict)
    )


def _old_issues_and_conflicts(
    candidates: tuple[RetirementCandidate, ...],
    children: tuple[IssueRecord, ...],
) -> tuple[tuple[IssueRecord, ...], tuple[str, ...]]:
    children_by_number = {issue.number: issue for issue in children}
    old_issues = tuple(
        children_by_number[number]
        for number in (item.issue_number for item in candidates)
        if number in children_by_number
    )
    conflicts = tuple(
        f"old Issue #{issue.number} declares subtask_id {body_id!r}, expected {candidate.subtask_id!r}"
        for candidate in candidates
        if (issue := children_by_number.get(candidate.issue_number)) is not None
        if (body_id := _body_subtask_id(issue)) is not None
        if body_id != candidate.subtask_id
    )
    return old_issues, conflicts


def collect_replan_snapshot(
    forge: ReplanSnapshotForge, parent_issue_number: int
) -> ReplanSnapshot:
    """Collect replan inputs through read-only Forge calls, failing closed on drift."""

    parent = forge.get_issue(parent_issue_number)
    if parent is None:
        raise ValueError(f"parent Issue #{parent_issue_number} was not found")
    candidates = _old_candidates(parent)
    children = tuple(find_children_by_parent(forge, parent_issue_number).issues)  # type: ignore[arg-type]
    old_issues, conflicts = _old_issues_and_conflicts(candidates, children)
    merged = forge.list_prs(state="merged")
    old_numbers = {item.issue_number for item in candidates}
    merged_closing = tuple(
        sorted(
            {
                number
                for pr in merged
                for number in pr.closes_issue_numbers
                if number in old_numbers
            }
        )
    )
    plan = decomposition_plan_from_parent_body(parent.body)
    retirement_comments = tuple(
        (candidate.issue_number, _comment_bodies(forge, candidate.issue_number))
        for candidate in candidates
    )
    return ReplanSnapshot(
        parent,
        _stable_fingerprint(plan),
        candidates,
        old_issues,
        children,
        merged_closing,
        conflicts,
        retirement_comments,
        tuple(
            comment
            for comment in _comment_bodies(forge, parent_issue_number)
            if "<!-- orchestune:replan" in comment
        ),
    )


__all__ = ["ReplanSnapshot", "ReplanSnapshotForge", "collect_replan_snapshot"]

"""Read-only observation of the state required for a replan preview."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from orchestune.issue_parsing import decomposition_plan_from_parent_body
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
            candidates.append(
                RetirementCandidate(str(entry["id"]), entry["issue_number"])
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "parent decomposition plan has an invalid old subtask"
            ) from exc
    if len({item.issue_number for item in candidates}) != len(candidates):
        raise ValueError("parent decomposition plan duplicates an old Issue number")
    return tuple(sorted(candidates, key=lambda item: item.subtask_id))


def _body_subtask_id(issue: IssueRecord) -> str | None:
    """Read a Footprint subtask ID without treating malformed text as a match."""

    import yaml

    marker = "```yaml"
    start = issue.body.find(marker)
    if start < 0:
        return None
    end = issue.body.find("```", start + len(marker))
    if end < 0:
        return None
    try:
        value = yaml.safe_load(issue.body[start + len(marker) : end])
    except yaml.YAMLError:
        return None
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
                }
                for issue in self.old_issues
            ],
            "children": [
                {
                    "number": issue.number,
                    "body": issue.body,
                    "labels": sorted(issue.labels),
                    "state": issue.state,
                }
                for issue in self.child_issues
            ],
            "merged_closing": self.merged_closing_issue_numbers,
            "conflicts": self.conflicts,
        }


def collect_replan_snapshot(
    forge: ReplanSnapshotForge, parent_issue_number: int
) -> ReplanSnapshot:
    """Collect replan inputs through read-only Forge calls, failing closed on drift."""

    parent = forge.get_issue(parent_issue_number)
    if parent is None:
        raise ValueError(f"parent Issue #{parent_issue_number} was not found")
    candidates = _old_candidates(parent)
    children = tuple(forge.list_sub_issues(parent_issue_number))
    children_by_number = {issue.number: issue for issue in children}
    old_issues = tuple(
        children_by_number[number]
        for number in (item.issue_number for item in candidates)
        if number in children_by_number
    )
    conflicts: list[str] = []
    for candidate in candidates:
        issue = children_by_number.get(candidate.issue_number)
        if issue is None:
            conflicts.append(
                f"old Issue #{candidate.issue_number} is not a current child of the parent"
            )
            continue
        body_id = _body_subtask_id(issue)
        if body_id is not None and body_id != candidate.subtask_id:
            conflicts.append(
                f"old Issue #{issue.number} declares subtask_id {body_id!r}, expected {candidate.subtask_id!r}"
            )
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
    return ReplanSnapshot(
        parent,
        _stable_fingerprint(plan),
        candidates,
        old_issues,
        children,
        merged_closing,
        tuple(conflicts),
    )


__all__ = ["ReplanSnapshot", "ReplanSnapshotForge", "collect_replan_snapshot"]

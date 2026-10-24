"""Observe all preview inputs using only read operations on a Forge."""

from __future__ import annotations

import re
import subprocess
from typing import Any
from urllib.parse import urlsplit

from orchestune.integrator.parent_branch_state import is_current_parent_branch_merged
from orchestune.issue_parsing import (
    decomposition_plan_from_parent_body,
    find_children_by_parent,
)
from orchestune.models import IssueRecord, PrRecord
from orchestune.remediation.models import (
    OriginObservation,
    RemediationSnapshot,
    RemediationSpec,
)


def _origin_target(reference: str, slug: str) -> tuple[str, int, str] | None:
    if re.fullmatch(r"#[1-9][0-9]*", reference):
        return "short", int(reference[1:]), ""
    url = urlsplit(reference)
    match = re.fullmatch(r"/([^/]+/[^/]+)/(issues|pull)/([1-9][0-9]*)", url.path)
    if (
        url.scheme != "https"
        or url.netloc.lower() != "github.com"
        or url.query
        or not match
    ):
        return None
    if match[1].lower() != slug.lower():
        return None
    return match[2], int(match[3]), url.fragment


def _comment_exists(forge: Any, number: int, fragment: str) -> bool:
    if not fragment:
        return True
    if re.fullmatch(r"issuecomment-[0-9]+", fragment):
        rows = forge.list_all_issue_comments(number)
        prefix = "issuecomment-"
    elif re.fullmatch(r"discussion_r[0-9]+", fragment):
        rows = forge.list_pull_request_review_comments(number)
        prefix = "discussion_r"
    elif re.fullmatch(r"pullrequestreview-[0-9]+", fragment):
        rows = forge.list_pull_request_reviews(number)
        prefix = "pullrequestreview-"
    else:
        return False
    return any(str(row.get("id")) == fragment.removeprefix(prefix) for row in rows)


def _observe_origin(
    forge: Any, reference: str, slug: str, children: set[int], branch: str
) -> OriginObservation:
    target = _origin_target(reference, slug)
    if target is None:
        return OriginObservation(
            reference, False, "origin must reference this repository"
        )
    kind, number, fragment = target
    valid = False
    if kind != "pull" and number in children:
        valid = forge.get_issue(number) is not None
    if not valid and kind != "issues":
        try:
            pr = forge.get_pull_request(number)
        except (ValueError, StopIteration):
            pr = None
        except subprocess.CalledProcessError as exc:
            if "404" not in (exc.stderr or ""):
                raise
            pr = None
        valid = pr is not None and branch in (pr.head_ref, pr.base_ref)
    if valid:
        valid = (
            kind != "issues" or not fragment or fragment.startswith("issuecomment-")
        ) and _comment_exists(forge, number, fragment)
    return OriginObservation(
        reference,
        valid,
        "parent-scope reference"
        if valid
        else "origin does not resolve to a parent-scope Issue/PR/comment",
    )


def _final_prs(forge: Any, branch: str) -> tuple[PrRecord, ...]:
    all_prs: list[PrRecord] = forge.list_prs(state="all", head=branch)
    return tuple(
        sorted(
            (
                p
                for p in all_prs
                if p.head_ref == branch
                and p.base_ref == "main"
                and p.is_cross_repository is not True
            ),
            key=lambda p: p.number,
        )
    )


def _collect_children_with_plan(
    forge: Any, parent_issue_number: int, parent_body: str
) -> tuple[IssueRecord, ...]:
    children_issues = list(find_children_by_parent(forge, parent_issue_number).issues)
    parent_plan = decomposition_plan_from_parent_body(parent_body) or {}
    plan_child_numbers: set[int] = set()
    for entry in parent_plan.get("subtasks", []):
        if isinstance(entry, dict):
            num = entry.get("issue_number")
            if isinstance(num, int) and not isinstance(num, bool) and num > 0:
                plan_child_numbers.add(num)
    existing_child_numbers = {c.number for c in children_issues}
    for child_num in sorted(plan_child_numbers - existing_child_numbers):
        child_issue = forge.get_issue(child_num)
        if child_issue is not None:
            children_issues.append(child_issue)
    return tuple(sorted(children_issues, key=lambda i: i.number))


def collect_remediation_snapshot(
    forge: Any, parent_issue_number: int, spec: RemediationSpec
) -> RemediationSnapshot:
    """Read a deterministic observation; never initialize or repair remote state."""
    parent: IssueRecord | None = forge.get_issue(parent_issue_number)
    if parent is None:
        raise ValueError(f"parent Issue #{parent_issue_number} was not found")
    children = _collect_children_with_plan(forge, parent_issue_number, parent.body)
    branch = f"parent/issue-{parent_issue_number}"
    finals = _final_prs(forge, branch)
    existing = tuple(
        issue
        for n in sorted(
            {
                t.subtask.issue_number
                for t in spec.tasks
                if t.subtask.issue_number is not None
            }
        )
        if (issue := forge.get_issue(n)) is not None
    )
    refs = sorted({r for t in spec.tasks for r in t.origins})
    slug = forge.get_repository_slug()
    observations = tuple(
        _observe_origin(
            forge,
            ref,
            slug,
            {i.number for i in children},
            branch,
        )
        for ref in refs
    )
    return RemediationSnapshot(
        parent,
        children,
        existing,
        finals,
        observations,
        is_current_parent_branch_merged(branch, parent_issue_number, forge),
        tuple(
            str(c.get("body", ""))
            for c in forge.list_all_issue_comments(parent_issue_number)
        ),
    )

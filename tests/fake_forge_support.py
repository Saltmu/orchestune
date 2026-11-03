"""In-memory Forge double shared by tests.

``conftest`` re-exports ``FakeForge`` so existing
``from tests.conftest import FakeForge`` imports keep working.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from orchestune.forge import BootstrapResult, LabelSpec
from orchestune.models import CommitCheck, IssueRecord, PrRecord
from tests.forge_event_support import EventCommentForgeMixin


class FakeForge(EventCommentForgeMixin):
    """An in-memory implementation of the Forge protocol for testing."""

    def __init__(self) -> None:
        self.issues: dict[int, IssueRecord] = {}
        self.issue_states: dict[int, str] = {}
        self.prs: dict[int, PrRecord] = {}
        self.pr_states: dict[int, str] = {}
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self.branches: set[str] = set()
        self.merged_branches: dict[tuple[str, str], str] = {}
        self.actor_permissions: dict[str, str] = {}
        self.label_actors: dict[tuple[int, str], str] = {}
        self.reopened_timestamps: dict[int, str] = {}
        self._next_issue_number: int = 1
        self._next_pr_number: int = 1

    def check_auth(self) -> None:
        pass

    def ensure_labels(self, labels: tuple[LabelSpec, ...]) -> BootstrapResult:
        return BootstrapResult((), ())

    def create_issue(self, title: str, body: str, labels: Sequence[str] = ()) -> int:
        num = self._next_issue_number
        self._next_issue_number += 1
        record = IssueRecord(
            number=num,
            title=title,
            body=body,
            labels=tuple(labels),
            created_at=datetime.now(UTC).isoformat(),
            state="OPEN",
        )
        self.issues[num] = record
        self.issue_states[num] = "OPEN"
        return num

    def get_issue(self, issue_number: int | str) -> IssueRecord | None:
        num = int(issue_number)
        return self.issues.get(num)

    def get_issue_state(self, issue_number: int | str) -> str:
        num = int(issue_number)
        return self.issue_states.get(num, "OPEN")

    def update_issue_body(self, issue_number: int | str, body: str) -> None:
        num = int(issue_number)
        if num in self.issues:
            cur = self.issues[num]
            self.issues[num] = IssueRecord(
                number=cur.number,
                title=cur.title,
                body=body,
                labels=cur.labels,
                created_at=cur.created_at,
                parent=cur.parent,
                blocked_by=cur.blocked_by,
                state=cur.state,
            )

    def update_issue_title(self, issue_number: int | str, title: str) -> None:
        num = int(issue_number)
        if num in self.issues:
            cur = self.issues[num]
            self.issues[num] = IssueRecord(
                number=cur.number,
                title=title,
                body=cur.body,
                labels=cur.labels,
                created_at=cur.created_at,
                parent=cur.parent,
                blocked_by=cur.blocked_by,
                state=cur.state,
            )

    def close_issue(
        self, issue_number: int | str, reason: str, comment: str | None = None
    ) -> None:
        num = int(issue_number)
        self.issue_states[num] = "CLOSED"
        if num in self.issues:
            cur = self.issues[num]
            self.issues[num] = IssueRecord(
                number=cur.number,
                title=cur.title,
                body=cur.body,
                labels=cur.labels,
                created_at=cur.created_at,
                parent=cur.parent,
                blocked_by=cur.blocked_by,
                state="CLOSED",
            )
        if comment:
            self.add_comment(num, comment)

    def add_label(
        self, issue_number: int | str, label: str, actor: str = "bot"
    ) -> None:
        num = int(issue_number)
        if num in self.issues:
            cur = self.issues[num]
            if label not in cur.labels:
                new_labels = cur.labels + (label,)
                self.issues[num] = IssueRecord(
                    number=cur.number,
                    title=cur.title,
                    body=cur.body,
                    labels=new_labels,
                    created_at=cur.created_at,
                    parent=cur.parent,
                    blocked_by=cur.blocked_by,
                    state=cur.state,
                )
        self.label_actors[(num, label)] = actor

    def remove_label(self, issue_number: int | str, label: str) -> None:
        num = int(issue_number)
        if num in self.issues:
            cur = self.issues[num]
            if label in cur.labels:
                new_labels = tuple(x for x in cur.labels if x != label)
                self.issues[num] = IssueRecord(
                    number=cur.number,
                    title=cur.title,
                    body=cur.body,
                    labels=new_labels,
                    created_at=cur.created_at,
                    parent=cur.parent,
                    blocked_by=cur.blocked_by,
                    state=cur.state,
                )
        self.label_actors.pop((num, label), None)

    def get_issue_labels(self, issue_number: int | str) -> tuple[str, ...]:
        num = int(issue_number)
        issue = self.issues.get(num)
        return issue.labels if issue else ()

    def list_issues_by_label(
        self, label: str, state: str = "open", limit: int = 1000
    ) -> list[IssueRecord]:
        results: list[IssueRecord] = []
        for issue in self.issues.values():
            issue_state = self.get_issue_state(issue.number)
            if state != "all" and issue_state.lower() != state.lower():
                continue
            if label in issue.labels:
                results.append(issue)
        return results[:limit]

    def add_comment(
        self, issue_number: int | str, body: str, author: str = "bot"
    ) -> None:
        num = int(issue_number)
        self.comments.setdefault(num, []).append(
            {
                "body": body,
                "author": author,
                "created_at": datetime.now(UTC).isoformat(),
            }
        )

    def list_comments(self, issue_number: int | str) -> list[dict[str, Any]]:
        num = int(issue_number)
        return list(self.comments.get(num, []))

    def get_authenticated_user(self) -> str:
        return "bot"

    def add_sub_issue(
        self, parent_issue_number: int | str, child_issue_number: int | str
    ) -> None:
        pnum = int(parent_issue_number)
        cnum = int(child_issue_number)
        if cnum in self.issues:
            cur = self.issues[cnum]
            self.issues[cnum] = IssueRecord(
                number=cur.number,
                title=cur.title,
                body=cur.body,
                labels=cur.labels,
                created_at=cur.created_at,
                parent={"number": pnum},
                blocked_by=cur.blocked_by,
                state=cur.state,
            )

    def remove_sub_issue(
        self, parent_issue_number: int | str, child_issue_number: int | str
    ) -> None:
        pnum = int(parent_issue_number)
        cnum = int(child_issue_number)
        cur = self.issues.get(cnum)
        if cur is None or cur.parent is None:
            return
        if cur.parent.get("number") != pnum:
            raise ValueError(f"Issue #{cnum} has a different parent")
        self.issues[cnum] = cur.__class__(
            number=cur.number,
            title=cur.title,
            body=cur.body,
            labels=cur.labels,
            created_at=cur.created_at,
            parent=None,
            blocked_by=cur.blocked_by,
            state=cur.state,
        )

    def list_sub_issues(self, parent_issue_number: int | str) -> list[IssueRecord]:
        pnum = int(parent_issue_number)
        return [
            issue
            for issue in self.issues.values()
            if issue.parent and issue.parent.get("number") == pnum
        ]

    def set_blocked_by(
        self, issue_number: int | str, blocking_issue_number: int | str
    ) -> None:
        num = int(issue_number)
        bnum = int(blocking_issue_number)
        if num in self.issues:
            cur = self.issues[num]
            existing = list(cur.blocked_by) if cur.blocked_by else []
            if bnum in existing:
                return
            existing.append(bnum)
            self.issues[num] = IssueRecord(
                number=cur.number,
                title=cur.title,
                body=cur.body,
                labels=cur.labels,
                created_at=cur.created_at,
                parent=cur.parent,
                blocked_by=tuple(existing),
                state=cur.state,
            )

    def remove_blocked_by(
        self, issue_number: int | str, blocking_issue_number: int | str
    ) -> None:
        num = int(issue_number)
        bnum = int(blocking_issue_number)
        cur = self.issues.get(num)
        if cur is None or bnum not in cur.blocked_by:
            return
        self.issues[num] = cur.__class__(
            number=cur.number,
            title=cur.title,
            body=cur.body,
            labels=cur.labels,
            created_at=cur.created_at,
            parent=cur.parent,
            blocked_by=tuple(x for x in cur.blocked_by if x != bnum),
            state=cur.state,
        )

    def find_open_issues_by_exact_title(self, title: str) -> list[IssueRecord]:
        return [
            issue
            for issue in self.issues.values()
            if issue.title == title and self.get_issue_state(issue.number) == "OPEN"
        ]

    def find_issues_by_parent_metadata(
        self, parent_issue_number: int | str
    ) -> list[IssueRecord]:
        pnum = int(parent_issue_number)
        results: list[IssueRecord] = []
        pattern = re.compile(
            rf"(?:parent_issue_number|parent_number):\s*['\"]?{pnum}['\"]?",
            re.MULTILINE,
        )
        for issue in self.issues.values():
            if issue.parent and issue.parent.get("number") == pnum:
                results.append(issue)
            elif pattern.search(issue.body):
                results.append(issue)
        return results

    def get_label_actor(self, issue_number: int | str, label: str) -> str:
        num = int(issue_number)
        return self.label_actors.get((num, label), "")

    def set_label_actor(self, issue_number: int | str, label: str, actor: str) -> None:
        num = int(issue_number)
        self.label_actors[(num, label)] = actor

    def get_actor_permission(self, username: str) -> str:
        return self.actor_permissions.get(username, "none")

    def set_actor_permission(self, username: str, permission: str) -> None:
        self.actor_permissions[username] = permission

    def get_issue_last_reopened_at(self, issue_number: int | str) -> str | None:
        num = int(issue_number)
        return self.reopened_timestamps.get(num)

    def set_issue_last_reopened_at(
        self, issue_number: int | str, timestamp: str | None
    ) -> None:
        num = int(issue_number)
        if timestamp is not None:
            self.reopened_timestamps[num] = timestamp
        else:
            self.reopened_timestamps.pop(num, None)

    def create_pull_request(self, head: str, base: str, title: str, body: str) -> int:
        num = self._next_pr_number
        self._next_pr_number += 1
        self.prs[num] = PrRecord(
            number=num,
            head_ref=head,
            base_ref=base,
            changed_files=(),
            created_at=datetime.now(UTC).isoformat(),
            is_cross_repository=False,
        )
        self.pr_states[num] = "open"
        self.branches.add(head)
        return num

    def get_pr(self, pr_number: int | str) -> PrRecord | None:
        num = int(pr_number)
        return self.prs.get(num)

    def update_pull_request(self, pr_number: int | str, title: str, body: str) -> None:
        num = int(pr_number)
        if num in self.prs:
            cur = self.prs[num]
            self.prs[num] = PrRecord(
                number=cur.number,
                head_ref=cur.head_ref,
                base_ref=cur.base_ref,
                changed_files=cur.changed_files,
                created_at=cur.created_at,
                closes_issue_numbers=cur.closes_issue_numbers,
                review_decision=cur.review_decision,
                is_ci_passing=cur.is_ci_passing,
                state=cur.state,
                closed_at=cur.closed_at,
                is_cross_repository=cur.is_cross_repository,
                is_files_truncated=cur.is_files_truncated,
            )

    def merge_pull_request(self, pr_number: int | str) -> None:
        num = int(pr_number)
        self.pr_states[num] = "merged"
        if num in self.prs:
            cur = self.prs[num]
            ts = datetime.now(UTC).isoformat()
            self.merged_branches[(cur.head_ref, cur.base_ref)] = ts
            self.prs[num] = PrRecord(
                number=cur.number,
                head_ref=cur.head_ref,
                base_ref=cur.base_ref,
                changed_files=cur.changed_files,
                created_at=cur.created_at,
                closes_issue_numbers=cur.closes_issue_numbers,
                review_decision=cur.review_decision,
                is_ci_passing=cur.is_ci_passing,
                state="MERGED",
                closed_at=ts,
                is_cross_repository=cur.is_cross_repository,
                is_files_truncated=cur.is_files_truncated,
            )

    def get_pull_request(self, pr_number: int | str) -> PrRecord:
        return self.prs[int(pr_number)]

    def list_commit_checks(self, sha: str) -> tuple[CommitCheck, ...]:
        return ()

    def close_pull_request(self, pr_number: int | str) -> None:
        """Close an open PR; closing a closed PR is a no-op and a merged PR stays merged."""
        num = int(pr_number)
        if num not in self.prs or self.pr_states.get(num, "open") != "open":
            return
        self.pr_states[num] = "closed"
        self.prs[num] = replace(
            self.prs[num], state="CLOSED", closed_at=datetime.now(UTC).isoformat()
        )

    def delete_branch(self, branch: str) -> None:
        self.branches.discard(branch)

    def branch_exists(self, branch: str) -> bool:
        return branch in self.branches or branch in {"main", "master"}

    def is_branch_merged_into(self, head: str, base: str) -> bool:
        return (head, base) in self.merged_branches

    def is_current_branch_tip_merged_into(self, head: str, base: str) -> bool:
        return self.is_branch_merged_into(head, base)

    def get_current_branch_tip_sha_if_merged_into(
        self, head: str, base: str
    ) -> str | None:
        return "0" * 40 if self.is_current_branch_tip_merged_into(head, base) else None

    def is_merge_commit_reachable_from(self, commit_oid: str, base: str) -> bool:
        return bool(commit_oid) and self.branch_exists(base)

    def get_merged_pr_timestamp(self, head: str, base: str) -> str | None:
        return self.merged_branches.get((head, base))

    def list_prs(
        self,
        state: str = "open",
        limit: int = 1000,
        paginate_files: bool = False,
        head: str | None = None,
        include_files: bool = False,
    ) -> list[PrRecord]:
        results: list[PrRecord] = []
        for num, pr in self.prs.items():
            pr_state = self.pr_states.get(num, "open")
            if state != "all" and pr_state.lower() != state.lower():
                continue
            if head is not None and pr.head_ref != head:
                continue
            results.append(pr)
        return results[:limit]

    def list_open_prs(
        self,
        limit: int = 1000,
        paginate_files: bool = False,
        include_files: bool = True,
    ) -> list[PrRecord]:
        return self.list_prs(
            state="open",
            limit=limit,
            paginate_files=paginate_files,
            include_files=include_files,
        )

    def list_merged_prs_for_base(self, base: str) -> list[PrRecord]:
        return [
            pr
            for pr in self.list_prs(state="merged", limit=100000)
            if pr.base_ref == base
        ]

    def seed_issue(self, issue: IssueRecord) -> None:
        self.issues[issue.number] = issue
        self.issue_states[issue.number] = (
            "CLOSED" if issue.state == "CLOSED" else "OPEN"
        )
        if issue.number >= self._next_issue_number:
            self._next_issue_number = issue.number + 1

    def seed_pr(self, pr: PrRecord, state: str | None = None) -> None:
        self.prs[pr.number] = pr
        resolved_state = (state if state is not None else pr.state or "open").lower()
        self.pr_states[pr.number] = resolved_state
        self.branches.add(pr.head_ref)
        if resolved_state == "merged":
            ts = pr.closed_at or datetime.now(UTC).isoformat()
            base = pr.base_ref or "main"
            self.merged_branches[(pr.head_ref, base)] = ts
        if pr.number >= self._next_pr_number:
            self._next_pr_number = pr.number + 1

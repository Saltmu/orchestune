"""Shared test factories and default test isolation fixtures."""

from __future__ import annotations

import contextlib
import re
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.scoring import Task
from orchestune.forge import BootstrapResult, Forge, GitHubForge, LabelSpec
from orchestune.infra.git_cli import (
    DANGEROUS_GIT_ENV_VARS,
)
from orchestune.infra.git_cli import (
    get_clean_git_env as get_clean_git_env,
)
from orchestune.integrator.types import IntegratorConfig
from orchestune.models import IssueRecord, PrRecord

pytest_plugins = ["tests.test_provisioning_support"]

GIT_ENV_VARS_TO_CLEAR = DANGEROUS_GIT_ENV_VARS
SUITE_MARKERS = frozenset({"unit", "integration", "e2e"})


def _suite_markers(item: Any) -> set[str]:
    return {
        marker.name for marker in item.iter_markers() if marker.name in SUITE_MARKERS
    }


def _classify_suite(item: Any) -> None:
    markers = _suite_markers(item)
    if len(markers) > 1:
        names = ", ".join(sorted(markers))
        raise pytest.UsageError(
            f"{item.nodeid} has multiple mutually exclusive suite markers: {names}"
        )
    if not markers:
        # Unit is the safe default. Tests crossing a real infrastructure or
        # public workflow boundary must opt into integration/e2e explicitly.
        item.add_marker(pytest.mark.unit)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Give every collected test exactly one mutually exclusive suite marker."""
    for item in items:
        _classify_suite(item)
        markers = _suite_markers(item)
        if len(markers) != 1:  # pragma: no cover - defensive invariant
            raise pytest.UsageError(
                f"{item.nodeid} must have exactly one suite marker; got {markers}"
            )


def make_task(issue_number: int = 1, **overrides: Any) -> Task:
    """Create a Task with valid defaults; override only fields relevant to a test."""
    values: dict[str, Any] = {
        "issue_number": issue_number,
        "subtask_id": f"task-{issue_number}",
        "footprint": ("src/foo.py",),
        "symbols": (),
        "risk": False,
        "priority": "medium",
        "progress_partial": False,
        "status_labels": ("status:queued",),
        "created_at": "2026-01-01T00:00:00+00:00",
        "depends_on": (),
        # #799: `make_issue`のparentデフォルト（`{"number": 100}`）と揃える。
        # 依存解決は`(parent_number, subtask_id)`でスコープするため、これが
        # 揃っていないと`make_task`で組んだ`depends_on`が既定では一切
        # 解決できない。
        "parent_number": 100,
    }
    values.update(overrides)
    return Task(**values)


def make_issue(number: int = 1, **overrides: Any) -> IssueRecord:
    """Create an IssueRecord containing a valid Footprint YAML block by default."""
    footprint = tuple(overrides.pop("footprint", ("src/foo.py",)))
    symbols = tuple(overrides.pop("symbols", ("foo.Foo",)))
    depends_on = tuple(overrides.pop("depends_on", ()))
    subtask_id = overrides.pop("subtask_id", f"task-{number}")
    footprint_lines = "\n".join(f"  - {item}" for item in footprint) or "  []"
    symbols_lines = "\n".join(f"  - {item}" for item in symbols) or "  []"
    depends_on_lines = "\n".join(f"  - {item}" for item in depends_on) or "  []"
    body = overrides.pop(
        "body",
        "\n".join(
            (
                "## Footprint",
                "```yaml",
                f"subtask_id: {subtask_id}",
                "footprint:",
                footprint_lines,
                "symbols:",
                symbols_lines,
                "depends_on:",
                depends_on_lines,
                "```",
                "",
            )
        ),
    )
    values: dict[str, Any] = {
        "number": number,
        "title": f"Test Issue {number}",
        "body": body,
        "labels": ("status:queued",),
        "created_at": "2026-01-01T00:00:00+00:00",
        "parent": {"number": 100},
    }
    values.update(overrides)
    return IssueRecord(**values)


def make_done_issue(number: int = 1, **overrides: Any) -> IssueRecord:
    """Create a `status:done` child IssueRecord for Integrator tests.

    Footprint/symbols default to empty so the parsed `Task` carries only the
    `subtask_id`/`depends_on` that integration ordering actually depends on.
    """
    values: dict[str, Any] = {
        "labels": ("status:done",),
        "footprint": (),
        "symbols": (),
        "parent": {"number": 100, "state": "OPEN"},
    }
    values.update(overrides)
    return make_issue(number, **values)


def make_pr(number: int = 1, **overrides: Any) -> PrRecord:
    """Create a PrRecord with a conventional issue branch by default."""
    values: dict[str, Any] = {
        "number": number,
        "head_ref": f"claude/issue-{number}-task-{number}",
        "changed_files": (),
    }
    values.update(overrides)
    return PrRecord(**values)


class FakeForge:
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

    def delete_branch(self, branch: str) -> None:
        self.branches.discard(branch)

    def branch_exists(self, branch: str) -> bool:
        return branch in self.branches or branch in {"main", "master"}

    def is_branch_merged_into(self, head: str, base: str) -> bool:
        return (head, base) in self.merged_branches

    def is_current_branch_tip_merged_into(self, head: str, base: str) -> bool:
        return self.is_branch_merged_into(head, base)

    def is_merge_commit_reachable_from(self, commit_oid: str, base: str) -> bool:
        return bool(commit_oid) and self.branch_exists(base)

    def get_merged_pr_timestamp(self, head: str, base: str) -> str | None:
        return self.merged_branches.get((head, base))

    def list_prs(
        self,
        state: str = "open",
        limit: int = 1000,
        paginate_files: bool = False,
    ) -> list[PrRecord]:
        results: list[PrRecord] = []
        for num, pr in self.prs.items():
            pr_state = self.pr_states.get(num, "open")
            if state != "all" and pr_state.lower() != state.lower():
                continue
            results.append(pr)
        return results[:limit]

    def list_open_prs(
        self, limit: int = 1000, paginate_files: bool = False
    ) -> list[PrRecord]:
        return self.list_prs(state="open", limit=limit, paginate_files=paginate_files)

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


@pytest.fixture
def fake_forge(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """A configurable Forge double injected into dispatcher configs that request it."""
    forge = MagicMock(spec=Forge)
    # RepoAdminForge
    forge.check_auth.return_value = None
    forge.ensure_labels.return_value = BootstrapResult((), ())
    # IssueForge
    forge.list_issues_by_label.return_value = []
    forge.list_sub_issues.return_value = []
    forge.get_issue_labels.return_value = ()
    forge.get_issue.return_value = None
    forge.get_issue_state.return_value = "OPEN"
    forge.get_issue_last_reopened_at.return_value = None
    forge.get_label_actor.return_value = ""
    forge.get_actor_permission.return_value = "none"
    forge.list_comments.return_value = []
    forge.find_open_issues_by_exact_title.return_value = []
    forge.find_issues_by_parent_metadata.return_value = []
    forge.create_issue.return_value = 1
    # PullRequestForge
    forge.list_open_prs.return_value = []
    forge.list_prs.return_value = []
    forge.create_pull_request.return_value = 1
    forge.branch_exists.return_value = True
    forge.is_branch_merged_into.return_value = False
    forge.is_current_branch_tip_merged_into.return_value = False
    forge.get_merged_pr_timestamp.return_value = None
    forge.is_merge_commit_reachable_from.return_value = True
    forge.list_merged_prs_for_base.return_value = []

    original_init = DispatcherConfig.__init__

    def init_with_fake_forge(
        config: DispatcherConfig, *args: Any, **kwargs: Any
    ) -> None:
        kwargs.setdefault("forge", forge)
        original_init(config, *args, **kwargs)

    monkeypatch.setattr(DispatcherConfig, "__init__", init_with_fake_forge)
    return forge


_FAKE_FORGE_MIGRATION_TESTS = frozenset(
    {
        "test_dispatch_reconciliation_recovery.py",
        "test_dispatch_reconciliation_promotions.py",
        "test_dispatch_rebase.py",
        "test_dispatch_rebase_git.py",
        "test_dispatch_recovery.py",
        "test_dispatch_locks.py",
        "test_dispatch_launch_basic.py",
        "test_dispatch_launch_persistence.py",
    }
)
_FAKE_FORGE_MIGRATION_MODULES = (
    "orchestune.dispatch.actor_verification",
    "orchestune.dispatch.config",
    "orchestune.dispatch.escalation",
    "orchestune.dispatch.rebase",
)


@pytest.fixture(autouse=True)
def inject_fake_forge_for_dispatch_migration(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route Issue #625 tests' default forge construction to their shared fake."""
    if request.path.name not in _FAKE_FORGE_MIGRATION_TESTS:
        return

    from fake_forge_proxy import active_fake_forge

    fake_forge = request.getfixturevalue("fake_forge")
    active_fake_forge.forge = fake_forge
    for module_name in _FAKE_FORGE_MIGRATION_MODULES:
        module = sys.modules.get(module_name)
        if module is not None:
            monkeypatch.setattr(module, "GitHubForge", lambda: fake_forge)


@pytest.fixture
def in_memory_forge() -> FakeForge:
    """An in-memory FakeForge instance tracking real state for tests."""
    return FakeForge()


def _completed(args: Sequence[str] | None = None, stdout: Any = "") -> Any:
    return subprocess.CompletedProcess(
        args=list(args or []), returncode=0, stdout=stdout, stderr=""
    )


@dataclass
class IntegratorEnv:
    """All external edges of an `Integrator.run()` replaced by doubles.

    Integrator tests otherwise repeat a six-to-eight deep `@patch` stack plus
    the same default return values; this collects them behind one fixture so a
    test only spells out the behaviour it is actually asserting on.
    """

    run: MagicMock
    list_issues_by_label: MagicMock
    list_open_prs: MagicMock
    create_pull_request: MagicMock
    merge_pull_request: MagicMock
    close_issue: MagicMock
    add_label: MagicMock
    remove_label: MagicMock
    add_comment: MagicMock
    is_current_branch_tip_merged_into: MagicMock
    delete_branch: MagicMock
    branch_exists: MagicMock
    get_issue_labels: MagicMock
    ensure_labels: MagicMock

    def set_done_issues(
        self,
        *issues: IssueRecord,
        done: Sequence[IssueRecord] | None = None,
    ) -> None:
        """Answer the `status:done` lookup and the DAG-building label lookups.

        `done` overrides only the `status:done` answer, which lets a test hand
        back a deliberately unsorted list to prove topological ordering.
        """
        done_issues = list(done) if done is not None else list(issues)
        all_issues = list(issues)
        self.list_issues_by_label.side_effect = (
            lambda label, *a, **k: done_issues if label == "status:done" else all_issues
        )

    def stub_git(self, handler: Callable[[list[str]], Any]) -> None:
        """Route `subprocess.run` through `handler`, defaulting to success.

        `handler` returns `None` for every command it does not care about.
        """

        def side_effect(args: list[str], **kwargs: Any) -> Any:
            result = handler(args)
            return _completed(args) if result is None else result

        self.run.side_effect = side_effect

    def fail_git(
        self,
        matches: Callable[[list[str]], bool],
        *,
        stderr: Any = b"",
        output: Any = None,
    ) -> None:
        """Raise `CalledProcessError` for the commands `matches` selects."""

        def handler(args: list[str]) -> Any:
            if matches(args):
                raise subprocess.CalledProcessError(
                    returncode=1, cmd=args, output=output, stderr=stderr
                )
            return None

        self.stub_git(handler)

    def calls_with(self, *tokens: str) -> list[Any]:
        """Recorded `subprocess.run` calls whose argv contains every token."""
        return [
            call
            for call in self.run.call_args_list
            if all(token in call.args[0] for token in tokens)
        ]

    def call_index(self, call: Any) -> int:
        """Position of a recorded call, for asserting relative ordering."""
        return self.run.call_args_list.index(call)


@pytest.fixture
def integrator_env(
    fake_forge: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> Iterator[IntegratorEnv]:
    original_init = IntegratorConfig.__init__

    def init_with_fake_forge(
        config: IntegratorConfig, *args: Any, **kwargs: Any
    ) -> None:
        kwargs.setdefault("forge", fake_forge)
        original_init(config, *args, **kwargs)

    monkeypatch.setattr(IntegratorConfig, "__init__", init_with_fake_forge)
    with patch("orchestune.integrator.subprocess.run") as run:
        list_issues = fake_forge.list_issues_by_label
        list_open_prs = fake_forge.list_open_prs
        create_pr = fake_forge.create_pull_request
        merge_pr = fake_forge.merge_pull_request
        close_issue = fake_forge.close_issue
        add_label = fake_forge.add_label
        remove_label = fake_forge.remove_label
        add_comment = fake_forge.add_comment
        tip = fake_forge.is_current_branch_tip_merged_into
        delete_branch = fake_forge.delete_branch
        branch_exists = fake_forge.branch_exists
        get_issue_labels = fake_forge.get_issue_labels
        ensure_labels = fake_forge.ensure_labels
        run.return_value = _completed(stdout=b"")
        list_issues.side_effect = lambda label, *a, **k: []
        list_open_prs.return_value = []
        create_pr.return_value = 999
        tip.return_value = False
        branch_exists.return_value = True
        get_issue_labels.return_value = ()
        ensure_labels.return_value = BootstrapResult((), ())
        yield IntegratorEnv(
            run=run,
            list_issues_by_label=list_issues,
            list_open_prs=list_open_prs,
            create_pull_request=create_pr,
            merge_pull_request=merge_pr,
            close_issue=close_issue,
            add_label=add_label,
            remove_label=remove_label,
            add_comment=add_comment,
            is_current_branch_tip_merged_into=tip,
            delete_branch=delete_branch,
            branch_exists=branch_exists,
            get_issue_labels=get_issue_labels,
            ensure_labels=ensure_labels,
        )


@pytest.fixture
def gh_run() -> Iterator[MagicMock]:
    """`subprocess.run` as seen by `GitHubForge`, defaulting to empty success.

    `GitHubForge` contract tests only ever vary the captured stdout, so the
    fixture exposes `gh_run.stdout(payload)` instead of making each test
    rebuild a `CompletedProcess`.
    """
    with patch("orchestune.forge.subprocess.run") as run:
        run.return_value = _completed()
        run.stdout = lambda payload: run.configure_mock(
            return_value=_completed(stdout=payload)
        )
        run.stdout_sequence = lambda *payloads: run.configure_mock(
            side_effect=[_completed(stdout=payload) for payload in payloads]
        )
        yield run


@pytest.fixture
def forge() -> GitHubForge:
    """A real `GitHubForge`, used with `gh_run` to assert the gh CLI contract."""
    return GitHubForge()


@pytest.fixture(autouse=True)
def _stub_file_lock_by_default(request: pytest.FixtureRequest):
    """Avoid process-wide fcntl contention except in lock-specific tests."""
    if request.node.get_closest_marker("uses_real_file_lock") is not None:
        yield
        return
    with (
        patch(
            "orchestune.dispatch.cycle.file_lock",
            lambda _lock_path: contextlib.nullcontext(),
        ),
        patch(
            "orchestune.integrator.coordinator.file_lock",
            lambda _lock_path: contextlib.nullcontext(),
        ),
        patch(
            "orchestune.integrator.steps.file_lock",
            lambda _lock_path: contextlib.nullcontext(),
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def _guard_events_log_path(monkeypatch: pytest.MonkeyPatch):
    """Ensure tests do not create or modify 'events.jsonl' in the repository root."""
    orig_init = DispatcherConfig.__init__

    def guarded_init(self: DispatcherConfig, *args: Any, **kwargs: Any) -> None:
        orig_init(self, *args, **kwargs)
        if self.events_log_path == Path("events.jsonl"):
            pytest.fail(
                "DispatcherConfig initialized with default events_log_path ('events.jsonl'). "
                "Specify events_log_path=tmp_path / 'events.jsonl' in tests for isolation."
            )

    monkeypatch.setattr(DispatcherConfig, "__init__", guarded_init)

    root_events_log = Path(__file__).resolve().parent.parent / "events.jsonl"
    exists_before = root_events_log.exists()
    mtime_before = root_events_log.stat().st_mtime if exists_before else None
    size_before = root_events_log.stat().st_size if exists_before else None
    yield
    exists_after = root_events_log.exists()
    mtime_after = root_events_log.stat().st_mtime if exists_after else None
    size_after = root_events_log.stat().st_size if exists_after else None
    if not exists_before and exists_after:
        pytest.fail(
            "Test created 'events.jsonl' in the repository root instead of using tmp_path."
        )
    elif exists_before and not exists_after:
        pytest.fail("Test deleted 'events.jsonl' from the repository root.")
    elif exists_before and exists_after:
        if mtime_before != mtime_after or size_before != size_after:
            pytest.fail(
                "Test modified 'events.jsonl' in the repository root instead of using tmp_path."
            )


@pytest.fixture(autouse=True)
def _guard_dispatch_cycle_ensure_parent_branch(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
):
    """Ensure unit tests do not execute real git operations via unmocked ensure_parent_branch during dispatch cycles."""
    if request.node.get_closest_marker("integration") is not None:
        yield
        return

    def guarded_ensure(parent_issue_number: int) -> None:
        pytest.fail(
            f"Test '{request.node.name}' called unmocked `ensure_parent_branch({parent_issue_number})`. "
            "Dispatch cycle tests must patch `orchestune.dispatch.phase_rebase.ensure_parent_branch` "
            "to prevent accidental git branch creation/push to remote origin."
        )

    monkeypatch.setattr(
        "orchestune.dispatch.phase_rebase.ensure_parent_branch", guarded_ensure
    )
    yield


@pytest.fixture(autouse=True)
def _isolate_git_env(monkeypatch: pytest.MonkeyPatch):
    """Ensure tests run in an isolated Git environment where GIT_* variables are stripped."""
    for var in DANGEROUS_GIT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield

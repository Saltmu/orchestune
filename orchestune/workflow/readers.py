"""Read explicit workflow evidence through filesystem, Git and Forge readers.

No recovery, lock, fetch, subprocess execution, posting or persistence path is
called here. MCP snapshots are caller-supplied observations, never proof of trust.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any, Protocol

import yaml

from orchestune.claim.workspace import resolve_claim_workspace
from orchestune.complete.review_evidence import ReviewEvidenceForge
from orchestune.dependencies.resolution import resolve_task_dependencies
from orchestune.forge import GitHubForge
from orchestune.infra.git_cli import inspect_worktree_status, run_git
from orchestune.issue_parsing import effective_parent_number, parse_task_from_issue
from orchestune.labels import StatusLabel
from orchestune.ledger.run_state import load_run_state_readonly
from orchestune.models import IssueRecord, PrRecord
from orchestune.review.snapshot import (
    CLOCK_SKEW_SECONDS,
    DEFAULT_MAX_SNAPSHOT_AGE_SECONDS,
    parse_utc,
    validate_snapshot,
)
from orchestune.workflow.contracts import WorkflowContext


class WorkflowForge(ReviewEvidenceForge, Protocol):
    """Only fully paginated reads are exposed to the workflow layer."""

    def get_issue(self, issue_number: int | str) -> IssueRecord | None: ...
    def get_pull_request(self, pr_number: int | str) -> PrRecord: ...
    def list_sub_issues(self, parent_issue_number: int | str) -> list[IssueRecord]: ...
    def find_issues_by_parent_metadata(
        self, parent_issue_number: int | str
    ) -> list[IssueRecord]: ...


@contextmanager
def readonly_git() -> Iterator[None]:
    """Suppress Git's optional index refresh locks in reused legacy validators.

    These validators do not accept a per-call Git environment. The CLI executes
    observations serially; restore the caller's environment even on failure.
    """
    previous = os.environ.get("GIT_OPTIONAL_LOCKS")
    os.environ["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("GIT_OPTIONAL_LOCKS", None)
        else:
            os.environ["GIT_OPTIONAL_LOCKS"] = previous


def session_path(session: Path, relative: object) -> Path:
    """Resolve a nonempty, contained relative path, including symlink checks."""
    if not isinstance(relative, str) or not relative.strip():
        raise ValueError("session artifact path must be a nonempty relative path")
    path = Path(relative)
    if path.is_absolute() or PureWindowsPath(relative).drive or "\\" in relative:
        raise ValueError(
            "session artifact path must be repository portable and relative"
        )
    resolved = (session / path).resolve()
    if not resolved.is_relative_to(session.resolve()):
        raise ValueError("session artifact path escapes the explicit session directory")
    return resolved


def read_object(path: Path) -> dict[str, Any]:
    """Strict JSON object read; malformed user evidence is not a tool failure."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def read_session(session: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Locate state using exactly one safe plan marker; never search sessions."""
    plan_path = session_path(session, "implementation-plan.md")
    plan = plan_path.read_text(encoding="utf-8")
    blocks = re.findall(
        r"^```orchestune-workflow[^\S\n]*\n(.*?)^```[^\S\n]*$", plan, re.M | re.S
    )
    if len(blocks) != 1:
        raise ValueError("plan must contain exactly one orchestune-workflow marker")
    try:
        marker = yaml.safe_load(blocks[0])
    except yaml.YAMLError as error:
        raise ValueError("invalid workflow marker YAML") from error
    if not isinstance(marker, dict) or set(marker) != {"schema_version", "state"}:
        raise ValueError("workflow marker has unsupported or missing keys")
    if (
        not isinstance(marker["schema_version"], int)
        or isinstance(marker["schema_version"], bool)
    ) or marker["schema_version"] != 1:
        raise ValueError("unsupported workflow marker schema_version")
    state_path = session_path(session, marker["state"])
    state = read_object(state_path)
    artifacts = {
        "plan": {
            "path": "implementation-plan.md",
            "digest": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        },
        "state": {"path": state_path.relative_to(session.resolve()).as_posix()},
    }
    return state, artifacts


def _snapshot_issue(value: object) -> IssueRecord:
    if not isinstance(value, dict):
        raise ValueError("snapshot Issue must be an object")
    allowed = {f.name for f in fields(IssueRecord)}
    if set(value) - allowed:
        raise ValueError(
            "snapshot Issue has unsupported fields; use IssueRecord schema"
        )
    if (
        not isinstance(value.get("number"), int)
        or isinstance(value.get("number"), bool)
        or value["number"] < 1
    ):
        raise ValueError("snapshot Issue number must be positive")
    if (
        any(
            not isinstance(value.get(k), str)
            for k in ("title", "body", "created_at", "state")
        )
        or not isinstance(value.get("labels"), list)
        or not all(isinstance(x, str) for x in value["labels"])
    ):
        raise ValueError("snapshot Issue fields have invalid types")
    return IssueRecord(**value)


class SnapshotForge:
    """Read-only MCP transport using explicit, fresh, complete observations."""

    def __init__(
        self, value: dict[str, Any], *, issue: int, pr: int | None, now: datetime
    ):
        if (
            not isinstance(value.get("snapshot_version"), int)
            or isinstance(value.get("snapshot_version"), bool)
        ) or value["snapshot_version"] != 1:
            raise ValueError("unsupported workflow snapshot_version")
        self.repository = value.get("repository")
        if not isinstance(self.repository, str) or not re.fullmatch(
            r"[\w.-]+/[\w.-]+", self.repository
        ):
            raise ValueError("snapshot repository must be owner/repo")
        observed = parse_utc(value.get("observed_at"), "observed_at")
        age = (now - observed).total_seconds()
        if age > DEFAULT_MAX_SNAPSHOT_AGE_SECONDS or age < -CLOCK_SKEW_SECONDS:
            raise ValueError("workflow snapshot is stale or in the future")
        self.issue = _snapshot_issue(value.get("issue"))
        if self.issue.number != issue:
            raise ValueError("snapshot Issue identity differs from requested Issue")
        if "claim_id" not in value or (
            value["claim_id"] is not None
            and (
                not isinstance(value["claim_id"], str) or not value["claim_id"].strip()
            )
        ):
            raise ValueError("snapshot must explicitly name claim_id or null")
        self.claim_id = value["claim_id"]
        self.issues = [_snapshot_issue(x) for x in value.get("dependency_issues", [])]
        self.dependencies_complete = value.get("dependencies_complete") is True
        self.pr = self._read_pr(value.get("pr"), pr)
        self.review = None
        if pr is not None:
            self.review = validate_snapshot(value.get("review"), pr_number=pr, now=now)
            if (
                self.review.repository.lower() != self.repository.lower()
                or self.pr is None
                or self.review.head_sha != self.pr.head_sha
            ):
                raise ValueError("snapshot PR/review repository or head conflict")

    @staticmethod
    def _read_pr(value: object, number: int | None) -> PrRecord | None:
        if number is None:
            return None
        if not isinstance(value, dict) or value.get("number") != number:
            raise ValueError("snapshot PR identity differs from requested PR")
        return PrRecord(**value)

    def get_repository_slug(self) -> str:
        return str(self.repository)

    def get_issue(self, issue_number: int | str) -> IssueRecord | None:
        return next(
            (i for i in [self.issue, *self.issues] if i.number == int(issue_number)),
            None,
        )

    def get_issue_reference(self, number: int) -> Any:
        raise ValueError("follow-up Issue type/state requires independent evidence")

    def get_pull_request(self, pr_number: int | str) -> PrRecord:
        if self.pr is None or self.pr.number != int(pr_number):
            raise ValueError("PR unavailable in explicit snapshot")
        return self.pr

    def list_sub_issues(self, parent_issue_number: int | str) -> list[IssueRecord]:
        if not self.dependencies_complete:
            raise ValueError("snapshot dependency Issues are incomplete")
        return self.issues

    def find_issues_by_parent_metadata(
        self, parent_issue_number: int | str
    ) -> list[IssueRecord]:
        return self.list_sub_issues(parent_issue_number)

    def _section(self, section: str) -> list[dict[str, Any]]:
        if self.review is None:
            raise ValueError("snapshot review unavailable")
        return self.review.state[section]

    def list_all_issue_comments(self, issue_number: int | str) -> list[dict[str, Any]]:
        self.get_pull_request(issue_number)
        return self._section("issue_comments")

    def list_pull_request_reviews(self, pr_number: int | str) -> list[dict[str, Any]]:
        self.get_pull_request(pr_number)
        return self._section("reviews")

    def list_pull_request_review_comments(
        self, pr_number: int | str
    ) -> list[dict[str, Any]]:
        self.get_pull_request(pr_number)
        return self._section("inline_comments")


def read_dependencies(issue: IssueRecord, forge: WorkflowForge) -> bool:
    """Resolve body/native dependencies conservatively without a dispatch cycle."""
    task = parse_task_from_issue(issue)
    if not task.depends_on and not task.native_depends_on:
        return True
    parent = effective_parent_number(issue)
    candidates = (
        []
        if parent is None
        else forge.list_sub_issues(parent)
        + forge.find_issues_by_parent_metadata(parent)
    )
    by_number = {i.number: i for i in candidates}
    for number in task.native_depends_on:
        found = forge.get_issue(number)
        if found is not None:
            by_number[number] = found
    by_number[issue.number] = issue
    tasks = {n: parse_task_from_issue(i) for n, i in by_number.items()}
    resolved = resolve_task_dependencies(task, tasks)
    if resolved.unresolved:
        return False
    return all(
        by_number[n].state.upper() == "CLOSED"
        or bool(set(by_number[n].labels) & {StatusLabel.DONE, StatusLabel.NOT_NEEDED})
        for n in resolved.resolved
    )


def _observe_git(context: WorkflowContext) -> None:
    context.worktree_status = inspect_worktree_status(context.root).value
    for args, attr in (
        (["rev-parse", "HEAD"], "head_sha"),
        (["branch", "--show-current"], "branch"),
    ):
        result = run_git(args, cwd=context.root, check=False)
        if result.returncode or not result.stdout.strip():
            raise ValueError(f"Git observation unavailable: {attr}")
        setattr(context, attr, result.stdout.strip())


def _observe_local(context: WorkflowContext) -> None:
    workspace = resolve_claim_workspace(context.root)
    context.root = workspace.repository_root
    context.repository_identity = workspace.repository_identity
    context.state_path = workspace.run_state_path
    context.ledger = load_run_state_readonly(workspace.run_state_path)
    context.active = context.ledger.active_worktrees.get(str(context.issue_number))
    _observe_git(context)


def _observe_forge(context: WorkflowContext, forge: WorkflowForge) -> None:
    context.repository = forge.get_repository_slug()
    if isinstance(forge, SnapshotForge) and forge.claim_id != context.state.get(
        "claim_id"
    ):
        raise ValueError("snapshot claim generation differs from workflow state")
    context.issue = forge.get_issue(context.issue_number)
    if context.issue is not None:
        context.dependencies_ready = read_dependencies(context.issue, forge)
    if context.pr_number is not None:
        context.pr = forge.get_pull_request(context.pr_number)
        context.pr_body = context.pr.body


def _read_context(
    *,
    issue_number: int,
    session_dir: Path,
    root: Path | None = None,
    pr_number: int | None = None,
    pr_body: Path | None = None,
    review_reply: Path | None = None,
    snapshot: Path | None = None,
    forge: WorkflowForge | None = None,
    now: datetime | None = None,
) -> WorkflowContext:
    """Collect evidence, recording acquisition failures rather than guessing."""
    context = WorkflowContext(
        issue_number,
        session_dir.resolve(),
        (root or Path.cwd()).resolve(),
        pr_number=pr_number,
        pr_body_path=pr_body,
        review_reply=review_reply,
    )
    try:
        context.state, context.artifacts = read_session(context.session_dir)
    except (OSError, UnicodeError, ValueError) as error:
        context.errors["context"] = str(error)
    try:
        _observe_local(context)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        context.errors["local"] = str(error)
    try:
        if context.state.get("backend") == "mcp":
            if snapshot is None:
                raise ValueError("MCP backend requires --snapshot PATH")
            forge = SnapshotForge(
                read_object(snapshot),
                issue=issue_number,
                pr=pr_number,
                now=now or datetime.now(UTC),
            )
        elif snapshot is not None:
            raise ValueError("--snapshot requires backend=mcp in the explicit state")
        context.forge = forge or GitHubForge(timeout_seconds=30)
        _observe_forge(context, context.forge)
    except Exception as error:
        context.errors["forge"] = str(error)
    if pr_body is not None:
        try:
            context.pr_body = pr_body.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            context.errors["pr_body"] = str(error)
    return context


def read_context(
    *,
    issue_number: int,
    session_dir: Path,
    root: Path | None = None,
    pr_number: int | None = None,
    pr_body: Path | None = None,
    review_reply: Path | None = None,
    snapshot: Path | None = None,
    forge: WorkflowForge | None = None,
    now: datetime | None = None,
) -> WorkflowContext:
    """Read serial observations without Git's optional lock/index writes."""
    with readonly_git():
        return _read_context(
            issue_number=issue_number,
            session_dir=session_dir,
            root=root,
            pr_number=pr_number,
            pr_body=pr_body,
            review_reply=review_reply,
            snapshot=snapshot,
            forge=forge,
            now=now,
        )

"""Shared test factories and default test isolation fixtures."""

from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_scoring import Task
from orchestune.forge import BootstrapResult, Forge, GitHubForge
from orchestune.git_cli import (
    DANGEROUS_GIT_ENV_VARS,
)
from orchestune.git_cli import (
    get_clean_git_env as get_clean_git_env,
)
from orchestune.models import IssueRecord, PrRecord

GIT_ENV_VARS_TO_CLEAR = DANGEROUS_GIT_ENV_VARS


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


@pytest.fixture
def fake_forge() -> MagicMock:
    """A configurable Forge double for tests that are migrated off GitHubForge."""
    forge = MagicMock(spec=Forge)
    forge.check_auth.return_value = None
    forge.ensure_labels.return_value = BootstrapResult((), ())
    forge.get_issue_labels.return_value = ()
    # #485: default to "no extra metadata-only children" so
    # `find_children_by_parent` degrades to plain `list_sub_issues` results
    # unless a test explicitly configures this to exercise the fallback.
    forge.find_issues_by_parent_metadata.return_value = []
    return forge


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
def integrator_env() -> Iterator[IntegratorEnv]:
    with (
        patch("orchestune.integrator.subprocess.run") as run,
        patch("orchestune.forge.GitHubForge.list_issues_by_label") as list_issues,
        patch("orchestune.forge.GitHubForge.list_open_prs") as list_open_prs,
        patch("orchestune.forge.GitHubForge.create_pull_request") as create_pr,
        patch("orchestune.forge.GitHubForge.merge_pull_request") as merge_pr,
        patch("orchestune.forge.GitHubForge.close_issue") as close_issue,
        patch("orchestune.forge.GitHubForge.add_label") as add_label,
        patch("orchestune.forge.GitHubForge.remove_label") as remove_label,
        patch("orchestune.forge.GitHubForge.add_comment") as add_comment,
        patch("orchestune.forge.GitHubForge.is_current_branch_tip_merged_into") as tip,
        patch("orchestune.forge.GitHubForge.delete_branch") as delete_branch,
        patch("orchestune.forge.GitHubForge.branch_exists") as branch_exists,
        patch("orchestune.forge.GitHubForge.get_issue_labels") as get_issue_labels,
        patch("orchestune.forge.GitHubForge.ensure_labels") as ensure_labels,
    ):
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
            "orchestune.dispatch_cycle.file_lock",
            lambda _lock_path: contextlib.nullcontext(),
        ),
        patch(
            "orchestune.integration_coordinator.file_lock",
            lambda _lock_path: contextlib.nullcontext(),
        ),
        patch(
            "orchestune.integrator_steps.file_lock",
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
            "Dispatch cycle tests must patch `orchestune.dispatch_phase_rebase.ensure_parent_branch` "
            "to prevent accidental git branch creation/push to remote origin."
        )

    monkeypatch.setattr(
        "orchestune.dispatch_phase_rebase.ensure_parent_branch", guarded_ensure
    )
    yield


@pytest.fixture(autouse=True)
def _isolate_git_env(monkeypatch: pytest.MonkeyPatch):
    """Ensure tests run in an isolated Git environment where GIT_* variables are stripped."""
    for var in DANGEROUS_GIT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield

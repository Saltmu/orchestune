"""Test helpers and fixtures for claim service tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestune.dispatch.state import RunState, save_run_state
from orchestune.infra.process_utils import run_state_lock
from orchestune.labels import StatusLabel
from orchestune.models import IssueRecord
from tests.conftest import FakeForge


class MockForge(FakeForge):
    """Mock implementation of Forge interface for issue querying and labeling."""

    def __init__(self, issues: dict[int, IssueRecord] | None = None) -> None:
        super().__init__()
        self.issues = dict(issues or {})
        self.labels_added: list[tuple[int, str]] = []
        self.labels_removed: list[tuple[int, str]] = []
        self.bodies_updated: list[tuple[int, str]] = []
        self.fail_add_label: bool = False
        self.fail_update_body: bool = False

    def get_issue(self, issue_number: int | str) -> IssueRecord | None:
        return self.issues.get(int(issue_number))

    def add_label(self, issue_number: int | str, label: str, actor: str = "") -> None:
        if self.fail_add_label:
            raise RuntimeError("GitHub API error: add_label failed")
        self.labels_added.append((int(issue_number), label))
        super().add_label(issue_number, label, actor=actor)

    def remove_label(self, issue_number: int | str, label: str) -> None:
        self.labels_removed.append((int(issue_number), label))
        super().remove_label(issue_number, label)

    def update_issue_body(self, issue_number: int | str, body: str) -> None:
        if self.fail_update_body:
            raise RuntimeError("GitHub API error: update_issue_body failed")
        self.bodies_updated.append((int(issue_number), body))
        super().update_issue_body(issue_number, body)


def _make_issue(
    number: int = 123,
    title: str = "[FEAT] example task",
    body: str = "",
    labels: tuple[str, ...] = (StatusLabel.QUEUED,),
    state: str = "OPEN",
) -> IssueRecord:
    footprint_body = (
        "## Footprint\n\n"
        "```yaml\n"
        "subtask_id: test-task\n"
        "footprint: [orchestune/foo.py]\n"
        "```\n"
    )
    return IssueRecord(
        number=number,
        title=title,
        body=body or footprint_body,
        labels=labels,
        created_at="2026-09-20T10:00:00Z",
        state=state,
    )


@pytest.fixture
def claim_env(tmp_path: Path):
    """Sets up a workspace directory with run_state.json and git structure."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    from orchestune.infra.git_cli import run_git

    run_git(["init", "-b", "main"], cwd=repo_root)
    run_git(["config", "user.name", "Test User"], cwd=repo_root)
    run_git(["config", "user.email", "test@example.com"], cwd=repo_root)
    (repo_root / "README.md").write_text("initial")
    run_git(["add", "README.md"], cwd=repo_root)
    run_git(["commit", "-m", "initial commit"], cwd=repo_root)

    worktrees_dir = repo_root / "worktrees"
    worktrees_dir.mkdir(exist_ok=True)

    state_path = repo_root / "run_state.json"
    with run_state_lock(state_path.with_suffix(".lock")):
        save_run_state(RunState(active_worktrees={}, launch_history=[]), state_path)

    return {
        "repo_root": repo_root,
        "state_path": state_path,
        "worktrees_dir": worktrees_dir,
    }

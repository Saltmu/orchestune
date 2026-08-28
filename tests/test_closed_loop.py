from __future__ import annotations

import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle import run_dispatch_cycle
from orchestune.dispatch.state import RunState, save_run_state
from orchestune.dispatch.targets import DispatchHandle, DispatchTarget
from orchestune.integrator import Integrator, IntegratorConfig
from orchestune.issue_parsing import PARENT_MARKER
from orchestune.models import IssueRecord, PrRecord, Task
from orchestune.outcome_record import OutcomeRecord
from tests.conftest import get_clean_git_env

pytestmark = pytest.mark.integration


class DummyGitRepo:
    def __init__(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name)
        # Create bare origin repository
        self.origin_path = self.path / "origin.git"
        self.origin_path.mkdir()
        self.run_git(
            ["init", "--bare"],
            cwd=self.origin_path,
            check=True,
        )
        # Create clone local repository
        self.local_path = self.path / "local"
        self.run_git(
            ["clone", str(self.origin_path), str(self.local_path)],
            cwd=self.path,
            check=True,
        )

        # Initial config for git user
        self.run_git(
            ["config", "user.name", "test-bot"],
            cwd=self.local_path,
            check=True,
        )
        self.run_git(
            ["config", "user.email", "test-bot@example.com"],
            cwd=self.local_path,
            check=True,
        )

        # Explicitly create and switch to 'main' branch
        self.run_git(
            ["checkout", "-b", "main"],
            cwd=self.local_path,
        )

        # Initial commit on main
        self._commit_file("README.md", "Dummy Repo\n", "Initial commit")

        # Setup initial target python file
        self._commit_file(
            "src/main.py", "def main():\n    print('Hello')\n", "Initial main.py"
        )

        # Set up the platform-local CI entrypoint. This CI script compiles the
        # Python file, so a syntax error makes integration fail.
        if sys.platform == "win32":
            ci_path = "scripts/local-ci.ps1"
            ci_content = (
                "python -m py_compile src/main.py\n"
                "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
            )
        else:
            ci_path = "scripts/local-ci.sh"
            ci_content = "#!/bin/bash\npython3 -m py_compile src/main.py\n"
        self._commit_file(
            ci_path, ci_content, f"Add {Path(ci_path).name}", executable=True
        )

    def run_git(
        self,
        args: list[str],
        cwd: str | Path | None = None,
        check: bool = False,
        capture_output: bool = True,
        text: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[Any]:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd is not None else str(self.local_path),
            check=check,
            capture_output=capture_output,
            text=text,
            env=get_clean_git_env(env),
        )

    def _commit_file(
        self,
        rel_path: str,
        content: str,
        msg: str,
        executable: bool = False,
    ):
        p = self.local_path / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        if executable:
            p.chmod(0o755)
        self.run_git(
            ["add", rel_path],
            cwd=self.local_path,
            check=True,
        )
        self.run_git(
            ["commit", "-m", msg],
            cwd=self.local_path,
            check=True,
        )
        # Push to origin
        self.run_git(
            ["push", "-u", "origin", "main"],
            cwd=self.local_path,
            check=True,
        )

    def cleanup(self):
        self.temp_dir.cleanup()


class DummyGitHub:
    def __init__(self, local_repo_path: Path):
        self.local_repo_path = local_repo_path
        self.issues: dict[int, IssueRecord] = {}
        self.issue_states: dict[int, str] = {}  # "open" or "closed"
        self.prs: dict[str, PrRecord] = {}  # head_ref -> PrRecord
        self.comments: dict[int, list[tuple[str, str]]] = {}

    def add_issue(self, issue: IssueRecord):
        self.issues[issue.number] = issue
        self.issue_states[issue.number] = "open"

    def list_issues_by_label(
        self, label: str, state: str = "open", limit: int = 1000
    ) -> list[IssueRecord]:
        results = []
        for issue in self.issues.values():
            issue_state = self.issue_states.get(issue.number, "open")
            if state != "all" and issue_state != state:
                continue
            if label in issue.labels:
                results.append(issue)
        return results

    def list_sub_issues(self, parent_issue_number: int | str) -> list[IssueRecord]:
        number = int(parent_issue_number)
        return [
            issue
            for issue in self.issues.values()
            if issue.parent and issue.parent.get("number") == number
        ]

    def find_issues_by_parent_metadata(
        self, parent_issue_number: int | str
    ) -> list[IssueRecord]:
        """#485: this dummy has no body-metadata search backend; every
        issue in this closed-loop test is linked via native `parent`."""
        return []

    def get_issue(self, issue_number: int | str) -> IssueRecord | None:
        return self.issues.get(int(issue_number))

    def update_issue_body(self, issue_number: int | str, body: str) -> None:
        """#513: recompute_count/forced_serial永続化の統合テスト対象。"""
        num = int(issue_number)
        if num in self.issues:
            issue = self.issues[num]
            self.issues[num] = IssueRecord(
                number=issue.number,
                title=issue.title,
                body=body,
                labels=issue.labels,
                created_at=issue.created_at,
                parent=issue.parent,
                blocked_by=issue.blocked_by,
            )

    def add_label(self, issue_number: int | str, label: str) -> None:
        num = int(issue_number)
        if num in self.issues:
            issue = self.issues[num]
            if label not in issue.labels:
                new_labels = issue.labels + (label,)
                self.issues[num] = IssueRecord(
                    number=issue.number,
                    title=issue.title,
                    body=issue.body,
                    labels=new_labels,
                    created_at=issue.created_at,
                    parent=issue.parent,
                    blocked_by=issue.blocked_by,
                )

    def remove_label(self, issue_number: int | str, label: str) -> None:
        num = int(issue_number)
        if num in self.issues:
            issue = self.issues[num]
            if label in issue.labels:
                new_labels = tuple(x for x in issue.labels if x != label)
                self.issues[num] = IssueRecord(
                    number=issue.number,
                    title=issue.title,
                    body=issue.body,
                    labels=new_labels,
                    created_at=issue.created_at,
                    parent=issue.parent,
                    blocked_by=issue.blocked_by,
                )

    def add_comment(self, issue_number: int | str, body: str) -> None:
        num = int(issue_number)
        now_str = datetime.now(UTC).isoformat()
        self.comments.setdefault(num, []).append((body, now_str))

    def list_comments(self, issue_number: int | str) -> list[dict[str, Any]]:
        num = int(issue_number)
        return [
            {"body": body, "created_at": ts, "author": "bot"}
            for body, ts in self.comments.get(num, [])
        ]

    def list_open_prs(
        self, limit: int = 1000, paginate_files: bool = False
    ) -> list[PrRecord]:
        return list(self.prs.values())

    def list_prs(
        self, state: str = "open", limit: int = 1000, paginate_files: bool = False
    ) -> list[PrRecord]:
        # #293: このダミー実装はclosed/mergedを追跡しないため、状態を問わず
        # 常に既知のPR（open相当）を返す。テストシナリオ上はopenなPRしか
        # 登場しないため、`state="all"`呼び出しでも十分。
        return list(self.prs.values())

    def list_remote_branches(self) -> list[str]:
        # Use subprocess to list remote tracking branches in the test local repo
        res = subprocess.run(
            ["git", "branch", "-r", "--format=%(refname:short)"],
            cwd=str(self.local_repo_path),
            capture_output=True,
            text=True,
        )
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]

    def branch_changed_files(self, branch: str, base: str = "origin/main") -> list[str]:
        res = subprocess.run(
            ["git", "diff", "--name-only", f"{base}...{branch}"],
            cwd=str(self.local_repo_path),
            capture_output=True,
            text=True,
        )
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]

    def get_label_actor(self, issue_number: int | str, label: str) -> str:
        """#119: このクローズドループテストでは常に信頼された付与者とみなす。"""
        return "trusted-actor"

    def get_actor_permission(self, username: str) -> str:
        return "write"


class DummyAgentDispatchTarget(DispatchTarget):
    def __init__(self, run_scenario_func):
        self.run_scenario_func = run_scenario_func
        self.active_handles = {}
        self.completed_ids = set()

    def launch(
        self,
        task: Task,
        branch_name: str,
        worktree_path: Path,
        *,
        force_push: bool = False,
        execution_selection=None,
        base_branch: str | None = None,
    ) -> DispatchHandle:
        # Run agent task scenario synchronously
        self.run_scenario_func(task, branch_name, worktree_path)
        handle = DispatchHandle(
            external_id=f"dummy-ext-id-{task.issue_number}",
            branch_name=branch_name,
            issue_number=task.issue_number,
        )
        self.active_handles[handle.external_id] = handle
        return handle

    def is_complete(self, handle: DispatchHandle, forge=None) -> bool:
        return handle.external_id in self.completed_ids

    def mark_complete(self, external_id: str):
        self.completed_ids.add(external_id)


def make_agent_scenario(dummy_github: DummyGitHub):
    attempt = 0

    def scenario(task: Task, branch_name: str, worktree_path: Path):
        nonlocal attempt
        attempt += 1

        # Git config in worktree (required for committing)
        subprocess.run(
            ["git", "config", "user.name", "agent-bot"],
            cwd=str(worktree_path),
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "agent-bot@example.com"],
            cwd=str(worktree_path),
            check=True,
        )

        if attempt == 1:
            # 1st attempt: syntax error
            content = (
                "def main():\n    print('Hello World'  # Syntax Error: missing paren\n"
            )
            p = worktree_path / "src/main.py"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

            # Commit and push
            subprocess.run(
                ["git", "add", "src/main.py"], cwd=str(worktree_path), check=True
            )
            subprocess.run(
                ["git", "commit", "-m", "Implement feature with bug"],
                cwd=str(worktree_path),
                check=True,
            )
            subprocess.run(
                ["git", "push", "-u", "origin", branch_name],
                cwd=str(worktree_path),
                check=True,
            )

            # Register PR on DummyGitHub
            dummy_github.prs[branch_name] = PrRecord(
                number=101,
                head_ref=branch_name,
                changed_files=("src/main.py",),
                closes_issue_numbers=(task.issue_number,),
                review_decision="",
                is_ci_passing=False,  # CI failing initially
            )
            dummy_github.add_comment(
                task.issue_number,
                OutcomeRecord(result="done", issue=task.issue_number, pr=101).render(),
            )
        else:
            # 2nd attempt: correct code
            content = "def main():\n    print('Hello World')\n"
            p = worktree_path / "src/main.py"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

            # Commit and push
            subprocess.run(
                ["git", "add", "src/main.py"], cwd=str(worktree_path), check=True
            )
            subprocess.run(
                ["git", "commit", "-m", "Fix syntax error in main.py"],
                cwd=str(worktree_path),
                check=True,
            )
            # Force push to update branch
            subprocess.run(
                ["git", "push", "--force", "origin", branch_name],
                cwd=str(worktree_path),
                check=True,
            )

            # Update PR
            dummy_github.prs[branch_name] = PrRecord(
                number=101,
                head_ref=branch_name,
                changed_files=("src/main.py",),
                closes_issue_numbers=(task.issue_number,),
                review_decision="",
                is_ci_passing=True,  # Now CI passes
            )
            dummy_github.add_comment(
                task.issue_number,
                OutcomeRecord(result="done", issue=task.issue_number, pr=101).render(),
            )

    return scenario


def test_closed_loop_flow():
    import os

    repo = DummyGitRepo()
    dummy_github = DummyGitHub(repo.local_path)

    # 1. Register status:queued issue
    issue_body = "```yaml\nsubtask_id: task-1\nfootprint:\n  - src/main.py\n```\n"
    issue = IssueRecord(
        number=1,
        title="Fix main.py",
        body=issue_body,
        labels=("status:queued",),
        created_at="2026-07-07T00:00:00Z",
    )
    dummy_github.add_issue(issue)

    scenario_func = make_agent_scenario(dummy_github)
    agent_target = DummyAgentDispatchTarget(scenario_func)

    # Setup dispatcher state
    run_state_path = repo.local_path / "run_state.json"
    save_run_state(RunState(), run_state_path)

    config = DispatcherConfig(
        max_concurrent=1,
        max_launches_per_window=5,
        window_seconds=3600,
        run_state_path=run_state_path,
        worktree_root=repo.local_path / "worktrees",
        log_dir=repo.local_path / "logs",
        events_log_path=repo.local_path / "events.jsonl",
        apply=True,
        dispatch_target=agent_target,
        forge=dummy_github,
        deviation_buffer_lines=1,
    )

    # Git operations remain patched because they are not Forge methods.
    patches = [
        patch(
            "orchestune.dispatch.phase_rebase.list_remote_branches",
            dummy_github.list_remote_branches,
        ),
        patch(
            "orchestune.dispatch.phase_rebase.branch_changed_files",
            dummy_github.branch_changed_files,
        ),
    ]

    for p in patches:
        p.start()

    original_cwd = os.getcwd()
    os.chdir(str(repo.local_path))

    try:
        # ---- Cycle 1: Dispatch Task ----
        report = run_dispatch_cycle(config)
        assert len(report.selected) == 1
        assert report.selected[0].subtask_id == "task-1"
        assert "status:in-progress" in dummy_github.issues[1].labels
        assert "status:queued" not in dummy_github.issues[1].labels
        assert "claude/issue-1-task-1" in dummy_github.prs

        # Mark agent execution complete
        agent_target.mark_complete("dummy-ext-id-1")

        # ---- Cycle 2: Detect Completion ----
        report2 = run_dispatch_cycle(config)
        assert len(report2.completion_events) == 1
        assert report2.completion_events[0]["action"] == "completed"
        assert "status:done" in dummy_github.issues[1].labels
        assert "status:in-progress" not in dummy_github.issues[1].labels

        # ---- Integrator Phase: Temp Merge and CI Error Reversion ----
        int_config = IntegratorConfig(
            repository_root=repo.local_path,
            base_branch="origin/main",
            temp_branch="integration/temp-main",
            apply=True,
            forge=dummy_github,
        )
        integrator = Integrator(int_config)
        res = integrator.run()

        print("--- DEBUG: git worktree list ---")
        subprocess.run(["git", "worktree", "list"])
        print("--------------------------------")

        if res.get("status") != "failure":
            print("Integrator failed unexpected. Output:", res)
        assert res["status"] == "failure"
        assert "task-1" in res["failed"]
        # Confirm that the issue has been reverted to queued
        assert "status:queued" in dummy_github.issues[1].labels
        assert "status:done" not in dummy_github.issues[1].labels
        assert len(dummy_github.comments.get(1, [])) > 0
        assert "仮マージCIでエラーが検出されたため" in dummy_github.comments[1][-1][0]

        # ---- Cycle 3: Dispatch Reverted Task (Fix Attempt) ----
        report3 = run_dispatch_cycle(config)
        assert len(report3.selected) == 1
        assert report3.selected[0].subtask_id == "task-1"
        assert "status:in-progress" in dummy_github.issues[1].labels
        assert "status:queued" not in dummy_github.issues[1].labels

        # Mark agent execution complete
        agent_target.mark_complete("dummy-ext-id-1")

        # ---- Cycle 4: Detect Correction Completion ----
        report4 = run_dispatch_cycle(config)
        assert len(report4.completion_events) == 1
        assert report4.completion_events[0]["action"] == "completed"
        assert "status:done" in dummy_github.issues[1].labels
        assert "status:in-progress" not in dummy_github.issues[1].labels

        # ---- Integrator Phase: Re-Merge (Success) ----
        int_config2 = IntegratorConfig(
            repository_root=repo.local_path,
            base_branch="origin/main",
            temp_branch="integration/temp-main",
            apply=True,
            forge=dummy_github,
        )
        integrator2 = Integrator(int_config2)
        res2 = integrator2.run()
        if res2.get("status") != "success":
            print("Integrator2 failed unexpected. Output:", res2)
        assert res2["status"] == "success"
        assert "task-1" in res2["merged"]

        # Verify output in temporary integration branch
        subprocess.run(["git", "fetch", "origin"], cwd=str(repo.local_path), check=True)
        res_show = subprocess.run(
            [
                "git",
                "show",
                f"origin/{integrator2.config.temp_branch}:src/main.py",
            ],
            cwd=str(repo.local_path),
            capture_output=True,
            text=True,
            check=True,
        )
        assert "print('Hello World')" in res_show.stdout
        assert "Syntax Error" not in res_show.stdout

    finally:
        os.chdir(original_cwd)
        for p in patches:
            p.stop()
        repo.cleanup()


def test_closed_loop_dag_recomputation_serialization():
    import os

    repo = DummyGitRepo()
    dummy_github = DummyGitHub(repo.local_path)

    # #327: parent_issue_number=100の`ensure_parent_branch`ガード検証
    # （`is_epic_issue`）が本物のGitHub `gh issue view 100`を呼ばないよう、
    # EPIC形のIssueを明示的に登録する。
    dummy_github.add_issue(
        IssueRecord(
            number=100,
            title="[EPIC] Concurrent dispatch scenario",
            body=f"...\n{PARENT_MARKER}",
            labels=(),
            created_at="2026-07-07T00:00:00Z",
        )
    )

    # 1. Register 2 issues (task-1, task-2)
    # They don't overlap initial footprints, so they can run concurrently
    issue_body_1 = "```yaml\nsubtask_id: task-1\nfootprint:\n  - src/main.py\n```\n"
    issue_1 = IssueRecord(
        number=1,
        title="Fix main.py",
        body=issue_body_1,
        labels=("status:queued",),
        created_at="2026-07-07T00:00:00Z",
        parent={"number": 100},
    )
    dummy_github.add_issue(issue_1)

    issue_body_2 = "```yaml\nsubtask_id: task-2\nfootprint:\n  - src/other.py\n```\n"
    issue_2 = IssueRecord(
        number=2,
        title="Fix other.py",
        body=issue_body_2,
        labels=("status:queued",),
        created_at="2026-07-07T00:00:00Z",
        parent={"number": 100},
    )
    dummy_github.add_issue(issue_2)

    # Dummy scenario that writes to a deviated file for task-2
    # For task-1, it just does normal work
    def make_concurrent_scenario():
        def scenario(task: Task, branch_name: str, worktree_path: Path):
            subprocess.run(
                ["git", "config", "user.name", "agent-bot"],
                cwd=str(worktree_path),
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "agent-bot@example.com"],
                cwd=str(worktree_path),
                check=True,
            )
            if task.subtask_id == "task-1":
                # Normal edit on declared footprint
                p = worktree_path / "src/main.py"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("def main():\n    pass\n", encoding="utf-8")
            elif task.subtask_id == "task-2":
                # Normal edit
                p = worktree_path / "src/other.py"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("def other():\n    pass\n", encoding="utf-8")
                # Deviation: edit src/main.py which is NOT in footprint!
                p_dev = worktree_path / "src/main.py"
                p_dev.write_text("# Deviated edit!\n", encoding="utf-8")
            elif task.subtask_id == "task-3":
                p = worktree_path / "src/third.py"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("def third():\n    pass\n", encoding="utf-8")

            subprocess.run(["git", "add", "."], cwd=str(worktree_path), check=True)
            subprocess.run(
                ["git", "commit", "-m", f"Edit for {task.subtask_id}"],
                cwd=str(worktree_path),
                check=True,
            )
            subprocess.run(
                ["git", "push", "-u", "origin", branch_name],
                cwd=str(worktree_path),
                check=True,
            )
            dummy_github.prs[branch_name] = PrRecord(
                number=100 + task.issue_number,
                head_ref=branch_name,
                changed_files=(
                    ("src/main.py", "src/other.py")
                    if task.subtask_id == "task-2"
                    else ("src/main.py",)
                ),
                closes_issue_numbers=(task.issue_number,),
                review_decision="",
                is_ci_passing=True,
            )
            dummy_github.add_comment(
                task.issue_number,
                OutcomeRecord(
                    result="done", issue=task.issue_number, pr=100 + task.issue_number
                ).render(),
            )

        return scenario

    scenario_func = make_concurrent_scenario()
    agent_target = DummyAgentDispatchTarget(scenario_func)

    # Setup dispatcher state
    run_state_path = repo.local_path / "run_state.json"
    save_run_state(RunState(), run_state_path)

    config = DispatcherConfig(
        max_concurrent=3,  # Independent task can still launch during force-serial
        max_launches_per_window=5,
        window_seconds=3600,
        run_state_path=run_state_path,
        worktree_root=repo.local_path / "worktrees",
        log_dir=repo.local_path / "logs",
        events_log_path=repo.local_path / "events.jsonl",
        apply=True,
        dispatch_target=agent_target,
        forge=dummy_github,
        deviation_buffer_lines=1,
        max_recompute_retries=0,  # Trigger force-serial immediately on first deviation
        parent_issue_number=100,  # For notification comments
    )

    # Git operations remain patched because they are not Forge methods.
    patches = [
        patch(
            "orchestune.dispatch.phase_rebase.list_remote_branches",
            dummy_github.list_remote_branches,
        ),
        patch(
            "orchestune.dispatch.phase_rebase.branch_changed_files",
            dummy_github.branch_changed_files,
        ),
    ]

    for p in patches:
        p.start()

    original_cwd = os.getcwd()
    os.chdir(str(repo.local_path))

    try:
        # ---- Cycle 1: Launch both tasks in parallel ----
        report = run_dispatch_cycle(config)
        assert len(report.selected) == 2
        assert {t.subtask_id for t in report.selected} == {"task-1", "task-2"}
        assert "status:in-progress" in dummy_github.issues[1].labels
        assert "status:in-progress" in dummy_github.issues[2].labels

        # Both agent executions are ongoing.
        # Now trigger Cycle 2. task-2 has edited src/main.py causing deviation.
        report2 = run_dispatch_cycle(config)

        # Confirm deviation event and force-serial transition
        assert len(report2.deviation_events) == 1
        assert report2.deviation_events[0]["action"] == "forced_serial"
        # Confirm that task-2 has status:force-serial label
        assert "status:force-serial" in dummy_github.issues[2].labels

        # force-serialは衝突範囲だけを直列化し、独立タスクの起動は維持する。
        # task-3は独立しているため、次サイクルで起動されるはず。
        issue_body_3 = (
            "```yaml\nsubtask_id: task-3\nfootprint:\n  - src/third.py\n```\n"
        )
        issue_3 = IssueRecord(
            number=3,
            title="Fix third.py",
            body=issue_body_3,
            labels=("status:queued",),
            created_at="2026-07-07T00:00:00Z",
            parent={"number": 100},
        )
        dummy_github.add_issue(issue_3)

        # Run Cycle 3. task-3 is independent from the forced-serial task and should launch.
        report3 = run_dispatch_cycle(config)
        assert len(report3.selected) == 1
        assert report3.selected[0].subtask_id == "task-3"
        assert report3.quota_slots_available == 1
        assert "status:in-progress" in dummy_github.issues[3].labels

    finally:
        os.chdir(original_cwd)
        for p in patches:
            p.stop()
        repo.cleanup()

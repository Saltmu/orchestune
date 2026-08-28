"""End-to-End integration tests for Execution Profiles.

Validates the full lifecycle across:
- Task planning and decomposition schema parsing (SubTask with execution_profile)
- Issue provisioning (orchestune provision embedding execution_profile in Footprint YAML)
- Configuration parsing (orchestune.toml / pyproject.toml execution_profiles table)
- Deterministic profile resolution (resolve_execution_profile for targets)
- Target launch parameter propagation (ExecutionSelection passed to DispatchTarget)
- State persistence (ActiveWorktree / CompletedWorktree in RunState)
- Cycle reporting (CycleReport, events.jsonl, GitHub Step Summary, parent issue comments)
- Fallbacks (unspecified profile -> default, unknown profile -> fallback with warning)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle import run_dispatch_cycle
from orchestune.dispatch.execution_profiles import (
    DEFAULT_EXECUTION_PROFILE,
    ExecutionProfileConfig,
    ExecutionSelection,
    extract_execution_profile_config,
    resolve_execution_profile,
)
from orchestune.dispatch.report import write_github_step_summary
from orchestune.dispatch.state import (
    load_run_state,
)
from orchestune.dispatch.targets import (
    ClaudeCodeCloudRoutineDispatchTarget,
    CodexCloudDispatchTarget,
    DispatchHandle,
    DispatchTarget,
    _format_local_cmd,
)
from orchestune.models import PrRecord, Task
from orchestune.outcome_record import OutcomeRecord
from orchestune.provisioning.flow import provision_issues
from tests.conftest import FakeForge


class RecordingDispatchTarget(DispatchTarget):
    """A test DispatchTarget recording launch invocations with execution selections."""

    def __init__(self, target_name: str = "claude-cli") -> None:
        self.target_name = target_name
        self.launched_tasks: list[
            tuple[Task, str, Path, ExecutionSelection | None]
        ] = []
        self.completed_handles: set[str] = set()

    def launch(
        self,
        task: Task,
        branch_name: str,
        worktree_path: Path,
        *,
        force_push: bool = False,
        execution_selection: ExecutionSelection | None = None,
        base_branch: str | None = None,
    ) -> DispatchHandle:
        self.launched_tasks.append(
            (task, branch_name, worktree_path, execution_selection)
        )
        handle_id = f"handle-{task.issue_number}"
        return DispatchHandle(
            pid=1000 + task.issue_number,
            external_id=handle_id,
            branch_name=branch_name,
            issue_number=task.issue_number,
        )

    def is_complete(self, handle: DispatchHandle, forge: Any = None) -> bool:
        return handle.external_id in self.completed_handles

    def mark_completed(self, issue_number: int) -> None:
        self.completed_handles.add(f"handle-{issue_number}")


_SAMPLE_TEMPLATE = """\
# [FEAT] {{subtask_id}}: {{description}}

## 概要 (Overview)
{{overview}}

## 変更内容 (Proposed Changes)
{{proposed_changes}}

## 受け入れ基準 (Acceptance Criteria)
{{acceptance_criteria}}

## 修正・検証計画 (Verification Plan)
{{verification_plan}}

## Footprint
```yaml
subtask_id: {{subtask_id_yaml}}
footprint: {{footprint}}
symbols: {{symbols}}
depends_on: {{depends_on}}
shared_contract: {{shared_contract}}
writes_shared_contract: {{writes_shared_contract}}
parent_issue_number: {{parent_issue_number}}
execution_profile: {{execution_profile}}
```
"""


class TestExecutionProfileEndToEnd:
    """E2E verification of execution profile planning, provisioning, dispatching, and reporting."""

    def test_full_lifecycle_explicit_default_and_unknown_profiles(
        self, tmp_path: Path, in_memory_forge: FakeForge
    ) -> None:
        """Verify full lifecycle: plan with explicit, null, and unknown profiles."""
        in_memory_forge.set_actor_permission("bot", "write")
        in_memory_forge.set_actor_permission("trusted-actor", "write")
        in_memory_forge.set_actor_permission("", "write")

        template_file = tmp_path / "issue_template.md"
        template_file.write_text(_SAMPLE_TEMPLATE, encoding="utf-8")

        # 1. Write decomposition plan
        plan_content = """\
---
title: "Execution Profile Integration Epic"
parent_issue_number: null
subtasks:
  - id: task-deep
    description: "Deep reasoning complex algorithm"
    priority: high
    footprint: [src/algo.py]
    symbols: [algo.solve]
    depends_on: []
    execution_profile: "deep-reasoning"
    issue_number: null
  - id: task-default
    description: "Standard task using default profile"
    priority: medium
    footprint: [src/utils.py]
    symbols: [utils.helper]
    depends_on: []
    execution_profile: null
    issue_number: null
  - id: task-unknown
    description: "Task specifying unknown profile fallback"
    priority: low
    footprint: [src/fallback.py]
    symbols: [fallback.run]
    depends_on: []
    execution_profile: "non-existent-profile"
    issue_number: null
---

# Decomposition Plan Description
Testing full lifecycle of execution profiles.
"""
        plan_file = tmp_path / "decomposition_plan.md"
        plan_file.write_text(plan_content, encoding="utf-8")

        # 2. Provision issues
        prov_result = provision_issues(
            plan_file,
            forge=in_memory_forge,
            template_path=template_file,
        )
        assert prov_result.applied is True
        assert prov_result.parent_issue_number is not None
        parent_num = prov_result.parent_issue_number

        deep_num = prov_result.created["task-deep"]
        default_num = prov_result.created["task-default"]
        unknown_num = prov_result.created["task-unknown"]

        for num in (deep_num, default_num, unknown_num):
            in_memory_forge.set_label_actor(num, "status:queued", "bot")

        # Verify Issue bodies contain execution_profile
        deep_issue = in_memory_forge.get_issue(deep_num)
        assert deep_issue is not None
        assert "execution_profile: deep-reasoning" in deep_issue.body

        default_issue = in_memory_forge.get_issue(default_num)
        assert default_issue is not None
        assert "execution_profile: null" in default_issue.body

        unknown_issue = in_memory_forge.get_issue(unknown_num)
        assert unknown_issue is not None
        assert "execution_profile: non-existent-profile" in unknown_issue.body

        # 3. Configure Execution Profiles in TOML
        profile_config_dict = {
            "default_execution_profile": "balanced",
            "execution_profiles": {
                "balanced": {
                    "claude-cli": {
                        "model": "claude-3-5-haiku-20241022",
                    },
                    "codex-cli": {
                        "model": "gpt-4o-mini",
                        "reasoning_effort": "low",
                    },
                },
                "deep-reasoning": {
                    "claude-cli": {
                        "model": "claude-3-7-sonnet-20250219",
                    },
                    "codex-cli": {
                        "model": "o3-mini",
                        "reasoning_effort": "high",
                    },
                },
                "fast-code": {
                    "claude-cli": {
                        "model": "claude-3-5-sonnet-20241022",
                    },
                    "codex-cli": {
                        "model": "gpt-4o",
                        "reasoning_effort": "low",
                    },
                },
            },
        }
        profile_config = extract_execution_profile_config(profile_config_dict)

        # 4. Setup Dispatcher with Recording Target
        target = RecordingDispatchTarget(target_name="claude-cli")
        run_state_path = tmp_path / "run_state.json"
        events_log_path = tmp_path / "events.jsonl"
        worktree_root = tmp_path / "worktrees"

        config = DispatcherConfig(
            max_concurrent=5,
            max_launches_per_window=10,
            window_seconds=3600,
            run_state_path=run_state_path,
            events_log_path=events_log_path,
            worktree_root=worktree_root,
            apply=True,
            zombie_gc=False,
            dispatch_target=target,
            forge=in_memory_forge,
            parent_issue_number=parent_num,
            execution_profile_config=profile_config,
        )

        # 5. Run Dispatch Cycle 1: Launch all 3 tasks
        with (
            patch("orchestune.dispatch.worktree._create_worktree"),
            patch("orchestune.dispatch.targets._push_branch_and_verify"),
            patch("orchestune.dispatch.phase_rebase.ensure_parent_branch"),
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
            ),
        ):
            report = run_dispatch_cycle(config)

        assert len(report.selected) == 3
        selected_by_num = {t.issue_number: t for t in report.selected}
        assert deep_num in selected_by_num
        assert default_num in selected_by_num
        assert unknown_num in selected_by_num

        # Check execution_selections in CycleReport
        assert deep_num in report.execution_selections
        deep_sel = report.execution_selections[deep_num]
        assert deep_sel.profile == "deep-reasoning"
        assert deep_sel.model == "claude-3-7-sonnet-20250219"

        assert default_num in report.execution_selections
        default_sel = report.execution_selections[default_num]
        assert default_sel.profile == "balanced"
        assert default_sel.model == "claude-3-5-haiku-20241022"

        assert unknown_num in report.execution_selections
        unknown_sel = report.execution_selections[unknown_num]
        assert unknown_sel.profile == "balanced"
        assert unknown_sel.model == "claude-3-5-haiku-20241022"
        assert "unknown profile" in unknown_sel.reason
        assert "fell back to default profile" in unknown_sel.reason

        # Verify target.launch received correct selections
        assert len(target.launched_tasks) == 3
        launched_by_num = {
            task.issue_number: sel for task, _, _, sel in target.launched_tasks
        }
        assert launched_by_num[deep_num] == deep_sel
        assert launched_by_num[default_num] == default_sel
        assert launched_by_num[unknown_num] == unknown_sel

        # Verify RunState active_worktrees contains profile metadata
        state = load_run_state(run_state_path)
        assert len(state.active_worktrees) == 3
        for issue_num, expected_sel in [
            (deep_num, deep_sel),
            (default_num, default_sel),
            (unknown_num, unknown_sel),
        ]:
            active_wt = next(
                wt
                for wt in state.active_worktrees.values()
                if wt.issue_number == issue_num
            )
            assert active_wt.profile == expected_sel.profile
            assert active_wt.model == expected_sel.model
            assert active_wt.reasoning_effort == expected_sel.reasoning_effort
            assert active_wt.selection_reason == expected_sel.reason

        # Verify events.jsonl contents
        assert events_log_path.exists()
        log_lines = [
            json.loads(line)
            for line in events_log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(log_lines) >= 1
        last_event = log_lines[-1]
        selected_event_entries = {
            entry["issue_number"]: entry for entry in last_event["selected"]
        }
        assert selected_event_entries[deep_num]["execution_profile"] == "deep-reasoning"
        assert selected_event_entries[deep_num]["model"] == "claude-3-7-sonnet-20250219"
        assert selected_event_entries[default_num]["execution_profile"] == "balanced"
        assert (
            selected_event_entries[default_num]["model"] == "claude-3-5-haiku-20241022"
        )
        assert selected_event_entries[unknown_num]["execution_profile"] == "balanced"
        assert (
            selected_event_entries[unknown_num]["model"] == "claude-3-5-haiku-20241022"
        )

        # Verify write_github_step_summary formatting
        summary_path = tmp_path / "step_summary.md"
        write_github_step_summary(report, None, str(summary_path))
        summary_text = summary_path.read_text(encoding="utf-8")
        assert "プロファイル" in summary_text
        assert "モデル" in summary_text
        assert "`deep-reasoning`" in summary_text
        assert "`claude-3-7-sonnet-20250219`" in summary_text
        assert "`balanced`" in summary_text
        assert "`claude-3-5-haiku-20241022`" in summary_text

        # 6. Complete task-deep and run Cycle 2 to test GC and CompletedWorktree persistence
        target.mark_completed(deep_num)
        in_memory_forge.add_comment(
            deep_num,
            OutcomeRecord(result="done", issue=deep_num, pr=201).render(),
        )
        in_memory_forge.seed_pr(
            PrRecord(
                number=201,
                head_ref=f"claude/issue-{deep_num}-task-deep",
                changed_files=("src/algo.py",),
                closes_issue_numbers=(deep_num,),
                review_decision="",
                is_ci_passing=True,
            )
        )

        with (
            patch("orchestune.dispatch.worktree._create_worktree"),
            patch("orchestune.dispatch.targets._push_branch_and_verify"),
            patch("orchestune.dispatch.phase_rebase.ensure_parent_branch"),
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
            ),
            patch(
                "orchestune.dispatch.gc.completion.remote_branch_commit_sha_if_ahead",
                return_value="abc1234",
            ),
            patch("orchestune.dispatch.gc.completion.remove_worktree"),
        ):
            report2 = run_dispatch_cycle(config)

        assert len(report2.completion_events) == 1
        assert report2.completion_events[0]["issue_number"] == deep_num

        # Verify CompletedWorktree in RunState
        state2 = load_run_state(run_state_path)
        assert len(state2.completed_worktrees) == 1
        completed_wt = state2.completed_worktrees[0]
        assert completed_wt.issue_number == deep_num
        assert completed_wt.subtask_id == "task-deep"
        assert completed_wt.profile == "deep-reasoning"
        assert completed_wt.model == "claude-3-7-sonnet-20250219"
        assert completed_wt.selection_reason == deep_sel.reason

    def test_codex_cli_profile_with_reasoning_effort_e2e(
        self, tmp_path: Path, in_memory_forge: FakeForge
    ) -> None:
        """Verify Codex CLI target resolves model and reasoning effort into launch and state."""
        in_memory_forge.set_actor_permission("bot", "write")
        in_memory_forge.set_actor_permission("", "write")

        # 1. Create issue with execution profile
        num = in_memory_forge.create_issue(
            title="[FEAT] task-reasoning: Complex algorithm",
            body=(
                "## Footprint\n```yaml\n"
                "subtask_id: task-reasoning\n"
                "footprint:\n  - src/reason.py\n"
                "symbols:\n  - reason.solve\n"
                "depends_on: []\n"
                "execution_profile: deep-reasoning\n"
                "```\n"
            ),
            labels=("status:queued",),
        )
        in_memory_forge.set_label_actor(num, "status:queued", "bot")

        # 2. Config with codex-cli target
        profile_config_dict = {
            "default_execution_profile": "balanced",
            "execution_profiles": {
                "balanced": {
                    "codex-cli": {
                        "model": "gpt-4o-mini",
                        "reasoning_effort": "low",
                    },
                },
                "deep-reasoning": {
                    "codex-cli": {
                        "model": "o3-mini",
                        "reasoning_effort": "high",
                    },
                },
            },
        }
        profile_config = extract_execution_profile_config(profile_config_dict)

        # 3. Test _format_local_cmd unit behavior
        task_sample = Task(
            issue_number=num,
            subtask_id="task-reasoning",
            footprint=("src/reason.py",),
            symbols=(),
            risk=False,
            priority="medium",
            progress_partial=False,
            status_labels=("status:queued",),
            created_at="2026-01-01T00:00:00+00:00",
            depends_on=(),
            execution_profile="deep-reasoning",
        )
        formatted = _format_local_cmd(
            "codex exec --issue {issue_number}",
            task_sample,
            "codex/issue-1-task-reasoning",
            tmp_path / "worktrees" / "wt-1",
            "o3-mini",
            "high",
            "deep-reasoning",
        )
        assert formatted == [
            "codex",
            "exec",
            "--issue",
            str(num),
            "--model",
            "o3-mini",
            "-c",
            "model_reasoning_effort=high",
        ]

        # 4. LocalProcessDispatchTarget with recording target
        target = RecordingDispatchTarget(target_name="codex-cli")

        run_state_path = tmp_path / "run_state.json"
        events_log_path = tmp_path / "events.jsonl"
        worktree_root = tmp_path / "worktrees"

        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=5,
            window_seconds=3600,
            run_state_path=run_state_path,
            events_log_path=events_log_path,
            worktree_root=worktree_root,
            apply=True,
            dispatch_target=target,
            forge=in_memory_forge,
            execution_profile_config=profile_config,
        )

        with (
            patch("orchestune.dispatch.worktree._create_worktree"),
            patch("orchestune.dispatch.targets._push_branch_and_verify"),
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
            ),
        ):
            report = run_dispatch_cycle(config)

        assert len(report.selected) == 1
        task = report.selected[0]
        assert task.issue_number == num

        # Check launch selection
        sel = report.execution_selections[num]
        assert sel.profile == "deep-reasoning"
        assert sel.model == "o3-mini"
        assert sel.reasoning_effort == "high"

        # Check RunState
        state = load_run_state(run_state_path)
        assert len(state.active_worktrees) == 1
        active_wt = next(iter(state.active_worktrees.values()))
        assert active_wt.profile == "deep-reasoning"
        assert active_wt.model == "o3-mini"
        assert active_wt.reasoning_effort == "high"

        # Check events log
        log_lines = [
            json.loads(line)
            for line in events_log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        last_event = log_lines[-1]
        entry = last_event["selected"][0]
        assert entry["execution_profile"] == "deep-reasoning"
        assert entry["model"] == "o3-mini"
        assert entry["reasoning_effort"] == "high"

    def test_unconfigured_profiles_fallback_gracefully_e2e(
        self, tmp_path: Path, in_memory_forge: FakeForge
    ) -> None:
        """Verify empty profile configuration resolves cleanly without errors."""
        in_memory_forge.set_actor_permission("bot", "write")
        in_memory_forge.set_actor_permission("", "write")

        num = in_memory_forge.create_issue(
            title="[FEAT] task-plain: Plain task",
            body=(
                "## Footprint\n```yaml\n"
                "subtask_id: task-plain\n"
                "footprint:\n  - src/plain.py\n"
                "symbols: []\n"
                "depends_on: []\n"
                "execution_profile: custom-algo\n"
                "```\n"
            ),
            labels=("status:queued",),
        )
        in_memory_forge.set_label_actor(num, "status:queued", "bot")

        # Empty ExecutionProfileConfig
        empty_profile_config = ExecutionProfileConfig()
        target = RecordingDispatchTarget(target_name="claude-cli")

        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=5,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            events_log_path=tmp_path / "events.jsonl",
            worktree_root=tmp_path / "worktrees",
            apply=True,
            dispatch_target=target,
            forge=in_memory_forge,
            execution_profile_config=empty_profile_config,
        )

        with (
            patch("orchestune.dispatch.worktree._create_worktree"),
            patch("orchestune.dispatch.targets._push_branch_and_verify"),
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
            ),
        ):
            report = run_dispatch_cycle(config)

        assert len(report.selected) == 1
        sel = report.execution_selections[num]
        assert sel.profile == DEFAULT_EXECUTION_PROFILE
        assert sel.model is None
        assert sel.reasoning_effort is None

    def test_cloud_targets_profile_resolution_e2e(
        self, tmp_path: Path, in_memory_forge: FakeForge
    ) -> None:
        """Verify profile resolution for CloudRoutine and CodexCloud targets."""
        in_memory_forge.create_issue(
            title="[FEAT] task-cloud: Cloud task",
            body=(
                "## Footprint\n```yaml\n"
                "subtask_id: task-cloud\n"
                "footprint:\n  - src/cloud.py\n"
                "symbols: []\n"
                "depends_on: []\n"
                "execution_profile: deep-reasoning\n"
                "```\n"
            ),
            labels=("status:queued",),
        )

        profile_config_dict = {
            "default_execution_profile": "balanced",
            "execution_profiles": {
                "balanced": {
                    "cloud-routine": {
                        "model": "claude-3-5-haiku-20241022",
                    },
                    "codex-cloud": {
                        "model": "gpt-4o-mini",
                    },
                },
                "deep-reasoning": {
                    "cloud-routine": {
                        "model": "claude-3-7-sonnet-20250219",
                    },
                    "codex-cloud": {
                        "model": "o3-mini",
                        "reasoning_effort": "high",
                    },
                },
            },
        }
        profile_config = extract_execution_profile_config(profile_config_dict)

        # Test Cloud Routine target
        cloud_target = ClaudeCodeCloudRoutineDispatchTarget(
            routine_id="routine-123",
            routine_token="token-abc",
        )
        sel_cloud = resolve_execution_profile(
            "deep-reasoning", cloud_target, profile_config
        )
        assert sel_cloud.profile == "deep-reasoning"
        assert sel_cloud.model == "claude-3-7-sonnet-20250219"

        # Test Codex Cloud target
        codex_cloud_target = CodexCloudDispatchTarget(
            environment_id="env-codex",
        )
        sel_codex = resolve_execution_profile(
            "deep-reasoning", codex_cloud_target, profile_config
        )
        assert sel_codex.profile == "deep-reasoning"
        assert sel_codex.model == "o3-mini"
        assert sel_codex.reasoning_effort == "high"

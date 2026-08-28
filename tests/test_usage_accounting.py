import json
from pathlib import Path
from unittest.mock import MagicMock

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.gc.completion import _apply_completed_worktree_outcome
from orchestune.dispatch.postcycle import _format_event_log_comment
from orchestune.dispatch.scoring import quota_available
from orchestune.dispatch.state import (
    ActiveWorktree,
    CompletedWorktree,
    RunState,
    load_run_state,
    save_run_state,
)
from orchestune.dispatch.targets import (
    CLAUDE_CLI_LOCAL_CMD_TEMPLATE,
    DispatchHandle,
    DispatchTarget,
    LocalProcessDispatchTarget,
)
from orchestune.models import Task, Usage
from orchestune.outcome_record import OutcomeRecord


class _DummyDispatchTarget(DispatchTarget):
    def launch(
        self,
        task: Task,
        branch_name: str,
        worktree_path: Path,
        *,
        force_push: bool = False,
        execution_selection=None,
        base_branch: str | None = None,
    ):
        return DispatchHandle(branch_name=branch_name)

    def is_complete(self, handle: DispatchHandle, forge=None) -> bool:
        return True


class TestUsageModel:
    def test_usage_dataclass_fields(self):
        usage = Usage(
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            model="claude-3-7-sonnet-20250219",
            cost_usd=0.015,
        )
        assert usage.input_tokens == 100
        assert usage.output_tokens == 50
        assert usage.total_tokens == 150
        assert usage.model == "claude-3-7-sonnet-20250219"
        assert usage.cost_usd == 0.015

    def test_usage_defaults(self):
        usage = Usage(input_tokens=10, output_tokens=20, total_tokens=30)
        assert usage.model is None
        assert usage.cost_usd is None


class TestDispatchTargetCollectUsage:
    def test_default_collect_usage_returns_none(self):
        target = _DummyDispatchTarget()
        assert target.collect_usage(DispatchHandle()) is None

    def test_claude_template_includes_stream_json_output_format(self):
        assert "--output-format stream-json" in CLAUDE_CLI_LOCAL_CMD_TEMPLATE

    def test_extract_usage_preserves_zero_cost(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True)
        slug = "claude-issue-99-task-z"
        log_file = log_dir / f"{slug}.log"
        sample_json = {
            "type": "result",
            "total_cost_combined": 0.0,
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "model": "claude-3-7-sonnet",
        }
        log_file.write_text(json.dumps(sample_json) + "\n", encoding="utf-8")
        target = LocalProcessDispatchTarget(log_dir=log_dir)
        handle = DispatchHandle(branch_name="claude/issue-99-task-z")
        usage = target.collect_usage(handle)
        assert usage is not None
        assert usage.cost_usd == 0.0

    def test_local_process_collect_usage_parses_json_log(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True)
        slug = "claude-issue-1-task-a"
        log_file = log_dir / f"{slug}.log"

        sample_json = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Done",
            "session_id": "sess-123",
            "total_cost_combined": 0.025,
            "usage": {
                "input_tokens": 1200,
                "output_tokens": 300,
            },
            "model": "claude-3-7-sonnet-20250219",
        }
        log_file.write_text(
            f"Starting agent...\nProcessing...\n{json.dumps(sample_json)}\n",
            encoding="utf-8",
        )

        target = LocalProcessDispatchTarget(log_dir=log_dir)
        handle = DispatchHandle(branch_name="claude/issue-1-task-a")
        usage = target.collect_usage(handle)

        assert usage is not None
        assert usage.input_tokens == 1200
        assert usage.output_tokens == 300
        assert usage.total_tokens == 1500
        assert usage.model == "claude-3-7-sonnet-20250219"
        assert usage.cost_usd == 0.025

    def test_local_process_collect_usage_parses_nested_total_tokens_if_present(
        self, tmp_path
    ):
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True)
        slug = "claude-issue-1-task-a"
        log_file = log_dir / f"{slug}.log"

        sample_json = {
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 200,
                "total_tokens": 1200,
            },
            "model": "claude-3-5-haiku",
        }
        log_file.write_text(f"{json.dumps(sample_json)}\n", encoding="utf-8")

        target = LocalProcessDispatchTarget(log_dir=log_dir)
        usage = target.collect_usage(
            DispatchHandle(branch_name="claude/issue-1-task-a")
        )

        assert usage is not None
        assert usage.total_tokens == 1200
        assert usage.model == "claude-3-5-haiku"

    def test_local_process_collect_usage_handles_trailing_non_json_lines(
        self, tmp_path
    ):
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True)
        slug = "claude-issue-1-task-a"
        log_file = log_dir / f"{slug}.log"

        sample_json = {
            "usage": {"input_tokens": 500, "output_tokens": 100},
            "model": "claude-3-7-sonnet",
        }
        log_file.write_text(
            f"header\n{json.dumps(sample_json)}\nProcess finished with exit code 0\n",
            encoding="utf-8",
        )

        target = LocalProcessDispatchTarget(log_dir=log_dir)
        usage = target.collect_usage(
            DispatchHandle(branch_name="claude/issue-1-task-a")
        )

        assert usage is not None
        assert usage.input_tokens == 500
        assert usage.output_tokens == 100
        assert usage.total_tokens == 600

    def test_local_process_collect_usage_returns_none_when_log_missing_or_empty(
        self, tmp_path
    ):
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True)
        target = LocalProcessDispatchTarget(log_dir=log_dir)

        # Missing log
        assert target.collect_usage(DispatchHandle(branch_name="nonexistent")) is None

        # Empty log
        empty_log = log_dir / "empty.log"
        empty_log.write_text("", encoding="utf-8")
        assert target.collect_usage(DispatchHandle(branch_name="empty")) is None

    def test_local_process_collect_usage_returns_none_when_invalid_json(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True)
        slug = "invalid-json"
        (log_dir / f"{slug}.log").write_text(
            "not a json string at all\n", encoding="utf-8"
        )

        target = LocalProcessDispatchTarget(log_dir=log_dir)
        assert target.collect_usage(DispatchHandle(branch_name="invalid-json")) is None


class TestDispatchStateUsagePersistence:
    def test_save_and_load_roundtrip_with_usage(self, tmp_path):
        path = tmp_path / "run_state.json"
        usage = Usage(
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            model="claude-3-7-sonnet",
            cost_usd=0.01,
        )
        cw = CompletedWorktree(
            issue_number=1,
            subtask_id="task-1",
            branch="b1",
            started_at=1000.0,
            completed_at=1100.0,
            usage=usage,
        )
        state = RunState(completed_worktrees=[cw])
        save_run_state(state, path, now=1100.0)

        loaded = load_run_state(path)
        assert len(loaded.completed_worktrees) == 1
        loaded_cw = loaded.completed_worktrees[0]
        assert loaded_cw.usage == usage

    def test_load_backwards_compatible_when_usage_missing(self, tmp_path):
        path = tmp_path / "run_state.json"
        data = {
            "active_worktrees": {},
            "launch_history": [],
            "completed_worktrees": [
                {
                    "issue_number": 1,
                    "subtask_id": "task-1",
                    "branch": "b1",
                    "started_at": 1000.0,
                    "completed_at": 1100.0,
                }
            ],
        }
        path.write_text(json.dumps(data), encoding="utf-8")

        loaded = load_run_state(path)
        assert loaded.completed_worktrees[0].usage is None


class TestQuotaAvailableTokens:
    def test_quota_available_unlimited_when_max_tokens_none(self):
        state = RunState(active_worktrees={}, launch_history=[])
        assert (
            quota_available(
                state,
                now=1000.0,
                max_concurrent=5,
                max_launches_per_window=5,
                window_seconds=3600,
                max_tokens_per_window=None,
            )
            == 5
        )

    def test_quota_available_throttles_when_token_limit_reached(self):
        now = 1000.0
        cw = CompletedWorktree(
            issue_number=1,
            subtask_id="t1",
            branch="b1",
            started_at=900.0,
            completed_at=950.0,
            usage=Usage(input_tokens=800, output_tokens=200, total_tokens=1000),
        )
        state = RunState(
            active_worktrees={}, launch_history=[], completed_worktrees=[cw]
        )

        # 累計1000 >= 上限1000 -> 0枠
        assert (
            quota_available(
                state,
                now=now,
                max_concurrent=5,
                max_launches_per_window=5,
                window_seconds=3600,
                max_tokens_per_window=1000,
            )
            == 0
        )

        # 上限1500 > 累計1000 -> まだ枠あり (min(5, 5, ...) -> 5)
        assert (
            quota_available(
                state,
                now=now,
                max_concurrent=5,
                max_launches_per_window=5,
                window_seconds=3600,
                max_tokens_per_window=1500,
            )
            == 5
        )

    def test_quota_available_ignores_tokens_outside_window(self):
        now = 5000.0
        cw_old = CompletedWorktree(
            issue_number=1,
            subtask_id="t1",
            branch="b1",
            started_at=100.0,
            completed_at=200.0,  # window_seconds=3600 より前
            usage=Usage(input_tokens=8000, output_tokens=2000, total_tokens=10000),
        )
        state = RunState(
            active_worktrees={}, launch_history=[], completed_worktrees=[cw_old]
        )

        assert (
            quota_available(
                state,
                now=now,
                max_concurrent=2,
                max_launches_per_window=2,
                window_seconds=3600,
                max_tokens_per_window=5000,
            )
            == 2
        )


class TestTaskTokenLimitEscalation:
    def test_exceeding_max_tokens_per_task_escalates_to_human_review(self, tmp_path):
        active = ActiveWorktree(
            issue_number=42,
            branch="claude/issue-42-task-x",
            worktree_path=str(tmp_path / "wt"),
            pid=None,
            started_at=100.0,
            declared_footprint=(),
        )
        (tmp_path / "wt").mkdir(parents=True)
        task = Task(
            issue_number=42,
            subtask_id="task-x",
            footprint=(),
            symbols=(),
            risk=False,
            priority="medium",
            progress_partial=False,
            status_labels=("status:in-progress",),
            created_at="2026-01-01T00:00:00+00:00",
        )
        forge = MagicMock()
        target = MagicMock()
        target.collect_usage.return_value = Usage(
            input_tokens=8000,
            output_tokens=3000,
            total_tokens=11000,
            model="claude-3-7-sonnet",
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            forge=forge,
            dispatch_target=target,
            worktree_root=tmp_path / "worktrees",
            max_tokens_per_task=10000,
        )

        from orchestune.dispatch.gc.completion import CompletedWorktreeDecision

        decision = CompletedWorktreeDecision(
            action="completed", subtask_id="task-x", commit_sha="abcdef"
        )
        event = _apply_completed_worktree_outcome(active, decision, config, task)

        assert event["action"] == "escalated_token_limit_exceeded"
        # status:blocked-human-review への変更
        forge.remove_label.assert_called_with(42, "status:in-progress")
        forge.add_label.assert_called_with(42, "status:blocked-human-review")
        # コメントに実消費量とモデル名が含まれる
        assert forge.add_comment.called
        comment = forge.add_comment.call_args.args[1]
        assert "11,000" in comment or "11000" in comment
        assert "claude-3-7-sonnet" in comment

    def test_escalated_worktree_is_recorded_in_completed_worktrees_and_throttles_window_quota(
        self, tmp_path
    ):
        from orchestune.dispatch.gc import _rule_completed
        from orchestune.dispatch.rules import CycleContext

        active = ActiveWorktree(
            issue_number=42,
            branch="claude/issue-42-task-x",
            worktree_path=str(tmp_path / "wt"),
            pid=None,
            started_at=100.0,
            declared_footprint=(),
        )
        (tmp_path / "wt").mkdir(parents=True)
        task = Task(
            issue_number=42,
            subtask_id="task-x",
            footprint=(),
            symbols=(),
            risk=False,
            priority="medium",
            progress_partial=False,
            status_labels=("status:in-progress",),
            created_at="2026-01-01T00:00:00+00:00",
        )
        forge = MagicMock()
        outcome = OutcomeRecord(result="done", issue=42)
        forge.list_comments.return_value = [
            {"body": outcome.render(), "created_at": "2026-01-01T00:00:00Z"}
        ]
        forge.list_prs.return_value = []
        target = MagicMock()

        target.collect_usage.return_value = Usage(
            input_tokens=15000,
            output_tokens=5000,
            total_tokens=20000,
            model="claude-3-7-sonnet",
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            forge=forge,
            dispatch_target=target,
            worktree_root=tmp_path / "worktrees",
            max_tokens_per_task=10000,
            max_tokens_per_window=15000,
        )
        run_state = RunState(
            active_worktrees={"42": active}, launch_history=[], completed_worktrees=[]
        )
        ctx = CycleContext(
            config=config,
            run_state=run_state,
            tasks_by_issue={42: task},
            issue_number_by_subtask_id={"task-x": 42},
            prs=[],
            pr_by_branch={},
            done_subtask_ids=set(),
            ci_passed_pr_subtask_ids=set(),
            changes_requested_subtask_ids=set(),
            subtask_branch_map={},
        )

        from unittest.mock import patch

        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                return_value=(True, "abcdef123456"),
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch.gc.completion.remove_worktree"),
        ):
            outcome = _rule_completed(ctx, "42", active, task)

        assert outcome is not None
        assert outcome.completion_event["action"] == "escalated_token_limit_exceeded"
        # 帳簿からactiveは消え、completed_worktreesにusage付きで記録されていること
        assert "42" not in run_state.active_worktrees
        assert len(run_state.completed_worktrees) == 1
        cw = run_state.completed_worktrees[0]
        assert cw.issue_number == 42
        assert cw.usage is not None
        assert cw.usage.total_tokens == 20000

        # quota_available がこのエスカレーションタスクの消費量を計算してスロットを 0 に制限すること
        assert (
            quota_available(
                run_state,
                now=150.0,
                max_concurrent=5,
                max_launches_per_window=5,
                window_seconds=3600,
                max_tokens_per_window=config.max_tokens_per_window,
            )
            == 0
        )


class TestPostcycleCycleReportFormatting:
    def test_format_cycle_report_renders_model_and_tokens(self):
        from orchestune.dispatch.cycle import CycleReport

        report = CycleReport(
            selected=[],
            quota_slots_available=1,
            lock_changes={"to_lock": [], "to_unlock": []},
            deviation_events=[],
            completion_events=[
                {
                    "issue_number": 10,
                    "subtask_id": "task-a",
                    "action": "completed",
                    "usage": {
                        "total_tokens": 1500,
                        "model": "claude-3-7-sonnet-20250219",
                    },
                },
                {
                    "issue_number": 11,
                    "subtask_id": "task-b",
                    "action": "completed",
                    "usage": None,
                },
            ],
            promotion_events=[],
            applied=True,
        )

        rendered = _format_event_log_comment(report, [])
        assert "task-a" in rendered
        assert "claude-3-7-sonnet-20250219" in rendered
        assert "1,500" in rendered or "1500" in rendered
        assert "不明" in rendered  # task-b の usage が None のため

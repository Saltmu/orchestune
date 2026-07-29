"""`IntegrationPipeline` の合成と、パイプライン全体を通した統合の成功パス。

個々のステップ（`PrepareTasksStep`〜`AutoMergeChildIntegrationStep`）の
振る舞いは `test_integrator_step_*.py` 側で検証する。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import ANY, MagicMock

from orchestune.integrator import (
    IntegrationComponent,
    IntegrationContext,
    IntegrationPipeline,
    Integrator,
    IntegratorConfig,
    MultiIssueIntegrator,
)
from tests.conftest import IntegratorEnv, make_done_issue


def _context(config: IntegratorConfig) -> IntegrationContext:
    return IntegrationContext(
        config=config,
        repository_root=Path("."),
        original_root=Path("."),
        base_branch="main",
        temp_branch="temp-main",
    )


class TestIntegratorRun:
    def test_no_done_tasks(self, integrator_env: IntegratorEnv):
        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "no_done_tasks"

    def test_success_integration(self, integrator_env: IntegratorEnv):
        """依存順にマージし、統合PRを作成し、自動マージとIssueクローズまで到達する。"""
        issue_a = make_done_issue(1, subtask_id="task-1")
        issue_b = make_done_issue(2, subtask_id="task-2", depends_on=("task-1",))
        # `status:done`側は依存と逆順で返し、トポロジカルソートが効くことを示す。
        integrator_env.set_done_issues(issue_a, issue_b, done=[issue_b, issue_a])

        config = IntegratorConfig(apply=True, parent_issue_number=100)
        res = Integrator(config).run()

        assert res["status"] == "success"
        assert res["merged"] == ["task-1", "task-2"]
        assert res["integration_pr_number"] == 999
        assert res["semantic_review_dispatched"] is False

        merge_calls = integrator_env.calls_with("merge")
        assert len(merge_calls) == 2
        assert any("claude/issue-1-task-1" in arg for arg in merge_calls[0].args[0])
        assert any("claude/issue-2-task-2" in arg for arg in merge_calls[1].args[0])

        # 統合ブランチから親ブランチへのPRが作成される。子レベル
        # （parent_issue_number指定時）は、この後Integratorが自らこのPRを
        # 自動マージし、対象Issueも自動クローズする（最終マージ＝親ブランチ→main
        # のみ人間が行う）。
        create_pr = integrator_env.create_pull_request
        create_pr.assert_called_once()
        assert create_pr.call_args.kwargs["head"] == "integration/temp-parent-issue-100"
        assert create_pr.call_args.kwargs["base"] == "parent/issue-100"
        assert "task-1" in create_pr.call_args.kwargs["body"]
        assert "自動的にマージ" in create_pr.call_args.kwargs["body"]

        integrator_env.merge_pull_request.assert_called_once_with(999)
        integrator_env.close_issue.assert_any_call(1, "completed", comment=ANY)
        integrator_env.close_issue.assert_any_call(2, "completed", comment=ANY)
        assert integrator_env.close_issue.call_count == 2
        assert res["auto_merged"] is True
        assert sorted(res["closed_issues"]) == [1, 2]


class TestIntegrationContext:
    def test_carries_config_and_branch_names(self):
        config = IntegratorConfig(apply=True, parent_issue_number=100)
        ctx = IntegrationContext(
            config=config,
            repository_root=Path("/tmp/repo"),
            original_root=Path("/tmp/repo"),
            base_branch="main",
            temp_branch="temp-main",
        )

        assert ctx.config == config
        assert ctx.repository_root == Path("/tmp/repo")
        assert ctx.base_branch == "main"
        assert ctx.temp_branch == "temp-main"
        assert ctx.merged_tasks == []


class TestIntegrationPipeline:
    def test_aggregates_step_results_on_success(self):
        class DummyStep1(IntegrationComponent):
            def execute(self, ctx: IntegrationContext) -> dict:
                ctx.merged_tasks.append("task-1")
                return {"step1": "ok"}

        class DummyStep2(IntegrationComponent):
            def execute(self, ctx: IntegrationContext) -> dict:
                ctx.merged_tasks.append("task-2")
                return {"step2": "ok"}

        ctx = _context(IntegratorConfig(apply=True))
        res = IntegrationPipeline([DummyStep1(), DummyStep2()]).execute(ctx)

        assert res == {
            "status": "success",
            "step1": "ok",
            "step2": "ok",
            "merged": ["task-1", "task-2"],
            "integration_pr_number": None,
            "semantic_review_dispatched": False,
            "newly_included": [],
        }
        assert ctx.merged_tasks == ["task-1", "task-2"]

    def test_short_circuits_remaining_steps_on_failure(self):
        class FailStep(IntegrationComponent):
            def execute(self, ctx: IntegrationContext) -> dict:
                return {"status": "failure", "error": "something wrong"}

        class DummyStep(IntegrationComponent):
            def execute(self, ctx: IntegrationContext) -> dict:
                ctx.merged_tasks.append("task-skipped")
                return {"step": "ok"}

        ctx = _context(IntegratorConfig(apply=True))
        res = IntegrationPipeline([FailStep(), DummyStep()]).execute(ctx)

        assert res["status"] == "failure"
        assert "error" in res
        assert ctx.merged_tasks == []


class TestMultiIssueIntegrator:
    def test_reports_each_parent_issue_result(self):
        class DummyIntegrator(IntegrationComponent):
            def __init__(self, issue_number: int):
                self.parent_issue = issue_number

            def execute(self, ctx: IntegrationContext) -> dict:
                return {"status": "success", "parent_issue": self.parent_issue}

        runner = MultiIssueIntegrator([DummyIntegrator(100), DummyIntegrator(200)])
        res = runner.execute(_context(IntegratorConfig(apply=True)))

        assert res["status"] == "composite_success"
        assert res["details"]["issue_100"] == {"status": "success", "parent_issue": 100}
        assert res["details"]["issue_200"] == {"status": "success", "parent_issue": 200}

    def test_partial_success_when_one_child_fails(self):
        class SuccessDummy(IntegrationComponent):
            def execute(self, ctx: IntegrationContext) -> dict:
                return {"status": "success"}

        class FailDummy(IntegrationComponent):
            def execute(self, ctx: IntegrationContext) -> dict:
                return {"status": "failure"}

        runner = MultiIssueIntegrator([SuccessDummy(), FailDummy()])
        res = runner.execute(_context(IntegratorConfig(apply=True)))

        assert res["status"] == "composite_partial_success"

    def test_composite_failure_when_all_children_fail(self):
        class FailDummy(IntegrationComponent):
            def execute(self, ctx: IntegrationContext) -> dict:
                return {"status": "failed_to_push_temp_branch"}

        runner = MultiIssueIntegrator([FailDummy(), FailDummy()])
        res = runner.execute(_context(IntegratorConfig(apply=True)))

        assert res["status"] == "composite_failure"

    def test_preserves_injected_forge_identity(self):
        """#313レビュー対応: `copy.deepcopy(ctx)`によって注入済みForgeが
        複製されず、各sub_ctxが同一のForgeインスタンスを参照し続けることを
        確認する（fake_forgeの呼び出し履歴が複製ごとに分断されない）。"""
        captured_forges = []

        class CaptureForgeIntegrator(IntegrationComponent):
            def execute(self, ctx: IntegrationContext) -> dict:
                captured_forges.append(ctx.forge)
                return {"status": "success"}

        injected_forge = MagicMock()
        runner = MultiIssueIntegrator(
            [CaptureForgeIntegrator(), CaptureForgeIntegrator()]
        )
        ctx = _context(IntegratorConfig(apply=True, forge=injected_forge))
        res = runner.execute(ctx)

        assert res["status"] == "composite_success"
        assert captured_forges == [injected_forge, injected_forge]

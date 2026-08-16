"""`IntegrationPipeline` の合成と、パイプライン全体を通した統合の成功パス。

個々のステップ（`PrepareTasksStep`〜`AutoMergeChildIntegrationStep`）の
振る舞いは `test_integrator_step_*.py` 側で検証する。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import ANY, MagicMock

from orchestune.integrator import (
    AutoMergeChildIntegrationStep,
    EnsureIntegrationPrStep,
    IntegrationComponent,
    IntegrationContext,
    IntegrationPipeline,
    IntegrationReport,
    IntegrationStatus,
    Integrator,
    IntegratorConfig,
    LabelIncludedStep,
    MergeAndTestStep,
    MultiIssueIntegrator,
    PrepareTasksStep,
    PushTempBranchStep,
    RetryChildIssueCloseStep,
    SemanticReviewStep,
    SetupWorktreeStep,
)
from orchestune.integrator_steps import (
    AutoMergeChildIntegrationStep as ExtractedAutoMergeChildIntegrationStep,
)
from orchestune.integrator_steps import (
    EnsureIntegrationPrStep as ExtractedEnsureIntegrationPrStep,
)
from orchestune.integrator_steps import LabelIncludedStep as ExtractedLabelIncludedStep
from orchestune.integrator_steps import MergeAndTestStep as ExtractedMergeAndTestStep
from orchestune.integrator_steps import PrepareTasksStep as ExtractedPrepareTasksStep
from orchestune.integrator_steps import (
    PushTempBranchStep as ExtractedPushTempBranchStep,
)
from orchestune.integrator_steps import (
    RetryChildIssueCloseStep as ExtractedRetryChildIssueCloseStep,
)
from orchestune.integrator_steps import (
    SemanticReviewStep as ExtractedSemanticReviewStep,
)
from orchestune.integrator_steps import SetupWorktreeStep as ExtractedSetupWorktreeStep
from tests.conftest import IntegratorEnv, make_done_issue


def test_legacy_step_exports_reference_extracted_implementations():
    assert PrepareTasksStep is ExtractedPrepareTasksStep
    assert RetryChildIssueCloseStep is ExtractedRetryChildIssueCloseStep
    assert SetupWorktreeStep is ExtractedSetupWorktreeStep
    assert MergeAndTestStep is ExtractedMergeAndTestStep
    assert PushTempBranchStep is ExtractedPushTempBranchStep
    assert EnsureIntegrationPrStep is ExtractedEnsureIntegrationPrStep
    assert SemanticReviewStep is ExtractedSemanticReviewStep
    assert LabelIncludedStep is ExtractedLabelIncludedStep
    assert AutoMergeChildIntegrationStep is ExtractedAutoMergeChildIntegrationStep


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

        config = IntegratorConfig(
            apply=True, parent_issue_number=100, integration_run_id="test-run"
        )
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
        assert (
            create_pr.call_args.kwargs["head"]
            == "integration/temp-parent-issue-100-test-run"
        )
        assert create_pr.call_args.kwargs["base"] == "parent/issue-100"
        assert "task-1" in create_pr.call_args.kwargs["body"]
        assert "自動的にマージ" in create_pr.call_args.kwargs["body"]

        integrator_env.merge_pull_request.assert_not_called()
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
            def execute(self, ctx: IntegrationContext) -> IntegrationReport:
                ctx.merged_tasks.append("task-1")
                return {"retried_closed_issues": [1]}

        class DummyStep2(IntegrationComponent):
            def execute(self, ctx: IntegrationContext) -> IntegrationReport:
                ctx.merged_tasks.append("task-2")
                return {"unparsable_done_issues": [7]}

        ctx = _context(IntegratorConfig(apply=True))
        res = IntegrationPipeline([DummyStep1(), DummyStep2()]).execute(ctx)

        assert res == {
            "status": IntegrationStatus.SUCCESS,
            "retried_closed_issues": [1],
            "unparsable_done_issues": [7],
            "merged": ["task-1", "task-2"],
            "integration_pr_number": None,
            "semantic_review_dispatched": False,
            "newly_included": [],
        }
        assert ctx.merged_tasks == ["task-1", "task-2"]

    def test_short_circuits_remaining_steps_on_failure(self):
        class FailStep(IntegrationComponent):
            def execute(self, ctx: IntegrationContext) -> IntegrationReport:
                return {
                    "status": IntegrationStatus.FAILURE,
                    "error": "something wrong",
                }

        class DummyStep(IntegrationComponent):
            def execute(self, ctx: IntegrationContext) -> IntegrationReport:
                ctx.merged_tasks.append("task-skipped")
                return {}

        ctx = _context(IntegratorConfig(apply=True))
        res = IntegrationPipeline([FailStep(), DummyStep()]).execute(ctx)

        assert res["status"] == IntegrationStatus.FAILURE
        assert "error" in res
        assert ctx.merged_tasks == []

    def test_does_not_clear_stale_marker_label_when_apply_is_false(self, fake_forge):
        # #437レビュー対応: dry-run（apply=False）は他の全ステップと同様に
        # GitHub側の変更を一切行わない契約のため、陳腐化マーカーのクリアも
        # 実行してはならない。ガード無しだと、dry-run実行が実運用（apply=True）
        # で付与された陳腐化マーカーを誤って消してしまい、次の本物のCAS拒否が
        # 「1回目」として扱われてしまう。
        class DummyStep(IntegrationComponent):
            def execute(self, ctx: IntegrationContext) -> IntegrationReport:
                return {"status": IntegrationStatus.SUCCESS}

        config = IntegratorConfig(
            apply=False, parent_issue_number=100, forge=fake_forge
        )
        ctx = _context(config)

        IntegrationPipeline([DummyStep()]).execute(ctx)

        fake_forge.remove_label.assert_not_called()

    def test_clears_stale_marker_label_when_apply_is_true(self, fake_forge):
        class DummyStep(IntegrationComponent):
            def execute(self, ctx: IntegrationContext) -> IntegrationReport:
                return {"status": IntegrationStatus.SUCCESS}

        config = IntegratorConfig(apply=True, parent_issue_number=100, forge=fake_forge)
        ctx = _context(config)

        IntegrationPipeline([DummyStep()]).execute(ctx)

        fake_forge.remove_label.assert_called_once_with(
            100, "integration:parent-branch-stale"
        )


class TestMultiIssueIntegrator:
    def test_reports_each_parent_issue_result(self):
        class DummyIntegrator(IntegrationComponent):
            def __init__(self, issue_number: int):
                self.parent_issue = issue_number

            def execute(self, ctx: IntegrationContext) -> IntegrationReport:
                return {
                    "status": IntegrationStatus.SUCCESS,
                    "merged": [f"parent-{self.parent_issue}"],
                }

        runner = MultiIssueIntegrator([DummyIntegrator(100), DummyIntegrator(200)])
        res = runner.execute(_context(IntegratorConfig(apply=True)))

        assert res["status"] == IntegrationStatus.COMPOSITE_SUCCESS
        assert res["details"]["issue_100"] == {
            "status": IntegrationStatus.SUCCESS,
            "merged": ["parent-100"],
        }
        assert res["details"]["issue_200"] == {
            "status": IntegrationStatus.SUCCESS,
            "merged": ["parent-200"],
        }

    def test_partial_success_when_one_child_fails(self):
        class SuccessDummy(IntegrationComponent):
            def execute(self, ctx: IntegrationContext) -> IntegrationReport:
                return {"status": IntegrationStatus.SUCCESS}

        class FailDummy(IntegrationComponent):
            def execute(self, ctx: IntegrationContext) -> IntegrationReport:
                return {"status": IntegrationStatus.FAILURE}

        runner = MultiIssueIntegrator([SuccessDummy(), FailDummy()])
        res = runner.execute(_context(IntegratorConfig(apply=True)))

        assert res["status"] == IntegrationStatus.COMPOSITE_PARTIAL_SUCCESS

    def test_composite_failure_when_all_children_fail(self):
        class FailDummy(IntegrationComponent):
            def execute(self, ctx: IntegrationContext) -> IntegrationReport:
                return {"status": IntegrationStatus.FAILED_TO_PUSH_TEMP_BRANCH}

        runner = MultiIssueIntegrator([FailDummy(), FailDummy()])
        res = runner.execute(_context(IntegratorConfig(apply=True)))

        assert res["status"] == IntegrationStatus.COMPOSITE_FAILURE

    def test_preserves_injected_forge_identity(self):
        """#313レビュー対応: `copy.deepcopy(ctx)`によって注入済みForgeが
        複製されず、各sub_ctxが同一のForgeインスタンスを参照し続けることを
        確認する（fake_forgeの呼び出し履歴が複製ごとに分断されない）。"""
        captured_forges = []

        class CaptureForgeIntegrator(IntegrationComponent):
            def execute(self, ctx: IntegrationContext) -> IntegrationReport:
                captured_forges.append(ctx.forge)
                return {"status": IntegrationStatus.SUCCESS}

        injected_forge = MagicMock()
        runner = MultiIssueIntegrator(
            [CaptureForgeIntegrator(), CaptureForgeIntegrator()]
        )
        ctx = _context(IntegratorConfig(apply=True, forge=injected_forge))
        res = runner.execute(ctx)

        assert res["status"] == IntegrationStatus.COMPOSITE_SUCCESS
        assert captured_forges == [injected_forge, injected_forge]

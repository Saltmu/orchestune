"""#777: `AutoMergeChildIntegrationStep`のブランチ削除・統合済み検証は、
①正規ブランチ名（Orchestuneが割り当てた名前）のみを対象とし、②のPR
head_ref由来の名前では絶対に削除・誤った統合済み判定を行わないことを検証する。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from orchestune.forge import Forge
from orchestune.integrator.steps import AutoMergeChildIntegrationStep
from orchestune.integrator.types import IntegrationContext, IntegratorConfig
from orchestune.models import Task


def _task(issue_number: int, subtask_id: str) -> Task:
    return Task(
        issue_number=issue_number,
        subtask_id=subtask_id,
        footprint=(),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=(),
        created_at="2026-01-01T00:00:00Z",
    )


def _ctx(
    forge: Forge,
    merged_tasks: list[str],
    active_done_tasks: list[Task],
    fallback_merged_subtask_ids: set[str] | None = None,
) -> IntegrationContext:
    config = IntegratorConfig(parent_issue_number=100, forge=forge)
    return IntegrationContext(
        config=config,
        repository_root=Path("."),
        original_root=Path("."),
        base_branch=config.base_branch,
        temp_branch=config.temp_branch,
        merged_tasks=merged_tasks,
        active_done_tasks=active_done_tasks,
        fallback_merged_subtask_ids=fallback_merged_subtask_ids or set(),
    )


class TestMergedBranchNamesUsesCanonicalOnly:
    def test_uses_default_claude_prefix_for_canonically_merged_tasks(self):
        forge = MagicMock(spec=Forge)
        task = _task(issue_number=5, subtask_id="t5")
        ctx = _ctx(forge, merged_tasks=["t5"], active_done_tasks=[task])

        names = AutoMergeChildIntegrationStep._merged_branch_names(ctx)

        assert "claude/issue-5-t5" in names
        assert not any("codex" in name or "feat" in name for name in names)

    def test_excludes_tasks_merged_via_fallback(self):
        """#777 Codexレビュー(Round3): 実際のマージが②（別prefix）を使った
        タスクは、正規名の実在有無に関わらず削除候補にすら含めない
        （統合ウィンドウ中に正規名のブランチが後から実在するようになる
        競合を考慮し、`branch_exists()`チェックに頼らず明示的に除外する）。"""
        forge = MagicMock(spec=Forge)
        canonical_task = _task(issue_number=5, subtask_id="t5")
        fallback_task = _task(issue_number=6, subtask_id="t6")
        ctx = _ctx(
            forge,
            merged_tasks=["t5", "t6"],
            active_done_tasks=[canonical_task, fallback_task],
            fallback_merged_subtask_ids={"t6"},
        )

        names = AutoMergeChildIntegrationStep._merged_branch_names(ctx)

        assert "claude/issue-5-t5" in names
        assert "claude/issue-6-t6" not in names

    def test_includes_temp_branch(self):
        forge = MagicMock(spec=Forge)
        ctx = _ctx(forge, merged_tasks=[], active_done_tasks=[])

        names = AutoMergeChildIntegrationStep._merged_branch_names(ctx)

        assert names == [ctx.temp_branch]


class TestDeleteMergedBranchesNeverTargetsNonCanonicalNames:
    def test_skips_deletion_when_canonical_branch_does_not_exist(self):
        forge = MagicMock(spec=Forge)
        forge.branch_exists.return_value = False
        task = _task(issue_number=6, subtask_id="t6")
        ctx = _ctx(forge, merged_tasks=["t6"], active_done_tasks=[task])
        step = AutoMergeChildIntegrationStep()

        step._delete_merged_branches(ctx)

        forge.branch_exists.assert_any_call("claude/issue-6-t6")
        forge.delete_branch.assert_not_called()

    def test_deletes_only_when_canonical_branch_exists(self):
        forge = MagicMock(spec=Forge)
        forge.branch_exists.return_value = True
        task = _task(issue_number=7, subtask_id="t7")
        ctx = _ctx(forge, merged_tasks=["t7"], active_done_tasks=[task])
        step = AutoMergeChildIntegrationStep()

        step._delete_merged_branches(ctx)

        forge.delete_branch.assert_any_call("claude/issue-7-t7")

    def test_never_checks_or_deletes_canonical_name_for_fallback_merged_task(self):
        """#777 Codexレビュー(Round3): ②でマージされたタスクの正規名は、
        たとえその後`branch_exists()`がTrueを返す状況になっても
        （元のagentのpush遅延・リトライ等で後から実在するようになった場合を
        含め）、削除チェックの対象にすら含めない。"""
        forge = MagicMock(spec=Forge)
        forge.branch_exists.return_value = True
        task = _task(issue_number=8, subtask_id="t8")
        ctx = _ctx(
            forge,
            merged_tasks=["t8"],
            active_done_tasks=[task],
            fallback_merged_subtask_ids={"t8"},
        )
        step = AutoMergeChildIntegrationStep()

        step._delete_merged_branches(ctx)

        checked_branches = [c.args[0] for c in forge.branch_exists.call_args_list]
        deleted_branches = [c.args[0] for c in forge.delete_branch.call_args_list]
        assert "claude/issue-8-t8" not in checked_branches
        assert "claude/issue-8-t8" not in deleted_branches


class TestVerifyAlreadyIntegratedStaysFailClosed:
    def test_returns_false_when_canonical_branch_cannot_be_verified(self):
        """②由来の名前へフォールバックせず、正規名の検証が失敗した場合は
        素直にFalse（fail-closed）を返す。"""
        forge = MagicMock(spec=Forge)
        forge.is_current_branch_tip_merged_into.return_value = False
        task = _task(issue_number=8, subtask_id="t8")
        ctx = _ctx(forge, merged_tasks=["t8"], active_done_tasks=[task])
        step = AutoMergeChildIntegrationStep()

        assert step._verify_already_integrated(ctx) is False

    def test_returns_false_on_lookup_exception(self):
        forge = MagicMock(spec=Forge)
        forge.is_current_branch_tip_merged_into.side_effect = RuntimeError("API down")
        task = _task(issue_number=9, subtask_id="t9")
        ctx = _ctx(forge, merged_tasks=["t9"], active_done_tasks=[task])
        step = AutoMergeChildIntegrationStep()

        assert step._verify_already_integrated(ctx) is False

    def test_returns_true_only_when_canonical_branch_verifies(self):
        forge = MagicMock(spec=Forge)
        forge.is_current_branch_tip_merged_into.return_value = True
        task = _task(issue_number=10, subtask_id="t10")
        ctx = _ctx(forge, merged_tasks=["t10"], active_done_tasks=[task])
        step = AutoMergeChildIntegrationStep()

        assert step._verify_already_integrated(ctx) is True
        forge.is_current_branch_tip_merged_into.assert_any_call(
            "claude/issue-10-t10", "parent/issue-100"
        )

    def test_returns_false_when_merged_task_has_no_matching_active_task(self):
        forge = MagicMock(spec=Forge)
        ctx = _ctx(forge, merged_tasks=["ghost-task"], active_done_tasks=[])
        step = AutoMergeChildIntegrationStep()

        assert step._verify_already_integrated(ctx) is False

    def test_returns_false_without_checking_when_task_was_merged_via_fallback(self):
        """#777 Codexレビュー(Round3): ②でマージされたタスクは、正規名での
        検証が本来の検証対象と異なるため、`is_current_branch_tip_merged_into`
        すら呼ばずに直ちにFalse（fail-closed）とする。"""
        forge = MagicMock(spec=Forge)
        forge.is_current_branch_tip_merged_into.return_value = True
        task = _task(issue_number=11, subtask_id="t11")
        ctx = _ctx(
            forge,
            merged_tasks=["t11"],
            active_done_tasks=[task],
            fallback_merged_subtask_ids={"t11"},
        )
        step = AutoMergeChildIntegrationStep()

        assert step._verify_already_integrated(ctx) is False
        forge.is_current_branch_tip_merged_into.assert_not_called()

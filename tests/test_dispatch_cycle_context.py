"""#777 Codexレビュー(Round4): `_build_pr_mappings`は`dispatch/rebase.py`の
自動リベースで実際のrebase対象ブランチとして使われる`subtask_branch_map`を
構築するため、canonical名限定のままでは非デフォルトprefixの依存先PR
（`codex/issue-N-a`等）を見落とし、CI成功済みでも依存元タスクが誤って
ブロックされ続ける。recovery/integrationと同じ①→②の解決順序で
実際のPRブランチを検出できることを検証する。
"""

from __future__ import annotations

from orchestune.dispatch.cycle_context import _build_pr_mappings
from orchestune.dispatch.scoring import Task
from orchestune.models import PrRecord


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


def _pr(
    head_ref: str,
    number: int = 1,
    review_decision: str = "",
    is_ci_passing: bool = True,
    is_cross_repository: bool | None = False,
) -> PrRecord:
    return PrRecord(
        number=number,
        head_ref=head_ref,
        changed_files=(),
        review_decision=review_decision,
        is_ci_passing=is_ci_passing,
        is_cross_repository=is_cross_repository,
    )


class TestBuildPrMappings:
    def test_canonical_branch_pr_is_detected(self):
        task = _task(1, "a")
        pr = _pr("claude/issue-1-a", is_ci_passing=True)

        pr_by_branch, ci_passed, changes_requested, branch_map = _build_pr_mappings(
            {1: task}, [pr]
        )

        assert branch_map["a"] == "claude/issue-1-a"
        assert "a" in ci_passed
        assert "a" not in changes_requested

    def test_non_default_prefix_pr_is_detected_via_fallback(self):
        """codex等の非canonicalプレフィックスでも、CI成功状態が正しく
        `ci_passed_pr_subtask_ids`へ反映される（依存元タスクが誤って
        ブロックされ続けない）。"""
        task = _task(10, "a")
        pr = _pr("codex/issue-10-a", is_ci_passing=True)

        pr_by_branch, ci_passed, changes_requested, branch_map = _build_pr_mappings(
            {10: task}, [pr]
        )

        assert branch_map["a"] == "codex/issue-10-a"
        assert "a" in ci_passed

    def test_non_default_prefix_changes_requested_is_detected(self):
        task = _task(11, "b")
        pr = _pr("agy/issue-11-b", review_decision="CHANGES_REQUESTED")

        _, ci_passed, changes_requested, branch_map = _build_pr_mappings(
            {11: task}, [pr]
        )

        assert branch_map["b"] == "agy/issue-11-b"
        assert "b" in changes_requested
        assert "b" not in ci_passed

    def test_fork_pr_is_not_treated_as_the_dependency_pr(self):
        """forkのhead_refは信頼できないため、他の解決経路と同様に除外する。"""
        task = _task(12, "c")
        fork_pr = _pr("codex/issue-12-c", is_ci_passing=True, is_cross_repository=True)

        _, ci_passed, changes_requested, branch_map = _build_pr_mappings(
            {12: task}, [fork_pr]
        )

        assert branch_map["c"] == "claude/issue-12-c"
        assert "c" not in ci_passed
        assert "c" not in changes_requested

    def test_ambiguous_multiple_prefixes_fails_closed_to_canonical_guess(self):
        task = _task(13, "d")
        prs = [
            _pr("codex/issue-13-d", number=1, is_ci_passing=True),
            _pr("feat/issue-13-d", number=2, is_ci_passing=True),
        ]

        _, ci_passed, changes_requested, branch_map = _build_pr_mappings(
            {13: task}, prs
        )

        assert branch_map["d"] == "claude/issue-13-d"
        assert "d" not in ci_passed

    def test_no_matching_pr_uses_canonical_guess(self):
        task = _task(14, "e")

        _, ci_passed, changes_requested, branch_map = _build_pr_mappings({14: task}, [])

        assert branch_map["e"] == "claude/issue-14-e"
        assert "e" not in ci_passed
        assert "e" not in changes_requested

    def test_skips_tasks_without_subtask_id(self):
        task_none = Task(
            issue_number=15,
            subtask_id=None,  # type: ignore[arg-type]
            footprint=(),
            symbols=(),
            risk=False,
            priority="medium",
            progress_partial=False,
            status_labels=(),
            created_at="2026-01-01T00:00:00Z",
        )

        _, ci_passed, changes_requested, branch_map = _build_pr_mappings(
            {15: task_none}, []
        )

        assert branch_map == {}

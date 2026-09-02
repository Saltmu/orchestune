"""Tests for orchestune.branch_naming — the single source of truth for
building/parsing Orchestune task branch names (Issue #777)."""

from __future__ import annotations

from orchestune.branch_naming import (
    DEFAULT_TASK_BRANCH_PREFIX,
    ParsedTaskBranch,
    branch_matches_task,
    build_task_branch_name,
    find_unique_matching_pr_branch,
    parse_task_branch_name,
)
from orchestune.models import PrRecord


def _pr(head_ref: str, number: int = 1) -> PrRecord:
    return PrRecord(number=number, head_ref=head_ref, changed_files=())


class TestBuildTaskBranchName:
    def test_default_prefix_matches_historical_claude_convention(self) -> None:
        assert DEFAULT_TASK_BRANCH_PREFIX == "claude"
        assert build_task_branch_name(123, "abc") == "claude/issue-123-abc"

    def test_custom_prefix(self) -> None:
        assert (
            build_task_branch_name(123, "abc", prefix="codex") == "codex/issue-123-abc"
        )

    def test_none_subtask_id_falls_back_to_task(self) -> None:
        assert build_task_branch_name(5, None) == "claude/issue-5-task"

    def test_empty_subtask_id_falls_back_to_task(self) -> None:
        assert build_task_branch_name(5, "") == "claude/issue-5-task"


class TestParseTaskBranchName:
    def test_parses_claude_prefixed_branch(self) -> None:
        assert parse_task_branch_name("claude/issue-123-abc") == ParsedTaskBranch(
            prefix="claude", issue_number=123, subtask_id="abc"
        )

    def test_parses_arbitrary_prefixes(self) -> None:
        for prefix in ("codex", "agy", "feat", "fix", "task", "docs"):
            branch = f"{prefix}/issue-42-mysubtask"
            assert parse_task_branch_name(branch) == ParsedTaskBranch(
                prefix=prefix, issue_number=42, subtask_id="mysubtask"
            )

    def test_subtask_id_may_contain_hyphens(self) -> None:
        assert parse_task_branch_name(
            "claude/issue-7-generation-contract"
        ) == ParsedTaskBranch(
            prefix="claude", issue_number=7, subtask_id="generation-contract"
        )

    def test_rejects_parent_base_branch_without_subtask(self) -> None:
        # `parent/issue-{N}` (no subtask suffix) is a stacked-task *base*
        # branch, not a task branch — must not be misparsed as one.
        assert parse_task_branch_name("parent/issue-700") is None

    def test_rejects_non_matching_shapes(self) -> None:
        assert parse_task_branch_name("main") is None
        assert parse_task_branch_name("issue-123-abc") is None
        assert parse_task_branch_name("claude/feature-123") is None
        assert parse_task_branch_name("") is None

    def test_round_trips_with_build(self) -> None:
        built = build_task_branch_name(9, "subtask-x", prefix="feat")
        assert parse_task_branch_name(built) == ParsedTaskBranch(
            prefix="feat", issue_number=9, subtask_id="subtask-x"
        )


class TestBranchMatchesTask:
    def test_matches_issue_only_across_any_prefix(self) -> None:
        assert branch_matches_task("codex/issue-42-a", 42)
        assert branch_matches_task("feat/issue-42-anything", 42)

    def test_rejects_different_issue_number(self) -> None:
        assert not branch_matches_task("claude/issue-42-a", 43)

    def test_matches_subtask_id_when_given(self) -> None:
        assert branch_matches_task("claude/issue-42-a", 42, "a")
        assert not branch_matches_task("claude/issue-42-a", 42, "b")

    def test_subtask_id_none_or_empty_normalizes_to_task(self) -> None:
        assert branch_matches_task("claude/issue-42-task", 42, None)
        assert branch_matches_task("claude/issue-42-task", 42, "")

    def test_rejects_unparsable_branch(self) -> None:
        assert not branch_matches_task("main", 42)
        assert not branch_matches_task("", 42)


class TestFindUniqueMatchingPrBranch:
    def test_single_match_returns_branch(self) -> None:
        prs = [_pr("codex/issue-5-a", number=1), _pr("claude/issue-6-a", number=2)]
        assert find_unique_matching_pr_branch(prs, 5, "a") == "codex/issue-5-a"

    def test_no_match_returns_none(self) -> None:
        prs = [_pr("claude/issue-6-a")]
        assert find_unique_matching_pr_branch(prs, 5, "a") is None

    def test_ambiguous_multiple_distinct_branches_returns_none(self) -> None:
        # Two *different* branches both shape-match issue 5 / subtask a —
        # fail-closed, do not tie-break.
        prs = [_pr("codex/issue-5-a", number=1), _pr("feat/issue-5-a", number=2)]
        assert find_unique_matching_pr_branch(prs, 5, "a") is None

    def test_multiple_prs_on_the_same_branch_name_is_not_ambiguous(self) -> None:
        # Same branch referenced by two PR records (e.g. closed + reopened) —
        # the branch identity itself is unambiguous.
        prs = [_pr("codex/issue-5-a", number=1), _pr("codex/issue-5-a", number=2)]
        assert find_unique_matching_pr_branch(prs, 5, "a") == "codex/issue-5-a"

    def test_requires_subtask_id_match_not_just_issue_number(self) -> None:
        prs = [_pr("codex/issue-5-other", number=1)]
        assert find_unique_matching_pr_branch(prs, 5, "a") is None

    def test_empty_pr_list_returns_none(self) -> None:
        assert find_unique_matching_pr_branch([], 5, "a") is None

    def test_ignores_prs_with_empty_head_ref(self) -> None:
        prs = [_pr("", number=1), _pr("codex/issue-5-a", number=2)]
        assert find_unique_matching_pr_branch(prs, 5, "a") == "codex/issue-5-a"

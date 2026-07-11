from unittest.mock import patch

from orchestune.dispatch_recovery import (
    _apply_restore_missing_active_worktrees,
    _decide_missing_active_worktrees,
    _extract_raw_subtask_id,
    _parse_subtask_info_from_issue,
)
from orchestune.dispatch_state import ActiveWorktree, RunState
from orchestune.dispatcher import DispatcherConfig
from orchestune.github import IssueRecord


def _issue_with_footprint(number, subtask_id=None, footprint=None, blocked_by=()):
    if subtask_id is None:
        body = "本文のみでFootprintブロックなし"
    else:
        footprint_lines = (
            "\n".join(f"  - {f}" for f in footprint) if footprint else "  []"
        )
        body = (
            "## Footprint\n```yaml\n"
            f"subtask_id: {subtask_id}\n"
            "footprint:\n"
            f"{footprint_lines}\n"
            "```\n"
        )
    return IssueRecord(
        number=number,
        title="t",
        body=body,
        labels=("status:in-progress",),
        created_at="2026-01-01T00:00:00+00:00",
        blocked_by=blocked_by,
    )


class TestExtractRawSubtaskId:
    """decide層の共通ヘルパー: フォールバックを持たない素の抽出結果を検証する。"""

    def test_returns_subtask_id_when_present(self):
        issue = _issue_with_footprint(1, subtask_id="task-a")
        assert _extract_raw_subtask_id(issue) == "task-a"

    def test_returns_none_when_no_footprint_block(self):
        issue = _issue_with_footprint(1, subtask_id=None)
        assert _extract_raw_subtask_id(issue) is None

    def test_returns_none_when_subtask_id_missing_in_yaml(self):
        issue = IssueRecord(
            number=1,
            title="t",
            body="## Footprint\n```yaml\nfootprint: []\n```\n",
            labels=(),
            created_at="2026-01-01T00:00:00+00:00",
        )
        assert _extract_raw_subtask_id(issue) is None


class TestParseSubtaskInfoFromIssue:
    def test_uses_synthetic_fallback_when_missing(self):
        issue = _issue_with_footprint(42, subtask_id=None)
        subtask_id, footprint = _parse_subtask_info_from_issue(issue)
        assert subtask_id == "issue-42"
        assert footprint == ()

    def test_extracts_declared_footprint(self):
        issue = _issue_with_footprint(1, subtask_id="task-a", footprint=["src/foo.py"])
        subtask_id, footprint = _parse_subtask_info_from_issue(issue)
        assert subtask_id == "task-a"
        assert footprint == ("src/foo.py",)


class TestDecideMissingActiveWorktrees:
    """decide層: githubのread-only呼び出し以外の副作用なしで復元計画のみを算出する。"""

    def test_no_missing_issues_returns_empty_without_calling_github(self):
        run_state = RunState(active_worktrees={"1": None})  # type: ignore[arg-type]
        issue = _issue_with_footprint(1, subtask_id="task-a")
        with patch("orchestune.dispatch_recovery.github.list_open_prs") as mock_prs:
            result = _decide_missing_active_worktrees(
                run_state,
                [issue],
                DispatcherConfig(run_state_path="dummy.json", worktree_root="worktrees"),
            )
        assert result == []
        mock_prs.assert_not_called()

    def test_missing_issue_without_pr_decides_synthetic_branch(self):
        run_state = RunState(active_worktrees={})
        issue = _issue_with_footprint(101, subtask_id="task-a", footprint=["src/foo.py"])
        config = DispatcherConfig(run_state_path="dummy.json", worktree_root="worktrees")

        with patch(
            "orchestune.dispatch_recovery.github.list_open_prs", return_value=[]
        ):
            result = _decide_missing_active_worktrees(run_state, [issue], config)

        assert len(result) == 1
        key, subtask_id, active = result[0]
        assert key == "101"
        assert subtask_id == "task-a"
        assert active.branch == "claude/issue-101-task-a"
        assert active.declared_footprint == ("src/foo.py",)
        # decide層はrun_stateを変更しない
        assert run_state.active_worktrees == {}


class TestApplyRestoreMissingActiveWorktrees:
    """act層: decideが算出した内容のみをrun_stateへ書き込む。"""

    def test_empty_restorations_returns_false(self):
        run_state = RunState(active_worktrees={})
        assert _apply_restore_missing_active_worktrees(run_state, []) is False
        assert run_state.active_worktrees == {}

    def test_writes_decided_restorations_into_run_state(self):
        run_state = RunState(active_worktrees={})
        active = ActiveWorktree(
            issue_number=101,
            branch="claude/issue-101-task-a",
            worktree_path="worktrees/claude-issue-101-task-a",
            pid=None,
            started_at=0.0,
            declared_footprint=("src/foo.py",),
        )
        modified = _apply_restore_missing_active_worktrees(
            run_state, [("101", "task-a", active)]
        )
        assert modified is True
        assert run_state.active_worktrees["101"] is active

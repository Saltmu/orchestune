from unittest.mock import patch

from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_recovery import (
    _apply_restore_missing_active_worktrees,
    _decide_missing_active_worktrees,
    _extract_raw_subtask_id,
    _parse_subtask_info_from_issue,
)
from orchestune.dispatch_state import ActiveWorktree, RunState
from orchestune.models import IssueRecord, PrRecord


def _issue_with_footprint(
    number,
    subtask_id=None,
    footprint=None,
    depends_on=None,
    blocked_by=(),
    parent=None,
    created_at="2026-01-01T00:00:00+00:00",
):
    if subtask_id is None:
        body = "本文のみでFootprintブロックなし"
    else:
        footprint_lines = (
            "\n".join(f"  - {f}" for f in footprint) if footprint else "  []"
        )
        depends_on_values = ", ".join(depends_on or ())
        body = (
            "## Footprint\n```yaml\n"
            f"subtask_id: {subtask_id}\n"
            "footprint:\n"
            f"{footprint_lines}\n"
            f"depends_on: [{depends_on_values}]\n"
            "```\n"
        )
    return IssueRecord(
        number=number,
        title="t",
        body=body,
        labels=("status:in-progress",),
        created_at=created_at,
        blocked_by=blocked_by,
        parent=parent,
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

    def test_no_missing_issues_returns_empty_without_calling_github(self, tmp_path):
        run_state = RunState(active_worktrees={"1": None})  # type: ignore[arg-type]
        issue = _issue_with_footprint(1, subtask_id="task-a")
        with patch("orchestune.forge.GitHubForge.list_open_prs") as mock_prs:
            result = _decide_missing_active_worktrees(
                run_state,
                [issue],
                DispatcherConfig(
                    events_log_path=tmp_path / "events.jsonl",
                    run_state_path=tmp_path / "run_state.json",
                    worktree_root=tmp_path / "worktrees",
                ),
            )
        assert result == []
        mock_prs.assert_not_called()

    def test_missing_issue_without_pr_decides_synthetic_branch(self, tmp_path):
        run_state = RunState(active_worktrees={})
        issue = _issue_with_footprint(
            101, subtask_id="task-a", footprint=["src/foo.py"]
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]):
            result = _decide_missing_active_worktrees(run_state, [issue], config)

        assert len(result) == 1
        key, subtask_id, active = result[0]
        assert key == "101"
        assert subtask_id == "task-a"
        assert active.branch == "claude/issue-101-task-a"
        assert active.declared_footprint == ("src/foo.py",)
        # decide層はrun_stateを変更しない
        assert run_state.active_worktrees == {}

    def test_restored_old_issue_has_unknown_start_time(self, tmp_path):
        """#198: Issue作成日時はdispatch開始日時ではないため、復元Taskの
        timeout基準に使ってはならない。"""
        run_state = RunState(active_worktrees={})
        issue = _issue_with_footprint(
            101,
            subtask_id="task-a",
            created_at="2020-01-01T00:00:00+00:00",
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]):
            result = _decide_missing_active_worktrees(run_state, [issue], config)

        assert result[0][2].started_at is None

    def test_restored_cloud_task_has_unknown_start_time(self, tmp_path):
        """PRに紐付くクラウドTaskでも、PRメタデータから実行開始時刻は復元できない。"""
        run_state = RunState(active_worktrees={})
        issue = _issue_with_footprint(
            101,
            subtask_id="task-a",
            created_at="2020-01-01T00:00:00+00:00",
        )
        pr = PrRecord(
            number=42,
            head_ref="agent/issue-101-task-a",
            changed_files=(),
            closes_issue_numbers=(101,),
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[pr]):
            result = _decide_missing_active_worktrees(run_state, [issue], config)

        active = result[0][2]
        assert active.external_id == "42"
        assert active.started_at is None

    def test_missing_issue_base_branch_uses_own_parent_not_multiple_parent_config(
        self,
        tmp_path,
    ):
        """#182: 自己修復はリポジトリ全体のin-progress Issueを対象にするため、
        `config.parent_issue_number`（現在のdispatcher起動時の親）ではなく、
        各Issue自身の`parent`から`base_branch`を復元しなければならない。"""
        run_state = RunState(active_worktrees={})
        issue_under_parent_100 = _issue_with_footprint(
            101, subtask_id="task-a", parent={"number": 100}
        )
        issue_under_parent_200 = _issue_with_footprint(
            201, subtask_id="task-b", parent={"number": 200}
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            parent_issue_number=100,
        )

        with patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]):
            result = _decide_missing_active_worktrees(
                run_state,
                [issue_under_parent_100, issue_under_parent_200],
                config,
            )

        active_by_key = {key: active for key, _, active in result}
        assert active_by_key["101"].base_branch == "parent/issue-100"
        assert active_by_key["201"].base_branch == "parent/issue-200"

    def test_yaml_dependency_restores_open_dependency_pr_as_base_branch(self, tmp_path):
        """#305: GitHub MCP起票でnative blocked_byが空でも、Footprint YAMLの
        depends_onから依存PRのhead branchを復元する。"""
        run_state = RunState(active_worktrees={})
        dependency = _issue_with_footprint(101, subtask_id="task-a")
        dependent = _issue_with_footprint(
            102,
            subtask_id="task-b",
            depends_on=["task-a"],
        )
        dependency_pr = PrRecord(
            number=41,
            head_ref="claude/issue-101-task-a",
            changed_files=(),
            closes_issue_numbers=(101,),
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch(
            "orchestune.forge.GitHubForge.list_open_prs",
            return_value=[dependency_pr],
        ):
            result = _decide_missing_active_worktrees(
                run_state,
                [dependency, dependent],
                config,
            )

        active_by_key = {key: active for key, _, active in result}
        assert active_by_key["102"].base_branch == "claude/issue-101-task-a"

    def test_native_blocked_by_takes_precedence_over_yaml_dependency(self, tmp_path):
        run_state = RunState(active_worktrees={})
        yaml_dependency = _issue_with_footprint(101, subtask_id="task-a")
        native_dependency = _issue_with_footprint(103, subtask_id="task-c")
        dependent = _issue_with_footprint(
            102,
            subtask_id="task-b",
            depends_on=["task-a"],
            blocked_by=(103,),
        )
        yaml_dependency_pr = PrRecord(
            number=41,
            head_ref="claude/issue-101-task-a",
            changed_files=(),
            closes_issue_numbers=(101,),
        )
        native_dependency_pr = PrRecord(
            number=43,
            head_ref="claude/issue-103-task-c",
            changed_files=(),
            closes_issue_numbers=(103,),
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch(
            "orchestune.forge.GitHubForge.list_open_prs",
            return_value=[yaml_dependency_pr, native_dependency_pr],
        ):
            result = _decide_missing_active_worktrees(
                run_state,
                [yaml_dependency, native_dependency, dependent],
                config,
            )

        active_by_key = {key: active for key, _, active in result}
        assert active_by_key["102"].base_branch == "claude/issue-103-task-c"

    def test_unresolved_yaml_dependency_keeps_parent_base_branch(self, tmp_path):
        run_state = RunState(active_worktrees={})
        dependent = _issue_with_footprint(
            102,
            subtask_id="task-b",
            depends_on=["unknown-task"],
            parent={"number": 100},
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]):
            result = _decide_missing_active_worktrees(
                run_state,
                [dependent],
                config,
            )

        assert result[0][2].base_branch == "parent/issue-100"


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

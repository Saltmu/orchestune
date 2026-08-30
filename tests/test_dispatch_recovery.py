from unittest.mock import patch

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.recovery import (
    _apply_restore_missing_active_worktrees,
    _decide_missing_active_worktrees,
    _decide_stale_recovery_counters,
    _extract_raw_subtask_id,
    _parse_subtask_info_from_issue,
    recover_run_state,
)
from orchestune.dispatch.state import ActiveWorktree, RunState
from orchestune.models import IssueRecord, PrRecord


def _issue_with_footprint(
    number,
    subtask_id=None,
    footprint=None,
    depends_on=None,
    blocked_by=(),
    parent=None,
    created_at="2026-01-01T00:00:00+00:00",
    recompute_count=None,
    forced_serial=None,
):
    if subtask_id is None:
        body = "本文のみでFootprintブロックなし"
    else:
        footprint_lines = (
            "\n".join(f"  - {f}" for f in footprint) if footprint else "  []"
        )
        depends_on_values = ", ".join(depends_on or ())
        extra_fields = ""
        if recompute_count is not None:
            extra_fields += f"recompute_count: {recompute_count}\n"
        if forced_serial is not None:
            extra_fields += f"forced_serial: {str(forced_serial).lower()}\n"
        body = (
            "## Footprint\n```yaml\n"
            f"subtask_id: {subtask_id}\n"
            "footprint:\n"
            f"{footprint_lines}\n"
            f"depends_on: [{depends_on_values}]\n"
            f"{extra_fields}"
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
        run_state = RunState(
            active_worktrees={
                "1": ActiveWorktree(
                    issue_number=1,
                    branch="claude/issue-1-task-a",
                    worktree_path=str(tmp_path),
                    pid=1,
                    started_at=1.0,
                    declared_footprint=(),
                )
            }
        )
        issue = _issue_with_footprint(1, subtask_id="task-a")
        with patch("fake_forge_proxy.active_fake_forge.list_open_prs") as mock_prs:
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

        with patch("fake_forge_proxy.active_fake_forge.list_open_prs", return_value=[]):
            result = _decide_missing_active_worktrees(run_state, [issue], config)

        assert len(result) == 1
        key, subtask_id, active = result[0]
        assert key == "101"
        assert subtask_id == "task-a"
        assert active.branch == "claude/issue-101-task-a"
        assert active.declared_footprint == ("src/foo.py",)
        # decide層はrun_stateを変更しない
        assert run_state.active_worktrees == {}

    def test_restores_recompute_count_and_forced_serial_from_issue_body(self, tmp_path):
        """#513再現テスト: run_state.json消失時、Issue本文に永続化された
        recompute_count/forced_serialから復元されるべきだが、現状は常に
        (0, False)へ初期化される（DAG再計算のリトライ上限とforced_serial
        フォールバックが、ステートレスCIランナーでは機能しない）。"""
        run_state = RunState(active_worktrees={})
        issue = _issue_with_footprint(
            101,
            subtask_id="task-a",
            footprint=["src/foo.py"],
            recompute_count=2,
            forced_serial=True,
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch("fake_forge_proxy.active_fake_forge.list_open_prs", return_value=[]):
            result = _decide_missing_active_worktrees(run_state, [issue], config)

        active = result[0][2]
        assert active.recompute_count == 2
        assert active.forced_serial is True

    def test_restores_zero_and_false_when_issue_predates_the_fields(self, tmp_path):
        """#513: 本フィールド導入前に作られたIssue（フェンスにフィールドが
        無い）は、後方互換として(0, False)にフォールバックする。"""
        run_state = RunState(active_worktrees={})
        issue = _issue_with_footprint(
            101, subtask_id="task-a", footprint=["src/foo.py"]
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch("fake_forge_proxy.active_fake_forge.list_open_prs", return_value=[]):
            result = _decide_missing_active_worktrees(run_state, [issue], config)

        active = result[0][2]
        assert active.recompute_count == 0
        assert active.forced_serial is False

    def test_treats_legacy_force_serial_label_as_authoritative(self, tmp_path):
        """#516再2巡目レビュー指摘: 本フィールド導入前からforced_serialだった
        Issue（本文にフィールドは無いがstatus:force-serialラベルは付いている）
        を、run_state.json消失後に「直列化されていない」と誤って復元しては
        ならない。ラベルは表示専用だが、本文が沈黙している場合の権威として
        扱う。"""
        run_state = RunState(active_worktrees={})
        issue = _issue_with_footprint(
            101, subtask_id="task-a", footprint=["src/foo.py"]
        )
        issue = IssueRecord(
            number=issue.number,
            title=issue.title,
            body=issue.body,
            labels=issue.labels + ("status:force-serial",),
            created_at=issue.created_at,
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch("fake_forge_proxy.active_fake_forge.list_open_prs", return_value=[]):
            result = _decide_missing_active_worktrees(run_state, [issue], config)

        assert result[0][2].forced_serial is True

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

        with patch("fake_forge_proxy.active_fake_forge.list_open_prs", return_value=[]):
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

        with patch(
            "fake_forge_proxy.active_fake_forge.list_open_prs", return_value=[pr]
        ):
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

        with patch("fake_forge_proxy.active_fake_forge.list_open_prs", return_value=[]):
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
            "fake_forge_proxy.active_fake_forge.list_open_prs",
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
            "fake_forge_proxy.active_fake_forge.list_open_prs",
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

        with patch("fake_forge_proxy.active_fake_forge.list_open_prs", return_value=[]):
            result = _decide_missing_active_worktrees(
                run_state,
                [dependent],
                config,
            )

        assert result[0][2].base_branch == "parent/issue-100"

    def test_restores_active_worktree_from_open_pr_with_empty_closes_issues_via_head_ref(
        self, tmp_path
    ):
        """#739: 非デフォルトブランチ宛てPRでcloses_issue_numbersが空でも、
        head_ref（例: codex/issue-709-guarded-repair-rollout）からPR番号とブランチを復元する。
        """
        run_state = RunState(active_worktrees={})
        issue = _issue_with_footprint(
            709, subtask_id="guarded-repair-rollout", footprint=["src/foo.py"]
        )
        pr = PrRecord(
            number=737,
            head_ref="codex/issue-709-guarded-repair-rollout",
            changed_files=("src/foo.py",),
            closes_issue_numbers=(),
            base_ref="parent/issue-700",
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch(
            "fake_forge_proxy.active_fake_forge.list_open_prs", return_value=[pr]
        ):
            result = _decide_missing_active_worktrees(run_state, [issue], config)

        assert len(result) == 1
        key, subtask_id, active = result[0]
        assert key == "709"
        assert subtask_id == "guarded-repair-rollout"
        assert active.branch == "codex/issue-709-guarded-repair-rollout"
        assert active.external_id == "737"
        assert active.external_url == "PR#737"

    def test_restores_open_dependency_pr_with_empty_closes_issues_as_base_branch(
        self, tmp_path
    ):
        """#739: 依存先PRのcloses_issue_numbersが空でもhead_refから依存PRを解決する。"""
        run_state = RunState(active_worktrees={})
        dependency = _issue_with_footprint(709, subtask_id="task-a")
        dependent = _issue_with_footprint(
            710,
            subtask_id="task-b",
            depends_on=["task-a"],
        )
        dependency_pr = PrRecord(
            number=737,
            head_ref="codex/issue-709-task-a",
            changed_files=(),
            closes_issue_numbers=(),
            base_ref="parent/issue-700",
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch(
            "fake_forge_proxy.active_fake_forge.list_open_prs",
            return_value=[dependency_pr],
        ):
            result = _decide_missing_active_worktrees(
                run_state,
                [dependency, dependent],
                config,
            )

        active_by_key = {key: active for key, _, active in result}
        assert active_by_key["710"].base_branch == "codex/issue-709-task-a"


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


class TestDecideStaleRecoveryCounters:
    """#516再2巡目レビュー指摘: `_persist_recovery_counters`がIssue本文への
    書き込みに成功した直後、サイクル終端の`save_run_state`前にプロセスが
    停止すると、run_state.json上のActiveWorktreeは古い値のまま残る。この
    エントリは`active_worktrees`に既に存在するため`_decide_missing_active_worktrees`
    の対象外——recover_run_stateは既存エントリも本文/ラベルと突き合わせて
    再照合しなければならない。
    """

    def test_reconciles_forced_serial_from_body_into_stale_existing_entry(
        self, tmp_path
    ):
        active = ActiveWorktree(
            issue_number=101,
            branch="claude/issue-101-task-a",
            worktree_path="worktrees/w1",
            pid=None,
            started_at=1_699_999_000.0,
            declared_footprint=("src/foo.py",),
            recompute_count=2,
            forced_serial=False,  # run_state側は古いまま
        )
        run_state = RunState(active_worktrees={"101": active})
        issue = _issue_with_footprint(
            101,
            subtask_id="task-a",
            footprint=["src/foo.py"],
            recompute_count=2,
            forced_serial=True,  # 本文側は既に更新済み
        )

        reconciliations = _decide_stale_recovery_counters(run_state, [issue])

        assert reconciliations == [("101", 2, True)]

    def test_never_rolls_back_recompute_count_when_body_lags_behind(self, tmp_path):
        """本文の書き込みがまだ追いついていないだけの一時的なラグで、
        run_state側の進捗（より大きいrecompute_count）を巻き戻してはならない。"""
        active = ActiveWorktree(
            issue_number=101,
            branch="claude/issue-101-task-a",
            worktree_path="worktrees/w1",
            pid=None,
            started_at=1_699_999_000.0,
            declared_footprint=("src/foo.py",),
            recompute_count=2,
            forced_serial=False,
        )
        run_state = RunState(active_worktrees={"101": active})
        issue = _issue_with_footprint(
            101,
            subtask_id="task-a",
            footprint=["src/foo.py"],
            recompute_count=1,  # 本文はまだ1（rebase処理途中）
            forced_serial=False,
        )

        reconciliations = _decide_stale_recovery_counters(run_state, [issue])

        assert reconciliations == []

    def test_no_reconciliation_when_values_already_match(self, tmp_path):
        active = ActiveWorktree(
            issue_number=101,
            branch="claude/issue-101-task-a",
            worktree_path="worktrees/w1",
            pid=None,
            started_at=1_699_999_000.0,
            declared_footprint=("src/foo.py",),
            recompute_count=1,
            forced_serial=True,
        )
        run_state = RunState(active_worktrees={"101": active})
        issue = _issue_with_footprint(
            101,
            subtask_id="task-a",
            footprint=["src/foo.py"],
            recompute_count=1,
            forced_serial=True,
        )

        assert _decide_stale_recovery_counters(run_state, [issue]) == []

    def test_skips_entries_with_no_corresponding_in_progress_issue(self, tmp_path):
        """対応するIssueがin_progress_issuesに無い（クローズ済み等）場合は
        スキップする（推測で書き換えない）。"""
        active = ActiveWorktree(
            issue_number=101,
            branch="claude/issue-101-task-a",
            worktree_path="worktrees/w1",
            pid=None,
            started_at=1_699_999_000.0,
            declared_footprint=("src/foo.py",),
            recompute_count=0,
            forced_serial=False,
        )
        run_state = RunState(active_worktrees={"101": active})

        assert _decide_stale_recovery_counters(run_state, []) == []


class TestRecoverRunStateReconcilesStaleEntries:
    """decide+applyの統合入口（recover_run_state）が既存エントリも
    実際に更新することを確認する。"""

    def test_updates_existing_active_worktree_in_place(self, tmp_path):
        active = ActiveWorktree(
            issue_number=101,
            branch="claude/issue-101-task-a",
            worktree_path="worktrees/w1",
            pid=None,
            started_at=1_699_999_000.0,
            declared_footprint=("src/foo.py",),
            recompute_count=2,
            forced_serial=False,
        )
        run_state = RunState(active_worktrees={"101": active})
        issue = _issue_with_footprint(
            101,
            subtask_id="task-a",
            footprint=["src/foo.py"],
            recompute_count=2,
            forced_serial=True,
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch("fake_forge_proxy.active_fake_forge.list_open_prs", return_value=[]):
            modified = recover_run_state(run_state, [issue], config)

        assert modified is True
        assert run_state.active_worktrees["101"].forced_serial is True

from unittest.mock import patch

from orchestune.consistency.invariants.execution import RUN_STATE_MISSING
from orchestune.consistency.models import (
    ConsistencyScope,
    RepairCommand,
    RepairStatus,
)
from orchestune.consistency.repairs.execution import COMMAND_BOOKKEEPING
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.recovery import (
    RecoveryBookkeepingSnapshot,
    _counter_targets,
    _extract_raw_subtask_id,
    _parse_subtask_info_from_issue,
    _resolve_recovery_pr_and_branch,
    _restoration_candidates,
    execute_bookkeeping_repair_command,
)
from orchestune.dispatch.state import ActiveWorktree, RunState
from orchestune.models import IssueRecord, PrRecord


def _snapshot(*restorations):
    return RecoveryBookkeepingSnapshot(
        tasks_by_issue={},
        open_prs=(),
        restorations=restorations,
        counter_targets=(),
        launch_history=(),
    )


def _bookkeeping_command(subject_id: str) -> RepairCommand:
    return RepairCommand(
        code=COMMAND_BOOKKEEPING,
        scope=ConsistencyScope.TASK,
        subject_id=subject_id,
        idempotency_key=f"execution:{subject_id}:bookkeeping",
        parameters=(("finding_codes", (RUN_STATE_MISSING,)),),
    )


def _project_restoration_candidates(
    _run_state: RunState,
    issues: list[IssueRecord],
    config: DispatcherConfig,
):
    """Project the adapter's authoritative Forge snapshot for focused tests."""
    return list(
        _restoration_candidates(
            issues,
            config.resolved_forge.list_open_prs(),
            config,
        )
    )


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


class TestRestorationCandidateProjection:
    """Supervisor adapter が観測する復元候補の projection を検証する。"""

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
            result = _project_restoration_candidates(run_state, [issue], config)

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
            result = _project_restoration_candidates(run_state, [issue], config)

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
            result = _project_restoration_candidates(run_state, [issue], config)

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
            result = _project_restoration_candidates(run_state, [issue], config)

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
            result = _project_restoration_candidates(run_state, [issue], config)

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
            is_cross_repository=False,
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch(
            "fake_forge_proxy.active_fake_forge.list_open_prs", return_value=[pr]
        ):
            result = _project_restoration_candidates(run_state, [issue], config)

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
            result = _project_restoration_candidates(
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
            result = _project_restoration_candidates(
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
            result = _project_restoration_candidates(
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
            result = _project_restoration_candidates(
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
            is_cross_repository=False,
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch(
            "fake_forge_proxy.active_fake_forge.list_open_prs", return_value=[pr]
        ):
            result = _project_restoration_candidates(run_state, [issue], config)

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
            result = _project_restoration_candidates(
                run_state,
                [dependency, dependent],
                config,
            )

        active_by_key = {key: active for key, _, active in result}
        assert active_by_key["710"].base_branch == "codex/issue-709-task-a"


class TestBookkeepingRepairCommand:
    """Typed handler applies only its observed command subject."""

    def test_typed_bookkeeping_command_applies_only_its_observed_subject(
        self, tmp_path, fake_forge
    ):
        run_state = RunState(active_worktrees={})
        active = ActiveWorktree(
            issue_number=101,
            branch="claude/issue-101-task-a",
            worktree_path="worktrees/claude-issue-101-task-a",
            pid=None,
            started_at=0.0,
            declared_footprint=("src/foo.py",),
            external_id="50",
        )
        command = _bookkeeping_command("101")
        config = DispatcherConfig(
            apply=True,
            run_state_path=tmp_path / "run_state.json",
            events_log_path=tmp_path / "events.jsonl",
            worktree_root=tmp_path / "worktrees",
            forge=fake_forge,
        )
        fake_forge.get_issue_labels.return_value = ("status:in-progress",)

        result = execute_bookkeeping_repair_command(
            command,
            run_state,
            _snapshot(("101", "task-a", active)),
            config,
        )

        assert result.status is RepairStatus.APPLIED
        assert run_state.active_worktrees == {"101": active}

    def test_typed_bookkeeping_command_defers_when_subject_is_not_observed(
        self, tmp_path, fake_forge
    ):
        run_state = RunState(active_worktrees={})
        command = _bookkeeping_command("102")
        config = DispatcherConfig(
            apply=True,
            run_state_path=tmp_path / "run_state.json",
            events_log_path=tmp_path / "events.jsonl",
            worktree_root=tmp_path / "worktrees",
            forge=fake_forge,
        )

        result = execute_bookkeeping_repair_command(
            command, run_state, _snapshot(), config
        )

        assert result.status is RepairStatus.SKIPPED
        assert result.diagnostics == ("missing-entry precondition no longer holds",)
        assert run_state.active_worktrees == {}

    def test_typed_bookkeeping_command_does_not_overwrite_an_occupied_key(
        self, tmp_path, fake_forge
    ):
        occupied = ActiveWorktree(
            issue_number=999,
            branch="codex/issue-999",
            worktree_path="worktrees/issue-999",
            pid=999,
            started_at=1.0,
            declared_footprint=(),
        )
        restoration = ActiveWorktree(
            issue_number=101,
            branch="codex/issue-101",
            worktree_path="worktrees/issue-101",
            pid=None,
            started_at=None,
            declared_footprint=(),
            external_id="51",
        )
        run_state = RunState(active_worktrees={"101": occupied})
        command = _bookkeeping_command("101")
        config = DispatcherConfig(
            apply=True,
            run_state_path=tmp_path / "run_state.json",
            events_log_path=tmp_path / "events.jsonl",
            worktree_root=tmp_path / "worktrees",
            forge=fake_forge,
        )
        fake_forge.get_issue_labels.return_value = ("status:in-progress",)

        result = execute_bookkeeping_repair_command(
            command,
            run_state,
            _snapshot(("101", "task-101", restoration)),
            config,
        )

        assert result.status is RepairStatus.SKIPPED
        assert run_state.active_worktrees == {"101": occupied}


class TestRecoveryCounterTargets:
    """#516再2巡目レビュー指摘: `_persist_recovery_counters`がIssue本文への
    書き込みに成功した直後、サイクル終端の`save_run_state`前にプロセスが
    停止すると、run_state.json上のActiveWorktreeは古い値のまま残る。
    typed bookkeeping の desired state は既存entryも本文/ラベルと突き合わせ、
    常に単調なtargetを導出しなければならない。
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

        targets = _counter_targets(run_state, [issue])

        assert targets == (("101", 2, True),)

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

        targets = _counter_targets(run_state, [issue])

        assert targets == (("101", 2, False),)

    def test_target_matches_current_values_when_already_consistent(self, tmp_path):
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

        assert _counter_targets(run_state, [issue]) == (("101", 1, True),)

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

        assert _counter_targets(run_state, []) == ()


def _issue(number, created_at="2026-01-01T00:00:00+00:00"):
    return IssueRecord(
        number=number, title="", body="", labels=(), created_at=created_at
    )


def _pr(head_ref, number=1, is_cross_repository=False):
    return PrRecord(
        number=number,
        head_ref=head_ref,
        changed_files=(),
        is_cross_repository=is_cross_repository,
    )


class TestResolveRecoveryPrAndBranch:
    """#777: 復旧時のブランチ解決は①正規ブランチ名の完全一致を②厳密PR一致
    より優先する（従来は逆順で、緩い`pr_matches_issue`を先に使っていた）。"""

    def test_canonical_branch_pr_wins_even_with_other_loosely_matching_prs(self):
        """②（緩いissue番号一致のみのPR）より①（正規ブランチ名との完全一致）
        を優先すること。"""
        issue = _issue(101)
        canonical_pr = _pr("claude/issue-101-task-a", number=1)
        loose_match_pr = _pr("some-other-branch-mentioning-101", number=2)

        branch, external_id, external_url = _resolve_recovery_pr_and_branch(
            issue, "task-a", [loose_match_pr, canonical_pr]
        )

        assert branch == "claude/issue-101-task-a"
        assert external_id == "1"
        assert external_url == "PR#1"

    def test_falls_back_to_strict_single_pr_match_for_non_canonical_prefix(self):
        """①が存在しない場合、厳密一致する②（issue番号・subtask_id双方一致）
        のPR head_refへフォールバックする（Codex/agy/人間の別prefixブランチ
        も認識する）。"""
        issue = _issue(202)
        pr = _pr("codex/issue-202-task-b", number=5)

        branch, external_id, external_url = _resolve_recovery_pr_and_branch(
            issue, "task-b", [pr]
        )

        assert branch == "codex/issue-202-task-b"
        assert external_id == "5"
        assert external_url == "PR#5"

    def test_ambiguous_multiple_distinct_branches_falls_back_to_canonical_guess(self):
        """②で複数の異なるブランチが厳密一致する場合はtie-breakせず、
        PRに紐付けない（fail-closed）。ブランチ名自体は正規名を返す
        （復旧時のstate再構築は非破壊的な最善推測であり、mergeやdeleteの
        対象選択には使われない）。"""
        issue = _issue(303)
        pr_a = _pr("codex/issue-303-task-c", number=1)
        pr_b = _pr("feat/issue-303-task-c", number=2)

        branch, external_id, external_url = _resolve_recovery_pr_and_branch(
            issue, "task-c", [pr_a, pr_b]
        )

        assert branch == "claude/issue-303-task-c"
        assert external_id is None
        assert external_url is None

    def test_no_match_returns_canonical_guess_without_pr_link(self):
        issue = _issue(404)

        branch, external_id, external_url = _resolve_recovery_pr_and_branch(
            issue, "task-d", []
        )

        assert branch == "claude/issue-404-task-d"
        assert external_id is None
        assert external_url is None

    def test_loose_closes_reference_alone_is_not_a_strict_match(self):
        """本文に`Closes #N`とだけ書かれた無関係PRは、subtask_idを含まない
        ため②のstrict matcherには一致しない。"""
        issue = _issue(505)
        unrelated_pr = PrRecord(
            number=9,
            head_ref="unrelated-branch-name",
            changed_files=(),
            closes_issue_numbers=(505,),
        )

        branch, external_id, external_url = _resolve_recovery_pr_and_branch(
            issue, "task-e", [unrelated_pr]
        )

        assert branch == "claude/issue-505-task-e"
        assert external_id is None
        assert external_url is None

    def test_fork_pr_is_excluded_from_strict_match_fallback(self):
        """PR#780 Codexレビュー: forkのhead_refは信頼できないため②の対象から
        除外し、fail-closedにする（無関係なupstreamブランチの誤fetch/merge、
        または正当なforkの貢献の誤却下を防ぐ）。"""
        issue = _issue(606)
        fork_pr = _pr("codex/issue-606-task-f", number=11, is_cross_repository=True)

        branch, external_id, external_url = _resolve_recovery_pr_and_branch(
            issue, "task-f", [fork_pr]
        )

        assert branch == "claude/issue-606-task-f"
        assert external_id is None
        assert external_url is None

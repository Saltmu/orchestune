from unittest.mock import patch

import pytest

from orchestune.consistency.invariants.execution import RUN_STATE_MISSING
from orchestune.consistency.models import (
    ConsistencyScope,
    RepairCommand,
    RepairStatus,
)
from orchestune.consistency.repairs.execution import COMMAND_BOOKKEEPING
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.recovery.service import (
    RecoveryBookkeepingSnapshot,
    _counter_targets,
    _include_queued_attempts,
    _parse_subtask_info_from_issue,
    execute_bookkeeping_repair_command,
)
from orchestune.ledger.run_state import RunState
from orchestune.models import IssueRecord
from tests.dispatch_recovery_test_support import _issue_with_footprint
from tests.dispatch_test_support import make_test_active_worktree


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


def _journal_body(parent_issue_number: int) -> str:
    return (
        "<!-- orchestune:launch-attempt -->\n"
        "```json\n"
        '{"attempt_id":"attempt-1","phase":"prepared","target":"local",'
        '"branch":"codex/issue-1-task","base_branch":"parent/issue-100",'
        '"started_at":1}\n'
        "```\n"
        "## Footprint\n"
        "```yaml\n"
        "subtask_id: task\n"
        f"parent_issue_number: {parent_issue_number}\n"
        "footprint: []\n"
        "depends_on: []\n"
        "```\n"
    )


class TestIncludeQueuedAttempts:
    def test_includes_journal_with_parent_from_body_metadata(
        self, tmp_path, fake_forge
    ):
        in_progress = IssueRecord(
            number=1,
            title="in progress",
            body="",
            labels=("status:in-progress",),
            created_at="2026-01-01T00:00:00Z",
        )
        body_parent = IssueRecord(
            number=2,
            title="queued",
            body=_journal_body(100),
            labels=("status:queued",),
            created_at="2026-01-01T00:00:00Z",
        )
        other_parent = IssueRecord(
            number=3,
            title="other queued",
            body=_journal_body(200),
            labels=("status:queued",),
            created_at="2026-01-01T00:00:00Z",
        )
        fake_forge.list_issues_by_label.return_value = [body_parent, other_parent]
        config = DispatcherConfig(
            parent_issue_number=100,
            forge=fake_forge,
            events_log_path=tmp_path / "events.jsonl",
        )

        included = _include_queued_attempts([in_progress], config)

        assert [issue.number for issue in included] == [1, 2]


class TestSubtaskIdExtractionAndFallback:
    """Issue Footprint からの subtask_id 抽出および空白・欠落フォールバックの検証。"""

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ('## Footprint\n```yaml\nsubtask_id: "task-a"\n```\n', "task-a"),
            ("本文のみでFootprintブロックなし", "issue-1"),
            ("## Footprint\n```yaml\nfootprint: []\n```\n", "issue-1"),
            ('## Footprint\n```yaml\nsubtask_id: " task-a "\n```\n', "task-a"),
            ('## Footprint\n```yaml\nsubtask_id: "   "\n```\n', "issue-1"),
            ("## Footprint\n```yaml\nsubtask_id: 123\n```\n", "123"),
            ("## Footprint\n```yaml\n: invalid yaml\n```\n", "issue-1"),
        ],
    )
    def test_subtask_id_resolution(self, body: str, expected: str) -> None:
        issue = IssueRecord(1, "t", body, (), "2026-01-01T00:00:00+00:00")
        subtask_id, _ = _parse_subtask_info_from_issue(issue)
        assert subtask_id == expected


class TestParseSubtaskInfoFromIssue:
    @pytest.mark.parametrize(
        ("subtask_id", "footprint_in", "expected_id", "expected_fp"),
        [
            (None, None, "issue-42", ()),
            ("task-a", ["src/foo.py"], "task-a", ("src/foo.py",)),
            ("task-a", ["./src/foo.py", "src/foo.py"], "task-a", ("src/foo.py",)),
            ("task-a", ["../outside.py"], "task-a", ()),
        ],
    )
    def test_parse_subtask_info(
        self, subtask_id, footprint_in, expected_id, expected_fp
    ):
        num = 42 if subtask_id is None else 1
        kw = {"subtask_id": subtask_id}
        if footprint_in is not None:
            kw["footprint"] = footprint_in
        sub_id, fp = _parse_subtask_info_from_issue(_issue_with_footprint(num, **kw))
        assert (sub_id, fp) == (expected_id, expected_fp)


class TestBookkeepingRepairCommand:
    """Typed handler applies only its observed command subject."""

    def test_typed_bookkeeping_command_applies_only_its_observed_subject(
        self, tmp_path, fake_forge
    ):
        run_state = RunState(active_worktrees={})
        active = make_test_active_worktree(
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
            parent_issue_number=100,
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
            parent_issue_number=100,
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
        occupied = make_test_active_worktree(
            issue_number=999,
            branch="codex/issue-999",
            worktree_path="worktrees/issue-999",
            pid=4_200_999,
            started_at=1.0,
            declared_footprint=(),
        )
        restoration = make_test_active_worktree(
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
            parent_issue_number=100,
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
        active = make_test_active_worktree(
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
        active = make_test_active_worktree(
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
        active = make_test_active_worktree(
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
        active = make_test_active_worktree(
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


def test_operator_release_blocks_standard_restoration(tmp_path, fake_forge):
    active = make_test_active_worktree(
        issue_number=101,
        branch="codex/issue-101",
        worktree_path="worktrees/issue-101",
        pid=None,
        started_at=None,
        declared_footprint=(),
        external_id="51",
        claim_id="released-claim",
    )
    state = RunState(
        recovery_receipts={
            "release": {
                "operation": "release",
                "issue_number": 101,
                "claim_id": active.claim.claim_id,
            }
        }
    )
    config = DispatcherConfig(
        parent_issue_number=100,
        apply=True,
        forge=fake_forge,
        run_state_path=tmp_path / "run_state.json",
        events_log_path=tmp_path / "events.jsonl",
        worktree_root=tmp_path / "worktrees",
    )
    result = execute_bookkeeping_repair_command(
        _bookkeeping_command("101"),
        state,
        _snapshot(("101", "task-101", active)),
        config,
    )
    assert result.status is RepairStatus.SKIPPED
    assert not state.active_worktrees
    fake_forge.get_issue_labels.assert_not_called()


class TestRecoveryCounterBookkeepingExecution:
    """Counter bookkeeping applies updates via launch owner API and rolls back on failure."""

    def test_counter_bookkeeping_updates_via_launch_owner_api(
        self, tmp_path, fake_forge
    ):
        from orchestune.dispatch.recovery.service import RECOVERY_COUNTERS_STALE
        from tests.dispatch_test_support import make_test_active_worktree

        active = make_test_active_worktree(
            issue_number=101,
            recompute_count=1,
            forced_serial=False,
        )
        run_state = RunState(active_worktrees={"101": active})
        snapshot = RecoveryBookkeepingSnapshot(
            tasks_by_issue={},
            open_prs=(),
            restorations=(),
            counter_targets=(("101", 2, True),),
            launch_history=(),
        )
        command = RepairCommand(
            code=COMMAND_BOOKKEEPING,
            scope=ConsistencyScope.TASK,
            subject_id="101",
            idempotency_key="execution:101:bookkeeping",
            parameters=(("finding_codes", (RECOVERY_COUNTERS_STALE,)),),
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            apply=True,
            run_state_path=tmp_path / "run_state.json",
            events_log_path=tmp_path / "events.jsonl",
            worktree_root=tmp_path / "worktrees",
            forge=fake_forge,
        )

        result = execute_bookkeeping_repair_command(
            command, run_state, snapshot, config
        )

        assert result.status is RepairStatus.APPLIED
        updated = run_state.active_worktrees["101"]
        # Updated record has new counter values
        assert updated.launch.recompute_count == 2
        assert updated.launch.forced_serial is True
        # Original instance was not mutated in place
        assert active.launch.recompute_count == 1
        assert active.launch.forced_serial is False

    def test_counter_bookkeeping_rolls_back_on_persist_failure(
        self, tmp_path, fake_forge
    ):
        import pytest

        from orchestune.dispatch.recovery.service import RECOVERY_COUNTERS_STALE
        from tests.dispatch_test_support import make_test_active_worktree

        active = make_test_active_worktree(
            issue_number=101,
            recompute_count=1,
            forced_serial=False,
        )
        run_state = RunState(active_worktrees={"101": active})
        snapshot = RecoveryBookkeepingSnapshot(
            tasks_by_issue={},
            open_prs=(),
            restorations=(),
            counter_targets=(("101", 3, True),),
            launch_history=(),
        )
        command = RepairCommand(
            code=COMMAND_BOOKKEEPING,
            scope=ConsistencyScope.TASK,
            subject_id="101",
            idempotency_key="execution:101:bookkeeping",
            parameters=(("finding_codes", (RECOVERY_COUNTERS_STALE,)),),
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            apply=True,
            run_state_path=tmp_path / "run_state.json",
            events_log_path=tmp_path / "events.jsonl",
            worktree_root=tmp_path / "worktrees",
            forge=fake_forge,
        )

        with patch(
            "orchestune.dispatch.recovery.service._persist_recovery_snapshot",
            side_effect=OSError("disk full"),
        ):
            with pytest.raises(IOError, match="disk full"):
                execute_bookkeeping_repair_command(command, run_state, snapshot, config)

        # After rollback, run_state still holds the original active record
        assert run_state.active_worktrees["101"] is active
        assert run_state.active_worktrees["101"].launch.recompute_count == 1
        assert run_state.active_worktrees["101"].launch.forced_serial is False


class TestYamlParseDeduplication:
    """cycle_context and recoveryにおけるYAMLパースの重複削減とCSafeLoader使用を検証する。"""

    @staticmethod
    def _install_yaml_counters(monkeypatch):
        import yaml

        from orchestune import issue_parsing
        from orchestune.dispatch.recovery import restoration as recovery_restoration

        counts = {"csafe": 0, "py": 0}
        orig_csafe = issue_parsing._yaml_safe_load
        orig_py = yaml.safe_load

        def count_csafe(stream):
            counts["csafe"] += 1
            return orig_csafe(stream)

        def count_py(stream):
            counts["py"] += 1
            return orig_py(stream)

        monkeypatch.setattr(issue_parsing, "_yaml_safe_load", count_csafe)
        monkeypatch.setattr(recovery_restoration, "_yaml_safe_load", count_csafe)
        monkeypatch.setattr(yaml, "safe_load", count_py)
        return counts

    def test_build_task_mappings_parses_yaml_once_per_issue(self, monkeypatch):
        from orchestune.dispatch.cycle.context import _build_task_mappings

        counts = self._install_yaml_counters(monkeypatch)
        issues = [
            _issue_with_footprint(1, subtask_id="task-1", footprint=["a.py"]),
            _issue_with_footprint(2, subtask_id="task-2", footprint=["b.py"]),
        ]
        tasks, _, _ = _build_task_mappings(issues)
        assert len(tasks) == 2 and counts["csafe"] == 2 and counts["py"] == 0

    def test_refresh_snapshot_parses_yaml_at_most_twice_per_issue(
        self, monkeypatch, tmp_path, fake_forge
    ):
        from orchestune.dispatch.recovery.service import RecoveryBookkeepingAdapter

        counts = self._install_yaml_counters(monkeypatch)
        fake_forge.list_issues_by_label.return_value = [
            _issue_with_footprint(101, subtask_id="task-1", footprint=["a.py"]),
            _issue_with_footprint(102, subtask_id="task-2", footprint=["b.py"]),
        ]
        fake_forge.list_open_prs.return_value = []
        fake_forge.get_issue.return_value = _issue_with_footprint(
            100, subtask_id="parent"
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            worktree_root=str(tmp_path / "worktrees"),
            forge=fake_forge,
        )
        adapter = RecoveryBookkeepingAdapter("repo", RunState(), config, now=0.0)
        snapshot = adapter._refresh_snapshot()
        assert (
            len(snapshot.tasks_by_issue) == 2
            and counts["csafe"] <= 4
            and counts["py"] == 0
        )

    def test_parse_claim_info_preserves_counters_with_malformed_metadata(self):
        from orchestune.dispatch.recovery.restoration import (
            _parse_claim_info_from_issue,
        )

        body = (
            "## Summary\n\n<!-- orchestune:subtask-footprint -->\n"
            "```yaml\nsubtask_id: task-1\nowner_kind: [invalid]\n"
            "reservation_kind: {bad: dict}\nrecompute_count: 3\nforced_serial: true\n```\n"
        )
        info = _parse_claim_info_from_issue(
            IssueRecord(42, "T", body, (), "2026-01-01T00:00:00+00:00")
        )
        assert (
            info.owner_kind,
            info.reservation_kind,
            info.recompute_count,
            info.forced_serial,
        ) == ("dispatch", "footprint", 3, True)

"""ステータス集計ドメインロジック（status_snapshot.py）のテスト。

`tests/test_monitor.py`から、CLI配線（引数解析・`--watch`ループ）とは独立した
ドメインロジック本体のテストを、`status_snapshot`モジュールの新設（アーキ
テクチャ層L2への切り出し）に合わせて分離している。`main()`のCLIレベルの
テストは`test_monitor_cli.py`に残す。
"""

import os

import pytest

import orchestune.status_snapshot as status_snapshot_module
from orchestune.infra.cli_guidance import render_guidance
from orchestune.ledger.run_state import RunState
from orchestune.operator_guidance import EvidenceState, NextOwner, ProgressOutlook
from orchestune.status_snapshot import (
    MonitorState,
    StatusSnapshot,
    WorktreeStatus,
    _derive_monitor_state,
    _extract_subtask_id,
    _fetch_labels_cached,
    _format_duration,
    _read_log_tail,
    build_status_snapshot,
    compact_status_attention,
    format_parent_integration,
    format_status_report,
    guidance_from_status,
)
from tests.dispatch_test_support import make_test_active_worktree
from tests.dispatch_test_support import save_locked_run_state as save_run_state


@pytest.fixture(autouse=True)
def _stub_get_issue_labels(fake_forge):
    """既定ではstatus:in-progressを返し、PID/external_idベースの分類テストが
    従来通り動作するようにする。優先順位そのもののテストは個別にオーバーライドする。"""
    fake_forge.get_issue_labels.return_value = ("status:in-progress",)
    yield fake_forge


class _FakeForgeTest:
    @pytest.fixture(autouse=True)
    def _inject_forge(self, _stub_get_issue_labels):
        self.forge = _stub_get_issue_labels


def _active(**overrides):
    defaults = dict(
        issue_number=133,
        branch="claude/issue-133-monitor-cli",
        worktree_path="worktrees/w1",
        pid=os.getpid(),
        started_at=1_700_000_000.0,
        declared_footprint=("orchestune/monitor.py",),
    )
    defaults.update(overrides)
    return make_test_active_worktree(**defaults)


class TestExtractSubtaskId:
    def test_extracts_subtask_id_from_branch(self):
        assert _extract_subtask_id("claude/issue-133-monitor-cli", 133) == "monitor-cli"

    def test_returns_none_when_pattern_does_not_match(self):
        assert _extract_subtask_id("some-other-branch", 133) is None


class TestReadLogTail:
    def test_returns_last_n_lines(self, tmp_path):
        log_path = tmp_path / "task.log"
        log_path.write_text("\n".join(f"line{i}" for i in range(10)) + "\n")
        assert _read_log_tail(log_path, 3) == ["line7", "line8", "line9"]

    def test_returns_empty_list_when_file_missing(self, tmp_path):
        assert _read_log_tail(tmp_path / "missing.log", 3) == []

    def test_returns_empty_list_when_file_empty(self, tmp_path):
        log_path = tmp_path / "empty.log"
        log_path.write_text("")
        assert _read_log_tail(log_path, 3) == []

    def test_zero_n_lines_returns_empty_list(self, tmp_path):
        log_path = tmp_path / "task.log"
        log_path.write_text("line0\nline1\nline2\n")
        assert _read_log_tail(log_path, 0) == []

    def test_negative_n_lines_returns_empty_list(self, tmp_path):
        log_path = tmp_path / "task.log"
        log_path.write_text("line0\nline1\nline2\n")
        assert _read_log_tail(log_path, -1) == []

    def test_correct_across_small_chunk_boundaries_with_multibyte_chars(
        self, tmp_path, monkeypatch
    ):
        # #134レビュー: 末尾を毎回ファイル全体読み込みではなく、末尾からチャンク単位
        # で逆向きに読む実装に変更したため、チャンク境界をまたぐケース（マルチバイト
        # 文字を含む）でも正しく末尾行を取得できることを確認する。
        monkeypatch.setattr(status_snapshot_module, "_TAIL_CHUNK_SIZE", 8)
        log_path = tmp_path / "task.log"
        log_path.write_text(
            "\n".join(f"行{i}" for i in range(20)) + "\n", encoding="utf-8"
        )
        assert _read_log_tail(log_path, 3) == ["行17", "行18", "行19"]

    def test_does_not_read_whole_file_when_tail_is_small(self, tmp_path, monkeypatch):
        # 長時間セッションの巨大ログでも、末尾数行を得るためにファイル全体を
        # read()しないことを、実際に呼ばれるreadサイズの合計で検証する。
        log_path = tmp_path / "task.log"
        log_path.write_text("\n".join(f"line{i}" for i in range(5000)) + "\n")
        file_size = log_path.stat().st_size

        real_open = open
        read_sizes: list[int] = []

        def _tracking_open(path, mode="r", *args, **kwargs):
            fh = real_open(path, mode, *args, **kwargs)
            if "b" in mode:
                original_read = fh.read

                def _tracked_read(size=-1):
                    read_sizes.append(size if size is not None else -1)
                    return original_read(size)

                fh.read = _tracked_read
            return fh

        monkeypatch.setattr("builtins.open", _tracking_open)
        result = _read_log_tail(log_path, 3)

        assert result == ["line4997", "line4998", "line4999"]
        assert sum(read_sizes) < file_size


class TestFormatDuration:
    def test_formats_minutes_and_seconds(self):
        assert _format_duration(754) == "12m34s"

    def test_formats_hours_minutes_seconds(self):
        assert _format_duration(3661) == "1h01m01s"

    def test_formats_seconds_only(self):
        assert _format_duration(9) == "9s"


class TestDeriveMonitorState:
    """#137: GitHubラベルを正とした状態判定の優先順位ロジック（decide、副作用なし）。"""

    @pytest.mark.parametrize(
        "labels,expected",
        [
            (("status:done",), MonitorState.DONE),
            (("status:blocked-human-review",), MonitorState.BLOCKED_HUMAN_REVIEW),
            (("status:not-needed",), MonitorState.NOT_NEEDED),
            (("status:manual-merge-required",), MonitorState.MANUAL_MERGE_REQUIRED),
            (("status:blocked",), MonitorState.BLOCKED),
            (("status:external-lock",), MonitorState.EXTERNAL_LOCK),
            (("status:queued",), MonitorState.QUEUED),
        ],
    )
    def test_label_maps_to_expected_state_regardless_of_pid(self, labels, expected):
        # run_stateのエントリはpidが生きていても死んでいても、GitHubラベルが
        # 上位の状態を示していればそちらを優先する（staleな帳簿エントリ）。
        assert _derive_monitor_state(labels, alive=True, external_id=None) == expected
        assert _derive_monitor_state(labels, alive=False, external_id=None) == expected

    def test_priority_done_wins_over_other_labels(self):
        labels = ("status:done", "status:queued", "priority:high")
        assert _derive_monitor_state(labels, alive=False, external_id=None) == (
            MonitorState.DONE
        )

    def test_in_progress_alive_is_running(self):
        labels = ("status:in-progress",)
        assert _derive_monitor_state(labels, alive=True, external_id=None) == (
            MonitorState.RUNNING
        )

    def test_in_progress_with_external_id_is_external(self):
        labels = ("status:in-progress",)
        assert _derive_monitor_state(labels, alive=False, external_id="sess_abc") == (
            MonitorState.EXTERNAL
        )

    def test_in_progress_dead_pid_no_external_id_is_process_exited(self):
        labels = ("status:in-progress",)
        assert _derive_monitor_state(labels, alive=False, external_id=None) == (
            MonitorState.PROCESS_EXITED
        )

    def test_unknown_label_combination_falls_back_to_unknown(self):
        assert _derive_monitor_state((), alive=True, external_id=None) == (
            MonitorState.UNKNOWN
        )

    def test_labels_none_falls_back_to_pid_based_running(self):
        assert _derive_monitor_state(None, alive=True, external_id=None) == (
            MonitorState.RUNNING
        )

    def test_labels_none_falls_back_to_pid_based_process_exited(self):
        assert _derive_monitor_state(None, alive=False, external_id=None) == (
            MonitorState.PROCESS_EXITED
        )

    def test_labels_none_alive_none_falls_back_to_external(self):
        assert _derive_monitor_state(None, alive=None, external_id="sess_abc") == (
            MonitorState.EXTERNAL
        )


class TestFetchLabelsCached(_FakeForgeTest):
    def test_fetches_and_caches_within_ttl(self):
        cache: dict[int, tuple[float, tuple[str, ...]]] = {}
        self.forge.get_issue_labels.return_value = ("status:queued",)

        first = _fetch_labels_cached(133, cache, now=1000.0, ttl=15.0, forge=self.forge)
        second = _fetch_labels_cached(
            133, cache, now=1005.0, ttl=15.0, forge=self.forge
        )

        assert first == ("status:queued",)
        assert second == ("status:queued",)
        self.forge.get_issue_labels.assert_called_once_with(133)

    def test_refetches_after_ttl_expires(self):
        cache: dict[int, tuple[float, tuple[str, ...]]] = {}
        self.forge.get_issue_labels.return_value = ("status:queued",)

        _fetch_labels_cached(133, cache, now=1000.0, ttl=15.0, forge=self.forge)
        _fetch_labels_cached(133, cache, now=1020.0, ttl=15.0, forge=self.forge)

        assert self.forge.get_issue_labels.call_count == 2

    def test_fetch_failure_without_cache_returns_none(self):
        cache: dict[int, tuple[float, tuple[str, ...]]] = {}
        self.forge.get_issue_labels.side_effect = RuntimeError("gh unavailable")

        result = _fetch_labels_cached(
            133, cache, now=1000.0, ttl=15.0, forge=self.forge
        )
        assert result is None

    def test_fetch_failure_with_stale_cache_returns_stale_value(self):
        cache: dict[int, tuple[float, tuple[str, ...]]] = {
            133: (900.0, ("status:queued",))
        }
        self.forge.get_issue_labels.side_effect = RuntimeError("gh unavailable")

        result = _fetch_labels_cached(
            133, cache, now=1000.0, ttl=15.0, forge=self.forge
        )
        assert result == ("status:queued",)


class TestFetchLabelsCachedWithFakeForge(_FakeForgeTest):
    """#294: `mock.patch`によるグローバルなクラスメソッド差し替えではなく、
    `forge`引数への注入だけでテストが書けることを示す。"""

    def test_uses_injected_fake_forge_instead_of_patching(self):
        cache: dict[int, tuple[float, tuple[str, ...]]] = {}

        result = _fetch_labels_cached(
            133, cache, now=1000.0, ttl=15.0, forge=self.forge
        )

        assert result == ("status:in-progress",)
        self.forge.get_issue_labels.assert_called_once_with(133)


class TestBuildStatusSnapshot(_FakeForgeTest):
    def build_status_snapshot(self, *args, **kwargs):
        return build_status_snapshot(*args, **kwargs, forge=self.forge)

    def test_empty_when_no_run_state_file(self, tmp_path):
        snapshot = self.build_status_snapshot(
            tmp_path / "run_state.json", tmp_path / "logs", now=1_700_000_100.0
        )
        assert snapshot.worktrees == []

    def test_builds_entries_sorted_by_issue_number(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        state = RunState(
            active_worktrees={
                "200": _active(issue_number=200, branch="claude/issue-200-b"),
                "133": _active(issue_number=133, branch="claude/issue-133-monitor-cli"),
            }
        )
        save_run_state(state, run_state_path)

        snapshot = self.build_status_snapshot(
            run_state_path, tmp_path / "logs", now=1_700_000_100.0
        )

        assert [s.issue_number for s in snapshot.worktrees] == [133, 200]

    def test_alive_pid_marked_alive(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        state = RunState(
            active_worktrees={"133": _active(pid=os.getpid())},
        )
        save_run_state(state, run_state_path)

        snapshot = self.build_status_snapshot(
            run_state_path, tmp_path / "logs", now=1_700_000_100.0
        )

        assert snapshot.worktrees[0].alive is True
        assert snapshot.worktrees[0].state == MonitorState.RUNNING

    def test_dead_pid_marked_not_alive(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        state = RunState(
            active_worktrees={"133": _active(pid=999_999_999)},
        )
        save_run_state(state, run_state_path)

        snapshot = self.build_status_snapshot(
            run_state_path, tmp_path / "logs", now=1_700_000_100.0
        )

        assert snapshot.worktrees[0].alive is False
        assert snapshot.worktrees[0].state == MonitorState.PROCESS_EXITED

    def test_cloud_dispatch_alive_is_none(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        state = RunState(
            active_worktrees={
                "133": _active(
                    pid=None, external_id="sess_abc", external_url="https://example.com"
                )
            },
        )
        save_run_state(state, run_state_path)

        snapshot = self.build_status_snapshot(
            run_state_path, tmp_path / "logs", now=1_700_000_100.0
        )

        assert snapshot.worktrees[0].alive is None
        assert snapshot.worktrees[0].external_id == "sess_abc"
        assert snapshot.worktrees[0].state == MonitorState.EXTERNAL

    def test_label_done_overrides_pid_alive_state(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        state = RunState(active_worktrees={"133": _active(pid=os.getpid())})
        save_run_state(state, run_state_path)

        self.forge.get_issue_labels.return_value = ("status:done",)

        snapshot = self.build_status_snapshot(
            run_state_path, tmp_path / "logs", now=1_700_000_100.0
        )

        assert snapshot.worktrees[0].state == MonitorState.DONE

    def test_label_fetch_failure_sets_flag_and_falls_back(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        state = RunState(active_worktrees={"133": _active(pid=999_999_999)})
        save_run_state(state, run_state_path)

        self.forge.get_issue_labels.side_effect = RuntimeError("network error")

        snapshot = self.build_status_snapshot(
            run_state_path, tmp_path / "logs", now=1_700_000_100.0
        )

        assert snapshot.worktrees[0].labels_fetch_failed is True
        assert snapshot.worktrees[0].state == MonitorState.PROCESS_EXITED

    def test_last_reconciled_at_propagated_from_run_state(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        state = RunState(active_worktrees={}, last_reconciled_at=1_700_000_000.0)
        save_run_state(state, run_state_path)

        snapshot = self.build_status_snapshot(
            run_state_path, tmp_path / "logs", now=1_700_000_100.0
        )

        assert snapshot.last_reconciled_at == 1_700_000_000.0

    def test_elapsed_seconds_computed_from_now(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        state = RunState(
            active_worktrees={"133": _active(started_at=1_700_000_000.0)},
        )
        save_run_state(state, run_state_path)

        snapshot = self.build_status_snapshot(
            run_state_path, tmp_path / "logs", now=1_700_000_100.0
        )

        assert snapshot.worktrees[0].elapsed_seconds == 100.0

    def test_unknown_start_time_has_unknown_elapsed_duration(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        state = RunState(active_worktrees={"133": _active(started_at=None)})
        save_run_state(state, run_state_path)

        snapshot = self.build_status_snapshot(
            run_state_path, tmp_path / "logs", now=1_700_000_100.0
        )

        assert snapshot.worktrees[0].elapsed_seconds is None

    def test_log_tail_read_from_log_dir(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "claude-issue-133-monitor-cli.log").write_text("hello\nworld\n")
        state = RunState(
            active_worktrees={"133": _active()},
        )
        save_run_state(state, run_state_path)

        snapshot = self.build_status_snapshot(
            run_state_path, log_dir, now=1_700_000_100.0
        )

        assert snapshot.worktrees[0].log_tail == ["hello", "world"]

    def test_subtask_id_extracted_from_branch(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        state = RunState(
            active_worktrees={"133": _active()},
        )
        save_run_state(state, run_state_path)

        snapshot = self.build_status_snapshot(
            run_state_path, tmp_path / "logs", now=1_700_000_100.0
        )

        assert snapshot.worktrees[0].subtask_id == "monitor-cli"

    def test_label_cache_reused_across_calls_within_ttl(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        state = RunState(active_worktrees={"133": _active()})
        save_run_state(state, run_state_path)
        cache: dict[int, tuple[float, tuple[str, ...]]] = {}

        self.build_status_snapshot(
            run_state_path,
            tmp_path / "logs",
            now=1_700_000_100.0,
            label_cache=cache,
        )
        self.build_status_snapshot(
            run_state_path,
            tmp_path / "logs",
            now=1_700_000_101.0,
            label_cache=cache,
        )

        self.forge.get_issue_labels.assert_called_once_with(133)


def _status(**overrides):
    defaults = dict(
        issue_number=133,
        subtask_id="monitor-cli",
        branch="claude/issue-133-monitor-cli",
        pid=4_212_345,
        alive=True,
        started_at=1_700_000_000.0,
        elapsed_seconds=100.0,
        worktree_path="worktrees/w1",
        external_id=None,
        external_url=None,
        log_tail=["hello", "world"],
        state=MonitorState.RUNNING,
        labels_fetch_failed=False,
    )
    defaults.update(overrides)
    return WorktreeStatus(**defaults)


class TestFormatStatusReport:
    def test_empty_snapshot_shows_no_active_message(self):
        report = format_status_report(
            StatusSnapshot(worktrees=[], last_reconciled_at=None), now=1_700_000_100.0
        )
        assert "現在アクティブなディスパッチはありません" in report

    def test_non_empty_snapshot_includes_key_fields(self):
        status = _status()
        report = format_status_report(
            StatusSnapshot(worktrees=[status], last_reconciled_at=None),
            now=1_700_000_100.0,
        )

        assert "#133" in report
        assert "monitor-cli" in report
        assert "claude/issue-133-monitor-cli" in report
        assert "12345" in report
        assert "RUNNING" in report
        assert "hello" in report

    def test_unknown_elapsed_duration_is_rendered_as_unknown(self):
        report = format_status_report(
            StatusSnapshot(
                worktrees=[_status(started_at=None, elapsed_seconds=None)],
                last_reconciled_at=None,
            ),
            now=1_700_000_100.0,
        )

        assert "不明（自己修復により開始時刻を復元できません）" in report
        assert "world" in report

    def test_process_exited_state_shows_description(self):
        status = _status(alive=False, state=MonitorState.PROCESS_EXITED)
        report = format_status_report(
            StatusSnapshot(worktrees=[status], last_reconciled_at=None),
            now=1_700_000_100.0,
        )
        assert "PROCESS_EXITED" in report
        assert "次回dispatch cycle" in report

    def test_cloud_dispatch_shows_external(self):
        status = _status(
            pid=None,
            alive=None,
            external_id="sess_abc",
            external_url="https://example.com",
            state=MonitorState.EXTERNAL,
        )
        report = format_status_report(
            StatusSnapshot(worktrees=[status], last_reconciled_at=None),
            now=1_700_000_100.0,
        )
        assert "EXTERNAL" in report
        assert "sess_abc" in report

    def test_done_state_shows_description(self):
        status = _status(state=MonitorState.DONE)
        report = format_status_report(
            StatusSnapshot(worktrees=[status], last_reconciled_at=None),
            now=1_700_000_100.0,
        )
        assert "DONE" in report
        assert "完了済み" in report

    def test_label_fetch_failure_shows_note(self):
        status = _status(labels_fetch_failed=True)
        report = format_status_report(
            StatusSnapshot(worktrees=[status], last_reconciled_at=None),
            now=1_700_000_100.0,
        )
        assert "ラベル取得失敗" in report

    def test_last_reconciled_at_shown_when_present(self):
        report = format_status_report(
            StatusSnapshot(worktrees=[], last_reconciled_at=1_700_000_000.0),
            now=1_700_000_100.0,
        )
        assert "最終dispatchサイクル" in report
        assert "1m40s" in report

    def test_last_reconciled_at_shown_as_unrecorded_when_none(self):
        report = format_status_report(
            StatusSnapshot(worktrees=[], last_reconciled_at=None), now=1_700_000_100.0
        )
        assert "最終dispatchサイクル" in report
        assert "未記録" in report


class TestStatusGuidance:
    def test_plain_report_includes_guidance_for_each_state(self):
        states = (
            MonitorState.QUEUED,
            MonitorState.RUNNING,
            MonitorState.PROCESS_EXITED,
            MonitorState.BLOCKED,
            MonitorState.BLOCKED_HUMAN_REVIEW,
            MonitorState.MANUAL_MERGE_REQUIRED,
            MonitorState.UNKNOWN,
        )
        for state in states:
            report = format_status_report(
                StatusSnapshot(
                    worktrees=[_status(state=state)], last_reconciled_at=None
                ),
                now=1_700_000_100.0,
            )
            assert "案内:" in report
            assert f"段階: {state.value}" in report

        stale = format_status_report(
            StatusSnapshot(
                worktrees=[
                    _status(
                        state=MonitorState.DONE,
                        label_evidence=EvidenceState.STALE.value,
                    )
                ],
                last_reconciled_at=None,
            ),
            now=1_700_000_100.0,
        )
        assert "根拠: stale" in stale
        assert "完了は断定しません" in stale

    def test_queued_running_exited_blocked_and_unknown(self):
        queued = render_guidance(
            guidance_from_status(_status(state=MonitorState.QUEUED))
        )
        assert "自動進行: conditional" in queued and "保証しません" in queued
        running = guidance_from_status(_status(state=MonitorState.RUNNING))
        assert running.next_owner is NextOwner.ENGINE
        assert running.progress is ProgressOutlook.IN_PROGRESS
        exited = render_guidance(
            guidance_from_status(
                _status(state=MonitorState.PROCESS_EXITED, alive=False)
            )
        )
        assert "停止確認ではありません" in exited
        blocked = render_guidance(
            guidance_from_status(_status(state=MonitorState.BLOCKED))
        )
        assert "依存待ちとは断定できません" in blocked
        assert "次の担当: unknown" in blocked
        unknown = render_guidance(
            guidance_from_status(_status(state=MonitorState.UNKNOWN))
        )
        assert "自動進行: unknown" in unknown

    def test_review_and_manual_merge_have_no_recovery_command(self):
        for state in (
            MonitorState.BLOCKED_HUMAN_REVIEW,
            MonitorState.MANUAL_MERGE_REQUIRED,
        ):
            text = render_guidance(guidance_from_status(_status(state=state)))
            assert "復旧引数は作りません" in text
            assert "コマンド:" not in text

    def test_unavailable_labels_do_not_assert_stop(self):
        text = render_guidance(
            guidance_from_status(
                _status(
                    state=MonitorState.PROCESS_EXITED,
                    labels_fetch_failed=True,
                    label_evidence=EvidenceState.UNAVAILABLE.value,
                )
            )
        )
        assert "根拠: unavailable" in text
        assert "自動再開・停止済み・完了は断定しません" in text

    def test_compact_attention_names_the_target(self):
        text = compact_status_attention(
            StatusSnapshot(
                worktrees=[
                    _status(issue_number=12, state=MonitorState.BLOCKED),
                    _status(issue_number=13, state=MonitorState.RUNNING),
                ],
                last_reconciled_at=None,
            )
        )
        assert "Issue #12" in text and "要対応:あり" in text
        assert "Issue #13" in text and "要対応:なし" in text
        assert "orchestune status" in text


class _NestedOnlyActive:
    """flat fieldを持たず`core`/`launch`だけを公開するactive（view経由の読取を強制）。"""

    def __init__(self, active):
        self.core = active.core
        self.launch = active.launch


class TestBuildStatusSnapshotReadsNestedViews(_FakeForgeTest):
    """#1128: monitor projectionはflat fieldではなくnested viewから読む。"""

    def test_projection_matches_flat_dto_using_only_core_and_launch(
        self, tmp_path, monkeypatch
    ):
        active = make_test_active_worktree(
            133,
            branch="claude/issue-133-monitor-cli",
            pid=None,
            started_at=1_700_000_000.0,
            external_id="cloud-1",
            external_url="https://example.test/cloud-1",
        )
        path = tmp_path / "run_state.json"
        save_run_state(RunState(active_worktrees={"133": active}), path)
        expected = build_status_snapshot(
            path, tmp_path, 1_700_000_100.0, forge=self.forge
        )

        monkeypatch.setattr(
            status_snapshot_module,
            "load_run_state",
            lambda _path: RunState(
                active_worktrees={"133": _NestedOnlyActive(active)}  # type: ignore[dict-item]
            ),
        )
        actual = build_status_snapshot(
            path, tmp_path, 1_700_000_100.0, forge=self.forge
        )

        assert actual == expected
        assert actual.worktrees[0].external_id == "cloud-1"
        assert actual.worktrees[0].elapsed_seconds == 100.0


def _parent_payload(status, failures, **overrides):
    record = {
        "version": 1,
        "repository_id": "Saltmu/orchestune",
        "parent_issue": 100,
        "observed_at": "2020-01-01T00:00:00Z",
        "mode": "apply",
        "executed": True,
        "status": status,
        "failures": failures,
    }
    record.update(overrides)
    return {"parent_integration": record}


def _parent_text(payload, **kwargs):
    values = {
        "parent_issue": 100,
        "repository_id": "Saltmu/orchestune",
        "evidence_path": "result.json",
    }
    values.update(kwargs)
    return format_parent_integration(payload, **values)


def test_display_reasons_match_the_saved_child_review_set():
    from orchestune.integrator.types import CHILD_REVIEW_REPORT_REASONS
    from orchestune.status_snapshot import _CHILD_REVIEW_REASONS

    assert _CHILD_REVIEW_REASONS == CHILD_REVIEW_REPORT_REASONS


class TestParentIntegrationStatus:
    def test_review_shortage_names_recorded_child_and_sha(self):
        text = _parent_text(
            _parent_payload(
                "review_gate_blocked",
                [
                    {
                        "kind": "child_review_gate",
                        "reason": "sha_mismatch",
                        "issue_number": 10,
                        "expected_sha": "abc",
                        "integration_pr_number": 55,
                    }
                ],
            )
        )
        assert "観測: 最後の観測" in text
        assert "確認済みの子 Issue: #10" in text
        assert "対象 SHA: abc" in text
        assert "統合 PR: #55" in text
        assert "レビュー証跡を確認してください" in text
        assert "--child-review-gate" not in text
        assert "差し替え" not in text
        assert "解消しました" not in text

    def test_lookup_unknown_does_not_claim_missing_evidence(self):
        text = _parent_text(
            _parent_payload(
                "review_gate_blocked",
                [{"kind": "child_review_gate", "reason": "lookup_unknown"}],
            )
        )
        assert "API を再確認してください" in text
        assert "証跡不足" not in text
        assert "確認済みの子 Issue" not in text
        assert "統合 PR" not in text

    def test_retryable_timeout_shows_the_recorded_next_time(self):
        text = _parent_text(
            _parent_payload(
                "execution_timed_out",
                [
                    {
                        "kind": "execution",
                        "cause": "ci_timeout",
                        "stage": "ci",
                        "next_retry_at": "2026-11-14T08:00:00Z",
                        "target": {"issue_number": 9, "subtask_id": "task-9"},
                    }
                ],
            )
        )
        assert "次回可能日時: 2026-11-14T08:00:00Z" in text
        assert "再試行条件が成立した場合" in text
        assert "対象: Issue #9 / task-9 / 段階:ci" in text
        assert "再試行しました" not in text

    def test_exhausted_retry_asks_the_operator_to_check_the_stage(self):
        text = _parent_text(
            _parent_payload(
                "execution_retry_exhausted",
                [
                    {
                        "kind": "execution",
                        "stage": "merge",
                        "target": {"subtask_id": "task-9"},
                    }
                ],
            )
        )
        assert "失敗段階を確認してください" in text
        assert "自動再試行" not in text
        assert "次回可能日時" not in text

    @pytest.mark.parametrize(
        "status",
        ["execution_cleanup_failed", "execution_indeterminate"],
    )
    def test_unconfirmed_cleanup_names_each_missing_check(self, status):
        text = _parent_text(
            _parent_payload(
                status,
                [
                    {
                        "kind": "execution",
                        "stop_confirmed": False,
                        "rollback_confirmed": True,
                        "side_effect_state": "unknown",
                    }
                ],
            )
        )
        assert "停止は未確認" in text
        assert "書き込みは未確認" in text
        assert "rollbackは未確認" not in text
        assert "運用者が状態を照合してください" in text

    @pytest.mark.parametrize(
        "payload",
        [
            {"selected": []},
            {"parent_integration": {"version": 0}},
            {"parent_integration": {"version": 2, "mode": "apply", "executed": True}},
            _parent_payload("review_gate_blocked", [], mode="preview"),
            _parent_payload("success", []),
            _parent_payload("review_gate_blocked", [], executed=False),
            _parent_payload(
                "review_gate_blocked",
                [
                    {
                        "kind": "child_review_gate",
                        "reason": "sha_mismatch",
                        "issue_number": 10,
                    }
                ],
                parent_issue=8,
            ),
            _parent_payload(
                "review_gate_blocked",
                [
                    {
                        "kind": "child_review_gate",
                        "reason": "sha_mismatch",
                        "issue_number": 10,
                    }
                ],
                repository_id="other/repo",
            ),
            _parent_payload(
                "review_gate_blocked",
                [{"kind": "child_review_gate", "reason": "stale_read_gate: stale"}],
            ),
            _parent_payload(["review_gate_blocked"], []),
            _parent_payload(
                "review_gate_blocked",
                [
                    {
                        "kind": "child_review_gate",
                        "reason": ["absent"],
                        "issue_number": 10,
                    }
                ],
            ),
        ],
    )
    def test_unusable_observations_stay_unknown(self, payload):
        text = _parent_text(payload)
        assert "現在の状態は不明" in text
        assert "対象親の apply 結果を確認してください。" in text
        assert "最後の観測" not in text
        assert "確認済みの子 Issue" not in text
        assert "対象 SHA" not in text

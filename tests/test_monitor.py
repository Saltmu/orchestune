import os
import time
from unittest.mock import Mock, patch

import pytest

import orchestune.monitor as monitor_module
from orchestune.dispatch_state import ActiveWorktree, RunState, save_run_state
from orchestune.monitor import (
    MonitorState,
    StatusSnapshot,
    WorktreeStatus,
    _derive_monitor_state,
    _extract_subtask_id,
    _fetch_labels_cached,
    _format_duration,
    _read_log_tail,
    build_status_snapshot,
    format_status_report,
    main,
)


@pytest.fixture(autouse=True)
def _stub_get_issue_labels():
    """既定ではstatus:in-progressを返し、PID/external_idベースの分類テストが
    従来通り動作するようにする。優先順位そのもののテストは個別にオーバーライドする。"""
    with patch(
        "orchestune.monitor.github.get_issue_labels",
        return_value=("status:in-progress",),
    ) as mock:
        yield mock


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
    return ActiveWorktree(**defaults)


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
        monkeypatch.setattr(monitor_module, "_TAIL_CHUNK_SIZE", 8)
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


class TestFetchLabelsCached:
    def test_fetches_and_caches_within_ttl(self):
        cache: dict[int, tuple[float, tuple[str, ...]]] = {}
        with patch(
            "orchestune.monitor.github.get_issue_labels",
            return_value=("status:queued",),
        ) as mock_fetch:
            first = _fetch_labels_cached(133, cache, now=1000.0, ttl=15.0)
            second = _fetch_labels_cached(133, cache, now=1005.0, ttl=15.0)

        assert first == ("status:queued",)
        assert second == ("status:queued",)
        mock_fetch.assert_called_once_with(133)

    def test_refetches_after_ttl_expires(self):
        cache: dict[int, tuple[float, tuple[str, ...]]] = {}
        with patch(
            "orchestune.monitor.github.get_issue_labels",
            return_value=("status:queued",),
        ) as mock_fetch:
            _fetch_labels_cached(133, cache, now=1000.0, ttl=15.0)
            _fetch_labels_cached(133, cache, now=1020.0, ttl=15.0)

        assert mock_fetch.call_count == 2

    def test_fetch_failure_without_cache_returns_none(self):
        cache: dict[int, tuple[float, tuple[str, ...]]] = {}
        with patch(
            "orchestune.monitor.github.get_issue_labels",
            side_effect=RuntimeError("gh unavailable"),
        ):
            result = _fetch_labels_cached(133, cache, now=1000.0, ttl=15.0)
        assert result is None

    def test_fetch_failure_with_stale_cache_returns_stale_value(self):
        cache: dict[int, tuple[float, tuple[str, ...]]] = {
            133: (900.0, ("status:queued",))
        }
        with patch(
            "orchestune.monitor.github.get_issue_labels",
            side_effect=RuntimeError("gh unavailable"),
        ):
            result = _fetch_labels_cached(133, cache, now=1000.0, ttl=15.0)
        assert result == ("status:queued",)


class TestBuildStatusSnapshot:
    def test_empty_when_no_run_state_file(self, tmp_path):
        snapshot = build_status_snapshot(
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

        snapshot = build_status_snapshot(
            run_state_path, tmp_path / "logs", now=1_700_000_100.0
        )

        assert [s.issue_number for s in snapshot.worktrees] == [133, 200]

    def test_alive_pid_marked_alive(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        state = RunState(
            active_worktrees={"133": _active(pid=os.getpid())},
        )
        save_run_state(state, run_state_path)

        snapshot = build_status_snapshot(
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

        snapshot = build_status_snapshot(
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

        snapshot = build_status_snapshot(
            run_state_path, tmp_path / "logs", now=1_700_000_100.0
        )

        assert snapshot.worktrees[0].alive is None
        assert snapshot.worktrees[0].external_id == "sess_abc"
        assert snapshot.worktrees[0].state == MonitorState.EXTERNAL

    def test_label_done_overrides_pid_alive_state(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        state = RunState(active_worktrees={"133": _active(pid=os.getpid())})
        save_run_state(state, run_state_path)

        with patch(
            "orchestune.monitor.github.get_issue_labels", return_value=("status:done",)
        ):
            snapshot = build_status_snapshot(
                run_state_path, tmp_path / "logs", now=1_700_000_100.0
            )

        assert snapshot.worktrees[0].state == MonitorState.DONE

    def test_label_fetch_failure_sets_flag_and_falls_back(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        state = RunState(active_worktrees={"133": _active(pid=999_999_999)})
        save_run_state(state, run_state_path)

        with patch(
            "orchestune.monitor.github.get_issue_labels",
            side_effect=RuntimeError("network error"),
        ):
            snapshot = build_status_snapshot(
                run_state_path, tmp_path / "logs", now=1_700_000_100.0
            )

        assert snapshot.worktrees[0].labels_fetch_failed is True
        assert snapshot.worktrees[0].state == MonitorState.PROCESS_EXITED

    def test_last_reconciled_at_propagated_from_run_state(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        state = RunState(active_worktrees={}, last_reconciled_at=1_700_000_000.0)
        save_run_state(state, run_state_path)

        snapshot = build_status_snapshot(
            run_state_path, tmp_path / "logs", now=1_700_000_100.0
        )

        assert snapshot.last_reconciled_at == 1_700_000_000.0

    def test_elapsed_seconds_computed_from_now(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        state = RunState(
            active_worktrees={"133": _active(started_at=1_700_000_000.0)},
        )
        save_run_state(state, run_state_path)

        snapshot = build_status_snapshot(
            run_state_path, tmp_path / "logs", now=1_700_000_100.0
        )

        assert snapshot.worktrees[0].elapsed_seconds == 100.0

    def test_log_tail_read_from_log_dir(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "claude-issue-133-monitor-cli.log").write_text("hello\nworld\n")
        state = RunState(
            active_worktrees={"133": _active()},
        )
        save_run_state(state, run_state_path)

        snapshot = build_status_snapshot(run_state_path, log_dir, now=1_700_000_100.0)

        assert snapshot.worktrees[0].log_tail == ["hello", "world"]

    def test_subtask_id_extracted_from_branch(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        state = RunState(
            active_worktrees={"133": _active()},
        )
        save_run_state(state, run_state_path)

        snapshot = build_status_snapshot(
            run_state_path, tmp_path / "logs", now=1_700_000_100.0
        )

        assert snapshot.worktrees[0].subtask_id == "monitor-cli"

    def test_label_cache_reused_across_calls_within_ttl(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        state = RunState(active_worktrees={"133": _active()})
        save_run_state(state, run_state_path)
        cache: dict[int, tuple[float, tuple[str, ...]]] = {}

        with patch(
            "orchestune.monitor.github.get_issue_labels",
            return_value=("status:in-progress",),
        ) as mock_fetch:
            build_status_snapshot(
                run_state_path,
                tmp_path / "logs",
                now=1_700_000_100.0,
                label_cache=cache,
            )
            build_status_snapshot(
                run_state_path,
                tmp_path / "logs",
                now=1_700_000_101.0,
                label_cache=cache,
            )

        mock_fetch.assert_called_once_with(133)


def _status(**overrides):
    defaults = dict(
        issue_number=133,
        subtask_id="monitor-cli",
        branch="claude/issue-133-monitor-cli",
        pid=12345,
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


class TestMain:
    def test_one_shot_mode_prints_report_and_returns_zero(self, tmp_path, capsys):
        run_state_path = tmp_path / "run_state.json"
        state = RunState(active_worktrees={"133": _active()})
        save_run_state(state, run_state_path)

        exit_code = main(
            [
                "--run-state-path",
                str(run_state_path),
                "--log-dir",
                str(tmp_path / "logs"),
            ]
        )

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "#133" in captured.out

    def test_one_shot_mode_no_active_worktrees(self, tmp_path, capsys):
        exit_code = main(
            [
                "--run-state-path",
                str(tmp_path / "run_state.json"),
                "--log-dir",
                str(tmp_path / "logs"),
            ]
        )

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "現在アクティブなディスパッチはありません" in captured.out

    def test_watch_mode_loops_until_keyboard_interrupt(
        self, tmp_path, capsys, monkeypatch
    ):
        run_state_path = tmp_path / "run_state.json"
        state = RunState(active_worktrees={"133": _active()})
        save_run_state(state, run_state_path)

        sleep_mock = Mock(side_effect=[None, KeyboardInterrupt])
        monkeypatch.setattr(time, "sleep", sleep_mock)

        exit_code = main(
            [
                "--run-state-path",
                str(run_state_path),
                "--log-dir",
                str(tmp_path / "logs"),
                "--watch",
                "--interval",
                "1",
            ]
        )

        captured = capsys.readouterr()
        assert exit_code == 0
        assert captured.out.count("#133") == 2
        assert sleep_mock.call_count == 2

    def test_watch_mode_reuses_label_cache_across_iterations(
        self, tmp_path, monkeypatch, _stub_get_issue_labels
    ):
        run_state_path = tmp_path / "run_state.json"
        state = RunState(active_worktrees={"133": _active()})
        save_run_state(state, run_state_path)

        sleep_mock = Mock(side_effect=[None, KeyboardInterrupt])
        monkeypatch.setattr(time, "sleep", sleep_mock)

        main(
            [
                "--run-state-path",
                str(run_state_path),
                "--log-dir",
                str(tmp_path / "logs"),
                "--watch",
                "--interval",
                "1",
            ]
        )

        # #137: watchループ間でラベルキャッシュを共有し、interval(1秒)ごとに
        # activeなIssue数だけgh呼び出しが増え続けないことを保証する。
        assert _stub_get_issue_labels.call_count == 1

    def test_interval_zero_is_rejected(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "--run-state-path",
                    str(tmp_path / "run_state.json"),
                    "--log-dir",
                    str(tmp_path / "logs"),
                    "--watch",
                    "--interval",
                    "0",
                ]
            )
        assert exc_info.value.code == 2

    def test_interval_negative_is_rejected(self, tmp_path):
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "--run-state-path",
                    str(tmp_path / "run_state.json"),
                    "--log-dir",
                    str(tmp_path / "logs"),
                    "--watch",
                    "--interval",
                    "-1",
                ]
            )
        assert exc_info.value.code == 2

    def test_tail_lines_negative_is_rejected(self, tmp_path):
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "--run-state-path",
                    str(tmp_path / "run_state.json"),
                    "--log-dir",
                    str(tmp_path / "logs"),
                    "--tail-lines",
                    "-1",
                ]
            )
        assert exc_info.value.code == 2

    def test_interval_non_integer_is_rejected(self, tmp_path):
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "--run-state-path",
                    str(tmp_path / "run_state.json"),
                    "--log-dir",
                    str(tmp_path / "logs"),
                    "--watch",
                    "--interval",
                    "abc",
                ]
            )
        assert exc_info.value.code == 2

    def test_tail_lines_non_integer_is_rejected(self, tmp_path):
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "--run-state-path",
                    str(tmp_path / "run_state.json"),
                    "--log-dir",
                    str(tmp_path / "logs"),
                    "--tail-lines",
                    "abc",
                ]
            )
        assert exc_info.value.code == 2

    def test_tail_lines_zero_shows_no_log_tail(self, tmp_path, capsys):
        run_state_path = tmp_path / "run_state.json"
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "claude-issue-133-monitor-cli.log").write_text("hello\nworld\n")
        save_run_state(RunState(active_worktrees={"133": _active()}), run_state_path)

        exit_code = main(
            [
                "--run-state-path",
                str(run_state_path),
                "--log-dir",
                str(log_dir),
                "--tail-lines",
                "0",
            ]
        )

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "hello" not in captured.out
        assert "world" not in captured.out

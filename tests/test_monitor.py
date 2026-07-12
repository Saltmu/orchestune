import os
import time
from unittest.mock import Mock

import pytest

import orchestune.monitor as monitor_module
from orchestune.dispatch_state import ActiveWorktree, RunState, save_run_state
from orchestune.monitor import (
    WorktreeStatus,
    _extract_subtask_id,
    _format_duration,
    _read_log_tail,
    build_status_snapshot,
    format_status_report,
    main,
)


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


class TestBuildStatusSnapshot:
    def test_empty_when_no_run_state_file(self, tmp_path):
        snapshot = build_status_snapshot(
            tmp_path / "run_state.json", tmp_path / "logs", now=1_700_000_100.0
        )
        assert snapshot == []

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

        assert [s.issue_number for s in snapshot] == [133, 200]

    def test_alive_pid_marked_alive(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        state = RunState(
            active_worktrees={"133": _active(pid=os.getpid())},
        )
        save_run_state(state, run_state_path)

        snapshot = build_status_snapshot(
            run_state_path, tmp_path / "logs", now=1_700_000_100.0
        )

        assert snapshot[0].alive is True

    def test_dead_pid_marked_not_alive(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        state = RunState(
            active_worktrees={"133": _active(pid=999_999_999)},
        )
        save_run_state(state, run_state_path)

        snapshot = build_status_snapshot(
            run_state_path, tmp_path / "logs", now=1_700_000_100.0
        )

        assert snapshot[0].alive is False

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

        assert snapshot[0].alive is None
        assert snapshot[0].external_id == "sess_abc"

    def test_elapsed_seconds_computed_from_now(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        state = RunState(
            active_worktrees={"133": _active(started_at=1_700_000_000.0)},
        )
        save_run_state(state, run_state_path)

        snapshot = build_status_snapshot(
            run_state_path, tmp_path / "logs", now=1_700_000_100.0
        )

        assert snapshot[0].elapsed_seconds == 100.0

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

        assert snapshot[0].log_tail == ["hello", "world"]

    def test_subtask_id_extracted_from_branch(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        state = RunState(
            active_worktrees={"133": _active()},
        )
        save_run_state(state, run_state_path)

        snapshot = build_status_snapshot(
            run_state_path, tmp_path / "logs", now=1_700_000_100.0
        )

        assert snapshot[0].subtask_id == "monitor-cli"


class TestFormatStatusReport:
    def test_empty_snapshot_shows_no_active_message(self):
        report = format_status_report([], now=1_700_000_100.0)
        assert "現在アクティブなディスパッチはありません" in report

    def test_non_empty_snapshot_includes_key_fields(self):
        status = WorktreeStatus(
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
        )
        report = format_status_report([status], now=1_700_000_100.0)

        assert "#133" in report
        assert "monitor-cli" in report
        assert "claude/issue-133-monitor-cli" in report
        assert "12345" in report
        assert "RUNNING" in report
        assert "hello" in report
        assert "world" in report

    def test_dead_pid_shows_stopped(self):
        status = WorktreeStatus(
            issue_number=133,
            subtask_id=None,
            branch="claude/issue-133-x",
            pid=999999999,
            alive=False,
            started_at=1_700_000_000.0,
            elapsed_seconds=100.0,
            worktree_path="worktrees/w1",
            external_id=None,
            external_url=None,
            log_tail=[],
        )
        report = format_status_report([status], now=1_700_000_100.0)
        assert "STOPPED" in report

    def test_cloud_dispatch_shows_external(self):
        status = WorktreeStatus(
            issue_number=133,
            subtask_id=None,
            branch="claude/issue-133-x",
            pid=None,
            alive=None,
            started_at=1_700_000_000.0,
            elapsed_seconds=100.0,
            worktree_path="worktrees/w1",
            external_id="sess_abc",
            external_url="https://example.com",
            log_tail=[],
        )
        report = format_status_report([status], now=1_700_000_100.0)
        assert "EXTERNAL" in report
        assert "sess_abc" in report


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

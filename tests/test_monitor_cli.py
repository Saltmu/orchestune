"""`orchestune status` CLI配線（引数解析・`--watch`ループ）のテスト。

`tests/test_monitor.py`から、ステータス集計ドメインロジック本体のテストを
`status_snapshot`モジュールの新設（アーキテクチャ層L2への切り出し）に合わせて
`test_status_snapshot.py`へ分離した残り、`main()`のCLIレベルの挙動のみを
本ファイルに残している。
"""

import os
import time
from unittest.mock import Mock, patch

import pytest

from orchestune.dispatch_state import ActiveWorktree, RunState, save_run_state
from orchestune.monitor import main


@pytest.fixture(autouse=True)
def _stub_get_issue_labels():
    """既定ではstatus:in-progressを返し、PID/external_idベースの分類テストが
    従来通り動作するようにする。"""
    with patch(
        "orchestune.forge.GitHubForge.get_issue_labels",
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

"""`orchestune status` CLI配線（引数解析・`--watch`ループ）のテスト。

`tests/test_monitor.py`から、ステータス集計ドメインロジック本体のテストを
`status_snapshot`モジュールの新設（アーキテクチャ層L2への切り出し）に合わせて
`test_status_snapshot.py`へ分離した残り、`main()`のCLIレベルの挙動のみを
本ファイルに残している。
"""

import io
import os
import re
import time
from unittest.mock import Mock

import pytest

import orchestune.monitor as monitor
from orchestune.infra.cli_output import CliOutput, make_output
from orchestune.ledger.run_state import RunState
from orchestune.monitor import main
from tests.dispatch_test_support import make_test_active_worktree
from tests.dispatch_test_support import save_locked_run_state as save_run_state


@pytest.fixture(autouse=True)
def _stub_get_issue_labels(fake_forge):
    """既定ではstatus:in-progressを返し、PID/external_idベースの分類テストが
    従来通り動作するようにする。"""
    fake_forge.get_issue_labels.return_value = ("status:in-progress",)
    yield fake_forge


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


class TestMain:
    def test_one_shot_mode_prints_report_and_returns_zero(
        self, tmp_path, capsys, fake_forge
    ):
        run_state_path = tmp_path / "run_state.json"
        state = RunState(active_worktrees={"133": _active()})
        save_run_state(state, run_state_path)

        exit_code = main(
            [
                "--run-state-path",
                str(run_state_path),
                "--log-dir",
                str(tmp_path / "logs"),
            ],
            forge=fake_forge,
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
        self, tmp_path, capsys, monkeypatch, _stub_get_issue_labels
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
            ],
            forge=_stub_get_issue_labels,
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
            ],
            forge=_stub_get_issue_labels,
        )

        # #137: watchループ間でラベルキャッシュを共有し、interval(1秒)ごとに
        # activeなIssue数だけgh呼び出しが増え続けないことを保証する。
        assert _stub_get_issue_labels.get_issue_labels.call_count == 1

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

    def test_tail_lines_zero_shows_no_log_tail(
        self, tmp_path, capsys, _stub_get_issue_labels
    ):
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
            ],
            forge=_stub_get_issue_labels,
        )

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "hello" not in captured.out


ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
NOW = 1_700_000_100.0
STOPPED = "監視を終了しました。"


class FakeStream(io.StringIO):
    def __init__(self, *, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"


def _output(*, tty: bool, env: dict[str, str] | None = None):
    stream = FakeStream(tty=tty)
    out: CliOutput = make_output(stream, env=env or {}, width=140)
    return out, stream


def _watch_args(tmp_path, *, interval: int = 2, tail_lines: int | None = None):
    run_state_path = tmp_path / "run_state.json"
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    (log_dir / "claude-issue-133-monitor-cli.log").write_text("log-one\nlog-two\n")
    save_run_state(RunState(active_worktrees={"133": _active()}), run_state_path)
    args = ["--run-state-path", str(run_state_path), "--log-dir", str(log_dir)]
    if tail_lines is not None:
        args += ["--tail-lines", str(tail_lines)]
    return [*args, "--watch", "--interval", str(interval)]


def _fake_sleep(*effects):
    return Mock(side_effect=list(effects))


class TestWatchPlain:
    def test_non_tty_appends_static_snapshots_without_control_characters(
        self, tmp_path, _stub_get_issue_labels
    ):
        out, stream = _output(tty=False)
        sleep = _fake_sleep(None, None, KeyboardInterrupt)

        exit_code = main(
            _watch_args(tmp_path, interval=2),
            forge=_stub_get_issue_labels,
            output=out,
            clock=lambda: NOW,
            sleep=sleep,
        )

        text = stream.getvalue()
        assert exit_code == 0
        assert text.count("Issue #133") == 3
        assert text.count("2秒ごとに自動更新します。Ctrl+Cで終了してください。") == 3
        assert "\x1b" not in text and "\r" not in text
        assert text.rstrip().endswith(STOPPED)
        assert [c.args for c in sleep.call_args_list] == [(2,), (2,), (2,)]

    def test_plain_snapshot_matches_the_existing_report(
        self, tmp_path, _stub_get_issue_labels
    ):
        from orchestune.status_snapshot import (
            build_status_snapshot,
            format_status_report,
        )

        out, stream = _output(tty=False)
        args = _watch_args(tmp_path)
        main(
            args,
            forge=_stub_get_issue_labels,
            output=out,
            clock=lambda: NOW,
            sleep=_fake_sleep(KeyboardInterrupt),
        )
        snapshot = build_status_snapshot(
            tmp_path / "run_state.json",
            tmp_path / "logs",
            NOW,
            3,
            {},
            forge=_stub_get_issue_labels,
        )
        assert format_status_report(snapshot, NOW) in stream.getvalue()

    def test_plain_env_on_a_tty_is_static(self, tmp_path, _stub_get_issue_labels):
        out, stream = _output(tty=True, env={"ORCHESTUNE_CLI_PLAIN": "1"})
        main(
            _watch_args(tmp_path),
            forge=_stub_get_issue_labels,
            output=out,
            clock=lambda: NOW,
            sleep=_fake_sleep(None, KeyboardInterrupt),
        )
        text = stream.getvalue()
        assert text.count("Issue #133") == 2
        assert "\x1b" not in text

    def test_snapshot_and_label_calls_are_not_multiplied_by_rendering(
        self, tmp_path, monkeypatch, _stub_get_issue_labels
    ):
        calls = []
        real = monitor.build_status_snapshot

        def counting(*args, **kwargs):
            calls.append(args[2])
            return real(*args, **kwargs)

        monkeypatch.setattr(monitor, "build_status_snapshot", counting)
        out, _ = _output(tty=False)
        times = iter([NOW, NOW + 3, NOW + 6])

        main(
            _watch_args(tmp_path),
            forge=_stub_get_issue_labels,
            output=out,
            clock=lambda: next(times),
            sleep=_fake_sleep(None, None, KeyboardInterrupt),
        )

        assert calls == [NOW, NOW + 3, NOW + 6]
        # label cache TTL (15s) still covers all three snapshots: one API call.
        assert _stub_get_issue_labels.get_issue_labels.call_count == 1


class TestWatchLive:
    def test_tty_updates_in_place_and_restores_the_cursor(
        self, tmp_path, _stub_get_issue_labels
    ):
        out, stream = _output(tty=True)
        sleep = _fake_sleep(None, KeyboardInterrupt)

        exit_code = main(
            _watch_args(tmp_path, interval=4),
            forge=_stub_get_issue_labels,
            output=out,
            clock=lambda: NOW,
            sleep=sleep,
        )

        raw = stream.getvalue()
        text = ANSI.sub("", raw)
        assert exit_code == 0
        assert HIDE_CURSOR in raw
        assert raw.rindex(SHOW_CURSOR) > raw.rindex(HIDE_CURSOR)
        assert "\x1b[2J" not in raw
        assert "#133" in text and "RUNNING" in text
        assert "4秒ごとに自動更新します。Ctrl+Cで終了してください。" in text
        assert text.rstrip().endswith(STOPPED)
        assert sleep.call_count == 2

    def test_exception_closes_the_live_display(
        self, tmp_path, monkeypatch, _stub_get_issue_labels
    ):
        real = monitor.build_status_snapshot
        count = {"n": 0}

        def flaky(*args, **kwargs):
            count["n"] += 1
            if count["n"] == 2:
                raise RuntimeError("snapshot failed")
            return real(*args, **kwargs)

        monkeypatch.setattr(monitor, "build_status_snapshot", flaky)
        out, stream = _output(tty=True)

        with pytest.raises(RuntimeError, match="snapshot failed"):
            main(
                _watch_args(tmp_path),
                forge=_stub_get_issue_labels,
                output=out,
                clock=lambda: NOW,
                sleep=_fake_sleep(None, None),
            )

        raw = stream.getvalue()
        assert raw.rindex(SHOW_CURSOR) > raw.rindex(HIDE_CURSOR)

    def test_interrupt_while_building_a_snapshot_still_exits_zero(
        self, tmp_path, monkeypatch, _stub_get_issue_labels
    ):
        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(monitor, "build_status_snapshot", interrupted)
        out, stream = _output(tty=True)

        exit_code = main(
            _watch_args(tmp_path),
            forge=_stub_get_issue_labels,
            output=out,
            clock=lambda: NOW,
            sleep=_fake_sleep(),
        )

        assert exit_code == 0
        assert STOPPED in ANSI.sub("", stream.getvalue())


def _many_tasks_args(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    active = {}
    for number in (134, 135, 136):
        branch = f"claude/issue-{number}-task"
        (log_dir / f"claude-issue-{number}-task.log").write_text(
            f"log-{number}-a\nlog-{number}-b\nlog-{number}-c\n"
        )
        active[str(number)] = _active(issue_number=number, branch=branch)
    run_state_path = tmp_path / "run_state.json"
    save_run_state(RunState(active_worktrees=active), run_state_path)
    return [
        "--run-state-path",
        str(run_state_path),
        "--log-dir",
        str(log_dir),
        "--watch",
    ]


def _watch_frame(tmp_path, forge, lines: str) -> str:
    """Render one watch frame (footer given) exactly as the Live display shows it.

    Live crops on screen while running, so the frame is rendered through ``emit``
    to see what the display is given.
    """
    args = _many_tasks_args(tmp_path)
    snapshot = monitor.build_status_snapshot(args[1], args[3], NOW, 3, {}, forge=forge)
    out, stream = _output(tty=True, env={"LINES": lines})
    out.emit(monitor.status_block(out, snapshot, NOW, "3秒ごとに自動更新します。"))
    return ANSI.sub("", stream.getvalue())


class TestWatchLiveOverflow:
    def test_frame_taller_than_the_screen_degrades_to_the_table(
        self, tmp_path, _stub_get_issue_labels
    ):
        text = _watch_frame(tmp_path, _stub_get_issue_labels, "14")

        assert "#134" in text and "#135" in text and "#136" in text
        assert "表示領域に収まらない" in text
        assert "orchestune status" in text
        assert "3秒ごとに自動更新します。" in text
        assert "log-136-a" not in text

    def test_frame_that_fits_keeps_every_detail(self, tmp_path, _stub_get_issue_labels):
        text = _watch_frame(tmp_path, _stub_get_issue_labels, "100")

        assert "表示領域に収まらない" not in text
        assert "log-136-c" in text

    def test_one_shot_is_never_degraded(self, tmp_path, _stub_get_issue_labels):
        out, stream = _output(tty=True, env={"LINES": "14"})

        main(
            _many_tasks_args(tmp_path)[:-1],
            forge=_stub_get_issue_labels,
            output=out,
            clock=lambda: NOW,
        )

        text = ANSI.sub("", stream.getvalue())
        assert "表示領域に収まらない" not in text
        assert "log-136-c" in text


class TestOneShotDisplay:
    def test_tty_shows_table_and_details(self, tmp_path, _stub_get_issue_labels):
        out, stream = _output(tty=True)
        args = _watch_args(tmp_path)[:-3]  # drop --watch --interval N

        exit_code = main(
            args,
            forge=_stub_get_issue_labels,
            output=out,
            clock=lambda: NOW,
        )

        text = ANSI.sub("", stream.getvalue())
        assert exit_code == 0
        for expected in (
            "State",
            "Issue",
            "Subtask",
            "Elapsed",
            "Branch",
            "monitor-cli",
            "claude/issue-133-monitor-cli",
            "workerは実行中",
            "worktrees/w1",
            "ログ末尾",
            "log-one",
            "log-two",
        ):
            assert expected in text, expected

    def test_tty_table_elapsed_matches_the_plain_report(
        self, tmp_path, _stub_get_issue_labels
    ):
        from orchestune.status_snapshot import (
            build_status_snapshot,
            format_status_report,
        )

        started = [NOW - 5, NOW - 754, NOW - 3661]
        snapshot = build_status_snapshot(
            tmp_path / "none.json", tmp_path, NOW, 0, {}, forge=_stub_get_issue_labels
        )
        for start in started:
            status = _worktree_status(start)
            snapshot.worktrees[:] = [status]
            elapsed = monitor.present_status(snapshot, NOW).rows[0][3]
            assert f"経過時間: {elapsed}" in format_status_report(snapshot, NOW)

    def test_tty_with_no_dispatch_says_so(self, tmp_path):
        out, stream = _output(tty=True)
        main(
            ["--run-state-path", str(tmp_path / "rs.json")],
            output=out,
            clock=lambda: NOW,
        )
        assert "現在アクティブなディスパッチはありません" in ANSI.sub(
            "", stream.getvalue()
        )

    def test_external_session_and_label_failure_are_shown(
        self, tmp_path, _stub_get_issue_labels
    ):
        _stub_get_issue_labels.get_issue_labels.side_effect = RuntimeError("gh")
        run_state_path = tmp_path / "run_state.json"
        active = _active(
            pid=None,
            external_id="ext-1",
            external_url="https://example.invalid/s/1",
            started_at=None,
        )
        save_run_state(RunState(active_worktrees={"133": active}), run_state_path)
        out, stream = _output(tty=True)

        main(
            ["--run-state-path", str(run_state_path), "--log-dir", str(tmp_path)],
            forge=_stub_get_issue_labels,
            output=out,
            clock=lambda: NOW,
        )

        text = ANSI.sub("", stream.getvalue())
        assert "ext-1" in text and "https://example.invalid/s/1" in text
        assert "ラベル取得失敗" in text
        assert "不明（自己修復により開始時刻を復元できません）" in text

    def test_control_characters_in_log_tail_are_not_emitted(
        self, tmp_path, _stub_get_issue_labels
    ):
        run_state_path = tmp_path / "run_state.json"
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "claude-issue-133-monitor-cli.log").write_text(
            "evil\x1b[2Jtext\x1b]0;title\x07\n"
        )
        save_run_state(RunState(active_worktrees={"133": _active()}), run_state_path)
        out, stream = _output(tty=True)

        main(
            ["--run-state-path", str(run_state_path), "--log-dir", str(log_dir)],
            forge=_stub_get_issue_labels,
            output=out,
            clock=lambda: NOW,
        )

        raw = stream.getvalue()
        assert "\x1b[2J" not in raw and "\x1b]0;" not in raw
        assert "eviltext" in ANSI.sub("", raw)


def _worktree_status(started_at: float):
    from orchestune.status_snapshot import MonitorState, WorktreeStatus

    return WorktreeStatus(
        issue_number=7,
        subtask_id="s",
        branch="claude/issue-7-s",
        pid=4_200_001,
        alive=True,
        started_at=started_at,
        elapsed_seconds=NOW - started_at,
        worktree_path="w",
        external_id=None,
        external_url=None,
        log_tail=[],
        state=MonitorState.RUNNING,
    )

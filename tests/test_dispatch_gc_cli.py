from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def test_gc_parser_defaults_to_apply_with_no_explicit_state():
    from orchestune.dispatch.gc_cli import _build_parser

    args = _build_parser().parse_args([])

    assert args.no_apply is False
    assert args.state is None
    assert args.timeout == 0.0


def test_gc_parser_accepts_preview_state_and_timeout():
    from orchestune.dispatch.gc_cli import _build_parser

    args = _build_parser().parse_args(
        ["--no-apply", "--state", "custom/run_state.json", "--timeout", "5"]
    )

    assert args.no_apply is True
    assert args.state == Path("custom/run_state.json")
    assert args.timeout == 5.0


@pytest.mark.parametrize("value", ["-1", "nan", "inf", "-inf"])
def test_gc_parser_rejects_negative_or_non_finite_timeout(value: str):
    from orchestune.dispatch.gc_cli import _build_parser

    with pytest.raises(SystemExit) as exc_info:
        _build_parser().parse_args(["--timeout", value])

    assert exc_info.value.code == 2


def test_gc_main_forwards_request_and_displays_preview(capsys):
    from orchestune.dispatch.gc_cli import main

    item = SimpleNamespace(
        issue_number=1037,
        result="done",
        action="would_release",
        reason="verified_merged",
        worktree_path=Path("/repo/worktrees/task"),
        worktree_action="remove",
    )
    result = SimpleNamespace(items=(item,), skipped=2, exit_code=0)
    with (
        patch(
            "sys.argv",
            [
                "orchestune",
                "--no-apply",
                "--state",
                "custom/run_state.json",
                "--timeout",
                "5",
            ],
        ),
        patch(
            "orchestune.dispatch.gc_cli.run_handoff_gc", return_value=result
        ) as run_gc,
    ):
        assert main() == 0

    request = run_gc.call_args.args[0]
    assert request.state_path == Path("custom/run_state.json")
    assert request.apply is False
    assert request.timeout_seconds == 5.0
    output = capsys.readouterr().out
    assert "1037" in output
    assert "would_release" in output
    assert "would_remove" in output
    assert "2" in output


class _TtyStream(io.StringIO):
    """A stand-in for an interactive UTF-8 terminal stream."""

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"

    def isatty(self) -> bool:
        return True


def _use_tty(monkeypatch: pytest.MonkeyPatch, columns: int) -> _TtyStream:
    stream = _TtyStream()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)
    for name in ("CI", "ORCHESTUNE_CLI_PLAIN", "NO_COLOR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLUMNS", str(columns))
    return stream


_WORKTREE = str(
    Path("/repo/worktrees/claude-issue-1037-long-task-name-for-wrapping-check")
)
_PREVIEW_ITEMS = (
    SimpleNamespace(
        issue_number=1037,
        result="done",
        action="would_release",
        reason="verified_merged",
        worktree_path=Path(_WORKTREE),
        worktree_action="remove",
    ),
)
_PLAIN_TABLE = (
    "ISSUE  RESULT      ACTION         REASON                    WORKTREE\n"
    "#1037  done        would_release  verified_merged           "
    f"would_remove: {_WORKTREE}\n"
)
_SUMMARY = "Summary: 1 would release, 0 held, 0 failed, 2 skipped\n"


def _run_preview(items=_PREVIEW_ITEMS) -> int:
    from orchestune.dispatch.gc_cli import main

    result = SimpleNamespace(items=items, skipped=2, exit_code=0)
    with (
        patch("sys.argv", ["orchestune", "--no-apply"]),
        patch("orchestune.dispatch.gc_cli.run_handoff_gc", return_value=result),
    ):
        return main()


def test_plain_preview_table_and_summary_are_unchanged(capsys) -> None:
    assert _run_preview() == 0
    assert capsys.readouterr().out == (
        "Preview only; no state, worktree, or GitHub changes were made.\n"
        + _PLAIN_TABLE
        + _SUMMARY
    )


def test_plain_empty_run_reports_no_reservations(capsys) -> None:
    assert _run_preview(items=()) == 0
    assert capsys.readouterr().out == (
        "Preview only; no state, worktree, or GitHub changes were made.\n"
        "No handoff-ready reservations found.\n"
        "Summary: 0 would release, 0 held, 0 failed, 2 skipped\n"
    )


def test_tty_wide_console_draws_a_table_with_every_value(monkeypatch) -> None:
    stream = _use_tty(monkeypatch, 200)
    assert _run_preview() == 0
    output = stream.getvalue()
    assert "╭" in output or "┌" in output
    for value in ("#1037", "done", "would_release", "verified_merged", _WORKTREE):
        assert value in output
    assert _SUMMARY in output


def test_tty_narrow_console_stacks_rows_without_truncating_values(monkeypatch) -> None:
    stream = _use_tty(monkeypatch, 40)
    assert _run_preview() == 0
    output = stream.getvalue()
    assert "╭" not in output
    assert "ISSUE: #1037" in output
    assert "RESULT: done" in output
    assert f"WORKTREE: would_remove: {_WORKTREE}" in output
    assert _SUMMARY in output

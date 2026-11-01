from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest

pytest_plugins = ["tests.test_local_claim_identity"]


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


class _ClosedTargetForge:
    """#1526: only the live Issue/PR state is needed for a closed-target release."""

    def __init__(self, state: str) -> None:
        self.state = state

    def get_issue_state(self, issue_number: int | str) -> str:
        return self.state


def _closed_target_factory(state: str):
    from orchestune.dispatch.gc.handoff import HandoffForge

    return lambda: cast("HandoffForge", _ClosedTargetForge(state))


def test_gc_preview_reports_closed_target_without_changing_state(
    local_claim, monkeypatch, capsys
):
    from functools import partial

    from orchestune.dispatch import gc_cli
    from orchestune.dispatch.gc_service import run_handoff_gc

    workspace, _, worktree = local_claim
    monkeypatch.chdir(workspace.repository_root)
    before = workspace.run_state_path.read_bytes()
    monkeypatch.setattr(
        gc_cli,
        "run_handoff_gc",
        partial(run_handoff_gc, forge_factory=_closed_target_factory("MERGED")),
    )

    assert gc_cli.main(["--no-apply"]) == 0

    output = capsys.readouterr().out
    assert "would_release" in output and "closed_target" in output
    assert "would_retain" in output
    assert "Summary: 1 would release, 0 held, 0 failed, 0 skipped" in output
    assert workspace.run_state_path.read_bytes() == before
    assert worktree.exists()


def test_gc_apply_releases_closed_target_and_retains_worktree(local_claim, monkeypatch):
    from orchestune.dispatch.gc.handoff import GcRequest
    from orchestune.dispatch.gc_service import run_handoff_gc
    from orchestune.ledger.run_state import claim_was_released, load_run_state_readonly
    from orchestune.worktree_ops.claim_marker import claim_marker_path

    workspace, active, worktree = local_claim
    monkeypatch.chdir(workspace.repository_root)

    result = run_handoff_gc(GcRequest(), forge_factory=_closed_target_factory("CLOSED"))

    assert result.exit_code == 0
    assert [
        (i.issue_number, i.action, i.reason, i.worktree_action) for i in result.items
    ] == [(7, "released", "closed_target", "retain")]
    state = load_run_state_readonly(workspace.run_state_path)
    assert "7" not in state.active_worktrees
    assert claim_was_released(state, 7, active.claim.claim_id)
    (receipt,) = state.recovery_receipts.values()
    assert receipt.reason == "closed-target:CLOSED"
    assert worktree.exists() and claim_marker_path(worktree).exists()


def test_gc_apply_keeps_open_target_claims(local_claim, monkeypatch):
    from orchestune.dispatch.gc.handoff import GcRequest
    from orchestune.dispatch.gc_service import run_handoff_gc

    workspace, _, _ = local_claim
    monkeypatch.chdir(workspace.repository_root)
    before = workspace.run_state_path.read_bytes()

    result = run_handoff_gc(GcRequest(), forge_factory=_closed_target_factory("OPEN"))

    assert result.exit_code == 0 and result.items == () and result.skipped == 1
    assert workspace.run_state_path.read_bytes() == before


def test_gc_holds_closed_target_when_forge_is_unavailable(local_claim, monkeypatch):
    from orchestune.dispatch.gc.handoff import GcRequest
    from orchestune.dispatch.gc_service import run_handoff_gc

    workspace, _, _ = local_claim
    monkeypatch.chdir(workspace.repository_root)

    def broken():
        raise RuntimeError("gh is not authenticated")

    result = run_handoff_gc(GcRequest(apply=False), forge_factory=broken)

    assert [(i.action, i.reason) for i in result.items] == [
        ("held", "forge_unavailable")
    ]

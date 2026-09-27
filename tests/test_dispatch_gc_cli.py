from __future__ import annotations

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

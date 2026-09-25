"""Command-line contracts for #1003 complete."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from orchestune.complete.contracts import CompleteResult


def test_dry_run_builds_done_request_without_calling_service() -> None:
    from orchestune.complete.cli import main

    with patch("orchestune.complete.cli.complete_task") as complete_task:
        exit_code = main(
            ["--issue", "1003", "--pr", "42", "--result", "done", "--no-apply"]
        )

    assert exit_code == 0
    complete_task.assert_called_once()
    request = complete_task.call_args.args[0]
    assert request.issue_number == 1003
    assert request.result == "done"
    assert request.dry_run is True


def test_blocked_requires_reason() -> None:
    from orchestune.complete.cli import main

    assert main(["--issue", "1003", "--result", "blocked", "--no-apply"]) != 0


def test_cli_returns_service_failure_exit_code() -> None:
    from orchestune.complete.cli import main
    from orchestune.complete.contracts import (
        CompleteFailure,
        CompleteFailureReason,
        CompleteStage,
    )

    failed = CompleteResult.failure_result(
        1003,
        "not-needed",
        CompleteStage.PREFLIGHT_VALIDATING,
        CompleteFailure(
            CompleteFailureReason.CLAIM_NOT_FOUND,
            "claim missing",
            issue_number=1003,
        ),
    )
    with patch("orchestune.complete.cli.complete_task", return_value=failed):
        exit_code = main(["--issue", "1003", "--result", "not-needed", "--no-apply"])

    assert failed.failure is not None
    assert exit_code == int(failed.failure.exit_code)


def test_help_keeps_argparse_success_exit_code() -> None:
    from orchestune.complete.cli import main

    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])

    assert excinfo.value.code == 0

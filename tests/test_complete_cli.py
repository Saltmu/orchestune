"""Command-line contracts for #1003 complete."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from orchestune.complete.contracts import CompleteResult


@pytest.mark.parametrize(
    "options", [[], ["--reviewer", "claude"], ["--reviewer", "codex"]]
)
def test_done_requires_reviewer_and_bot_judgment_file(options):
    from orchestune.complete.cli import main

    with patch("orchestune.complete.cli._credentials", return_value=(None, None, None)):
        assert (
            main(["--issue", "1029", "--pr", "42", "--result", "done", *options]) == 40
        )


def test_done_carries_reviewer_and_reply_path():
    from pathlib import Path

    from orchestune.complete.cli import main

    with (
        patch("orchestune.complete.cli._credentials", return_value=(None, None, None)),
        patch("orchestune.complete.cli.complete_task") as service,
    ):
        assert (
            main(
                [
                    "--issue",
                    "1029",
                    "--pr",
                    "42",
                    "--result",
                    "done",
                    "--reviewer",
                    "codex",
                    "--review-reply",
                    "reply.md",
                ]
            )
            == 0
        )
    assert service.call_args.args[0].payload.review_reply == Path("reply.md")
    assert service.call_args.args[0].payload.reviewer == "codex"


def test_dry_run_builds_done_request_without_calling_service() -> None:
    from orchestune.complete.cli import main

    with (
        patch("orchestune.complete.cli._credentials", return_value=(None, None, None)),
        patch("orchestune.complete.cli.complete_task") as complete_task,
    ):
        exit_code = main(
            [
                "--issue",
                "1003",
                "--pr",
                "42",
                "--result",
                "done",
                "--reviewer",
                "skip",
                "--no-apply",
            ]
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
    with (
        patch("orchestune.complete.cli._credentials", return_value=(None, None, None)),
        patch("orchestune.complete.cli.complete_task", return_value=failed),
    ):
        exit_code = main(["--issue", "1003", "--result", "not-needed", "--no-apply"])

    assert failed.failure is not None
    assert exit_code == int(failed.failure.exit_code)


def test_cli_displays_resumable_completion_id_and_reached_stage(capsys) -> None:
    from orchestune.complete.cli import main
    from orchestune.complete.contracts import (
        CompleteFailure,
        CompleteFailureReason,
        CompleteStage,
    )

    failed = CompleteResult.failure_result(
        1003,
        "done",
        CompleteStage.OUTCOME_POSTED,
        CompleteFailure(
            CompleteFailureReason.LABEL_CONFLICT,
            "A protected status label prevents completion.",
            issue_number=1003,
        ),
        completion_id="completion-1003",
    )
    with (
        patch("orchestune.complete.cli._credentials", return_value=(None, None, None)),
        patch("orchestune.complete.cli.complete_task", return_value=failed),
    ):
        exit_code = main(
            ["--issue", "1003", "--pr", "42", "--result", "done", "--reviewer", "skip"]
        )

    output = capsys.readouterr().out
    assert exit_code == 55
    assert "completion-1003" in output
    assert "outcome_posted" in output


def test_help_keeps_argparse_success_exit_code() -> None:
    from orchestune.complete.cli import main

    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])

    assert excinfo.value.code == 0


def test_cli_passes_explicit_replay_identity_without_credentials():
    from orchestune.complete.cli import main

    with (
        patch("orchestune.complete.cli._credentials", return_value=(None, None, None)),
        patch(
            "orchestune.complete.cli.complete_task",
            return_value=CompleteResult.success_result(1003, "not-needed"),
        ) as service,
    ):
        assert (
            main(
                [
                    "--issue",
                    "1003",
                    "--result",
                    "not-needed",
                    "--completion-id",
                    "completion-old",
                ]
            )
            == 0
        )
    assert service.call_args.args[0].completion_id == "completion-old"


def test_actual_cli_replays_saved_result_after_gc_without_token(
    tmp_path, monkeypatch, capsys
):
    from complete_lifecycle_test_support import lifecycle_environment

    from orchestune.complete.cli import main
    from orchestune.complete.journal import completion_journal_lock
    from orchestune.complete.service import complete_task
    from orchestune.ledger.run_state import load_run_state_readonly, save_run_state

    request, forge, _ = lifecycle_environment(tmp_path, monkeypatch)
    result = complete_task(request, forge=forge)
    with completion_journal_lock(request.state_path):
        state = load_run_state_readonly(request.state_path)
        state.active_worktrees.clear()
        save_run_state(state, request.state_path)
    monkeypatch.setattr(
        "orchestune.complete.cli._credentials",
        lambda *args: (None, None, request.state_path),
    )
    before = request.state_path.read_bytes()
    assert (
        main(
            [
                "--issue",
                "1110",
                "--result",
                "not-needed",
                "--completion-id",
                result.completion_id,
            ]
        )
        == 0
    )
    assert result.completion_id in capsys.readouterr().out
    assert request.state_path.read_bytes() == before


def test_cli_corrupt_state_preview_never_renames_or_backs_up(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from orchestune.complete.cli import main

    path = tmp_path / "run_state.json"
    path.write_text("{broken")
    monkeypatch.setattr(
        "orchestune.complete.cli.resolve_claim_workspace",
        lambda: SimpleNamespace(run_state_path=path),
    )
    before = {item.name: item.read_bytes() for item in tmp_path.iterdir()}
    assert main(["--issue", "1110", "--result", "not-needed", "--no-apply"]) != 0
    assert {item.name: item.read_bytes() for item in tmp_path.iterdir()} == before


def test_cli_reports_generated_completion_id_before_service_returns(capsys):
    from orchestune.complete.cli import main
    from orchestune.complete.contracts import CompleteStage

    def complete(request, *, on_progress):
        on_progress("completion-start", CompleteStage.RESERVED)
        assert "completion-start" in capsys.readouterr().out
        return CompleteResult.success_result(
            1003, "not-needed", completion_id="completion-start"
        )

    with (
        patch("orchestune.complete.cli._credentials", return_value=(None, None, None)),
        patch("orchestune.complete.cli.complete_task", side_effect=complete),
    ):
        assert main(["--issue", "1003", "--result", "not-needed"]) == 0

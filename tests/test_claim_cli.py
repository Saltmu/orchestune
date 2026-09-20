"""Tests for the public ``orchestune claim`` command boundary."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from orchestune.claim.contracts import (
    ClaimFailure,
    ClaimFailureReason,
    ClaimOutcome,
    ClaimStage,
    OwnerKind,
    ReservationKind,
)


def _success(*, owner_token: str = "owner-token-should-not-be-printed") -> ClaimOutcome:
    return ClaimOutcome(
        success=True,
        issue_number=123,
        claim_id="claim-123",
        branch="codex/issue-123-example",
        worktree_path=Path("/tmp/worktrees/claim-123"),
        base_ref="origin/main",
        owner_kind=OwnerKind.INTERACTIVE,
        reservation_kind=ReservationKind.FOOTPRINT,
        stage=ClaimStage.COMPLETED,
        owner_token=owner_token,
    )


def test_no_apply_passes_read_only_request_and_renders_preview(capsys):
    from orchestune.claim.cli import main

    with patch("orchestune.claim.cli.claim_task", return_value=_success()) as claim:
        assert (
            main(["123", "--no-apply", "--state", "state.json", "--timeout", "2.5"])
            == 0
        )

    request = claim.call_args.args[0]
    assert request.issue_number == 123
    assert request.dry_run is True
    assert request.state_path == Path("state.json")
    assert request.timeout_seconds == 2.5
    assert claim.call_args.kwargs["apply"] is False
    output = capsys.readouterr().out
    assert "Dry run" in output
    assert "Issue: #123" in output
    assert "Planned branch: codex/issue-123-example" in output
    assert "Unverified:" in output
    assert "owner-token-should-not-be-printed" not in output


def test_success_renders_claim_details_and_persists_token(tmp_path, capsys):
    from orchestune.claim.cli import main

    with (
        patch("orchestune.claim.cli.claim_task", return_value=_success()) as claim,
        patch("orchestune.claim.cli._token_directory", return_value=tmp_path),
    ):
        assert main(["123"]) == 0

    output = capsys.readouterr().out
    assert "Issue: #123" in output
    assert "Claim ID: claim-123" in output
    assert "Branch: codex/issue-123-example" in output
    assert "Worktree: /tmp/worktrees/claim-123" in output
    assert "Base: origin/main" in output
    assert "Owner kind: interactive" in output
    assert "owner-token-should-not-be-printed" not in output
    assert claim.call_args.kwargs["apply"] is True
    token_record = tmp_path / "claim-123.token"
    assert (
        token_record.read_text(encoding="utf-8")
        == "owner-token-should-not-be-printed\n"
    )
    assert token_record.stat().st_mode & 0o777 == 0o600


def test_failure_uses_reason_exit_code_and_recovery_diagnostic(capsys):
    from orchestune.claim.cli import main

    failure = ClaimFailure(
        reason=ClaimFailureReason.CLAIM_CONFLICT,
        message="Claim conflict with issue #99",
        conflicting_issue_number=99,
        conflicting_branch="codex/issue-99-active",
        conflicting_path=Path("/tmp/worktrees/active"),
        next_actions=("Resume the existing claim or select another issue.",),
    )
    outcome = ClaimOutcome(success=False, issue_number=123, failure=failure)
    with patch("orchestune.claim.cli.claim_task", return_value=outcome):
        assert main(["123"]) == 20

    stderr = capsys.readouterr().err
    assert "reason=claim_conflict" in stderr
    assert "conflicting_issue=#99" in stderr
    assert "conflicting_branch=codex/issue-99-active" in stderr
    assert "conflicting_path=/tmp/worktrees/active" in stderr
    assert "Next action: Resume the existing claim" in stderr


def test_unexpected_service_error_is_redacted_without_a_traceback(capsys):
    from orchestune.claim.cli import main

    with patch(
        "orchestune.claim.cli.claim_task",
        side_effect=RuntimeError("owner-token-should-not-be-printed"),
    ):
        assert main(["123", "--no-apply"]) == 1

    stderr = capsys.readouterr().err
    assert "reason=generic_error" in stderr
    assert "owner-token-should-not-be-printed" not in stderr


def test_resume_uses_protected_token_record(tmp_path, capsys):
    from orchestune.claim.cli import main

    token_record = tmp_path / "claim-123.token"
    token_record.write_text("stored-owner-token\n", encoding="utf-8")
    token_record.chmod(0o600)
    with (
        patch("orchestune.claim.cli._token_directory", return_value=tmp_path),
        patch("orchestune.claim.cli.resume_claim", return_value=_success()) as resume,
    ):
        assert main(["123", "--resume", "claim-123", "--timeout", "3"]) == 0

    assert resume.call_args.args == ("claim-123", "stored-owner-token")
    assert resume.call_args.kwargs["timeout_seconds"] == 3.0
    assert "stored-owner-token" not in capsys.readouterr().out

"""Real CLI diagnosis and explicit generation recovery."""

import json

import pytest

from orchestune.recovery.cli import main
from tests.cli_tty_support import patch_tty_stdout

pytest_plugins = ["tests.test_local_claim_identity"]


def test_preview_and_apply_require_generation_and_reason(
    local_claim, monkeypatch, capsys
):
    workspace, active, worktree = local_claim
    assert active.claim.claim_id
    monkeypatch.chdir(workspace.repository_root)
    before = workspace.run_state_path.read_bytes()
    assert main(["--issue", "7"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["diagnostics"]["claim_id"] == active.claim.claim_id
    assert "owner_token" not in json.dumps(preview)
    assert workspace.run_state_path.read_bytes() == before
    assert main(["--issue", "7", "--apply"]) == 43
    capsys.readouterr()
    assert workspace.run_state_path.read_bytes() == before
    assert (
        main(
            [
                "--issue",
                "7",
                "--claim-id",
                active.claim.claim_id,
                "--reason",
                "stopped",
                "--apply",
            ]
        )
        == 0
    )
    assert worktree.exists()


@pytest.mark.parametrize(
    "args",
    [
        ["--external-id", "run"],
        ["--launch-attempt-id", "attempt"],
        ["--confirm-external-stopped"],
        [
            "--confirm-external-stopped",
            "--claim-id",
            "claim",
            "--external-id",
            "run",
            "--reason",
            "  ",
        ],
        [
            "--confirm-external-stopped",
            "--restore-marker",
            "--claim-id",
            "claim",
            "--external-id",
            "run",
            "--reason",
            "stopped",
        ],
    ],
)
def test_external_argument_errors_are_detected_before_workspace(args):
    with pytest.raises(SystemExit) as error:
        main(["--issue", "7", *args])
    assert error.value.code == 2


def test_restart_cli_requires_operator_identity_and_reason(monkeypatch, capsys):
    from orchestune.recovery import cli
    from orchestune.recovery.contracts import RecoveryResult

    requests = []

    def capture(request):
        requests.append(request)
        return RecoveryResult(
            True, request.issue_number, "would_restart_blocked", "preview"
        )

    monkeypatch.setattr(cli, "recover_claim", capture)
    with pytest.raises(SystemExit):
        cli.main(["--issue", "7", "--restart-blocked"])
    capsys.readouterr()
    assert (
        cli.main(
            [
                "--issue",
                "7",
                "--restart-blocked",
                "--claim-id",
                "claim-old",
                "--reason",
                "resolved",
            ]
        )
        == 0
    )
    assert requests[0].restart_blocked and not requests[0].apply


def test_recovery_output_is_identical_on_tty(local_claim, monkeypatch, capsys):
    # #1409: the JSON default and the failure note keep one machine stream, so a
    # TTY must receive the same bytes as a pipe, with no Rich decoration.
    workspace, _active, _worktree = local_claim
    monkeypatch.chdir(workspace.repository_root)
    assert main(["--issue", "7"]) == 0
    preview_plain = capsys.readouterr().out
    assert main(["--issue", "7", "--apply"]) == 43
    failure_plain = capsys.readouterr().out
    assert failure_plain.rstrip("\n").splitlines()[-1].startswith("Next: ")

    stream = patch_tty_stdout(monkeypatch)
    assert main(["--issue", "7"]) == 0
    assert stream.getvalue() == preview_plain
    stream = patch_tty_stdout(monkeypatch)
    assert main(["--issue", "7", "--apply"]) == 43
    assert stream.getvalue() == failure_plain

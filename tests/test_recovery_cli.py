"""Real CLI diagnosis and explicit generation recovery."""

import json

import pytest

from orchestune.recovery.cli import main

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

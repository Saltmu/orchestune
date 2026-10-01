"""Real CLI diagnosis and explicit generation recovery."""

import json

from orchestune.recovery.cli import main

pytest_plugins = ["tests.test_local_claim_identity"]


def test_preview_and_apply_require_generation_and_reason(
    local_claim, monkeypatch, capsys
):
    workspace, active, worktree = local_claim
    assert active.claim_id
    monkeypatch.chdir(workspace.repository_root)
    before = workspace.run_state_path.read_bytes()
    assert main(["--issue", "7"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["diagnostics"]["claim_id"] == active.claim_id
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
                active.claim_id,
                "--reason",
                "stopped",
                "--apply",
            ]
        )
        == 0
    )
    assert worktree.exists()

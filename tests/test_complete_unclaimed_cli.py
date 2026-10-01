"""Real CLI argument/credential/service integration for unclaimed completion."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_complete_unclaimed import unclaimed as _unclaimed_fixture

from orchestune.complete import cli
from orchestune.complete.service import complete_task

unclaimed = _unclaimed_fixture


def _cli_environment(request, forge, monkeypatch):
    monkeypatch.chdir(request.worktree_root)
    workspace = SimpleNamespace(
        run_state_path=request.state_path, repository_identity="repo"
    )
    monkeypatch.setattr(cli, "resolve_claim_workspace", lambda: workspace)
    monkeypatch.setattr(cli, "caller_claim_id", lambda: None)
    monkeypatch.setattr(
        cli,
        "complete_task",
        lambda req, **kwargs: complete_task(req, forge=forge, **kwargs),
    )


def test_cli_displays_uuid_and_resumes_after_post_failure(
    unclaimed, monkeypatch, capsys
):
    request, forge = unclaimed
    _cli_environment(request, forge, monkeypatch)
    forge.inject = (
        lambda op, after: (_ for _ in ()).throw(OSError("offline"))
        if op == "post"
        else None
    )
    assert cli.main(["--issue", "1111", "--result", "not-needed"]) != 0
    out = capsys.readouterr().out
    completion_id = next(
        line.split(": ", 1)[1]
        for line in out.splitlines()
        if line.startswith("Completion ID:")
    )
    assert completion_id.startswith("completion-")
    assert "Reached stage: reserved" in out
    token, claim, state_path = cli._credentials(1111)
    assert token is None and claim is None and state_path == request.state_path
    forge.inject = lambda *_: None
    assert (
        cli.main(
            [
                "--issue",
                "1111",
                "--result",
                "not-needed",
                "--completion-id",
                completion_id,
            ]
        )
        == 0
    )
    assert "Reached stage: handed_off" in capsys.readouterr().out
    assert len(forge.comments) == 1
    for path in request.state_path.parent.glob(".orchestune/completion-tokens/*.token"):
        path.unlink()
    before = request.state_path.read_bytes()
    forge.inject = lambda *_: (_ for _ in ()).throw(
        AssertionError("receipt contacted Forge")
    )
    assert (
        cli.main(
            [
                "--issue",
                "1111",
                "--result",
                "not-needed",
                "--completion-id",
                completion_id,
            ]
        )
        == 0
    )
    assert request.state_path.read_bytes() == before


def test_cli_dry_run_never_generates_private_credentials(unclaimed, monkeypatch):
    request, forge = unclaimed
    _cli_environment(request, forge, monkeypatch)
    before = request.state_path.read_bytes()
    assert cli.main(["--issue", "1111", "--result", "not-needed", "--no-apply"]) == 0
    assert request.state_path.read_bytes() == before
    assert not (request.state_path.parent / ".orchestune").exists()


def test_pending_completion_resumes_without_token(unclaimed, monkeypatch):
    request, forge = unclaimed
    forge.inject = (
        lambda op, after: (_ for _ in ()).throw(OSError("offline"))
        if op == "post"
        else None
    )
    first = complete_task(request, forge=forge)
    assert first.completion_id
    for path in request.state_path.parent.glob(".orchestune/completion-tokens/*.token"):
        path.unlink()
    _cli_environment(request, forge, monkeypatch)
    forge.inject = lambda *_: None
    assert (
        cli.main(
            [
                "--issue",
                "1111",
                "--result",
                "not-needed",
                "--completion-id",
                first.completion_id,
            ]
        )
        == 0
    )
    assert len(forge.comments) == 1


def test_cli_foreign_completion_id_cannot_apply_to_another_issue(unclaimed):
    request, forge = unclaimed
    first = complete_task(request, forge=forge)
    assert first.success
    before = request.state_path.read_bytes()
    result = complete_task(
        replace(request, issue_number=1112, completion_id=first.completion_id),
        forge=forge,
    )
    assert not result.success and request.state_path.read_bytes() == before
    assert len(forge.comments) == 1


@pytest.mark.parametrize("boundary", ["add", "save"])
def test_cli_resumes_after_label_or_posting_evidence_save_failure(
    unclaimed, monkeypatch, capsys, boundary
):
    from orchestune.complete import journal

    request, forge = unclaimed
    _cli_environment(request, forge, monkeypatch)
    real_save = journal.save_run_state
    saves = 0

    def save(state, path):
        nonlocal saves
        saves += 1
        if boundary == "save" and saves == 2:
            raise OSError("posting evidence save failed")
        return real_save(state, path)

    monkeypatch.setattr(journal, "save_run_state", save)
    forge.inject = (
        lambda op, after: (_ for _ in ()).throw(OSError("offline"))
        if op == boundary and not after
        else None
    )
    assert cli.main(["--issue", "1111", "--result", "not-needed"]) != 0
    output = capsys.readouterr().out
    completion_id = next(
        line.split(": ", 1)[1]
        for line in output.splitlines()
        if line.startswith("Completion ID:")
    )
    expected_stage = "outcome_posted" if boundary == "add" else "reserved"
    assert f"Reached stage: {expected_stage}" in output
    assert len(forge.comments) == 1
    fixed_body = forge.comments[0]["body"]
    forge.inject = lambda *_: None
    assert (
        cli.main(
            [
                "--issue",
                "1111",
                "--result",
                "not-needed",
                "--completion-id",
                completion_id,
            ]
        )
        == 0
    )
    assert len(forge.comments) == 1 and forge.comments[0]["body"] == fixed_body
    assert forge.labels == {"status:not-needed", "priority:high"}

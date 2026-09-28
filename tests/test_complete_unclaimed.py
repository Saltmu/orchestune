"""Worktree-free completion reservations, recovery, and generation isolation."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from complete_lifecycle_test_support import PublicationForge

from orchestune.complete.contracts import CompleteFailureReason, CompleteRequest
from orchestune.complete.journal import completion_journal_lock
from orchestune.complete.service import complete_task
from orchestune.ledger.completion_reservations import dependency_completion_blocked
from orchestune.ledger.run_state import (
    RunState,
    load_run_state_readonly,
    save_run_state,
)


@pytest.fixture
def unclaimed(tmp_path, monkeypatch):
    path = tmp_path / "run_state.json"
    workspace = SimpleNamespace(run_state_path=path, repository_identity="repo")
    monkeypatch.setattr(
        "orchestune.complete.service.resolve_claim_workspace", lambda **_: workspace
    )
    with completion_journal_lock(path):
        save_run_state(RunState(), path)
    forge = PublicationForge()
    forge.labels = {"status:queued", "priority:high"}
    request = CompleteRequest.not_needed(1111, state_path=path, worktree_root=tmp_path)
    return request, forge


def test_unclaimed_publication_has_independent_reservation_and_pending_review(
    unclaimed,
):
    request, forge = unclaimed
    result = complete_task(request, forge=forge)
    assert result.success, result.failure
    assert result.completion_id != "unclaimed-not-needed-1111"
    state = load_run_state_readonly(request.state_path)
    assert not state.active_worktrees
    assert state.completion_reservations and state.completion_replay_receipts
    assert forge.labels == {"status:not-needed", "priority:high"}
    record = next(iter(state.completion_journal.values()))
    assert record["outcome_payload"]["body"] == forge.comments[0]["body"]
    assert record["generation_id"].startswith("unclaimed-")
    assert dependency_completion_blocked(state, 1111)
    assert (
        next(iter(record["downstream_policy_records"].values()))["status"] == "pending"
    )
    tokens = list(
        request.state_path.parent.glob(".orchestune/completion-tokens/*.token")
    )
    assert len(tokens) == 1
    assert tokens[0].stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("save_number", [1, 2, 3, 4])
@pytest.mark.parametrize("after", [False, True])
def test_save_windows_resume_with_same_id_and_body(
    unclaimed, monkeypatch, save_number, after
):
    from orchestune.complete import journal

    request, forge = unclaimed
    real_save = journal.save_run_state
    calls = 0

    def save(state, path):
        nonlocal calls
        calls += 1
        if calls == save_number and not after:
            raise OSError("before save")
        real_save(state, path)
        if calls == save_number and after:
            raise OSError("after save")

    monkeypatch.setattr(journal, "save_run_state", save)
    first = complete_task(request, forge=forge)
    assert not first.success
    assert first.completion_id
    monkeypatch.setattr(journal, "save_run_state", real_save)
    resumed = complete_task(
        replace(request, completion_id=first.completion_id), forge=forge
    )
    assert resumed.success, resumed.failure
    assert resumed.completion_id == first.completion_id
    assert len(forge.comments) == 1
    assert resumed.outcome_record.render() == forge.comments[0]["body"]


@pytest.mark.parametrize("operation", ["post", "add", "remove:status:queued", "get"])
@pytest.mark.parametrize("after", [False, True])
def test_remote_failure_windows_recover_without_duplicate(unclaimed, operation, after):
    request, forge = unclaimed
    fired = False

    def inject(current, phase):
        nonlocal fired
        if not fired and (current, phase) == (operation, after):
            fired = True
            raise OSError("network response lost")

    forge.inject = inject
    first = complete_task(request, forge=forge)
    assert fired
    forge.inject = lambda *_: None
    resumed = complete_task(
        replace(request, completion_id=first.completion_id), forge=forge
    )
    assert resumed.success, resumed.failure
    assert len(forge.comments) == 1
    assert forge.labels == {"status:not-needed", "priority:high"}


def test_explicit_receipt_is_readonly_without_owner_or_forge(unclaimed):
    request, forge = unclaimed
    first = complete_task(request, forge=forge)
    assert first.success
    for token in request.state_path.parent.glob(
        ".orchestune/completion-tokens/*.token"
    ):
        token.unlink()
    before = request.state_path.read_bytes()
    forge.inject = lambda *_: pytest.fail("Receipt replay must not call Forge")
    replay = complete_task(
        replace(request, completion_id=first.completion_id), forge=forge
    )
    assert replay.success and replay.outcome_record == first.outcome_record
    assert request.state_path.read_bytes() == before


def test_reopened_history_requires_explicit_new_uuid(unclaimed):
    request, forge = unclaimed
    first = complete_task(request, forge=forge)
    assert first.success
    forge.labels = {"status:queued"}
    ambiguous = complete_task(request, forge=forge)
    assert not ambiguous.success
    assert ambiguous.failure.reason == CompleteFailureReason.GENERATION_MISMATCH
    replay = complete_task(
        replace(request, completion_id=first.completion_id), forge=forge
    )
    assert replay.success and forge.labels == {"status:queued"}
    fresh = complete_task(
        replace(request, completion_id=f"completion-{uuid4().hex}"), forge=forge
    )
    assert fresh.success, fresh.failure
    assert fresh.completion_id != first.completion_id
    assert len(forge.comments) == 2


@pytest.mark.parametrize(
    "issue_state,labels",
    [
        ("CLOSED", set()),
        ("OPEN", {"status:blocked-human-review"}),
        ("OPEN", {"status:done"}),
        ("OPEN", {"status:external-lock"}),
        ("OPEN", {"status:not-needed"}),
    ],
)
def test_new_reservation_validates_issue_before_credentials(
    unclaimed, issue_state, labels
):
    request, forge = unclaimed
    forge.labels = labels
    forge.get_issue_state = lambda _: issue_state
    result = complete_task(request, forge=forge)
    assert not result.success
    assert not forge.comments
    assert not (request.state_path.parent / ".orchestune").exists()


def test_dry_run_creates_no_files_or_ids(unclaimed):
    request, forge = unclaimed
    before = {
        str(p): p.read_bytes()
        for p in request.state_path.parent.rglob("*")
        if p.is_file()
    }
    result = complete_task(replace(request, dry_run=True), forge=forge)
    assert result.success and result.preview and result.completion_id is None
    assert {
        str(p): p.read_bytes()
        for p in request.state_path.parent.rglob("*")
        if p.is_file()
    } == before
    assert not forge.comments and forge.labels == {"status:queued", "priority:high"}


def test_old_fixed_completion_id_is_rejected(unclaimed):
    request, forge = unclaimed
    result = complete_task(
        replace(request, completion_id="unclaimed-not-needed-1111"), forge=forge
    )
    assert not result.success and not forge.comments


def test_pending_resume_requires_matching_owner(unclaimed):
    request, forge = unclaimed
    forge.inject = (
        lambda operation, after: (_ for _ in ()).throw(OSError("post failed"))
        if operation == "post"
        else None
    )
    first = complete_task(request, forge=forge)
    assert first.completion_id
    forge.inject = lambda *_: None
    denied = complete_task(
        replace(request, completion_id=first.completion_id, owner_token="other-owner"),
        forge=forge,
    )
    assert not denied.success
    assert denied.failure.reason == CompleteFailureReason.OWNER_TOKEN_MISMATCH
    assert not forge.comments


def test_terminal_journal_without_receipt_is_not_a_new_generation(unclaimed):
    request, forge = unclaimed
    first = complete_task(request, forge=forge)
    assert first.success
    with completion_journal_lock(request.state_path):
        state = load_run_state_readonly(request.state_path)
        state.completion_replay_receipts.clear()
        save_run_state(state, request.state_path)
    forge.labels = {"status:queued"}
    before = request.state_path.read_bytes()
    denied = complete_task(
        replace(request, completion_id=f"completion-{uuid4().hex}"), forge=forge
    )
    assert not denied.success
    assert denied.failure.reason == CompleteFailureReason.INVALID_COMPLETION_STATE
    assert request.state_path.read_bytes() == before and len(forge.comments) == 1


def test_foreign_repository_cannot_resume_pending_reservation(unclaimed, monkeypatch):
    request, forge = unclaimed
    forge.inject = (
        lambda op, after: (_ for _ in ()).throw(OSError("offline"))
        if op == "post"
        else None
    )
    first = complete_task(request, forge=forge)
    assert first.completion_id
    monkeypatch.setattr(
        "orchestune.complete.service.resolve_claim_workspace",
        lambda **_: SimpleNamespace(
            run_state_path=request.state_path, repository_identity="another-repo"
        ),
    )
    forge.inject = lambda *_: None
    result = complete_task(
        replace(request, completion_id=first.completion_id), forge=forge
    )
    assert not result.success
    assert result.failure.reason == CompleteFailureReason.REPOSITORY_IDENTITY_MISMATCH
    assert not forge.comments


def test_old_fixed_comment_is_not_adopted_for_new_generation(unclaimed):
    from orchestune.outcome_record import OutcomeRecord

    request, forge = unclaimed
    legacy = OutcomeRecord(
        result="not-needed", issue=1111, completion_id="unclaimed-not-needed-1111"
    )
    forge.comments.append(
        {
            "id": 1,
            "html_url": "https://example.test/comments/1",
            "body": legacy.render(),
        }
    )
    result = complete_task(request, forge=forge)
    assert result.success, result.failure
    assert len(forge.comments) == 2
    assert result.outcome_record.render() == forge.comments[-1]["body"]


def test_claimed_history_does_not_choose_new_unclaimed_intent(tmp_path, monkeypatch):
    from complete_lifecycle_test_support import lifecycle_environment

    request, forge, _ = lifecycle_environment(tmp_path, monkeypatch)
    first = complete_task(request, forge=forge)
    assert first.success
    with completion_journal_lock(request.state_path):
        state = load_run_state_readonly(request.state_path)
        state.active_worktrees.clear()
        save_run_state(state, request.state_path)
    forge.labels = {"status:queued"}
    ambiguous = complete_task(
        replace(request, claim_id=None, owner_token=None), forge=forge
    )
    assert not ambiguous.success
    assert ambiguous.failure.reason == CompleteFailureReason.GENERATION_MISMATCH
    assert forge.labels == {"status:queued"} and len(forge.comments) == 1


def test_pending_id_from_other_issue_cannot_create_another_reservation(unclaimed):
    request, forge = unclaimed
    forge.inject = (
        lambda op, after: (_ for _ in ()).throw(OSError("offline"))
        if op == "post"
        else None
    )
    first = complete_task(request, forge=forge)
    assert first.completion_id
    before = request.state_path.read_bytes()
    token_path = next(
        request.state_path.parent.glob(".orchestune/completion-tokens/*.token")
    )
    token_before = token_path.read_bytes()
    forge.inject = lambda *_: None
    result = complete_task(
        replace(request, issue_number=1112, completion_id=first.completion_id),
        forge=forge,
    )
    assert not result.success
    assert result.failure.reason == CompleteFailureReason.GENERATION_MISMATCH
    assert request.state_path.read_bytes() == before
    assert token_path.read_bytes() == token_before and not forge.comments

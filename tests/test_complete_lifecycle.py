"""Label-confirmed publication and read-only replay regressions for #1110."""

from dataclasses import replace
from pathlib import Path

import pytest

from orchestune.complete.contracts import CompleteRequest, CompleteResult, CompleteStage
from orchestune.ledger import run_state


def test_explicit_completion_identity_is_preserved_by_factories():
    request = CompleteRequest.done(1110, 10, completion_id="completion-fixed")
    assert request.completion_id == "completion-fixed"
    assert (
        CompleteRequest.blocked(
            1110, "blocked", completion_id="completion-fixed"
        ).completion_id
        == "completion-fixed"
    )
    assert (
        CompleteRequest.not_needed(1110, completion_id="completion-fixed").completion_id
        == "completion-fixed"
    )


def test_label_confirmed_handoff_is_a_valid_success():
    result = CompleteResult.success_result(1110, "done", CompleteStage.HANDED_OFF)
    assert result.success and result.handed_off_to_gc


def test_readonly_corrupt_state_does_not_backup_or_rename(tmp_path: Path):
    path = tmp_path / "run_state.json"
    path.write_text("{broken")
    before = {item.name: item.read_bytes() for item in tmp_path.iterdir()}
    with pytest.raises(ValueError):
        run_state.load_run_state_readonly(path)
    assert {item.name: item.read_bytes() for item in tmp_path.iterdir()} == before


@pytest.mark.parametrize("outcome", ["done", "blocked", "not-needed"])
def test_publication_confirms_label_and_receipt_in_every_claimed_mode(
    tmp_path, monkeypatch, outcome
):
    from complete_lifecycle_test_support import lifecycle_environment

    from orchestune.complete.service import complete_task

    request, forge, check = lifecycle_environment(tmp_path, monkeypatch, outcome)
    result = complete_task(request, forge=forge)
    assert result.success, result.failure
    state = run_state.load_run_state_readonly(request.state_path)
    assert state.completion_replay_receipts
    assert forge.labels == {f"status:{outcome}"}
    assert len(forge.comments) == 1
    assert check.call_count == 2


@pytest.mark.parametrize("save_number", [1, 2, 3, 4])
@pytest.mark.parametrize("after", [False, True])
def test_failure_around_each_save_recovers_without_duplicate_post(
    tmp_path, monkeypatch, save_number, after
):
    from complete_lifecycle_test_support import lifecycle_environment

    from orchestune.complete import journal
    from orchestune.complete.service import complete_task

    request, forge, _ = lifecycle_environment(tmp_path, monkeypatch)
    real_save = journal.save_run_state
    calls = 0

    def save(state, path):
        nonlocal calls
        calls += 1
        if calls == save_number and not after:
            raise OSError("injected before save")
        real_save(state, path)
        if calls == save_number and after:
            raise OSError("injected after save")

    monkeypatch.setattr(journal, "save_run_state", save)
    first = complete_task(request, forge=forge)
    assert not first.success
    monkeypatch.setattr(journal, "save_run_state", real_save)
    recovered = complete_task(request, forge=forge)
    assert recovered.success, recovered.failure
    assert len(forge.comments) == 1
    assert recovered.outcome_record.render() == forge.comments[0]["body"]


@pytest.mark.parametrize(
    "operation",
    [
        "post",
        "add",
        "remove:status:in-progress",
        "remove:status:queued",
        "remove:status:blocked",
        "get",
    ],
)
@pytest.mark.parametrize("after", [False, True])
def test_remote_failure_windows_recover_fixed_payload(
    tmp_path, monkeypatch, operation, after
):
    from complete_lifecycle_test_support import lifecycle_environment

    from orchestune.complete.service import complete_task

    request, forge, _ = lifecycle_environment(tmp_path, monkeypatch)
    fired = False

    def inject(current, phase):
        nonlocal fired
        if not fired and current == operation and phase == after:
            fired = True
            raise OSError("injected remote failure")

    forge.inject = inject
    complete_task(request, forge=forge)
    forge.inject = lambda *_: None
    recovered = complete_task(request, forge=forge)
    assert recovered.success, recovered.failure
    assert len(forge.comments) == 1
    assert recovered.outcome_record.render() == forge.comments[0]["body"]
    assert forge.labels == {"status:not-needed"}


def test_simultaneous_identical_completes_publish_only_once(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from complete_lifecycle_test_support import lifecycle_environment

    from orchestune.complete.service import complete_task

    request, forge, check = lifecycle_environment(tmp_path, monkeypatch)
    barrier = Barrier(2)
    check.side_effect = (
        lambda *_: barrier.wait(timeout=5) if not check.call_count > 2 else None
    )
    with ThreadPoolExecutor(2) as executor:
        results = list(
            executor.map(lambda _: complete_task(request, forge=forge), range(2))
        )
    assert all(result.success for result in results), results
    assert results[0].completion_id == results[1].completion_id
    assert len(forge.comments) == 1


def test_replay_after_reclaim_and_requeue_has_no_remote_or_state_writes(
    tmp_path, monkeypatch
):
    from complete_lifecycle_test_support import lifecycle_environment

    from orchestune.complete.journal import completion_journal_lock
    from orchestune.complete.service import complete_task

    request, forge, check = lifecycle_environment(tmp_path, monkeypatch)
    first = complete_task(request, forge=forge)
    assert first.success
    with completion_journal_lock(request.state_path):
        state = run_state.load_run_state_readonly(request.state_path)
        state.active_worktrees["1110"].claim = replace(
            state.active_worktrees["1110"].claim, claim_id="claim-new"
        )
        run_state.save_run_state(state, request.state_path)
    forge.labels = {"status:queued"}
    before = request.state_path.read_bytes()
    forge.inject = lambda *_: pytest.fail("replay called Forge")
    check.reset_mock()
    replay = complete_task(
        replace(
            request, completion_id=first.completion_id, owner_token=None, claim_id=None
        ),
        forge=forge,
    )
    assert replay.success
    assert replay.outcome_record == first.outcome_record
    assert request.state_path.read_bytes() == before
    check.assert_not_called()


def test_protected_label_prevents_outcome_post_before_transition(tmp_path, monkeypatch):
    from complete_lifecycle_test_support import lifecycle_environment

    from orchestune.complete.service import complete_task

    request, forge, _ = lifecycle_environment(tmp_path, monkeypatch)
    forge.labels.add("status:blocked-human-review")
    result = complete_task(request, forge=forge)
    assert not result.success
    assert forge.comments == []
    assert "status:not-needed" not in forge.labels


def test_simultaneous_different_payloads_cannot_overwrite_reservation(
    tmp_path, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from complete_lifecycle_test_support import lifecycle_environment

    from orchestune.complete.service import complete_task

    request, forge, check = lifecycle_environment(tmp_path, monkeypatch, "blocked")
    other = CompleteRequest.blocked(
        1110,
        "other-reason",
        owner_token=request.owner_token,
        claim_id=request.claim_id,
        state_path=request.state_path,
        worktree_root=request.worktree_root,
    )
    barrier = Barrier(2)
    check.side_effect = (
        lambda *_: barrier.wait(timeout=5) if not check.call_count > 2 else None
    )
    with ThreadPoolExecutor(2) as executor:
        results = list(
            executor.map(lambda req: complete_task(req, forge=forge), [request, other])
        )
    assert sum(result.success for result in results) == 1
    assert len(forge.comments) == 1


@pytest.mark.parametrize("drift", ["HEAD", "CI", "PR"])
def test_pending_resume_rejects_changed_validation_inputs(tmp_path, monkeypatch, drift):
    from types import SimpleNamespace

    from complete_lifecycle_test_support import lifecycle_environment

    from orchestune.complete.service import complete_task

    request, forge, _ = lifecycle_environment(tmp_path, monkeypatch, "done")
    forge.inject = (
        lambda operation, after: (_ for _ in ()).throw(OSError("before POST"))
        if operation == "post" and not after
        else None
    )
    first = complete_task(request, forge=forge)
    assert not first.success
    forge.inject = lambda *_: None
    if drift == "HEAD":
        monkeypatch.setattr("orchestune.complete.service._head_sha", lambda _: "z" * 40)
    elif drift == "CI":
        monkeypatch.setattr(
            "orchestune.complete.service.validate_ci_evidence",
            lambda _: SimpleNamespace(to_dict=lambda: {"definition": "changed"}),
        )
    else:
        forge.pr = replace(forge.pr, changed_files=("different.py",))
    resumed = complete_task(request, forge=forge)
    assert not resumed.success
    assert forge.comments == []


def test_completion_forge_bounds_text_and_body_subprocesses(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from orchestune.forge import GitHubForge

    process = Mock(return_value=SimpleNamespace(stdout=b"ok", stderr=b"", returncode=0))
    monkeypatch.setattr("orchestune.forge.subprocess.run", process)
    forge = GitHubForge(timeout_seconds=30)
    forge._run(["gh", "api", "test"])
    assert process.call_args.kwargs["timeout"] == 30
    forge._run(["gh", "api", "test"], input_text="fixed body")
    assert process.call_args.kwargs["timeout"] == 30


@pytest.mark.parametrize("read_number", [1, 2])
def test_failure_at_final_label_readback_resumes_before_handoff(
    tmp_path, monkeypatch, read_number
):
    from complete_lifecycle_test_support import lifecycle_environment

    from orchestune.complete.service import complete_task

    request, forge, _ = lifecycle_environment(tmp_path, monkeypatch)
    count = 0

    def inject(operation, after):
        nonlocal count
        if operation == "get" and not after and forge.labels == {"status:not-needed"}:
            count += 1
            if count == read_number:
                raise OSError("final readback unavailable")

    forge.inject = inject
    first = complete_task(request, forge=forge)
    assert not first.success
    assert first.stage is CompleteStage.OUTCOME_POSTED
    state = run_state.load_run_state_readonly(request.state_path)
    assert not state.completion_replay_receipts
    forge.inject = lambda *_: None
    assert complete_task(request, forge=forge).success
    assert len(forge.comments) == 1


def test_no_apply_keeps_state_remote_and_ci_unchanged(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from complete_lifecycle_test_support import lifecycle_environment

    from orchestune.complete.service import complete_task

    request, forge, _ = lifecycle_environment(tmp_path, monkeypatch, "done")
    ci = Mock(side_effect=AssertionError("preview executed CI"))
    monkeypatch.setattr("orchestune.complete.service.run_local_ci_if_needed", ci)
    before = request.state_path.read_bytes()
    result = complete_task(replace(request, dry_run=True), forge=forge)
    assert result.preview and result.success
    assert request.state_path.read_bytes() == before
    assert forge.operations == [] and forge.comments == []
    ci.assert_not_called()


def test_request_keeps_existing_positional_claim_identity():
    request = CompleteRequest(1110, "not-needed", "token", "claim-1110")
    assert request.claim_id == "claim-1110"
    assert request.completion_id is None


@pytest.mark.parametrize("mismatch", ["repository", "worktree"])
def test_new_completion_rejects_foreign_claim_context_before_publication(
    tmp_path, monkeypatch, mismatch
):
    from complete_lifecycle_test_support import lifecycle_environment

    from orchestune.complete.journal import completion_journal_lock
    from orchestune.complete.service import complete_task

    request, forge, _ = lifecycle_environment(tmp_path, monkeypatch)
    if mismatch == "repository":
        with completion_journal_lock(request.state_path):
            state = run_state.load_run_state_readonly(request.state_path)
            state.active_worktrees["1110"].claim = replace(
                state.active_worktrees["1110"].claim, repository_id="foreign-repo"
            )
            run_state.save_run_state(state, request.state_path)
    else:
        request = replace(request, worktree_root=tmp_path / "different-checkout")
    before = request.state_path.read_bytes()
    result = complete_task(request, forge=forge)
    assert not result.success
    assert forge.comments == [] and forge.operations == []
    assert request.state_path.read_bytes() == before


def test_claim_disappearing_before_locked_publication_returns_claim_not_found(
    tmp_path, monkeypatch
):
    from complete_lifecycle_test_support import lifecycle_environment

    from orchestune.complete.journal import completion_journal_lock
    from orchestune.complete.service import complete_task

    request, forge, _ = lifecycle_environment(tmp_path, monkeypatch)

    def release_claim(*_):
        with completion_journal_lock(request.state_path):
            state = run_state.load_run_state_readonly(request.state_path)
            state.active_worktrees.clear()
            run_state.save_run_state(state, request.state_path)
        return {"decision": "allowed"}

    monkeypatch.setattr(
        "orchestune.complete.service._policy_for_request", release_claim
    )
    result = complete_task(request, forge=forge)
    assert not result.success
    assert result.failure.reason.value == "claim_not_found"
    assert not forge.comments
    assert not run_state.load_run_state_readonly(request.state_path).completion_journal


@pytest.mark.parametrize("query", ["labels", "state", "publication_labels"])
def test_issue_evidence_failure_has_remote_failure_reason(tmp_path, monkeypatch, query):
    from complete_lifecycle_test_support import lifecycle_environment

    from orchestune.complete.service import complete_task

    request, forge, _ = lifecycle_environment(tmp_path, monkeypatch)
    calls = 0

    def unavailable(*_):
        nonlocal calls
        calls += 1
        if query == "publication_labels" and calls == 1:
            return tuple(forge.labels)
        raise OSError("remote query unavailable")

    monkeypatch.setattr(
        forge,
        "get_issue_state" if query == "state" else "get_issue_labels",
        unavailable,
    )
    result = complete_task(request, forge=forge)
    assert not result.success
    assert result.failure.reason.value == (
        "label_state_unknown" if query == "publication_labels" else "evidence_missing"
    )
    assert not forge.comments

"""Completion identity and immutable receipt lookup."""

from dataclasses import replace

from orchestune.complete.contracts import CompleteRequest
from orchestune.complete.replay import find_replay
from orchestune.ledger.run_state import RunState


def test_unknown_explicit_id_never_selects_latest_claim():
    request = CompleteRequest.blocked(1110, "blocked", completion_id="unknown")
    assert find_replay(request, RunState(), "repo") is None


def test_legacy_done_replay_does_not_acquire_or_attach_read_evidence(
    tmp_path, monkeypatch
):
    from complete_lifecycle_test_support import lifecycle_environment
    from read_dependency_test_support import evidence

    from orchestune.complete.service import complete_task

    request, forge, _ = lifecycle_environment(tmp_path, monkeypatch, "done")
    before = request.request_fingerprint
    assert before == "25c8d20011f9e374acf285dc556414c37e7a05eccbd58dd2fb25989f352a09cf"
    completed = complete_task(request, forge=forge)
    assert completed.success
    assert completed.outcome_record.read_evidence is None
    ledger = request.state_path.read_bytes()

    def unavailable(*args):
        raise AssertionError("Replay must not reacquire remote evidence")

    forge.list_all_issue_comments = unavailable
    forge.get_pull_request = unavailable
    replayed = complete_task(request, forge=forge)
    assert replayed.success
    assert replayed.outcome_record == completed.outcome_record
    assert request.request_fingerprint == before
    attached = replace(
        request, payload=replace(request.payload, read_evidence=evidence(declared=()))
    )
    rejected = complete_task(attached, forge=forge)
    assert not rejected.success
    assert rejected.failure.reason.value == "request_fingerprint_mismatch"
    assert request.state_path.read_bytes() == ledger

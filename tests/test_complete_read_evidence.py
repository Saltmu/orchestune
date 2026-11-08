"""Completion request contract tests for parsed stale-read evidence."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from read_dependency_test_support import (
    BLOB_V1,
    BLOB_V2,
    SHA_OTHER,
    observation,
)
from read_dependency_test_support import (
    evidence as read_evidence,
)

from orchestune.complete import CompleteRequest, CompletionEvidence, DonePayload
from orchestune.complete import service as complete_service
from orchestune.outcome_record import RESULT_DONE


def _read_evidence(*, blob=BLOB_V1, head_sha="b" * 40, claim_id="claim-reader"):
    return read_evidence(
        declared=("src/registry.py",),
        observations=(observation("src/registry.py", blob),),
        head_sha=head_sha,
        claim_id=claim_id,
    )


def test_done_read_evidence_flows_to_outcome_record() -> None:
    evidence = _read_evidence()
    from_evidence = CompleteRequest.done(
        1663, 2001, evidence=CompletionEvidence(read_evidence=evidence)
    )
    direct = CompleteRequest.done(1663, 2001, read_evidence=evidence)

    assert isinstance(from_evidence.payload, DonePayload)
    assert isinstance(direct.payload, DonePayload)
    assert from_evidence.payload.read_evidence == evidence
    assert direct.payload.read_evidence == evidence
    assert direct.to_outcome_record().read_evidence == evidence


def test_read_evidence_rejects_invalid_values_and_non_done_use() -> None:
    invalid = object()
    with pytest.raises(TypeError, match="read_evidence must be ReadEvidenceV1"):
        CompletionEvidence(read_evidence=invalid)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="read_evidence must be ReadEvidenceV1"):
        DonePayload(pr=2001, read_evidence=invalid)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="read_evidence is only valid for done"):
        CompleteRequest.blocked(
            1663, "blocked", evidence=CompletionEvidence(read_evidence=_read_evidence())
        )

    payload = DonePayload(pr=2001)
    object.__setattr__(payload, "read_evidence", invalid)
    request = CompleteRequest(issue_number=1663, result=RESULT_DONE, payload=payload)
    with pytest.raises(
        ValueError, match="Done request read_evidence must be ReadEvidenceV1"
    ):
        request.validate()


def test_request_fingerprint_includes_canonical_read_evidence() -> None:
    fingerprints = [
        CompleteRequest.done(1663, 2001, read_evidence=value).request_fingerprint
        for value in (
            _read_evidence(),
            _read_evidence(),
            _read_evidence(blob=BLOB_V2),
            _read_evidence(head_sha=SHA_OTHER),
            _read_evidence(claim_id="claim-other"),
        )
    ]

    assert fingerprints[0] == fingerprints[1]
    assert len(set(fingerprints)) == 4


@pytest.fixture
def completion_reads(tmp_path, monkeypatch):
    from complete_lifecycle_test_support import lifecycle_environment

    from orchestune.claim.read_checkpoint import checkpoint_marker
    from orchestune.infra.git_cli import run_git
    from orchestune.read_dependencies.contracts import (
        CheckpointAvailability,
        ReadCheckpointV1,
        ReadCoverage,
        ReadEvidenceV1,
        ReadSource,
    )
    from orchestune.read_dependencies.git import lookup_resource

    request, forge, _ = lifecycle_environment(tmp_path, monkeypatch, "done")
    run_git(["init"], cwd=tmp_path)
    run_git(["config", "user.email", "test@example.test"], cwd=tmp_path)
    run_git(["config", "user.name", "Test"], cwd=tmp_path)
    (tmp_path / "input.py").write_text("original\n", encoding="utf-8")
    run_git(["add", "input.py"], cwd=tmp_path)
    run_git(["commit", "-m", "input"], cwd=tmp_path)
    base = run_git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()
    observed = lookup_resource(base, "input.py", cwd=tmp_path)
    (tmp_path / "input.py").write_text("changed\n", encoding="utf-8")
    run_git(["commit", "-am", "implementation"], cwd=tmp_path)
    head = run_git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()
    forge.pr = replace(forge.pr, head_sha=head)
    forge.get_repository_slug = lambda: "Saltmu/orchestune"
    checkpoint = ReadCheckpointV1(
        repository="Saltmu/orchestune",
        issue_number=1110,
        claim_id="claim-1110",
        availability=CheckpointAvailability.RECORDED,
        input_base_sha=base,
    )
    forge.comments.append({"body": checkpoint_marker(checkpoint)})
    monkeypatch.setattr(complete_service, "_head_sha", lambda _: head)
    ci = SimpleNamespace(to_dict=lambda: {"head_sha": head, "definition": "fixed"})
    monkeypatch.setattr(complete_service, "run_local_ci_if_needed", lambda _: ci)
    monkeypatch.setattr(complete_service, "validate_ci_evidence", lambda _: ci)
    value = ReadEvidenceV1(
        repository="Saltmu/orchestune",
        issue_number=1110,
        claim_id="claim-1110",
        input_base_sha=base,
        head_sha=head,
        declared_reads=("input.py",),
        observations=(replace(observed, source=ReadSource.AGENT_REPORTED),),
        source=ReadSource.AGENT_REPORTED,
        coverage=ReadCoverage.COMPLETE,
    )
    return replace(
        request, payload=replace(request.payload, read_evidence=value)
    ), forge


def test_complete_validates_and_freezes_original_read_snapshot(completion_reads):
    from orchestune.complete.service import complete_task
    from orchestune.ledger.run_state import load_run_state_readonly

    request, forge = completion_reads
    result = complete_task(request, forge=forge)
    assert result.success
    saved = result.outcome_record.read_evidence
    assert saved.digest is not None
    assert saved.observations == request.payload.read_evidence.observations
    assert saved.head_sha == result.outcome_record.head_sha
    assert saved.head_sha == result.outcome_record.review.reviewed_head_sha
    state = load_run_state_readonly(request.state_path)
    record = next(iter(state.completion_journal.values()))
    assert (
        record.prepublication_policy_evidence["validation"]["read_evidence_digest"]
        == saved.digest
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "repository",
        "issue_number",
        "claim_id",
        "head_sha",
        "input_base_sha",
        "blob_oid",
        "file_mode",
        "absent",
        "snapshot_commit",
        "digest",
        "missing_checkpoint",
        "unavailable_checkpoint",
        "conflicting_checkpoint",
    ],
)
def test_complete_rejects_unbound_or_forged_reads(completion_reads, mutation):
    from orchestune.complete.service import complete_task
    from orchestune.read_dependencies.contracts import ResourcePresence

    request, forge = completion_reads
    value = request.payload.read_evidence
    if mutation == "repository":
        value = replace(value, repository="other/repo")
    elif mutation == "issue_number":
        value = replace(value, issue_number=999)
    elif mutation == "claim_id":
        value = replace(value, claim_id="claim-other")
    elif mutation in {"head_sha", "input_base_sha"}:
        value = replace(value, **{mutation: "c" * 40})
    elif mutation in {"blob_oid", "file_mode", "snapshot_commit", "absent"}:
        changes = {
            "blob_oid": "c" * 40,
            "file_mode": "100755",
            "snapshot_commit": "c" * 40,
        }
        delta = (
            {"presence": ResourcePresence.ABSENT, "blob_oid": None, "file_mode": None}
            if mutation == "absent"
            else {mutation: changes[mutation]}
        )
        value = replace(value, observations=(replace(value.observations[0], **delta),))
    elif mutation == "digest":
        value = replace(value, digest="0" * 64)
    elif mutation == "missing_checkpoint":
        forge.comments.clear()
    elif mutation == "unavailable_checkpoint":
        forge.comments[0]["body"] = (
            forge.comments[0]["body"]
            .replace('"recorded"', '"unavailable"')
            .replace('"' + value.input_base_sha + '"', "null")
        )
    else:
        forge.comments.append(
            {"body": forge.comments[0]["body"].replace(value.input_base_sha, "c" * 40)}
        )
    request = replace(request, payload=replace(request.payload, read_evidence=value))
    result = complete_task(request, forge=forge)
    assert not result.success
    assert "post" not in forge.operations


def test_absent_and_unknown_observations_keep_their_meaning(completion_reads):
    from orchestune.complete.service import complete_task
    from orchestune.read_dependencies.contracts import ResourcePresence
    from orchestune.read_dependencies.git import lookup_resource

    request, forge = completion_reads
    value = request.payload.read_evidence
    absent = lookup_resource(
        value.input_base_sha, "missing.py", cwd=request.worktree_root
    )
    unknown = replace(
        absent,
        path="unknown.py",
        presence=ResourcePresence.UNKNOWN,
        snapshot_commit=None,
    )
    value = replace(value, observations=(absent, unknown))
    result = complete_task(
        replace(request, payload=replace(request.payload, read_evidence=value)),
        forge=forge,
    )
    assert result.success
    assert result.outcome_record.read_evidence.observations == value.observations


def test_missing_input_commit_rejected_even_without_observations(completion_reads):
    from orchestune.complete.service import complete_task

    request, forge = completion_reads
    value = replace(
        request.payload.read_evidence,
        input_base_sha="c" * 40,
        observations=(),
        declared_reads=(),
    )
    forge.comments[0]["body"] = forge.comments[0]["body"].replace(
        request.payload.read_evidence.input_base_sha, "c" * 40
    )
    result = complete_task(
        replace(request, payload=replace(request.payload, read_evidence=value)),
        forge=forge,
    )
    assert not result.success


def test_push_during_read_acquisition_is_rejected(completion_reads):
    from orchestune.complete.service import complete_task

    request, forge = completion_reads
    original = forge.list_all_issue_comments

    def pushed(issue):
        comments = original(issue)
        if issue == request.issue_number:
            forge.pr = replace(forge.pr, head_sha="c" * 40)
        return comments

    forge.list_all_issue_comments = pushed
    result = complete_task(request, forge=forge)
    assert not result.success
    assert "post" not in forge.operations


def test_reserved_reads_reject_changes_and_resume_fixed_payload(completion_reads):
    from orchestune.complete.service import complete_task
    from orchestune.ledger.run_state import load_run_state_readonly

    request, forge = completion_reads

    def interrupt(operation, after):
        if operation == "post" and not after:
            raise OSError("interrupted before posting")

    forge.inject = interrupt
    assert not complete_task(request, forge=forge).success
    state = load_run_state_readonly(request.state_path)
    reserved = next(iter(state.completion_journal.values()))
    body = reserved.outcome_payload["body"]
    changed = replace(request.payload.read_evidence, declared_reads=())
    forge.inject = lambda *args: None
    rejected = complete_task(
        replace(request, payload=replace(request.payload, read_evidence=changed)),
        forge=forge,
    )
    assert not rejected.success
    resumed = complete_task(request, forge=forge)
    assert resumed.success
    assert any(body in comment["body"] for comment in forge.comments)
    assert complete_task(request, forge=forge).outcome_record == resumed.outcome_record


def test_reserved_reads_reject_local_head_change(completion_reads, monkeypatch):
    from orchestune.complete.service import complete_task

    request, forge = completion_reads

    def interrupt(operation, after):
        if operation == "post" and not after:
            raise OSError("interrupted before posting")

    forge.inject = interrupt
    assert not complete_task(request, forge=forge).success
    forge.inject = lambda *args: None
    monkeypatch.setattr(complete_service, "_head_sha", lambda _: "c" * 40)
    result = complete_task(request, forge=forge)
    assert not result.success
    assert result.failure.reason.value == "request_fingerprint_mismatch"


def test_read_evidence_replay_never_reacquires_remote_state(completion_reads):
    from orchestune.complete.service import complete_task

    request, forge = completion_reads
    completed = complete_task(request, forge=forge)
    assert completed.success
    ledger = request.state_path.read_bytes()

    def unavailable(*args):
        raise AssertionError("Replay cannot reacquire evidence")

    forge.list_all_issue_comments = unavailable
    forge.get_repository_slug = unavailable
    replayed = complete_task(request, forge=forge)
    assert replayed.success
    assert replayed.outcome_record == completed.outcome_record
    assert request.state_path.read_bytes() == ledger

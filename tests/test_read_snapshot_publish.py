"""Atomic parent/snapshot publication and recovery against a real bare remote."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from orchestune.claim.read_checkpoint import checkpoint_marker
from orchestune.infra.git_cli import run_git
from orchestune.integrator import attempt, read_publish, semantic_claim
from orchestune.integrator.attempt import publish_parent_under_claim
from orchestune.integrator.read_context import ReadContextResult
from orchestune.integrator.read_gate import make_default_read_context_builder
from orchestune.integrator.read_publish import prepare_parent_read_publication
from orchestune.integrator.semantic_claim import (
    ClaimResult,
    ClaimStatus,
    PushResult,
    PushStatus,
    acquire_semantic_claim,
)
from orchestune.outcome_record import OutcomeRecord, ReviewSummary
from orchestune.read_dependencies.contracts import (
    CheckpointAvailability,
    ParentReadSnapshotV1,
    ReadCheckpointV1,
    ReadContextInputs,
    ReadCoverage,
    ReaderIdentity,
    ReaderInput,
    ReadEvidenceV1,
    ReadSource,
    UnknownReason,
    parent_read_snapshot_ref,
    seal_evidence,
    seal_snapshot,
)
from orchestune.read_dependencies.detection import detect_stale_reads
from orchestune.read_dependencies.storage import read_snapshot

PARENT_REF = "refs/heads/parent/issue-1230"
SNAPSHOT_REF = parent_read_snapshot_ref(1230)


def git(cwd: Path, *args: str) -> str:
    return run_git(list(args), cwd=cwd).stdout.strip()


def commit(cwd: Path, content: str) -> str:
    (cwd / "input.txt").write_text(content, encoding="utf-8")
    git(cwd, "add", "input.txt")
    git(cwd, "commit", "-m", content)
    return git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    remote, local = tmp_path / "origin.git", tmp_path / "local"
    git(tmp_path, "init", "--bare", str(remote))
    git(tmp_path, "init", "-b", "main", str(local))
    git(local, "remote", "add", "origin", str(remote))
    base = commit(local, "parent")
    git(local, "push", "origin", f"HEAD:{PARENT_REF}")
    head = commit(local, "candidate")
    evidence = seal_evidence(
        ReadEvidenceV1(
            repository="owner/repo",
            issue_number=10,
            claim_id="claim-reader",
            input_base_sha=base,
            head_sha=head,
            declared_reads=(),
            source=ReadSource.ADAPTER_OBSERVED,
            coverage=ReadCoverage.COMPLETE,
        )
    )
    inputs = ReadContextInputs(
        repository="owner/repo",
        parent_issue_number=1230,
        parent_sha=base,
        candidate_head_sha=head,
        readers=(
            ReaderInput(
                identity=ReaderIdentity(
                    repository="owner/repo",
                    issue_number=10,
                    claim_id="claim-reader",
                    head_sha=head,
                ),
                evidence=evidence,
            ),
        ),
        writers=(),
    )
    built = ReadContextResult(
        inputs=inputs,
        snapshot=seal_snapshot(
            ParentReadSnapshotV1(
                repository="owner/repo",
                parent_issue_number=1230,
                parent_head_sha=base,
            )
        ),
        bootstrap=True,
        coverage=ReadCoverage.COMPLETE,
    )
    report = detect_stale_reads(inputs)
    outcome = OutcomeRecord(
        issue=10,
        result="done",
        claim_id="claim-reader",
        completion_id="completion-reader",
        head_sha=head,
        review=ReviewSummary(reviewed_head_sha=head),
        read_evidence=evidence,
    )
    forge = Mock()
    forge.get_repository_slug.return_value = "owner/repo"
    forge.get_authenticated_user.return_value = "holder"
    forge.list_all_issue_comments.return_value = [
        {"body": outcome.render(), "user": {"login": "holder"}}
    ]
    state = SimpleNamespace(
        head_sha=head,
        base_sha=base,
        read_report_digest=report.digest,
        reviewed_read_report_digest=report.digest,
    )
    ctx = SimpleNamespace(
        repository_root=local,
        forge=forge,
        semantic_review_state=state,
        config=SimpleNamespace(stale_read_gate="required"),
        read_report=report,
        attempt_identity=SimpleNamespace(
            parent_issue_number=1230,
            attempt_id="attempt-1230",
            child_sources=(SimpleNamespace(issue_number=10, source_sha=head),),
        ),
    )
    monkeypatch.setattr(attempt, "validate_finalization_context", lambda _: None)
    monkeypatch.setattr(
        read_publish, "make_default_read_context_builder", lambda *_: lambda *_: built
    )
    return SimpleNamespace(
        ctx=ctx, local=local, remote=remote, base=base, head=head, built=built
    )


def refs(value: SimpleNamespace) -> tuple[str, str]:
    return (
        git(value.remote, "for-each-ref", "--format=%(objectname)", PARENT_REF),
        git(value.remote, "for-each-ref", "--format=%(objectname)", SNAPSHOT_REF),
    )


def test_parent_and_snapshot_publish_together_and_retain_objects(publication):
    p = publication
    result = publish_parent_under_claim(p.ctx)
    assert result.pushed, result.detail
    snapshot = read_snapshot("owner/repo", 1230, p.head, cwd=p.local)
    assert snapshot.readers[0].source_sha == p.head
    checkpoint = snapshot.readers[0].validations[-1]
    assert checkpoint.candidate_head_sha == p.head
    assert checkpoint.report_digest == p.ctx.read_report.digest
    oid = refs(p)[1]
    for commit_oid in (p.base, p.head):
        git(p.remote, "merge-base", "--is-ancestor", commit_oid, oid)


def test_restart_adopts_published_pair_without_another_push(publication):
    p = publication
    assert publish_parent_under_claim(p.ctx).pushed
    with patch.object(attempt, "push_refs_under_claim") as push:
        assert publish_parent_under_claim(p.ctx).pushed
    push.assert_not_called()


@pytest.mark.parametrize("ref", [PARENT_REF, SNAPSHOT_REF])
def test_cas_race_never_advances_only_one_ref(publication, ref):
    p = publication
    original = semantic_claim.push_refs_under_claim
    other = commit(p.local, "concurrent change")

    def race(root, claim, updates, *, now):
        git(p.local, "push", "origin", f"{other}:{ref}")
        return original(root, claim, updates, now=now)

    with patch.object(attempt, "push_refs_under_claim", side_effect=race):
        result = publish_parent_under_claim(p.ctx)
    assert not result.pushed
    assert refs(p) == ((other, "") if ref == PARENT_REF else (p.base, other))


def test_response_loss_requires_both_refs_and_recovers_success(publication):
    p = publication
    real = semantic_claim.push_refs_under_claim

    def lose_response(root, claim, updates, *, now):
        assert real(root, claim, updates, now=now).status is PushStatus.PUSHED
        return PushResult(PushStatus.UNKNOWN, None, "response lost")

    with patch.object(attempt, "push_refs_under_claim", side_effect=lose_response):
        result = publish_parent_under_claim(p.ctx)
    assert result.pushed, result.detail
    assert refs(p)[0] == p.head


@pytest.mark.parametrize("failure", ["other_holder", "expired", "unsupported"])
def test_claim_and_backend_failures_preserve_parent_and_snapshot(publication, failure):
    p = publication
    now = datetime.now(UTC)
    if failure == "other_holder":
        acquire_semantic_claim(
            p.local,
            parent_issue_number=1230,
            attempt_id="other",
            holder="other",
            now=now,
            ttl_seconds=300,
        )
        result = publish_parent_under_claim(p.ctx)
    elif failure == "expired":
        acquired = acquire_semantic_claim(
            p.local,
            parent_issue_number=1230,
            attempt_id="attempt-1230",
            holder="holder",
            now=now,
            ttl_seconds=300,
        )
        expired = replace(acquired.claim, expires_at=now - timedelta(seconds=1))
        with patch.object(
            attempt,
            "acquire_semantic_claim",
            return_value=ClaimResult(ClaimStatus.ACQUIRED, expired),
        ):
            result = publish_parent_under_claim(p.ctx)
    else:
        git(p.remote, "config", "receive.advertiseAtomic", "false")
        result = publish_parent_under_claim(p.ctx)
    assert not result.pushed
    assert git(p.remote, "rev-parse", PARENT_REF) == p.base
    assert not git(p.remote, "for-each-ref", "--format=%(objectname)", SNAPSHOT_REF)


def test_required_refuses_missing_snapshot_inputs(publication):
    p = publication
    p.ctx.read_report = None
    with patch.object(attempt, "push_refs_under_claim") as push:
        result = publish_parent_under_claim(p.ctx)
    assert not result.pushed
    push.assert_not_called()


def test_preparation_does_not_publish(publication):
    p = publication
    prepared = prepare_parent_read_publication(p.ctx)
    assert prepared.write is not None
    assert prepared.write.expected_oid is None
    assert git(p.remote, "rev-parse", PARENT_REF) == p.base
    assert not git(p.remote, "for-each-ref", "--format=%(objectname)", SNAPSHOT_REF)


@pytest.mark.parametrize(
    "failure", ["unknown", "report_changed", "completion_missing", "untrusted"]
)
def test_preparation_rejects_unverified_inputs(publication, failure):
    p = publication
    built = p.built
    if failure == "unknown":
        built = replace(built, inputs=None, unknowns=(UnknownReason.FETCH_FAILED,))
    elif failure == "report_changed":
        built = replace(built, inputs=replace(built.inputs, readers=()))
    elif failure == "completion_missing":
        p.ctx.forge.list_all_issue_comments.return_value = []
    else:
        p.ctx.forge.get_authenticated_user.return_value = "different-holder"
    with (
        patch.object(
            read_publish,
            "make_default_read_context_builder",
            return_value=lambda *_: built,
        ),
        patch.object(attempt, "push_refs_under_claim") as push,
    ):
        result = publish_parent_under_claim(p.ctx)
    assert not result.pushed
    push.assert_not_called()


@pytest.mark.parametrize(
    "tamper", ["parent_only", "snapshot_only", "different_digest", "unreadable"]
)
def test_recovery_never_accepts_an_unverified_pair(publication, tamper):
    p = publication
    prepared = prepare_parent_read_publication(p.ctx)
    write = prepared.write
    if tamper in {"parent_only", "different_digest", "unreadable"}:
        git(p.local, "push", "origin", f"{p.head}:{PARENT_REF}")
    if tamper in {"snapshot_only", "different_digest"}:
        git(p.local, "push", "origin", f"{write.new_commit_oid}:{SNAPSHOT_REF}")
    if tamper == "different_digest":
        p.ctx.semantic_review_state.read_report_digest = "f" * 64
    if tamper == "unreadable":
        git(p.local, "push", "origin", f"{p.base}:{SNAPSHOT_REF}")
    with patch.object(attempt, "push_refs_under_claim") as push:
        assert not publish_parent_under_claim(p.ctx).pushed
    push.assert_not_called()


def test_pushed_claim_alone_is_not_proof_of_both_refs(publication):
    p = publication
    with patch.object(
        attempt, "push_refs_under_claim", return_value=PushResult(PushStatus.PUSHED)
    ):
        assert not publish_parent_under_claim(p.ctx).pushed
    assert refs(p) == (p.base, "")


def test_restart_from_fresh_clone_after_child_branch_deletion(publication):
    p = publication
    git(p.local, "push", "origin", f"{p.head}:refs/heads/child")
    assert publish_parent_under_claim(p.ctx).pushed
    git(p.local, "push", "origin", ":refs/heads/child")
    git(p.remote, "reflog", "expire", "--expire=now", "--all")
    git(p.remote, "gc", "--prune=now")
    fresh = p.local.parent / "fresh"
    git(p.local.parent, "clone", str(p.remote), str(fresh))
    p.ctx.repository_root = fresh
    with (
        patch.object(read_publish, "make_default_read_context_builder") as builder,
        patch.object(attempt, "push_refs_under_claim") as push,
    ):
        assert publish_parent_under_claim(p.ctx).pushed
    builder.assert_not_called()
    push.assert_not_called()


def test_real_context_builder_prepares_from_frozen_outcome_and_checkpoint(publication):
    p = publication
    ctx = p.ctx
    ctx.forge.find_issues_by_parent_metadata.return_value = []
    ctx.forge.list_sub_issues.return_value = []
    child = ctx.attempt_identity.child_sources[0]
    child.subtask_id = "reader"
    state = ctx.semantic_review_state
    state.parent_issue_number = 1230
    state.child_sources = (child,)
    state.attempt_id = "attempt-1230"
    checkpoint = ReadCheckpointV1(
        repository="owner/repo",
        issue_number=10,
        claim_id="claim-reader",
        availability=CheckpointAvailability.RECORDED,
        input_base_sha=p.base,
    )
    ctx.forge.list_all_issue_comments.return_value.append(
        {
            "body": checkpoint_marker(checkpoint),
            "user": {"login": "holder"},
        }
    )
    with patch.object(
        read_publish,
        "make_default_read_context_builder",
        make_default_read_context_builder,
    ):
        result = publish_parent_under_claim(ctx)
    assert result.pushed, result.detail


def test_next_publication_preserves_previous_snapshot_and_checkpoints(publication):
    p = publication
    assert publish_parent_under_claim(p.ctx).pushed
    old_oid = refs(p)[1]
    previous = read_snapshot("owner/repo", 1230, p.head, cwd=p.local)
    next_head = commit(p.local, "next candidate")
    inputs = replace(p.built.inputs, parent_sha=p.head, candidate_head_sha=next_head)
    built = replace(p.built, inputs=inputs, snapshot=previous, bootstrap=False)
    report = detect_stale_reads(inputs)
    state = p.ctx.semantic_review_state
    state.base_sha, state.head_sha = p.head, next_head
    state.read_report_digest = state.reviewed_read_report_digest = report.digest
    p.ctx.read_report = report
    with patch.object(
        read_publish, "make_default_read_context_builder", return_value=lambda *_: built
    ):
        result = publish_parent_under_claim(p.ctx)
    assert result.pushed, result.detail
    restored = read_snapshot("owner/repo", 1230, next_head, cwd=p.local)
    assert set(previous.readers[0].validations) < set(restored.readers[0].validations)
    git(p.remote, "merge-base", "--is-ancestor", old_oid, refs(p)[1])


def test_remote_read_failure_never_publishes(publication):
    p = publication
    with (
        patch.object(
            read_publish, "run_git", side_effect=OSError("transport unavailable")
        ),
        patch.object(attempt, "push_refs_under_claim") as push,
    ):
        result = publish_parent_under_claim(p.ctx)
    assert not result.pushed
    push.assert_not_called()

"""Real Git round trips, object retention, and fail-closed snapshot storage."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from orchestune.infra.git_cli import run_git
from orchestune.read_dependencies.contracts import (
    ParentReadSnapshotV1,
    ReadCoverage,
    ReaderSnapshotEntry,
    ReadEvidenceV1,
    ReadObservationV1,
    ReadSource,
    ResourcePresence,
    UnknownReason,
    ValidationCheckpoint,
    parent_read_snapshot_ref,
    seal_evidence,
    seal_snapshot,
)
from orchestune.read_dependencies.snapshot_codec import parent_read_snapshot_to_dict
from orchestune.read_dependencies.storage import (
    SnapshotStorageError,
    prepare_snapshot_write,
    read_snapshot,
)


def git(cwd: Path, *args: str) -> str:
    return run_git(list(args), cwd=cwd).stdout.strip()


def commit(cwd: Path, content: str) -> str:
    (cwd / "input.txt").write_text(content, encoding="utf-8")
    git(cwd, "add", "input.txt")
    git(cwd, "commit", "-m", content)
    return git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, Path, ParentReadSnapshotV1, ReadEvidenceV1]:
    remote, local = tmp_path / "remote.git", tmp_path / "local"
    git(tmp_path, "init", "--bare", str(remote))
    git(tmp_path, "init", "-b", "main", str(local))
    git(local, "config", "user.name", "Snapshot Test")
    git(local, "config", "user.email", "test@example.invalid")
    git(local, "remote", "add", "origin", str(remote))
    parent = commit(local, "parent")
    git(local, "push", "origin", "HEAD:refs/heads/main")
    git(local, "checkout", "-b", "child")
    input_sha = commit(local, "read version")
    source = commit(local, "child head")
    blob = git(local, "rev-parse", f"{input_sha}:input.txt")
    evidence = seal_evidence(
        ReadEvidenceV1(
            repository="owner/repo",
            issue_number=1,
            claim_id="claim-1",
            input_base_sha=input_sha,
            head_sha=source,
            declared_reads=("input.txt",),
            observations=(
                ReadObservationV1(
                    path="input.txt",
                    snapshot_commit=input_sha,
                    presence=ResourcePresence.PRESENT,
                    blob_oid=blob,
                    file_mode="100644",
                    source=ReadSource.ADAPTER_OBSERVED,
                ),
            ),
            source=ReadSource.ADAPTER_OBSERVED,
            coverage=ReadCoverage.COMPLETE,
        )
    )
    assert evidence.digest is not None
    snap = seal_snapshot(
        ParentReadSnapshotV1(
            repository="owner/repo",
            parent_issue_number=1230,
            parent_head_sha=parent,
            readers=(
                ReaderSnapshotEntry(
                    issue_number=1,
                    claim_id="claim-1",
                    completion_id="completion-1",
                    source_sha=source,
                    evidence_digest=evidence.digest,
                ),
            ),
        )
    )
    return local, remote, snap, evidence


def prepare(
    repo: tuple[Path, Path, ParentReadSnapshotV1, ReadEvidenceV1], **kwargs: Any
) -> Any:
    local, _, snap, evidence = repo
    return prepare_snapshot_write(
        snap,
        parent_head_sha=snap.parent_head_sha,
        read_evidence=(evidence,),
        cwd=local,
        **kwargs,
    )


def test_prepare_changes_no_refs_or_worktree(repo: Any) -> None:
    local, remote, snap, _ = repo
    before = git(local, "show-ref")
    index = (local / ".git/index").read_bytes()
    write = prepare(repo, bootstrap=True)
    assert write.expected_oid is None
    assert write.parent_head_sha == snap.parent_head_sha
    assert write.snapshot_digest == snap.digest
    assert git(local, "show-ref") == before
    assert git(local, "status", "--porcelain") == ""
    assert (local / ".git/index").read_bytes() == index
    assert git(remote, "show-ref") == f"{snap.parent_head_sha} refs/heads/main"


def test_restore_after_branch_deletion_gc_and_fresh_clone(repo: Any) -> None:
    local, remote, snap, evidence = repo
    write = prepare(repo, bootstrap=True)
    git(local, "push", "origin", f"{write.new_commit_oid}:{write.ref}")
    git(local, "checkout", "main")
    git(local, "branch", "-D", "child")
    fresh = local.parent / "fresh"
    git(local.parent, "clone", str(remote), str(fresh))
    restored = read_snapshot("owner/repo", 1230, snap.parent_head_sha, cwd=fresh)
    assert restored == snap
    git(fresh, "update-ref", write.ref, write.new_commit_oid)
    git(fresh, "reflog", "expire", "--expire=now", "--all")
    git(fresh, "gc", "--prune=now")
    for oid in (evidence.head_sha, evidence.input_base_sha):
        assert git(fresh, "cat-file", "-t", oid) == "commit"
        assert git(fresh, "merge-base", "--is-ancestor", oid, write.ref) == ""
    assert read_snapshot("owner/repo", 1230, snap.parent_head_sha, cwd=fresh) == snap


def test_next_snapshot_retains_previous_and_original_evidence(repo: Any) -> None:
    local, _, snap, _ = repo
    first = prepare(repo, bootstrap=True)
    git(local, "push", "origin", f"{first.new_commit_oid}:{first.ref}")
    head = commit(local, "new parent")
    point = ValidationCheckpoint(candidate_head_sha=head, report_digest="f" * 64)
    updated = seal_snapshot(
        replace(
            snap,
            parent_head_sha=head,
            readers=(replace(snap.readers[0], validations=(point,)),),
        )
    )
    second = prepare_snapshot_write(
        updated, parent_head_sha=snap.parent_head_sha, cwd=local
    )
    assert second.expected_oid == first.new_commit_oid
    git(
        local,
        "merge-base",
        "--is-ancestor",
        first.new_commit_oid,
        second.new_commit_oid,
    )
    git(local, "push", "origin", f"{second.new_commit_oid}:{second.ref}")
    assert read_snapshot("owner/repo", 1230, head, cwd=local) == updated
    with pytest.raises(SnapshotStorageError) as caught:
        read_snapshot("owner/repo", 1230, snap.parent_head_sha, cwd=local)
    assert caught.value.reason == UnknownReason.PARENT_SNAPSHOT_MISMATCH


def test_absence_requires_explicit_bootstrap_and_evidence(repo: Any) -> None:
    local, _, snap, _ = repo
    with pytest.raises(SnapshotStorageError):
        read_snapshot("owner/repo", 1230, snap.parent_head_sha, cwd=local)
    with pytest.raises(SnapshotStorageError):
        prepare(repo)
    with pytest.raises(SnapshotStorageError):
        prepare_snapshot_write(
            snap, parent_head_sha=snap.parent_head_sha, cwd=local, bootstrap=True
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository", "other/repo"),
        ("parent_issue_number", 999),
        ("parent_head_sha", "f" * 40),
    ],
)
def test_wrong_identity_or_forced_parent_is_unknown(
    repo: Any, field: str, value: Any
) -> None:
    local, _, snap, _ = repo
    write = prepare(repo, bootstrap=True)
    # Publish under the requested ref, but with the wrong payload identity.
    if field != "parent_head_sha":
        wrong = seal_snapshot(replace(snap, **{field: value}))
        payload = json.dumps(parent_read_snapshot_to_dict(wrong))
        git(local, "checkout", "main")
        (local / "snapshot.json").write_text(payload, encoding="utf-8")
        git(local, "add", "snapshot.json")
        git(local, "commit", "-m", "wrong identity")
        oid = git(local, "rev-parse", "HEAD")
    else:
        oid = write.new_commit_oid
    git(local, "push", "origin", f"{oid}:{write.ref}")
    expected = value if field == "parent_head_sha" else snap.parent_head_sha
    with pytest.raises(SnapshotStorageError):
        read_snapshot("owner/repo", 1230, expected, cwd=local)


def test_corrupt_snapshot_is_unknown_and_cannot_be_overwritten(repo: Any) -> None:
    local, _, snap, _ = repo
    git(local, "push", "origin", f"HEAD:{parent_read_snapshot_ref(1230)}")
    with pytest.raises(SnapshotStorageError):
        read_snapshot("owner/repo", 1230, snap.parent_head_sha, cwd=local)
    with pytest.raises(SnapshotStorageError):
        prepare(repo, bootstrap=True)


def test_transport_failure_and_missing_objects_stay_unknown(repo: Any) -> None:
    local, _, snap, evidence = repo
    with pytest.raises(SnapshotStorageError) as caught:
        read_snapshot(
            "owner/repo", 1230, snap.parent_head_sha, cwd=local, remote="missing"
        )
    assert caught.value.reason == UnknownReason.FETCH_FAILED
    missing = replace(snap, parent_head_sha="f" * 40, digest=None)
    with pytest.raises(SnapshotStorageError):
        prepare_snapshot_write(
            missing,
            parent_head_sha=snap.parent_head_sha,
            read_evidence=(evidence,),
            cwd=local,
            bootstrap=True,
        )


def test_read_evidence_identity_and_git_content_are_validated(repo: Any) -> None:
    local, _, snap, evidence = repo
    for changed in (
        replace(evidence, repository="other/repo", digest=None),
        replace(
            evidence,
            observations=(replace(evidence.observations[0], blob_oid="f" * 40),),
            digest=None,
        ),
    ):
        changed = seal_evidence(changed)
        assert changed.digest is not None
        reader = replace(snap.readers[0], evidence_digest=changed.digest)
        new = seal_snapshot(replace(snap, readers=(reader,)))
        with pytest.raises(SnapshotStorageError) as caught:
            prepare_snapshot_write(
                new,
                parent_head_sha=snap.parent_head_sha,
                read_evidence=(changed,),
                cwd=local,
                bootstrap=True,
            )
        assert caught.value.reason == UnknownReason.EVIDENCE_MISMATCH


@pytest.mark.parametrize(
    "change", ["remove_reader", "change_completion", "remove_validation"]
)
def test_original_read_and_validation_cannot_be_erased(repo: Any, change: str) -> None:
    local, remote, snap, evidence = repo
    point = ValidationCheckpoint(
        candidate_head_sha=evidence.head_sha, report_digest="f" * 64
    )
    snap = seal_snapshot(
        replace(snap, readers=(replace(snap.readers[0], validations=(point,)),))
    )
    first = prepare((local, remote, snap, evidence), bootstrap=True)
    git(local, "push", "origin", f"{first.new_commit_oid}:{first.ref}")
    readers: tuple[ReaderSnapshotEntry, ...]
    if change == "remove_reader":
        readers = ()
    elif change == "change_completion":
        readers = (replace(snap.readers[0], completion_id="other"),)
    else:
        readers = (replace(snap.readers[0], validations=()),)
    modified = seal_snapshot(replace(snap, readers=readers))
    with pytest.raises(SnapshotStorageError, match="original reader"):
        prepare_snapshot_write(
            modified, parent_head_sha=snap.parent_head_sha, cwd=local
        )


def test_unavailable_read_observation_is_preserved(repo: Any) -> None:
    local, _, snap, evidence = repo
    observation = replace(
        evidence.observations[0],
        snapshot_commit=None,
        presence=ResourcePresence.UNKNOWN,
        blob_oid=None,
        file_mode=None,
    )
    evidence = seal_evidence(
        replace(
            evidence,
            observations=(observation,),
            coverage=ReadCoverage.PARTIAL,
            digest=None,
        )
    )
    assert evidence.digest is not None
    snap = seal_snapshot(
        replace(
            snap, readers=(replace(snap.readers[0], evidence_digest=evidence.digest),)
        )
    )
    write = prepare_snapshot_write(
        snap,
        parent_head_sha=snap.parent_head_sha,
        cwd=local,
        read_evidence=(evidence,),
        bootstrap=True,
    )
    git(local, "push", "origin", f"{write.new_commit_oid}:{write.ref}")
    assert read_snapshot("owner/repo", 1230, snap.parent_head_sha, cwd=local) == snap


@pytest.mark.parametrize(
    "payload", ["{invalid", '{"version": 1}', '{"schema": "parent-read-snapshot-v1"}']
)
def test_malformed_stored_json_is_unknown(repo: Any, payload: str) -> None:
    local, _, snap, _ = repo
    (local / "snapshot.json").write_text(payload, encoding="utf-8")
    git(local, "add", "snapshot.json")
    git(local, "commit", "-m", "invalid snapshot payload")
    git(local, "push", "origin", f"HEAD:{parent_read_snapshot_ref(1230)}")
    with pytest.raises(SnapshotStorageError) as caught:
        read_snapshot("owner/repo", 1230, snap.parent_head_sha, cwd=local)
    assert caught.value.reason == UnknownReason.EVIDENCE_MISMATCH


def test_missing_evidence_blob_is_unknown(repo: Any) -> None:
    local, _, snap, _ = repo
    (local / "snapshot.json").write_text(
        json.dumps(parent_read_snapshot_to_dict(snap)), encoding="utf-8"
    )
    git(local, "add", "snapshot.json")
    git(local, "commit", "-m", "snapshot without evidence")
    git(local, "push", "origin", f"HEAD:{parent_read_snapshot_ref(1230)}")
    with pytest.raises(SnapshotStorageError):
        read_snapshot("owner/repo", 1230, snap.parent_head_sha, cwd=local)


def test_local_shadow_ref_cannot_replace_remote_snapshot(repo: Any) -> None:
    local, _, snap, _ = repo
    first = prepare(repo, bootstrap=True)
    git(local, "push", "origin", f"{first.new_commit_oid}:{first.ref}")
    git(local, "update-ref", first.ref, "HEAD")
    before = git(local, "show-ref")
    assert read_snapshot("owner/repo", 1230, snap.parent_head_sha, cwd=local) == snap
    second = prepare(repo)
    assert second.expected_oid == first.new_commit_oid
    assert git(local, "show-ref") == before

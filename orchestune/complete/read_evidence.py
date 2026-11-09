"""Validate submitted reads without rewriting their original snapshots."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import NoReturn

from orchestune.claim.read_checkpoint import restore_read_checkpoint
from orchestune.complete.contracts import CompleteFailureReason, CompleteRequest
from orchestune.complete.journal import CompletionJournalError
from orchestune.forge import Forge, ForgeError
from orchestune.infra.git_cli import run_git
from orchestune.outcome_record import OutcomeRecord
from orchestune.read_dependencies.codec import (
    parse_read_evidence,
    read_evidence_to_dict,
)
from orchestune.read_dependencies.contracts import (
    CheckpointAvailability,
    ReadEvidenceV1,
    ResourcePresence,
)
from orchestune.read_dependencies.git import lookup_resource


def load_read_evidence(path: Path) -> ReadEvidenceV1:
    """Load the artifact once; structural validation belongs to the codec."""
    try:
        return parse_read_evidence(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError) as error:
        raise ValueError(f"Invalid --read-evidence: {error}") from error


def _reject(message: str) -> NoReturn:
    raise CompletionJournalError(CompleteFailureReason.EVIDENCE_MISSING, message)


def _require_commit(sha: str, worktree: Path) -> None:
    result = run_git(
        ["rev-parse", "--verify", f"{sha}^{{commit}}"], cwd=worktree, check=False
    )
    if result.returncode or result.stdout.strip() != sha:
        _reject("Read evidence snapshot commit is unavailable")


def _validate_snapshots(value: ReadEvidenceV1, worktree: Path) -> None:
    _require_commit(value.input_base_sha, worktree)
    for observation in value.observations:
        if observation.snapshot_commit is None:
            continue
        _require_commit(observation.snapshot_commit, worktree)
        if observation.presence is ResourcePresence.UNKNOWN:
            continue
        actual = lookup_resource(
            observation.snapshot_commit, observation.path, cwd=worktree
        )
        if (actual.presence, actual.blob_oid, actual.file_mode) != (
            observation.presence,
            observation.blob_oid,
            observation.file_mode,
        ):
            _reject("Read observation does not match its Git snapshot")


def _validate_checkpoint(value: ReadEvidenceV1, forge: Forge) -> None:
    checkpoint = restore_read_checkpoint(
        forge.list_all_issue_comments(value.issue_number),
        value.repository,
        value.issue_number,
        value.claim_id,
    )
    if (
        checkpoint.availability is not CheckpointAvailability.RECORDED
        or checkpoint.input_base_sha != value.input_base_sha
    ):
        _reject("Read evidence claim checkpoint is unavailable or mismatched")


def verify_read_evidence(
    request: CompleteRequest,
    forge: Forge,
    claim_id: str | None,
    worktree: Path,
    reviewed_head_sha: str | None,
) -> str | None:
    """Bind reads to the current claim checkpoint and independently reviewed head."""
    evidence = request.to_outcome_record().read_evidence
    if evidence is None:
        return None
    try:
        value = parse_read_evidence(read_evidence_to_dict(evidence))
        slug = getattr(forge, "get_repository_slug", None)
        if not callable(slug):
            _reject("Read evidence repository identity is unavailable")
        repository = slug()
        if (value.repository, value.issue_number, value.claim_id) != (
            repository,
            request.issue_number,
            claim_id,
        ):
            _reject("Read evidence repository, Issue or claim mismatch")
        if value.head_sha != reviewed_head_sha:
            raise CompletionJournalError(
                CompleteFailureReason.REVIEW_HEAD_MISMATCH,
                "Read evidence HEAD differs from the reviewed HEAD",
            )
        _validate_checkpoint(value, forge)
        _validate_snapshots(value, worktree)
    except (
        ForgeError,
        OSError,
        subprocess.SubprocessError,
        ValueError,
        TypeError,
    ) as error:
        raise CompletionJournalError(
            CompleteFailureReason.EVIDENCE_MISSING,
            f"Read evidence verification failed: {error}",
        ) from error
    return value.digest


def freeze_read_evidence(outcome: OutcomeRecord, digest: str | None) -> OutcomeRecord:
    """Seal exactly the verified value before reserving the immutable Outcome body."""
    if outcome.read_evidence is None:
        return outcome
    value = parse_read_evidence(read_evidence_to_dict(outcome.read_evidence))
    if (
        value.digest != digest
        or value.head_sha != outcome.head_sha
        or value.head_sha != outcome.review.reviewed_head_sha
    ):
        raise CompletionJournalError(
            CompleteFailureReason.REQUEST_FINGERPRINT_MISMATCH,
            "Read evidence changed after validation",
        )
    return replace(outcome, read_evidence=value)

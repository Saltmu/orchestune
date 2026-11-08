"""Immutable, non-secret claim-start evidence stored in Issue comments."""

from __future__ import annotations

import dataclasses
import json
import re
import subprocess
from collections.abc import Sequence
from typing import Any

from orchestune.claim.contracts import (
    ClaimFailure,
    ClaimFailureReason,
    OwnerKind,
    ReservationKind,
)
from orchestune.claim.issue_metadata import publish_claim_ownership_to_issue
from orchestune.forge import Forge, ForgeError
from orchestune.ledger.active_records import ActiveWorktree
from orchestune.models import IssueRecord
from orchestune.read_dependencies.contracts import (
    READ_CHECKPOINT_MARKER,
    CheckpointAvailability,
    ReadCheckpointV1,
)

_OPEN = re.compile(rf"^```{READ_CHECKPOINT_MARKER}[ \t]*$", re.MULTILINE)
_BLOCK = re.compile(
    rf"^```{READ_CHECKPOINT_MARKER}[ \t]*\n(.*?)^```[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
_FIELDS = frozenset(field.name for field in dataclasses.fields(ReadCheckpointV1))
_STORAGE_ERRORS = (ForgeError, OSError, subprocess.CalledProcessError)


def checkpoint_marker(checkpoint: ReadCheckpointV1) -> str:
    """Serialize only the versioned checkpoint fields, without local metadata."""
    payload = json.dumps(dataclasses.asdict(checkpoint), sort_keys=True)
    return f"```{READ_CHECKPOINT_MARKER}\n{payload}\n```\n"


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate checkpoint field")
        payload[key] = value
    return payload


def _parse_checkpoint(payload: str) -> ReadCheckpointV1:
    raw = json.loads(payload, object_pairs_hook=_unique_keys)
    if not isinstance(raw, dict) or raw.keys() != _FIELDS:
        raise ValueError("invalid checkpoint fields")
    if isinstance(raw["version"], bool) or not isinstance(raw["version"], int):
        raise ValueError("checkpoint version must be an integer")
    raw["availability"] = CheckpointAvailability(raw["availability"])
    return ReadCheckpointV1(**raw)


def _restore(
    comments: Sequence[dict[str, Any]],
    repository: str,
    issue_number: int,
    claim_id: str,
) -> ReadCheckpointV1 | None:
    selected = None
    for comment in comments:
        body = str(comment.get("body", "")).replace("\r\n", "\n")
        blocks = list(_BLOCK.finditer(body))
        if len(blocks) != len(_OPEN.findall(body)):
            raise ValueError("incomplete checkpoint marker")
        for block in blocks:
            checkpoint = _parse_checkpoint(block.group(1))
            if (checkpoint.repository, checkpoint.issue_number) != (
                repository,
                issue_number,
            ):
                raise ValueError("checkpoint repository or issue mismatch")
            if checkpoint.claim_id != claim_id:
                continue
            if selected is not None and checkpoint != selected:
                raise ValueError("conflicting checkpoints for one claim")
            selected = checkpoint
    return selected


def restore_read_checkpoint(
    comments: Sequence[dict[str, Any]],
    repository: str,
    issue_number: int,
    claim_id: str,
) -> ReadCheckpointV1:
    """Restore one generation; equal duplicates are harmless, conflicts fail.

    Missing evidence stays unavailable, never inferred from a current Git ref.
    """
    unavailable = ReadCheckpointV1(
        repository=repository,
        issue_number=issue_number,
        claim_id=claim_id,
        availability=CheckpointAvailability.UNAVAILABLE,
        input_base_sha=None,
    )
    return _restore(comments, repository, issue_number, claim_id) or unavailable


def ensure_read_checkpoint(
    forge: Forge,
    repository: str,
    issue_number: int,
    claim_id: str,
    *,
    input_base_sha: str | None = None,
) -> ReadCheckpointV1:
    """Store a new claim's start once; only a fresh claim supplies a base.

    Fetch failure forbids posting. A lost POST response is recovered by reading
    the same marker, without a second POST or a new baseline.
    """

    def read() -> ReadCheckpointV1 | None:
        return _restore(
            forge.list_all_issue_comments(issue_number),
            repository,
            issue_number,
            claim_id,
        )

    unavailable = restore_read_checkpoint([], repository, issue_number, claim_id)
    existing = read()
    if existing is not None or input_base_sha is None:
        return existing or unavailable
    expected = dataclasses.replace(
        unavailable,
        availability=CheckpointAvailability.RECORDED,
        input_base_sha=input_base_sha,
    )
    try:
        forge.create_issue_comment(issue_number, checkpoint_marker(expected))
    except _STORAGE_ERRORS:
        if read() == expected:
            return expected
        raise
    if read() != expected:
        raise ValueError("checkpoint publication could not be verified")
    return expected


def publish_claim_read_checkpoint(
    forge: Forge,
    reservation: ActiveWorktree,
    *,
    input_base_sha: str | None = None,
) -> ClaimFailure | None:
    """Claim boundary leaving ownership and label transitions with service.

    Legacy Forge implementations without a repository slug cannot record a
    portable checkpoint. Adopted PRs have no known claim-start read either.
    """
    slug = getattr(forge, "get_repository_slug", None)
    if not callable(slug) or reservation.claim.adopted_pr_number is not None:
        return None
    try:
        ensure_read_checkpoint(
            forge,
            slug(),
            reservation.core.issue_number,
            reservation.claim.claim_id or "",
            input_base_sha=input_base_sha,
        )
    except (*_STORAGE_ERRORS, ValueError, TypeError):
        return ClaimFailure(
            reason=ClaimFailureReason.STATE_SAVE_FAILED,
            message="Could not verify the immutable claim read checkpoint; resume without guessing a new start.",
        )
    return None


def publish_claim_issue_metadata(
    forge: Forge,
    issue: IssueRecord,
    reservation: ActiveWorktree,
    owner_kind: OwnerKind,
    reservation_kind: ReservationKind,
    *,
    input_base_sha: str | None = None,
) -> ClaimFailure | None:
    """Publish the checkpoint before the existing ownership finalization write."""
    error = publish_claim_read_checkpoint(
        forge, reservation, input_base_sha=input_base_sha
    )
    if error is not None:
        return error
    return publish_claim_ownership_to_issue(
        forge, issue, owner_kind, reservation.claim.claim_id or "", reservation_kind
    )

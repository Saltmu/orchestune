"""Verified takeover of a worktree retained by a GC-released generation (#1381).

GC keeps a dirty blocked/not-needed worktree and its claim marker when it
releases the generation, and saves a ``gc-release`` receipt in the same
run_state write. A later claim of the same Issue may move that marker to its
new generation only when the receipt still matches the immutable completion
evidence, the old execution is stopped and every retained change fits the new
reservation. Anything less leaves the worktree untouched for a human.

The takeover is recorded on the receipt as ``prepared`` before the marker
moves, ``active_saved`` with the new reservation and ``completed`` once the
claim is finalized, so an interrupted claim resumes with the same old/new pair.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal

from orchestune.claim.local_identity import (
    registered_claim_path,
    validate_claim_worktree,
)
from orchestune.claim.workspace import ClaimWorkspace
from orchestune.infra.git_cli import run_git
from orchestune.infra.process_utils import is_process_alive
from orchestune.ledger.completion_records import InvalidLedgerRecord
from orchestune.ledger.completion_reservations import completion_handoff_matches_active
from orchestune.ledger.recovery_receipts import (
    GcReleaseReceipt,
    ReclaimReceipt,
    RecoveryReceiptValue,
    invalid_recovery_receipt_identity,
)
from orchestune.ledger.run_state import GC_RELEASE_OPERATION, ActiveWorktree, RunState
from orchestune.ownership_contracts import ClaimStage, ReservationKind
from orchestune.worktree_ops.claim_marker import read_claim_marker
from orchestune.worktree_ops.preparation import MarkerTakeover, resolve_worktree_path

PHASE_PREPARED: Literal["prepared"] = "prepared"
PHASE_ACTIVE_SAVED: Literal["active_saved"] = "active_saved"
PHASE_COMPLETED: Literal["completed"] = "completed"
_STAGE_PHASES: dict[str, Literal["prepared", "active_saved", "completed"]] = {
    ClaimStage.ACTIVE_SAVED.value: PHASE_ACTIVE_SAVED,
    ClaimStage.COMPLETED.value: PHASE_COMPLETED,
}


@dataclass(frozen=True)
class ReclaimAssessment:
    """``takeover`` is set only when the evidence allows it; otherwise
    ``diagnostic`` explains why a mismatched marker needs a human."""

    takeover: MarkerTakeover | None = None
    receipt_key: str | None = None
    diagnostic: str | None = None


def assess_worktree_reclaim(
    run_state: RunState,
    workspace: ClaimWorkspace,
    reservation: ActiveWorktree,
) -> ReclaimAssessment:
    """Run under the run_state lock, after the normal preflight and conflicts."""
    branch = reservation.core.branch
    try:
        path = resolve_worktree_path(workspace.worktree_root, branch)
    except ValueError:
        # preparation raises the same error and reports INVALID_BRANCH_NAME
        return ReclaimAssessment()
    marker = read_claim_marker(path)
    marker_claim = marker.get("claim_id") if marker else None
    if marker_claim is None or marker_claim == reservation.claim.claim_id:
        return ReclaimAssessment()
    manual = partial(_manual_review, path, branch, marker_claim)
    matches = _matching_receipts(run_state, workspace, reservation, str(marker_claim))
    if len(matches) != 1:
        return ReclaimAssessment(
            diagnostic=manual(
                "no GC release receipt for this generation"
                if not matches
                else "ambiguous GC release receipts"
            )
        )
    key, receipt = matches[0]
    if not isinstance(receipt, GcReleaseReceipt):
        detail = (
            "the GC release receipt snapshot is unreadable"
            if isinstance(receipt, InvalidLedgerRecord)
            and "active snapshot" in receipt.reason
            else "GC release receipt is invalid"
        )
        return ReclaimAssessment(diagnostic=manual(detail))
    try:
        released = _released_generation(run_state, workspace, reservation, receipt)
        if receipt.worktree_path != str(path.resolve()):
            raise ValueError("the retained worktree path differs from the receipt")
        previous = _previous_claim_ids(receipt, reservation)
    except ValueError as error:
        return ReclaimAssessment(diagnostic=manual(str(error)))
    verify = partial(_verify_retained, released, workspace.run_state_path, reservation)
    return ReclaimAssessment(
        takeover=MarkerTakeover(previous_claim_ids=previous, verify=verify),
        receipt_key=key,
    )


def begin_worktree_reclaim(
    run_state: RunState,
    workspace: ClaimWorkspace,
    reservation: ActiveWorktree,
    save: Callable[[RunState, Path], Any],
) -> tuple[ReclaimAssessment, str | None]:
    """Assess the reservation's worktree and persist a takeover as prepared.

    Returns the assessment and, when ``save`` failed, why the claim must stop
    before the marker moves.
    """
    assessment = assess_worktree_reclaim(run_state, workspace, reservation)
    if assessment.takeover is None:
        return assessment, None
    record_reclaim_phase(
        run_state,
        reservation.claim.claim_id,
        PHASE_PREPARED,
        receipt_key=assessment.receipt_key,
    )
    try:
        save(run_state, workspace.run_state_path)
    except Exception as error:
        return assessment, f"Failed to persist the prepared worktree takeover: {error}"
    return assessment, None


def takeover_rejection_message(reason: str | None, reclaim: ReclaimAssessment) -> str:
    message = f"prepare_task_worktree rejected: {reason}"
    return f"{message} ({reclaim.diagnostic})" if reclaim.diagnostic else message


def record_takeover_stage(run_state: RunState, reservation: ActiveWorktree) -> None:
    """Follow the claim stage being saved; a no-op for ordinary claims."""
    phase = _STAGE_PHASES.get(reservation.claim.claim_stage or "")
    if phase is not None:
        record_reclaim_phase(run_state, reservation.claim.claim_id, phase)


def record_reclaim_phase(
    run_state: RunState,
    claim_id: str | None,
    phase: Literal["prepared", "active_saved", "completed"],
    *,
    receipt_key: str | None = None,
) -> None:
    """Record the takeover phase on its receipt; a no-op for ordinary claims.

    The caller persists ``run_state`` with the matching claim stage.
    """
    key = receipt_key or next(
        (
            k
            for k, r in run_state.recovery_receipts.items()
            if isinstance(r, GcReleaseReceipt)
            and r.reclaim is not None
            and r.reclaim.new_claim_id == claim_id
        ),
        None,
    )
    if key is None or claim_id is None:
        return
    receipt = run_state.recovery_receipts[key]
    if not isinstance(receipt, GcReleaseReceipt):
        raise ValueError("reclaim phase receipt is not a gc-release record")
    previous = receipt.reclaim
    superseded = list(previous.superseded_claim_ids if previous else ())
    if previous is not None and previous.new_claim_id != claim_id:
        superseded.append(previous.new_claim_id)
    run_state.recovery_receipts[key] = replace(
        receipt,
        reclaim=ReclaimReceipt(
            new_claim_id=claim_id,
            phase=phase,
            recorded_at=datetime.now(UTC).isoformat(),
            superseded_claim_ids=tuple(superseded),
        ),
    )


def _manual_review(path: Path, branch: str, marker_claim: Any, problem: str) -> str:
    return (
        f"retained worktree {path} (branch {branch}) is marked by claim "
        f"{marker_claim}; {problem}. Inspect it manually; do not rewrite the "
        "claim marker by hand"
    )


def _matching_receipts(
    run_state: RunState,
    workspace: ClaimWorkspace,
    reservation: ActiveWorktree,
    marker_claim: str,
) -> list[tuple[str, RecoveryReceiptValue]]:
    """The receipt of the marker's own generation, else of a takeover it only
    prepared (a generation that went further has a receipt of its own)."""
    receipts = [
        (key, receipt)
        for key, receipt in run_state.recovery_receipts.items()
        if (
            isinstance(receipt, GcReleaseReceipt)
            and receipt.repository_id == workspace.repository_identity
            and receipt.issue_number == reservation.core.issue_number
        )
        or invalid_recovery_receipt_identity(
            receipt,
            operation=GC_RELEASE_OPERATION,
            repository_id=workspace.repository_identity,
            issue_number=reservation.core.issue_number,
        )
    ]
    released = [
        (key, receipt)
        for key, receipt in receipts
        if (isinstance(receipt, GcReleaseReceipt) and receipt.claim_id == marker_claim)
        or invalid_recovery_receipt_identity(
            receipt,
            operation=GC_RELEASE_OPERATION,
            repository_id=workspace.repository_identity,
            issue_number=reservation.core.issue_number,
            claim_id=marker_claim,
        )
    ]
    return released or [
        (key, receipt)
        for key, receipt in receipts
        if isinstance(receipt, GcReleaseReceipt)
        and receipt.reclaim is not None
        and receipt.reclaim.new_claim_id == marker_claim
        and receipt.reclaim.phase == PHASE_PREPARED
    ]


def _released_generation(
    run_state: RunState,
    workspace: ClaimWorkspace,
    reservation: ActiveWorktree,
    receipt: GcReleaseReceipt,
) -> ActiveWorktree:
    """Bind the receipt to the immutable completion evidence it released."""
    if (
        receipt.schema_version != 1
        or receipt.repository_id != workspace.repository_identity
        or receipt.issue_number != reservation.core.issue_number
        or receipt.branch != reservation.core.branch
        or receipt.worktree_action != "retain"
        or receipt.stop_confirmed is not True
    ):
        raise ValueError("the GC release receipt does not match this claim")
    released = receipt.active.value
    if (
        released.core.issue_number,
        released.core.branch,
        released.claim.claim_id,
        released.claim.base_sha,
        released.completion.completion_id,
    ) != (
        reservation.core.issue_number,
        reservation.core.branch,
        receipt.claim_id,
        receipt.base_sha,
        receipt.completion_id,
    ):
        raise ValueError("the GC release receipt snapshot differs from the receipt")
    if not completion_handoff_matches_active(run_state, released):
        raise ValueError("the completion evidence does not match the released claim")
    if any(
        active.core.issue_number == reservation.core.issue_number
        and active.claim.claim_id != reservation.claim.claim_id
        for active in run_state.active_worktrees.values()
    ):
        raise ValueError("another generation of this Issue is still active")
    if is_process_alive(released.launch.pid):
        raise ValueError(
            f"process {released.launch.pid} of the released claim is alive"
        )
    return released


def _previous_claim_ids(
    receipt: GcReleaseReceipt, reservation: ActiveWorktree
) -> frozenset[str]:
    """Generations this claim may move the marker away from.

    A takeover that another claim only prepared never saved its reservation,
    so it never launched a provider and may be superseded. A saved one may not.
    """
    previous = {str(receipt.claim_id)}
    pending = receipt.reclaim
    pending_claim = pending.new_claim_id if pending else None
    if pending_claim not in (None, reservation.claim.claim_id):
        if pending is None or pending.phase != PHASE_PREPARED:
            raise ValueError(f"claim {pending_claim} already took over this worktree")
        previous.add(str(pending_claim))
    return frozenset(previous)


def _verify_retained(
    released: ActiveWorktree,
    state_path: Path,
    reservation: ActiveWorktree,
    path: Path,
) -> str | None:
    """Re-check the checkout under the worktree claim lock."""
    try:
        validate_claim_worktree(released, state_path, require_marker=False)
        if registered_claim_path(released, state_path).resolve() != path.resolve():
            return "the released claim registered a different worktree"
        outside = _changes_outside_footprint(path, released.claim.base_sha, reservation)
    except (OSError, ValueError) as error:
        return str(error)
    if is_process_alive(released.launch.pid):
        return f"process {released.launch.pid} of the released claim is still alive"
    if outside:
        return "retained changes are outside the reservation footprint: " + ", ".join(
            outside
        )
    return None


def _changes_outside_footprint(
    path: Path, base_sha: str | None, reservation: ActiveWorktree
) -> list[str]:
    """Committed, staged, unstaged and untracked files; ignored files are caches."""
    if not base_sha:
        raise ValueError("the released claim has no recorded base commit")
    if reservation.claim.reservation_kind == ReservationKind.REPOSITORY.value:
        return []
    changed: set[str] = set()
    for args in (
        # commits, the index and the working tree are compared with the base
        # separately: each can hide a change the others restored to the base
        ["diff", "--name-only", "--no-renames", "-z", base_sha, "HEAD"],
        ["diff", "--cached", "--name-only", "--no-renames", "-z", base_sha],
        ["diff", "--name-only", "--no-renames", "-z", base_sha],
        ["ls-files", "--others", "--exclude-standard", "-z"],
    ):
        result = run_git(args, cwd=path, check=False)
        if result.returncode != 0:
            raise ValueError(
                "unable to list retained changes: " + (result.stderr or "").strip()
            )
        changed.update(name for name in result.stdout.split("\0") if name)
    return sorted(changed - set(reservation.core.declared_footprint))

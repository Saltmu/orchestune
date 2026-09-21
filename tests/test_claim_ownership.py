"""Tests for pure claim ownership reservation and conflict evaluation."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from orchestune.claim.contracts import (
    ClaimOutcome,
    ClaimRequest,
    OwnerKind,
    ReservationKind,
)
from orchestune.claim.ownership import (
    ClaimConflictReason,
    OwnerToken,
    build_reservation,
    evaluate_claim_conflicts,
    new_claim_id,
    new_owner_token,
    owner_token_digest,
)
from orchestune.dispatch.state import ActiveWorktree, RunState
from orchestune.task_metadata import CycleTask


@dataclass(frozen=True)
class _View:
    tasks: dict[int, CycleTask]

    def task(self, issue_number: int) -> CycleTask | None:
        return self.tasks.get(issue_number)


def _task(
    issue_number: int,
    *,
    footprint: tuple[str, ...] = (),
    contract: str | None = None,
    writer: bool = False,
) -> CycleTask:
    return CycleTask(
        issue_number=issue_number,
        subtask_id=f"task-{issue_number}",
        footprint=footprint,
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=(),
        created_at="",
        shared_contract=contract,
        writes_shared_contract=writer,
    )


def _active(
    issue_number: int,
    *,
    footprint: tuple[str, ...] = (),
    reservation_kind: str = "footprint",
    forced_serial: bool = False,
) -> ActiveWorktree:
    return ActiveWorktree(
        issue_number=issue_number,
        branch=f"feat/issue-{issue_number}",
        worktree_path=f"/tmp/issue-{issue_number}",
        pid=None,
        started_at=None,
        declared_footprint=footprint,
        reservation_kind=reservation_kind,
        forced_serial=forced_serial,
    )


def _conflict(
    candidate: ActiveWorktree,
    active: ActiveWorktree,
    candidate_task: CycleTask,
    active_task: CycleTask,
) -> ClaimConflictReason:
    result = evaluate_claim_conflicts(
        candidate,
        RunState(active_worktrees={str(active.issue_number): active}),
        _View(
            {
                candidate_task.issue_number: candidate_task,
                active_task.issue_number: active_task,
            }
        ),
    )
    assert result is not None
    return result.reason


def test_generated_claim_ids_are_unique_and_owner_token_repr_is_masked() -> None:
    assert new_claim_id() != new_claim_id()
    token = new_owner_token()
    assert token.value not in repr(token)
    assert token.value not in str(token)
    assert owner_token_digest(token) != token.value
    assert owner_token_digest(token) == owner_token_digest(token)


def test_request_and_outcome_repr_do_not_expose_owner_token() -> None:
    raw_token = new_owner_token().value
    assert raw_token not in repr(ClaimRequest(issue_number=10, owner_token=raw_token))
    assert raw_token not in repr(
        ClaimOutcome(success=True, issue_number=10, owner_token=raw_token)
    )


def test_build_reservation_uses_footprint_or_explicit_repository_scope() -> None:
    request = ClaimRequest(
        issue_number=10,
        owner_kind=OwnerKind.INTERACTIVE,
        owner_token=new_owner_token().value,
    )
    footprint = build_reservation(request, _task(10, footprint=("a.py",)))
    repository = build_reservation(request, _task(10))

    assert footprint.declared_footprint == ("a.py",)
    assert footprint.reservation_kind == ReservationKind.FOOTPRINT.value
    assert repository.declared_footprint == ()
    assert repository.reservation_kind == ReservationKind.REPOSITORY.value
    assert footprint.claim_id and footprint.claim_stage == "reserved"
    assert footprint.owner_token_digest is not None


def test_build_reservation_requires_the_caller_to_retain_an_owner_token() -> None:
    with pytest.raises(ValueError, match="owner token"):
        build_reservation(ClaimRequest(issue_number=10), _task(10))


def test_build_reservation_and_owner_token_reject_empty_or_whitespace() -> None:
    with pytest.raises(ValueError, match="owner token"):
        build_reservation(ClaimRequest(issue_number=10, owner_token=""), _task(10))
    with pytest.raises(ValueError, match="owner token"):
        build_reservation(ClaimRequest(issue_number=10, owner_token="   "), _task(10))
    with pytest.raises(ValueError, match="owner token"):
        OwnerToken("")
    with pytest.raises(ValueError, match="owner token"):
        OwnerToken("   ")
    with pytest.raises(ValueError, match="owner token"):
        owner_token_digest("")
    with pytest.raises(ValueError, match="owner token"):
        owner_token_digest("   ")


def test_same_issue_and_overlapping_footprints_conflict() -> None:
    candidate = _active(10, footprint=("a.py",))
    assert (
        _conflict(
            candidate,
            _active(10, footprint=("z.py",)),
            _task(10, footprint=("a.py",)),
            _task(10, footprint=("z.py",)),
        )
        == ClaimConflictReason.SAME_ISSUE
    )
    assert (
        _conflict(
            candidate,
            _active(11, footprint=("a.py",)),
            _task(10, footprint=("a.py",)),
            _task(11, footprint=("a.py",)),
        )
        == ClaimConflictReason.FOOTPRINT_OVERLAP
    )


def test_repository_reservation_conflicts_symmetrically() -> None:
    footprint = _active(10, footprint=("a.py",))
    repository = _active(11, reservation_kind="repository")
    assert (
        _conflict(footprint, repository, _task(10, footprint=("a.py",)), _task(11))
        == ClaimConflictReason.REPOSITORY_RESERVATION
    )
    assert (
        _conflict(repository, footprint, _task(11), _task(10, footprint=("a.py",)))
        == ClaimConflictReason.REPOSITORY_RESERVATION
    )


def test_forced_serial_and_shared_contract_writers_conflict() -> None:
    candidate = _active(10, footprint=("a.py",))
    # #943: forced_serialは、dispatch自身のスケジューラ
    # （`_candidate_conflicts_with_forced_serial_active`）と同じく、footprintが
    # 重なる場合のみ衝突として報告する（無関係な候補まで一律ブロックしない）。
    assert (
        _conflict(
            candidate,
            _active(11, forced_serial=True, footprint=("a.py",)),
            _task(10, footprint=("a.py",)),
            _task(11, footprint=("a.py",)),
        )
        == ClaimConflictReason.FORCED_SERIAL
    )
    assert (
        _conflict(
            candidate,
            _active(11, footprint=("z.py",)),
            _task(10, footprint=("a.py",), contract="claim", writer=True),
            _task(11, footprint=("z.py",), contract="claim", writer=True),
        )
        == ClaimConflictReason.SHARED_CONTRACT
    )


def test_forced_serial_without_footprint_overlap_does_not_conflict() -> None:
    """#943: dispatchが自身のスケジューラで既に「無関係」と判定した候補まで、
    claimのforced_serial判定が一律にブロックしてはならない
    （dispatch/filters.pyの既存の絞り込み挙動と揃える）。"""
    candidate = _active(10, footprint=("a.py",))
    active = _active(11, forced_serial=True, footprint=("z.py",))
    result = evaluate_claim_conflicts(
        candidate,
        RunState(active_worktrees={str(active.issue_number): active}),
        _View({10: _task(10, footprint=("a.py",)), 11: _task(11, footprint=("z.py",))}),
    )
    assert result is None
    assert (
        _conflict(
            candidate,
            _active(11, footprint=("formats/b_registry.py",)),
            _task(10, footprint=("plugins/a_registry.py",), contract="claim"),
            _task(11, footprint=("formats/b_registry.py",), contract="claim"),
        )
        == ClaimConflictReason.SHARED_CONTRACT
    )

"""Tests for pure claim ownership reservation and conflict evaluation."""

from __future__ import annotations

from dataclasses import dataclass, replace

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
    build_claim_info,
    build_reservation,
    evaluate_claim_conflicts,
    new_claim_id,
    new_owner_token,
    owner_token_digest,
    with_claim,
)
from orchestune.ledger.active_records import (
    ActiveCompletionJournal,
    ActiveWorktreeCore,
    ClaimInfo,
    LaunchInfo,
)
from orchestune.ledger.run_state import ActiveWorktree, RunState
from orchestune.task_metadata import CycleTask
from tests.dispatch_test_support import make_test_active_worktree


def test_dispatch_claim_identity_factory_preserves_owner_and_scope() -> None:
    request = ClaimRequest(
        issue_number=10, owner_kind=OwnerKind.DISPATCH, owner_token="dispatch-token"
    )
    claim = build_claim_info(request, _task(10, footprint=("a.py",)))
    assert claim.owner_kind == "dispatch"
    assert claim.claim_id and claim.claim_stage == "reserved"
    assert claim.owner_token_digest == owner_token_digest("dispatch-token")
    assert claim.reservation_kind == "footprint"
    assert build_claim_info(request, _task(10)).reservation_kind == "repository"


def test_claim_update_replaces_only_claim_and_keeps_original_generation() -> None:
    original = ActiveWorktree.from_records(
        core=ActiveWorktreeCore(10, "task/10", "worktrees/10", ("a.py",)),
        launch=LaunchInfo(pid=123, launch_phase="launched"),
        claim=ClaimInfo(
            owner_kind="dispatch",
            claim_id="generation-1",
            owner_token_digest="digest-1",
        ),
        completion=ActiveCompletionJournal(
            completion_id="completion-1", completion_payload={"items": [1]}
        ),
    )
    updated = with_claim(
        original, replace(original.claim, claim_stage="completed", base_sha="base")
    )
    assert updated.claim == replace(
        original.claim, claim_stage="completed", base_sha="base"
    )
    assert updated.core == original.core
    assert updated.launch == original.launch
    assert updated.completion == original.completion
    assert original.claim.claim_stage is None
    assert original.claim.claim_id == updated.claim.claim_id == "generation-1"
    assert updated.claim.owner_token_digest == "digest-1"


@pytest.mark.parametrize(
    "journal",
    [
        ActiveCompletionJournal(completion_id="completion-1"),
        ActiveCompletionJournal(
            completion_id="completion-1", completion_stage="handed_off"
        ),
        ActiveCompletionJournal(
            completion_id="completion-1", completion_handoff_ready=True
        ),
    ],
)
def test_completion_lifecycle_suppresses_amend_recovery_hint(
    journal: ActiveCompletionJournal,
) -> None:
    from orchestune.claim.ownership import held_claim_next_actions

    active = ActiveWorktree.from_records(
        core=ActiveWorktreeCore(10, "task/10", "worktrees/10", ("a.py",)),
        launch=LaunchInfo(pid=123),
        claim=ClaimInfo(
            owner_kind="interactive", claim_id="generation-1", claim_stage="completed"
        ),
        completion=journal,
    )
    assert not any(
        "--amend-footprint" in action for action in held_claim_next_actions(active)
    )


@pytest.mark.parametrize(
    "journal",
    [
        ActiveCompletionJournal(completion_stage="handed_off"),
        ActiveCompletionJournal(completion_handoff_ready=True),
    ],
)
def test_handoff_candidate_without_identity_preserves_legacy_claim_eligibility(
    journal: ActiveCompletionJournal,
) -> None:
    from orchestune.claim.amend import _check_eligibility
    from orchestune.claim.ownership import held_claim_next_actions

    active = ActiveWorktree.from_records(
        core=ActiveWorktreeCore(10, "task/10", "worktrees/10", ("a.py",)),
        launch=LaunchInfo(pid=123),
        claim=ClaimInfo(
            owner_kind="interactive",
            claim_id="generation-1",
            claim_stage="completed",
            repository_id="repo/.git",
        ),
        completion=journal,
    )
    assert any(
        "--amend-footprint" in action for action in held_claim_next_actions(active)
    )
    _check_eligibility(active, "repo/.git")


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
    return make_test_active_worktree(
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
        RunState(active_worktrees={str(active.core.issue_number): active}),
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

    assert footprint.core.declared_footprint == ("a.py",)
    assert footprint.claim.reservation_kind == ReservationKind.FOOTPRINT.value
    assert repository.core.declared_footprint == ()
    assert repository.claim.reservation_kind == ReservationKind.REPOSITORY.value
    assert footprint.claim.claim_id and footprint.claim.claim_stage == "reserved"
    assert footprint.claim.owner_token_digest is not None


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
        RunState(active_worktrees={str(active.core.issue_number): active}),
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


def test_footprint_overlap_matches_canonicalized_paths() -> None:
    # 台帳を経由した古い表記の `./src/foo.py` を持つ active と、
    # Windows区切り文字を含む予約候補がどちらも正規化され FOOTPRINT_OVERLAP になることを検証する。
    from orchestune.dag.models import canonicalize_footprint

    candidate = _active(10, footprint=canonicalize_footprint((r"src\foo.py",)))
    active = _active(11, footprint=canonicalize_footprint(("./src/foo.py",)))
    assert (
        _conflict(
            candidate,
            active,
            _task(10, footprint=("src/foo.py",)),
            _task(11, footprint=("src/foo.py",)),
        )
        == ClaimConflictReason.FOOTPRINT_OVERLAP
    )

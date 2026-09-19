from __future__ import annotations

from itertools import permutations

import pytest

from orchestune.models import PrRecord
from orchestune.task_branch_resolution import (
    BranchCapability,
    CanonicalBranchState,
    ResolutionSource,
    TaskBranchResolver,
    TaskMergeReceipt,
)


def _pr(
    number: int,
    head: str,
    *,
    issue: int = 42,
    state: str = "OPEN",
    cross_repository: bool | None = False,
    closes: tuple[int, ...] | None = None,
) -> PrRecord:
    return PrRecord(
        number=number,
        head_ref=head,
        changed_files=(),
        state=state,
        is_cross_repository=cross_repository,
        closes_issue_numbers=(issue,) if closes is None else closes,
    )


def test_canonical_present_always_wins_and_returns_verified_canonical_pr() -> None:
    canonical = _pr(10, "claude/issue-42-task-a")
    fallback = _pr(11, "feat/issue-42-task-a")
    resolution = TaskBranchResolver((fallback, canonical)).resolve(
        42, "task-a", CanonicalBranchState.PRESENT
    )

    assert resolution.branch_name == canonical.head_ref
    assert resolution.source is ResolutionSource.CANONICAL
    assert resolution.canonical_state is CanonicalBranchState.PRESENT
    assert resolution.pr is canonical
    assert resolution.allows(BranchCapability.FETCH_MERGE)
    assert resolution.allows(BranchCapability.LINK_PR)
    assert resolution.allows(BranchCapability.DELETE)
    assert not resolution.allows(BranchCapability.VERIFY_MERGED)


def test_confirmed_absence_selects_one_exact_upstream_open_pr() -> None:
    fallback = _pr(11, "feat/issue-42-task-a")
    resolution = TaskBranchResolver((fallback,)).resolve(
        42, "task-a", CanonicalBranchState.ABSENT
    )

    assert resolution.branch_name == fallback.head_ref
    assert resolution.source is ResolutionSource.PR_FALLBACK
    assert resolution.pr is fallback
    assert resolution.allows(BranchCapability.FETCH_MERGE)
    assert resolution.allows(BranchCapability.LINK_PR)
    assert not resolution.allows(BranchCapability.DELETE)
    assert not resolution.allows(BranchCapability.VERIFY_MERGED)


@pytest.mark.parametrize(
    "candidate",
    [
        _pr(1, "feat/issue-42-task-a", state="CLOSED"),
        _pr(2, "feat/issue-42-task-a", cross_repository=True),
        _pr(3, "feat/issue-42-task-a", cross_repository=None),
        _pr(4, "feat/issue-42-other"),
        _pr(5, "feat/issue-99-task-a", issue=99),
        _pr(6, "feat/issue-42-task-a", closes=(999,)),
        _pr(7, "manual-branch", closes=(42,)),
    ],
)
def test_unsafe_fallback_candidates_are_rejected(candidate: PrRecord) -> None:
    resolution = TaskBranchResolver((candidate,)).resolve(
        42, "task-a", CanonicalBranchState.ABSENT
    )

    assert resolution.branch_name == "claude/issue-42-task-a"
    assert resolution.source is ResolutionSource.CANONICAL
    assert resolution.pr is None
    assert not resolution.allows(BranchCapability.FETCH_MERGE)
    assert not resolution.allows(BranchCapability.LINK_PR)
    assert not resolution.allows(BranchCapability.DELETE)


def test_empty_closing_metadata_is_allowed_when_branch_identity_is_exact() -> None:
    fallback = _pr(11, "fix/issue-42-task-a", closes=())
    resolution = TaskBranchResolver((fallback,)).resolve(
        42, "task-a", CanonicalBranchState.ABSENT
    )

    assert resolution.source is ResolutionSource.PR_FALLBACK
    assert resolution.pr is fallback


def test_multiple_distinct_fallback_prs_fail_closed_regardless_of_order() -> None:
    candidates = (
        _pr(10, "feat/issue-42-task-a"),
        _pr(11, "fix/issue-42-task-a"),
    )

    for ordered in permutations(candidates):
        resolution = TaskBranchResolver(ordered).resolve(
            42, "task-a", CanonicalBranchState.ABSENT
        )
        assert resolution.source is ResolutionSource.CANONICAL
        assert resolution.pr is None
        assert not resolution.allows(BranchCapability.FETCH_MERGE)


def test_duplicate_fetch_records_do_not_create_ambiguity() -> None:
    fallback = _pr(11, "feat/issue-42-task-a")
    resolution = TaskBranchResolver((fallback, fallback)).resolve(
        42, "task-a", CanonicalBranchState.ABSENT
    )

    assert resolution.source is ResolutionSource.PR_FALLBACK
    assert resolution.pr is fallback


def test_conflicting_duplicate_metadata_fails_closed_regardless_of_order() -> None:
    upstream = _pr(11, "feat/issue-42-task-a")
    conflicting = _pr(
        11,
        "feat/issue-42-task-a",
        cross_repository=None,
    )

    for ordered in permutations((upstream, conflicting)):
        resolution = TaskBranchResolver(ordered).resolve(
            42, "task-a", CanonicalBranchState.ABSENT
        )
        assert resolution.source is ResolutionSource.CANONICAL
        assert resolution.pr is None
        assert not resolution.allows(BranchCapability.FETCH_MERGE)


def test_indeterminate_canonical_state_never_enables_operational_capability() -> None:
    fallback = _pr(11, "feat/issue-42-task-a")
    resolution = TaskBranchResolver((fallback,)).resolve(
        42, "task-a", CanonicalBranchState.INDETERMINATE
    )

    assert resolution.branch_name == "claude/issue-42-task-a"
    assert resolution.source is ResolutionSource.CANONICAL
    assert resolution.pr is None
    assert not resolution.allows(BranchCapability.FETCH_MERGE)
    assert not resolution.allows(BranchCapability.LINK_PR)
    assert not resolution.allows(BranchCapability.DELETE)
    assert not resolution.allows(BranchCapability.VERIFY_MERGED)


def test_canonical_pr_metadata_is_not_reselected_from_same_head_fork() -> None:
    upstream = _pr(10, "claude/issue-42-task-a")
    fork = _pr(
        99,
        "claude/issue-42-task-a",
        cross_repository=True,
    )

    for ordered in permutations((fork, upstream)):
        resolution = TaskBranchResolver(ordered).resolve(
            42, "task-a", CanonicalBranchState.PRESENT
        )
        assert resolution.pr is upstream


def test_merge_receipt_uses_issue_identity_and_immutable_fetched_oid() -> None:
    resolution = TaskBranchResolver((_pr(11, "feat/issue-42-task-a"),)).resolve(
        42, "task-a", CanonicalBranchState.ABSENT
    )

    receipt = TaskMergeReceipt.from_resolution(resolution, "a" * 40)

    assert receipt.issue_number == 42
    assert receipt.branch_name == "feat/issue-42-task-a"
    assert receipt.fetched_commit_oid == "a" * 40
    assert receipt.source is ResolutionSource.PR_FALLBACK
    assert receipt.allows(BranchCapability.VERIFY_MERGED)
    assert not receipt.allows(BranchCapability.DELETE)


def test_merge_receipt_rejects_invalid_oid_on_every_construction_path() -> None:
    with pytest.raises(ValueError, match="invalid fetched commit OID"):
        TaskMergeReceipt(
            issue_number=42,
            branch_name="feat/issue-42-task-a",
            fetched_commit_oid="not-an-oid",
            source=ResolutionSource.PR_FALLBACK,
        )

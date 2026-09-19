"""Canonical-first task branch resolution shared by dispatch and integration."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeGuard

from orchestune.branch_naming import (
    branch_matches_task,
    build_task_branch_name,
)
from orchestune.models import PrRecord

_COMMIT_OID = re.compile(r"^[0-9a-fA-F]{40}$")


def is_commit_oid(value: object) -> TypeGuard[str]:
    """Return whether *value* is a complete SHA-1 object identifier."""
    return isinstance(value, str) and _COMMIT_OID.fullmatch(value) is not None


class ResolutionSource(StrEnum):
    CANONICAL = "canonical"
    PR_FALLBACK = "pr_fallback"


class CanonicalBranchState(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    INDETERMINATE = "indeterminate"


def probe_canonical_state(
    canonical_branch: str, lookup: Callable[[str], bool]
) -> CanonicalBranchState:
    """Convert Forge's bool-or-exception branch contract into three states."""
    try:
        return (
            CanonicalBranchState.PRESENT
            if lookup(canonical_branch)
            else CanonicalBranchState.ABSENT
        )
    except Exception:
        return CanonicalBranchState.INDETERMINATE


class BranchCapability(StrEnum):
    FETCH_MERGE = "fetch_merge"
    LINK_PR = "link_pr"
    DELETE = "delete"
    VERIFY_MERGED = "verify_merged"


@dataclass(frozen=True, slots=True)
class TaskBranchResolution:
    issue_number: int
    canonical_branch: str
    branch_name: str
    source: ResolutionSource
    canonical_state: CanonicalBranchState
    pr: PrRecord | None

    def allows(self, capability: BranchCapability) -> bool:
        operational = (
            self.canonical_state is CanonicalBranchState.PRESENT
            if self.source is ResolutionSource.CANONICAL
            else (
                self.canonical_state is CanonicalBranchState.ABSENT
                and self.pr is not None
            )
        )
        if capability is BranchCapability.LINK_PR:
            return operational and self.pr is not None
        if capability is BranchCapability.DELETE:
            return operational and self.source is ResolutionSource.CANONICAL
        if capability is BranchCapability.VERIFY_MERGED:
            # A resolution has no immutable fetched OID. Verification becomes
            # available only after it is promoted to a TaskMergeReceipt.
            return False
        return operational


@dataclass(frozen=True, slots=True)
class TaskMergeReceipt:
    issue_number: int
    branch_name: str
    fetched_commit_oid: str
    source: ResolutionSource

    def __post_init__(self) -> None:
        # Receipts are also reconstructed from durable integration proofs after
        # restart, so enforce the immutable-OID invariant at the value boundary
        # rather than only in the live-resolution factory.
        if not is_commit_oid(self.fetched_commit_oid):
            raise ValueError(f"invalid fetched commit OID: {self.fetched_commit_oid!r}")

    @classmethod
    def from_resolution(
        cls, resolution: TaskBranchResolution, fetched_commit_oid: str
    ) -> TaskMergeReceipt:
        if not resolution.allows(BranchCapability.FETCH_MERGE):
            raise ValueError("branch resolution does not permit fetch/merge")
        return cls.from_verified_merge(
            issue_number=resolution.issue_number,
            branch_name=resolution.branch_name,
            fetched_commit_oid=fetched_commit_oid,
            source=resolution.source,
        )

    @classmethod
    def from_verified_merge(
        cls,
        *,
        issue_number: int,
        branch_name: str,
        fetched_commit_oid: str,
        source: ResolutionSource,
    ) -> TaskMergeReceipt:
        """Reconstruct a receipt from trusted, already-integrated proof data."""
        return cls(
            issue_number=issue_number,
            branch_name=branch_name,
            fetched_commit_oid=fetched_commit_oid,
            source=source,
        )

    def allows(self, capability: BranchCapability) -> bool:
        if capability is BranchCapability.DELETE:
            return self.source is ResolutionSource.CANONICAL
        return capability is BranchCapability.VERIFY_MERGED


class TaskBranchResolver:
    """Resolve task refs from one immutable, cycle-scoped PR snapshot."""

    def __init__(self, prs: Iterable[PrRecord]) -> None:
        by_identity: dict[tuple[int, str], list[PrRecord]] = {}
        for pr in prs:
            by_identity.setdefault((pr.number, pr.head_ref), []).append(pr)
        unique = [
            records[0]
            for records in by_identity.values()
            if all(record == records[0] for record in records[1:])
        ]
        self._prs = tuple(sorted(unique, key=lambda pr: (pr.number, pr.head_ref)))

    @staticmethod
    def _matches(pr: PrRecord, issue_number: int, subtask_id: str) -> bool:
        if pr.state != "OPEN" or pr.is_cross_repository is not False:
            return False
        if not branch_matches_task(pr.head_ref, issue_number, subtask_id):
            return False
        closings = tuple(pr.closes_issue_numbers)
        if any(
            not isinstance(number, int) or isinstance(number, bool)
            for number in closings
        ):
            return False
        return not closings or issue_number in closings

    def _matching_prs(self, issue_number: int, subtask_id: str) -> tuple[PrRecord, ...]:
        return tuple(
            pr for pr in self._prs if self._matches(pr, issue_number, subtask_id)
        )

    @staticmethod
    def _unique(candidates: Iterable[PrRecord]) -> PrRecord | None:
        values = tuple(candidates)
        identities = {(pr.number, pr.head_ref) for pr in values}
        return values[0] if len(identities) == 1 else None

    def has_verified_candidate(self, issue_number: int, subtask_id: str) -> bool:
        return bool(self._matching_prs(issue_number, subtask_id))

    def resolve(
        self,
        issue_number: int,
        subtask_id: str,
        canonical_state: CanonicalBranchState,
    ) -> TaskBranchResolution:
        canonical = build_task_branch_name(issue_number, subtask_id)
        matches = self._matching_prs(issue_number, subtask_id)
        canonical_pr = self._unique(pr for pr in matches if pr.head_ref == canonical)
        if canonical_state is CanonicalBranchState.PRESENT:
            return TaskBranchResolution(
                issue_number,
                canonical,
                canonical,
                ResolutionSource.CANONICAL,
                canonical_state,
                canonical_pr,
            )
        if canonical_state is CanonicalBranchState.ABSENT:
            fallback = self._unique(pr for pr in matches if pr.head_ref != canonical)
            if fallback is not None:
                return TaskBranchResolution(
                    issue_number,
                    canonical,
                    fallback.head_ref,
                    ResolutionSource.PR_FALLBACK,
                    canonical_state,
                    fallback,
                )
        return TaskBranchResolution(
            issue_number,
            canonical,
            canonical,
            ResolutionSource.CANONICAL,
            canonical_state,
            None,
        )


__all__ = [
    "BranchCapability",
    "CanonicalBranchState",
    "ResolutionSource",
    "TaskBranchResolution",
    "TaskBranchResolver",
    "TaskMergeReceipt",
    "is_commit_oid",
    "probe_canonical_state",
]

"""Immutable evidence captured for one child branch integration."""

from __future__ import annotations

from dataclasses import dataclass, field

from orchestune.task_branch_resolution import (
    ResolutionSource,
    TaskBranchResolution,
    TaskMergeReceipt,
)


@dataclass(frozen=True)
class TaskIntegrationProof:
    """The exact child-branch commit that integration CI verified."""

    issue_number: int
    subtask_id: str
    branch_name: str
    source_sha: str
    source: ResolutionSource = ResolutionSource.CANONICAL
    resolution: TaskBranchResolution | None = field(
        default=None, compare=False, repr=False
    )

    @property
    def merge_receipt(self) -> TaskMergeReceipt:
        if self.resolution is not None:
            return TaskMergeReceipt.from_resolution(self.resolution, self.source_sha)
        # Durable proofs recovered from trusted integration-receipt comments no
        # longer have the cycle's resolution object, but already attest that the
        # merge happened. They use the explicit post-merge reconstruction path.
        return TaskMergeReceipt.from_verified_merge(
            issue_number=self.issue_number,
            branch_name=self.branch_name,
            fetched_commit_oid=self.source_sha,
            source=self.source,
        )

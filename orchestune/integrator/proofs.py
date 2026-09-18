"""Immutable evidence captured for one child branch integration."""

from __future__ import annotations

from dataclasses import dataclass

from orchestune.task_branch_resolution import ResolutionSource, TaskMergeReceipt


@dataclass(frozen=True)
class TaskIntegrationProof:
    """The exact child-branch commit that integration CI verified."""

    issue_number: int
    subtask_id: str
    branch_name: str
    source_sha: str
    source: ResolutionSource = ResolutionSource.CANONICAL

    @property
    def merge_receipt(self) -> TaskMergeReceipt:
        return TaskMergeReceipt(
            issue_number=self.issue_number,
            branch_name=self.branch_name,
            fetched_commit_oid=self.source_sha,
            source=self.source,
        )

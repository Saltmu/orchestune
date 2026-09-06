"""Immutable evidence captured for one child branch integration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TaskIntegrationProof:
    """The exact child-branch commit that integration CI verified."""

    issue_number: int
    subtask_id: str
    branch_name: str
    source_sha: str

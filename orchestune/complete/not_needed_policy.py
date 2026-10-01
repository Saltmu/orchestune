"""Shared downstream target selection for claimed and worktree-free completion."""

from typing import Any

from orchestune.complete.contracts import CompleteRequest, DownstreamPolicyRecord


def not_needed_policies(
    request: CompleteRequest,
    active: Any | None,
    repository: str,
    generation: str,
    completion_id: str,
    context: dict[str, Any],
) -> tuple[DownstreamPolicyRecord, ...]:
    # A missing worktree does not establish an independent-review exemption.
    if request.result != "not-needed" or (
        active is not None and active.launch.external_id is None
    ):
        return ()
    return (
        DownstreamPolicyRecord(
            repository,
            request.issue_number,
            generation,
            completion_id,
            "not-needed-review",
            metadata={"context": context, "close_after_review": True},
        ),
    )

"""Dispatch launch owner API for ActiveWorktree launch fields (#1130).

Launch construction, launch-only updates, execution selection and the
launch-time token estimate are written only through these functions. Claim
identity stays a ``ClaimInfo`` owned by ``claim.ownership``; completion fields
stay an ``ActiveCompletionJournal``. Launch attempt markers and dispatch handles
are a separate durable contract and are not modelled here.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from orchestune.ledger.active_records import (
    ActiveCompletionJournal,
    ActiveWorktree,
    ActiveWorktreeCore,
    ClaimInfo,
    LaunchInfo,
)

if TYPE_CHECKING:
    from orchestune.targets.contracts import ExecutionSelection


def build_launch_record(
    *,
    core: ActiveWorktreeCore,
    launch: LaunchInfo,
    claim: ClaimInfo | None = None,
    completion: ActiveCompletionJournal | None = None,
) -> ActiveWorktree:
    """Build a dispatch record from typed owner records."""
    return ActiveWorktree.from_records(
        core=core,
        launch=launch,
        claim=ClaimInfo() if claim is None else claim,
        completion=ActiveCompletionJournal() if completion is None else completion,
    )


def with_launch(active: ActiveWorktree, launch: LaunchInfo) -> ActiveWorktree:
    """Return a copy replacing only launch-owned fields."""
    if not isinstance(launch, LaunchInfo):
        raise TypeError("launch must be LaunchInfo")
    return replace(active, launch=launch)


def update_launch(active: ActiveWorktree, launch: LaunchInfo) -> None:
    """Apply a launch update in place for observers retaining the active object."""
    if not isinstance(launch, LaunchInfo):
        raise TypeError("launch must be LaunchInfo")
    active.launch = launch


def with_launch_phase(active: ActiveWorktree, phase: str | None) -> ActiveWorktree:
    """Return a copy with only ``launch_phase`` changed."""
    return with_launch(active, replace(active.launch, launch_phase=phase))


def launch_info_with_selection(
    launch: LaunchInfo,
    selection: ExecutionSelection | None,
    *,
    fallback_profile: str | None = None,
) -> LaunchInfo:
    """Record the execution selection, or only the declared profile without one."""
    if selection is None:
        return replace(
            launch,
            profile=fallback_profile,
            model=None,
            reasoning_effort=None,
            selection_reason=None,
        )
    return replace(
        launch,
        profile=selection.profile,
        model=selection.model,
        reasoning_effort=selection.reasoning_effort,
        selection_reason=selection.reason,
    )


def launch_info_with_token_estimate(
    launch: LaunchInfo, estimated_tokens: int | None
) -> LaunchInfo:
    """Record the launch-time estimate; ``None`` still marks it as recorded."""
    return replace(
        launch, estimated_tokens=estimated_tokens, token_estimate_recorded=True
    )

"""Release interactive claims whose Issue or PR is already closed (#1526).

An interactive claim is normally collected only after ``orchestune complete``
hands it off. When the agent never runs ``complete`` (for example after its PR
was merged by override), the claim stays in ``active_worktrees`` and blocks
later claims with ``footprint_overlap``. This module releases such a claim with
the same meaning as ``orchestune recover`` release: the active entry leaves the
ledger with a ``release`` receipt, while the worktree, branch and claim marker
are retained because uncommitted work cannot be ruled out.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from orchestune.claim.inspection import inspect_claim
from orchestune.claim.local_identity import registered_claim_path
from orchestune.claim.workspace import ClaimWorkspace, resolve_claim_workspace
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_events import (
    InteractiveClosedTargetHeld,
    InteractiveClosedTargetReleased,
)
from orchestune.dispatch.gc.collection import GcItemResult, _make_item
from orchestune.infra.json_state import write_json_atomic
from orchestune.infra.process_utils import FileLockContentionError, file_lock
from orchestune.ledger.completion_reservations import completion_reservation_status
from orchestune.ledger.recovery_receipts import (
    RELEASE_OPERATION,
    build_recovery_receipt,
    record_recovery_in_raw,
)
from orchestune.ledger.run_state import (
    ActiveWorktree,
    RunState,
    load_run_state_readonly,
    save_run_state,
)
from orchestune.models import PrRecord
from orchestune.worktree_ops.claim_marker import claim_lock_path

CLOSED_TARGET_STATES = frozenset({"CLOSED", "MERGED"})
CLOSED_TARGET_REASON = "closed_target"


class IssueStateReader(Protocol):
    def get_issue_state(self, issue_number: int | str) -> str: ...


@dataclass(frozen=True, slots=True)
class ClosedTargetDecision:
    key: str
    active: ActiveWorktree
    action: Literal["release", "hold", "skip"]
    reason: str
    target_state: str | None = None

    @property
    def receipt_reason(self) -> str:
        return f"closed-target:{self.target_state}"


def _reservation_status(state: RunState, issue_number: int) -> str:
    try:
        return completion_reservation_status(state, issue_number)
    except ValueError:
        return "invalid"


def is_closed_target_candidate(active: ActiveWorktree, state: RunState) -> bool:
    """Interactive claims that no completion or handoff path owns yet."""
    completion = active.completion
    return (
        active.claim.owner_kind == "interactive"
        and not completion.completion_id
        and not completion.completion_stage
        and not completion.completion_handoff_ready
        and _reservation_status(state, active.core.issue_number) == "legacy"
    )


def closed_target_candidates(state: RunState) -> list[tuple[str, ActiveWorktree]]:
    return sorted(
        (key, active)
        for key, active in state.active_worktrees.items()
        if is_closed_target_candidate(active, state)
    )


def decide_closed_target(
    key: str,
    active: ActiveWorktree,
    state: RunState,
    workspace: ClaimWorkspace,
    forge: IssueStateReader | None,
    cwd: Path | None = None,
) -> ClosedTargetDecision:
    """Decide from live Forge state and a fresh claim inspection; never guess."""
    if not is_closed_target_candidate(active, state):
        return ClosedTargetDecision(key, active, "skip", "not_candidate")
    if forge is None:
        return ClosedTargetDecision(key, active, "hold", "forge_unavailable")
    try:
        target_state = str(forge.get_issue_state(active.core.issue_number)).upper()
    except Exception as error:  # noqa: BLE001 - an unknown state never releases
        return ClosedTargetDecision(key, active, "hold", f"forge_error: {error}")
    if target_state not in CLOSED_TARGET_STATES:
        return ClosedTargetDecision(key, active, "skip", "target_open", target_state)
    _, problem = inspect_claim(
        active,
        state,
        workspace,
        (cwd or workspace.repository_root).resolve(),
        restore_marker=False,
    )
    if problem:
        return ClosedTargetDecision(key, active, "hold", problem, target_state)
    return ClosedTargetDecision(
        key, active, "release", CLOSED_TARGET_REASON, target_state
    )


def release_in_state(
    state: RunState, decision: ClosedTargetDecision, repository_id: str
) -> None:
    """Remove the entry and add its receipt to an in-memory ``RunState``."""
    if decision.action != "release":
        raise ValueError("only a release decision can be applied")
    active = decision.active
    receipt_key, receipt = build_recovery_receipt(
        repository_id=repository_id,
        issue_number=active.core.issue_number,
        active=active,
        operation=RELEASE_OPERATION,
        reason=decision.receipt_reason,
    )
    del state.active_worktrees[decision.key]
    state.recovery_receipts[receipt_key] = receipt


def decision_event(
    decision: ClosedTargetDecision, *, applied: bool
) -> InteractiveClosedTargetReleased | InteractiveClosedTargetHeld | None:
    active = decision.active
    if decision.action == "hold":
        return InteractiveClosedTargetHeld(
            issue_number=active.core.issue_number,
            claim_id=active.claim.claim_id,
            reason=decision.reason,
        )
    if decision.action != "release" or decision.target_state is None:
        return None
    return InteractiveClosedTargetReleased(
        issue_number=active.core.issue_number,
        claim_id=active.claim.claim_id,
        target_state=decision.target_state,
        reason=decision.receipt_reason,
        action="gc_released_interactive_closed_target"
        if applied
        else "gc_would_release_interactive_closed_target",
    )


def _dispatcher_workspace(config: DispatcherConfig) -> ClaimWorkspace:
    """Resolve from the Dispatcher's own checkout, like its shared-path setup.

    ``worktree_root`` may live outside the repository, so its parent is not a
    reliable checkout for the repository identity.
    """
    return resolve_claim_workspace(
        None,
        explicit_state_path=config.run_state_path,
        explicit_worktree_root=config.worktree_root,
    )


def _persist_release(
    run_state: RunState,
    decision: ClosedTargetDecision,
    workspace: ClaimWorkspace,
    config: DispatcherConfig,
    now: float | None,
    open_prs: Sequence[PrRecord],
) -> str | None:
    """Release and save before launch, because ``claim_task`` reads the file."""
    receipts_before = dict(run_state.recovery_receipts)
    release_in_state(run_state, decision, workspace.repository_identity)
    try:
        save_run_state(
            run_state,
            config.run_state_path,
            now=now,
            launch_window_seconds=config.window_seconds,
            open_prs=open_prs,
        )
    except Exception as error:  # noqa: BLE001 - keep the claim when unsaved
        run_state.active_worktrees[decision.key] = decision.active
        run_state.recovery_receipts = receipts_before
        print(
            "Warning: kept the interactive claim of issue "
            f"#{decision.active.core.issue_number}: failed to persist its "
            f"closed-target release to {config.run_state_path}: {error}",
            file=sys.stderr,
        )
        return f"persistence_failed: {error}"
    return None


def release_closed_target_claims(
    run_state: RunState,
    config: DispatcherConfig,
    *,
    now: float | None = None,
    open_prs: Sequence[PrRecord] = (),
) -> list[InteractiveClosedTargetReleased | InteractiveClosedTargetHeld]:
    """Dispatcher GC step: release closed-target interactive claims once.

    ``config.apply`` false only reports what would be released.
    """
    candidates = closed_target_candidates(run_state)
    if not candidates:
        return []
    try:
        workspace = _dispatcher_workspace(config)
        forge: IssueStateReader | None = config.resolved_forge
    except Exception as error:  # noqa: BLE001 - unverifiable claims are held
        return [
            InteractiveClosedTargetHeld(
                issue_number=active.core.issue_number,
                claim_id=active.claim.claim_id,
                reason=f"workspace_unavailable: {error}",
            )
            for _, active in candidates
        ]
    events: list[InteractiveClosedTargetReleased | InteractiveClosedTargetHeld] = []
    for key, active in candidates:
        decision = decide_closed_target(key, active, run_state, workspace, forge)
        if decision.action == "release" and config.apply:
            failure = _persist_release(
                run_state, decision, workspace, config, now, open_prs
            )
            if failure is not None:
                decision = ClosedTargetDecision(
                    key, active, "hold", failure, decision.target_state
                )
        event = decision_event(decision, applied=config.apply)
        if event is not None:
            events.append(event)
    return events


def _decision_item(
    decision: ClosedTargetDecision, workspace: ClaimWorkspace, *, applied: bool
) -> GcItemResult | None:
    if decision.action == "skip":
        return None
    if decision.action == "hold":
        action = "held"
    else:
        action = "released" if applied else "would_release"
    return _make_item(
        decision.key, decision.active, action, decision.reason, "retain", workspace
    )


def preview_closed_target_items(
    state: RunState,
    workspace: ClaimWorkspace,
    forge: IssueStateReader | None,
    cwd: Path | None = None,
) -> list[GcItemResult]:
    """``orchestune gc --no-apply``: report ``would_release`` without writing."""
    items = (
        _decision_item(
            decide_closed_target(key, active, state, workspace, forge, cwd),
            workspace,
            applied=False,
        )
        for key, active in closed_target_candidates(state)
    )
    return [item for item in items if item is not None]


def _release_raw(workspace: ClaimWorkspace, decision: ClosedTargetDecision) -> None:
    path = workspace.run_state_path
    raw = json.loads(path.read_text(encoding="utf-8"))
    record_recovery_in_raw(
        raw,
        decision.key,
        decision.active,
        repository_id=workspace.repository_identity,
        issue_number=decision.active.core.issue_number,
        operation=RELEASE_OPERATION,
        reason=decision.receipt_reason,
    )
    # Like ``orchestune recover``: keep unrelated records and extensions intact.
    write_json_atomic(path, raw)


def _apply_one_locked(
    key: str,
    initial: ActiveWorktree,
    workspace: ClaimWorkspace,
    forge: IssueStateReader | None,
    cwd: Path | None,
) -> GcItemResult | None:
    state = load_run_state_readonly(workspace.run_state_path)
    current = state.active_worktrees.get(key)
    if current is None or current.claim.claim_id != initial.claim.claim_id:
        return _make_item(key, initial, "held", "state_changed", "retain", workspace)
    decision = decide_closed_target(key, current, state, workspace, forge, cwd)
    if decision.action == "release":
        _release_raw(workspace, decision)
    return _decision_item(decision, workspace, applied=True)


def apply_closed_target_items(
    workspace: ClaimWorkspace,
    forge: IssueStateReader | None,
    timeout_seconds: float,
    cwd: Path | None = None,
) -> list[GcItemResult]:
    """``orchestune gc``: re-decide under the claim lock, then release.

    The caller holds the run_state lock; each claim is re-read and re-inspected
    under its own claim lock just before the write.
    """
    items: list[GcItemResult] = []
    state = load_run_state_readonly(workspace.run_state_path)
    for key, initial in closed_target_candidates(state):
        target = registered_claim_path(initial, workspace.run_state_path)
        lock = (
            file_lock(claim_lock_path(target), timeout=timeout_seconds)
            if initial.core.worktree_path.strip()
            else nullcontext()
        )
        try:
            with lock:
                item = _apply_one_locked(key, initial, workspace, forge, cwd)
        except FileLockContentionError:
            item = _make_item(key, initial, "held", "lock_timeout", "retain", workspace)
        except (OSError, ValueError, KeyError) as error:
            item = _make_item(
                key, initial, "failed", f"release_failed: {error}", "retain", workspace
            )
        if item is not None:
            items.append(item)
    return items


__all__ = [
    "CLOSED_TARGET_REASON",
    "CLOSED_TARGET_STATES",
    "ClosedTargetDecision",
    "IssueStateReader",
    "apply_closed_target_items",
    "closed_target_candidates",
    "decide_closed_target",
    "decision_event",
    "is_closed_target_candidate",
    "preview_closed_target_items",
    "release_closed_target_claims",
    "release_in_state",
]

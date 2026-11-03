"""Diagnostic inspection of OPEN unattended interactive claims (#1643).

This module inspects active interactive claims that are not undergoing completion
and whose target issues remain OPEN. It diagnoses process state without mutating
ledger, labels, worktree, or receipts, producing typed inspection events that
guide operators to explicit recovery.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from orchestune.dispatch.config.model import DispatcherConfig
from orchestune.dispatch.cycle.events import InteractiveClaimInspectionRequired
from orchestune.infra import process_utils
from orchestune.ledger.active_lifecycle import (
    ActiveWorktreeLifecycle,
    has_completion_reservation,
    lifecycle,
)
from orchestune.ledger.run_state import ActiveWorktree, RunState
from orchestune.models import PrRecord
from orchestune.ownership_contracts import OwnerKind

_CLOSED_STATES = frozenset({"CLOSED", "MERGED"})


def _check_issue_state(forge: Any, issue_number: int) -> tuple[str | None, str | None]:
    """Return (state, error_message). state is None on error."""
    if forge is None:
        return "OPEN", None
    try:
        if hasattr(forge, "get_issue_state"):
            state = forge.get_issue_state(issue_number)
        elif hasattr(forge, "get_issue"):
            issue = forge.get_issue(issue_number)
            state = getattr(issue, "state", "OPEN")
        else:
            state = "OPEN"
        return str(state).upper(), None
    except Exception as exc:
        return None, str(exc)


def _diagnose_single_interactive_claim(
    active: ActiveWorktree, forge: Any
) -> InteractiveClaimInspectionRequired | None:
    issue_number = active.core.issue_number
    claim_id = active.claim.claim_id or ""

    state, forge_error = _check_issue_state(forge, issue_number)
    if state in _CLOSED_STATES:
        # Handled by closed_target GC
        return None

    pid = active.launch.pid if active.launch is not None else None
    process_state: Literal["alive", "dead", "unknown"]
    if forge_error is not None:
        process_state = "unknown"
        reason = f"Forge check failed ({forge_error}); claim retained"
    elif pid is None:
        process_state = "unknown"
        reason = "PID未記録のため停止確認が必要; 停止確認後に明示的に解放可能"
    elif process_utils.is_process_alive(pid):
        process_state = "alive"
        reason = f"Worker process {pid} is running; claim retained"
    else:
        process_state = "dead"
        reason = f"Worker process {pid} is not running; 停止確認後に明示的に解放可能"

    recovery_command = (
        f"orchestune recover --issue {issue_number} "
        f"--claim-id {claim_id} --release-to-review "
        f'--reason "ワーカーの停止を確認" --apply'
    )

    return InteractiveClaimInspectionRequired(
        issue_number=issue_number,
        claim_id=claim_id,
        branch=active.core.branch or "",
        worktree=active.core.worktree_path or "",
        pid=pid,
        process_state=process_state,
        reason=reason,
        recovery_command=recovery_command,
    )


def diagnose_unattended_interactive_claims(
    run_state: RunState,
    config: DispatcherConfig,
    open_prs: Sequence[PrRecord] | None = None,
    *,
    forge: Any = None,
) -> list[InteractiveClaimInspectionRequired]:
    """Diagnose OPEN interactive claims requiring operator inspection."""
    active_forge = forge if forge is not None else config.forge
    events: list[InteractiveClaimInspectionRequired] = []

    for active in sorted(
        run_state.active_worktrees.values(), key=lambda a: a.core.issue_number
    ):
        if active.claim.owner_kind != OwnerKind.INTERACTIVE:
            continue
        if has_completion_reservation(active) or lifecycle(active) in {
            ActiveWorktreeLifecycle.COMPLETING,
            ActiveWorktreeLifecycle.HANDOFF_READY,
        }:
            continue

        diag = _diagnose_single_interactive_claim(active, active_forge)
        if diag is not None:
            events.append(diag)

    return events


__all__ = ["diagnose_unattended_interactive_claims"]

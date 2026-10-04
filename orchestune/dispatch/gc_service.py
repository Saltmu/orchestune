"""Local run-state cleanup for handoff-ready task reservations."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from orchestune.claim.workspace import ClaimWorkspace, resolve_claim_workspace
from orchestune.dispatch.cycle_events import TokenHoldCompletion
from orchestune.dispatch.cycle_records import CompletionReceipt
from orchestune.dispatch.gc.collection import GcItemResult as GcItemResult
from orchestune.dispatch.gc.collection import GcRunResult as GcRunResult
from orchestune.dispatch.gc.collection import (
    _apply_candidate,
    _inspect_or_hold,
    _make_item,
    _read_gc_state,
)
from orchestune.dispatch.gc.handoff import (
    GcRequest,
    HandoffForge,
)
from orchestune.dispatch.gc.outcome_decision import _is_handoff_ready
from orchestune.dispatch.gc.policies import standalone_policies
from orchestune.dispatch.gc.policy_discovery import (
    policy_candidates,
    reclaim_completed_tokens,
)
from orchestune.dispatch.gc.unclaimed import unclaimed_completion_events
from orchestune.forge import GitHubForge
from orchestune.infra.process_utils import (
    FileLockContentionError,
    run_state_lock,
)
from orchestune.ledger.completion_reservations import (
    completion_mutation_blocked,
)
from orchestune.ledger.run_state import (
    ActiveWorktree,
    load_run_state_readonly,
)


def _is_standalone_gc_candidate(active: ActiveWorktree) -> bool:
    """Limit this standalone command to interactive claims.

    Dispatch-owned worktrees need TaskMetadata to record their subtask id in
    CompletedWorktree; the dispatch cycle owns that lifecycle and KPI record.
    """
    return active.claim.owner_kind == "interactive" and _is_handoff_ready(active)


def _completion_gc_candidates(
    active: dict[str, ActiveWorktree], state_path: Path
) -> list[tuple[str, ActiveWorktree]]:
    state = load_run_state_readonly(state_path)
    return [
        (key, item)
        for key, item in active.items()
        if item.claim.owner_kind == "interactive"
        and (
            item.completion.completion_id
            or item.completion.completion_handoff_ready
            or completion_mutation_blocked(state, item.core.issue_number)
        )
    ]


def _policy_items(
    events: Sequence[dict[str, Any] | TokenHoldCompletion],
) -> list[GcItemResult]:
    return [
        GcItemResult(
            key=str(
                e.issue_number
                if isinstance(e, TokenHoldCompletion)
                else e["issue_number"]
            ),
            issue_number=e.issue_number
            if isinstance(e, TokenHoldCompletion)
            else e["issue_number"],
            result=None,
            action="held"
            if isinstance(e, TokenHoldCompletion)
            or e["action"] != "completion_policy_applied"
            else "policy_applied",
            reason=e.reason
            if isinstance(e, TokenHoldCompletion)
            else e.get("reason", e["action"]),
            worktree_path="",
            worktree_action="none",
        )
        for e in events
    ]


def _preview(
    workspace: ClaimWorkspace,
    forge_factory: Callable[[], HandoffForge],
) -> GcRunResult:
    try:
        _, active = _read_gc_state(workspace.run_state_path)
    except (OSError, ValueError):
        return GcRunResult((), 0, (), 1)
    candidates = _completion_gc_candidates(active, workspace.run_state_path)
    skipped = len(active) - len(candidates)
    forge: HandoffForge | None = None
    if any(_is_handoff_ready(item) for _, item in candidates) or policy_candidates(
        load_run_state_readonly(workspace.run_state_path)
    ):
        try:
            forge = forge_factory()
        except Exception:
            forge = None
    policy_events = _gc_policy_events(workspace, forge, apply=False)
    items = _policy_items(
        [e for e in policy_events if str(e["issue_number"]) not in active]
    )
    for key, item in sorted(candidates):
        plan = _inspect_or_hold(item, workspace, forge)
        action = "would_release" if plan.action == "release" else "held"
        items.append(
            _make_item(key, item, action, plan.reason, plan.worktree_action, workspace)
        )
    return GcRunResult(tuple(items), skipped, (), 0)


def _run_state_lock_error(exc: RuntimeError) -> bool:
    return isinstance(exc.__cause__, FileLockContentionError) or (
        "another process is currently holding the lock." in str(exc)
    )


def _apply_locked(
    request: GcRequest,
    workspace: ClaimWorkspace,
    forge_factory: Callable[[], HandoffForge],
) -> GcRunResult:
    try:
        _, active = _read_gc_state(workspace.run_state_path)
    except (OSError, ValueError):
        return GcRunResult((), 0, (), 1)
    candidates = _completion_gc_candidates(active, workspace.run_state_path)
    skipped = len(active) - len(candidates)
    forge: HandoffForge | None = None
    if any(_is_handoff_ready(item) for _, item in candidates) or policy_candidates(
        load_run_state_readonly(workspace.run_state_path)
    ):
        try:
            forge = forge_factory()
        except Exception:
            forge = None
    policy_events = _gc_policy_events(workspace, forge, apply=True)
    held_issues = {
        e["issue_number"]
        for e in policy_events
        if e["action"] == "completion_policy_hold"
    }
    items = _policy_items(
        [e for e in policy_events if str(e["issue_number"]) not in active]
    )
    receipts: list[CompletionReceipt] = []
    return _run_candidates(
        candidates, held_issues, request, workspace, forge, items, receipts, skipped
    )


def _run_candidates(
    candidates: list[tuple[str, ActiveWorktree]],
    held_issues: set[int],
    request: GcRequest,
    workspace: ClaimWorkspace,
    forge: HandoffForge | None,
    items: list[GcItemResult],
    receipts: list[CompletionReceipt],
    skipped: int,
) -> GcRunResult:
    for key, initial in sorted(candidates):
        if initial.core.issue_number in held_issues:
            items.append(
                _make_item(
                    key, initial, "held", "completion_policy_hold", "retain", workspace
                )
            )
            continue
        exit_code, new_skipped = _apply_candidate(
            key, initial, request, workspace, forge, items, receipts
        )
        skipped += new_skipped
        if exit_code is not None:
            return GcRunResult(tuple(items), skipped, tuple(receipts), exit_code)
    items.extend(
        _policy_items(
            reclaim_completed_tokens(
                load_run_state_readonly(workspace.run_state_path),
                workspace.run_state_path,
                workspace.repository_identity,
            )
        )
    )
    return GcRunResult(tuple(items), skipped, tuple(receipts), 0)


def run_handoff_gc(
    request: GcRequest,
    *,
    forge_factory: Callable[[], HandoffForge] = GitHubForge,
) -> GcRunResult:
    """Inspect or release handoff-ready reservations from the shared local state."""
    if not math.isfinite(request.timeout_seconds) or request.timeout_seconds < 0:
        return GcRunResult((), 0, (), 2)
    try:
        workspace = resolve_claim_workspace(explicit_state_path=request.state_path)
    except Exception:
        return GcRunResult((), 0, (), 1)
    if not request.apply:
        return _preview(workspace, forge_factory)
    try:
        with run_state_lock(workspace.lock_path, timeout=request.timeout_seconds):
            return _apply_locked(request, workspace, forge_factory)
    except RuntimeError as exc:
        if _run_state_lock_error(exc):
            return GcRunResult((), 0, (), 22)
        return GcRunResult((), 0, (), 1)
    except Exception:
        return GcRunResult((), 0, (), 1)


def _gc_policy_events(
    workspace: ClaimWorkspace, forge: HandoffForge | None, *, apply: bool
) -> list[dict[str, Any]]:
    state = load_run_state_readonly(workspace.run_state_path)
    if forge is not None:
        events = standalone_policies(workspace, forge, apply=apply)
    else:
        events = [
            {
                "issue_number": r.issue_number,
                "action": "completion_policy_hold",
                "reason": "outcome_unknown",
            }
            for r in policy_candidates(state)
        ]
    observed = {event["issue_number"] for event in events}
    events.extend(
        e.to_dict()
        for e in unclaimed_completion_events(state)
        if str(e.issue_number) not in observed
    )
    return events

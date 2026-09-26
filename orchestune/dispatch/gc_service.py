"""Local run-state cleanup for handoff-ready task reservations."""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from orchestune.claim.workspace import ClaimWorkspace, resolve_claim_workspace
from orchestune.dispatch.claim_marker import (
    claim_lock_path,
    claim_marker_path,
    read_claim_marker,
    remove_claim_marker,
)
from orchestune.dispatch.cycle_records import CompletionReceipt
from orchestune.dispatch.gc import _completed_worktree_record
from orchestune.dispatch.gc.git import (
    evaluate_worktree_removal,
    remove_verified_worktree,
)
from orchestune.dispatch.gc.handoff import (
    GcRequest,
    HandoffForge,
    HandoffPlan,
    inspect_handoff,
)
from orchestune.dispatch.gc.outcome_decision import _is_handoff_ready
from orchestune.dispatch.state import ActiveWorktree, _parse_active_worktrees
from orchestune.forge import GitHubForge
from orchestune.infra.json_state import write_json_atomic
from orchestune.infra.process_utils import (
    FileLockContentionError,
    assert_run_state_lock_held,
    file_lock,
    run_state_lock,
)


@dataclass(frozen=True)
class GcItemResult:
    key: str
    issue_number: int
    result: str | None
    action: str
    reason: str
    worktree_path: str
    worktree_action: str


@dataclass(frozen=True)
class GcRunResult:
    items: tuple[GcItemResult, ...]
    skipped: int
    receipts: tuple[CompletionReceipt, ...]
    exit_code: int


def _read_gc_state(path: Path) -> tuple[dict[str, Any], dict[str, ActiveWorktree]]:
    if not path.exists():
        return {"active_worktrees": {}}, {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("run_state root must be an object")
    active = _parse_active_worktrees(raw)
    completed = raw.get("completed_worktrees", [])
    if not isinstance(completed, list):
        raise ValueError("completed_worktrees schema error: value must be an array")
    return raw, active


def _absolute_worktree_path(active: ActiveWorktree, workspace: ClaimWorkspace) -> Path:
    path = Path(active.worktree_path)
    if not path.is_absolute():
        path = workspace.worktree_root.parent / path
    return path


def _is_standalone_gc_candidate(active: ActiveWorktree) -> bool:
    """Limit this standalone command to interactive claims.

    Dispatch-owned worktrees need TaskMetadata to record their subtask id in
    CompletedWorktree; the dispatch cycle owns that lifecycle and KPI record.
    """
    return active.owner_kind == "interactive" and _is_handoff_ready(active)


def _make_item(
    key: str,
    active: ActiveWorktree,
    action: str,
    reason: str,
    worktree_action: str,
    workspace: ClaimWorkspace,
) -> GcItemResult:
    worktree_path = (
        str(_absolute_worktree_path(active, workspace))
        if active.worktree_path.strip()
        else ""
    )
    return GcItemResult(
        key=key,
        issue_number=active.issue_number,
        result=active.completion_result,
        action=action,
        reason=reason,
        worktree_path=worktree_path,
        worktree_action=worktree_action,
    )


def _inspect_or_hold(
    active: ActiveWorktree,
    workspace: ClaimWorkspace,
    forge: HandoffForge | None,
) -> HandoffPlan:
    if forge is None:
        return HandoffPlan(
            key=str(active.issue_number),
            issue_number=active.issue_number,
            result=active.completion_result,
            action="hold",
            reason="outcome_unknown",
            worktree_action="retain",
        )
    try:
        return inspect_handoff(active, workspace, forge)
    except Exception:
        return HandoffPlan(
            key=str(active.issue_number),
            issue_number=active.issue_number,
            result=active.completion_result,
            action="hold",
            reason="inspection_failed",
            worktree_action="retain",
        )


def _preview(
    workspace: ClaimWorkspace,
    forge_factory: Callable[[], HandoffForge],
) -> GcRunResult:
    try:
        _, active = _read_gc_state(workspace.run_state_path)
    except (OSError, ValueError):
        return GcRunResult((), 0, (), 1)
    candidates = [
        (key, item) for key, item in active.items() if _is_standalone_gc_candidate(item)
    ]
    skipped = len(active) - len(candidates)
    forge: HandoffForge | None = None
    if candidates:
        try:
            forge = forge_factory()
        except Exception:
            forge = None
    items = []
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


def _remove_absent_claim_marker(active: ActiveWorktree, target: Path) -> str | None:
    marker_path = claim_marker_path(target)
    if not (marker_path.exists() or marker_path.is_symlink()):
        return None
    if marker_path.is_symlink():
        return "owner_unknown"
    marker = read_claim_marker(target)
    if marker is None:
        return "owner_unknown"
    if (
        marker.get("claim_id") != active.claim_id
        or marker.get("branch") != active.branch
    ):
        return "owner_mismatch"
    try:
        remove_claim_marker(target)
    except OSError:
        return "claim_marker_remove_failed"
    return None


def _remove_verified_worktree(
    active: ActiveWorktree, target: Path, workspace: ClaimWorkspace
) -> str | None:
    inspected = replace(active, worktree_path=str(target))
    evaluation = evaluate_worktree_removal(
        inspected, repo_root=workspace.worktree_root.parent
    )
    if not evaluation.can_remove or evaluation.request is None:
        return evaluation.rejection_reason or "worktree_unverified"
    try:
        removed = remove_verified_worktree(evaluation.request)
    except Exception:
        return "worktree_remove_failed"
    if not removed.success or target.exists():
        return "worktree_remove_failed"
    after = evaluate_worktree_removal(
        inspected, repo_root=workspace.worktree_root.parent
    )
    if after.rejection_reason != "unregistered_worktree":
        return "worktree_registration_remains"
    return None


def _updated_state_after_release(
    key: str,
    active: ActiveWorktree,
    plan: HandoffPlan,
    raw: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    updated = copy.deepcopy(raw)
    active_records = updated.get("active_worktrees")
    if not isinstance(active_records, dict) or key not in active_records:
        return None, "state_changed"
    del active_records[key]
    outcome = plan.outcome
    if outcome is not None and outcome.result == "done":
        completed = updated.setdefault("completed_worktrees", [])
        if not isinstance(completed, list):
            return None, "completed_worktrees_invalid"
        record = _completed_worktree_record(
            active,
            None,
            {"action": "already_merged", "commit_sha": outcome.head_sha},
        )
        completed.append(asdict(record))
    return updated, None


def _apply_release(
    key: str,
    active: ActiveWorktree,
    plan: HandoffPlan,
    raw: dict[str, Any],
    workspace: ClaimWorkspace,
) -> tuple[bool, str]:
    target = _absolute_worktree_path(active, workspace)
    if plan.worktree_action == "remove":
        reason = _remove_verified_worktree(active, target, workspace)
    elif plan.worktree_action == "absent":
        reason = _remove_absent_claim_marker(active, target)
    else:
        reason = None
    if reason:
        return False, reason
    updated, reason = _updated_state_after_release(key, active, plan, raw)
    if reason or updated is None:
        return False, reason or "state_update_failed"
    try:
        assert_run_state_lock_held(workspace.lock_path)
        write_json_atomic(workspace.run_state_path, updated)
    except Exception:
        return False, "state_save_failed"
    return True, "released"


def _apply_locked(
    request: GcRequest,
    workspace: ClaimWorkspace,
    forge_factory: Callable[[], HandoffForge],
) -> GcRunResult:
    try:
        _, active = _read_gc_state(workspace.run_state_path)
    except (OSError, ValueError):
        return GcRunResult((), 0, (), 1)
    candidates = [
        (key, item) for key, item in active.items() if _is_standalone_gc_candidate(item)
    ]
    skipped = len(active) - len(candidates)
    if not candidates:
        return GcRunResult((), skipped, (), 0)
    try:
        forge: HandoffForge | None = forge_factory()
    except Exception:
        forge = None
    items: list[GcItemResult] = []
    receipts: list[CompletionReceipt] = []
    for key, initial in sorted(candidates):
        exit_code, new_skipped = _apply_candidate(
            key, initial, request, workspace, forge, items, receipts
        )
        skipped += new_skipped
        if exit_code is not None:
            return GcRunResult(tuple(items), skipped, tuple(receipts), exit_code)
    return GcRunResult(tuple(items), skipped, tuple(receipts), 0)


def _apply_candidate(
    key: str,
    initial: ActiveWorktree,
    request: GcRequest,
    workspace: ClaimWorkspace,
    forge: HandoffForge | None,
    items: list[GcItemResult],
    receipts: list[CompletionReceipt],
) -> tuple[int | None, int]:
    if not initial.worktree_path.strip():
        items.append(
            _make_item(
                key, initial, "held", "worktree_path_missing", "retain", workspace
            )
        )
        return None, 0
    target = _absolute_worktree_path(initial, workspace)
    try:
        with file_lock(claim_lock_path(target), timeout=request.timeout_seconds):
            return _apply_one(key, workspace, forge, items, receipts)
    except FileLockContentionError:
        items.append(
            _make_item(key, initial, "failed", "lock_timeout", "retain", workspace)
        )
        return 22, 0


def _apply_one(
    key: str,
    workspace: ClaimWorkspace,
    forge: HandoffForge | None,
    items: list[GcItemResult],
    receipts: list[CompletionReceipt],
) -> tuple[int | None, int]:
    try:
        raw, active = _read_gc_state(workspace.run_state_path)
    except (OSError, ValueError):
        return 1, 0
    current = active.get(key)
    if current is None or not _is_handoff_ready(current):
        return None, 1
    plan = _inspect_or_hold(current, workspace, forge)
    if plan.action != "release":
        items.append(
            _make_item(
                key, current, "held", plan.reason, plan.worktree_action, workspace
            )
        )
        return None, 0
    success, reason = _apply_release(key, current, plan, raw, workspace)
    if not success:
        items.append(
            _make_item(key, current, "failed", reason, plan.worktree_action, workspace)
        )
        return 1, 0
    items.append(
        _make_item(
            key, current, "released", plan.reason, plan.worktree_action, workspace
        )
    )
    if plan.outcome is not None and plan.outcome.result == "done":
        receipts.append(CompletionReceipt(issue_number=current.issue_number))
    return None, 0


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

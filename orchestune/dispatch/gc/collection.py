"""Guarded physical collection shared by standalone GC and Dispatcher."""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from orchestune.claim.workspace import ClaimWorkspace
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_events import WorktreeCompletion
from orchestune.dispatch.cycle_records import CompletionReceipt
from orchestune.dispatch.gc.external_guard import (
    collection_stop_problem,
    same_execution,
)
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
from orchestune.dispatch.gc.policy_discovery import policy_candidates
from orchestune.dispatch.gc.records import _completed_worktree_record
from orchestune.infra.json_state import write_json_atomic
from orchestune.infra.process_utils import (
    FileLockContentionError,
    assert_run_state_lock_held,
    file_lock,
)
from orchestune.ledger.completion_reservations import (
    completion_handoff_matches_active,
)
from orchestune.ledger.run_state import (
    ActiveWorktree,
    _parse_active_worktrees,
    load_run_state_readonly,
)
from orchestune.task_metadata import TaskMetadata
from orchestune.worktree_ops.claim_marker import (
    claim_lock_path,
    claim_marker_path,
    read_claim_marker,
    remove_claim_marker,
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
    path = Path(active.core.worktree_path)
    if not path.is_absolute():
        path = workspace.worktree_root.parent / path
    return path


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
        if active.core.worktree_path.strip()
        else ""
    )
    return GcItemResult(
        key=key,
        issue_number=active.core.issue_number,
        result=active.completion.completion_result,
        action=action,
        reason=reason,
        worktree_path=worktree_path,
        worktree_action=worktree_action,
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
        marker.get("claim_id") != active.claim.claim_id
        or marker.get("branch") != active.core.branch
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
    inspected = active.with_core(replace(active.core, worktree_path=str(target)))
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
    task: TaskMetadata | None = None,
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
            task,
            WorktreeCompletion(
                issue_number=active.core.issue_number,
                worktree_path=active.core.worktree_path,
                action="already_merged",
                subtask_id=task.subtask_id if task else None,
                commit_sha=outcome.head_sha,
            ),
        )
        completed.append(asdict(record))
    return updated, None


def _apply_release(
    key: str,
    active: ActiveWorktree,
    plan: HandoffPlan,
    raw: dict[str, Any],
    workspace: ClaimWorkspace,
    task: TaskMetadata | None = None,
    *,
    config: DispatcherConfig | None = None,
) -> tuple[bool, str]:
    if problem := _release_evidence_problem(active, workspace):
        return False, problem
    if problem := collection_stop_problem(active, workspace, config):
        return False, problem
    target = _absolute_worktree_path(active, workspace)
    if plan.worktree_action == "remove":
        reason = _remove_verified_worktree(active, target, workspace)
    elif plan.worktree_action == "absent":
        reason = _remove_absent_claim_marker(active, target)
    else:
        reason = None
    if reason:
        return False, reason
    if problem := collection_stop_problem(active, workspace, config):
        return False, problem
    if problem := _release_evidence_problem(active, workspace):
        return False, problem
    raw, fresh = _read_gc_state(workspace.run_state_path)
    current = fresh.get(key)
    if current is None or not same_execution(current, active):
        return False, "state_changed"
    updated, reason = _updated_state_after_release(key, active, plan, raw, task)
    if reason or updated is None:
        return False, reason or "state_update_failed"
    try:
        assert_run_state_lock_held(workspace.lock_path)
        write_json_atomic(workspace.run_state_path, updated)
    except Exception:
        return False, "state_save_failed"
    return True, "released"


def _release_evidence_problem(
    active: ActiveWorktree, workspace: ClaimWorkspace
) -> str | None:
    state = load_run_state_readonly(workspace.run_state_path)
    if not completion_handoff_matches_active(state, active):
        return "completion_evidence_mismatch"
    record = next(
        (
            r
            for r in policy_candidates(state)
            if r.issue_number == active.core.issue_number
        ),
        None,
    )
    if record is None:
        return "completion_evidence_mismatch"
    if (
        record.result == "done"
        and (record.prepublication_policy_evidence or {}).get("decision") != "allowed"
    ):
        return "done_policy_evidence_missing"
    return None


def _apply_candidate(
    key: str,
    initial: ActiveWorktree,
    request: GcRequest,
    workspace: ClaimWorkspace,
    forge: HandoffForge | None,
    items: list[GcItemResult],
    receipts: list[CompletionReceipt],
    task: TaskMetadata | None = None,
    *,
    config: DispatcherConfig | None = None,
) -> tuple[int | None, int]:
    if not initial.core.worktree_path.strip():
        items.append(
            _make_item(
                key, initial, "held", "worktree_path_missing", "retain", workspace
            )
        )
        return None, 0
    target = _absolute_worktree_path(initial, workspace)
    try:
        with file_lock(claim_lock_path(target), timeout=request.timeout_seconds):
            return _apply_one(
                key, workspace, forge, items, receipts, task, initial, config=config
            )
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
    task: TaskMetadata | None = None,
    expected: ActiveWorktree | None = None,
    *,
    config: DispatcherConfig | None = None,
) -> tuple[int | None, int]:
    try:
        raw, active = _read_gc_state(workspace.run_state_path)
    except (OSError, ValueError):
        return 1, 0
    current = active.get(key)
    if current is None or (
        expected is not None
        and (current.claim.claim_id, current.completion.completion_id)
        != (expected.claim.claim_id, expected.completion.completion_id)
        or expected is not None
        and not same_execution(current, expected)
    ):
        return None, 1
    if held := _handoff_contract_hold(key, current, workspace):
        items.append(held)
        return None, 0
    plan = _inspect_or_hold(current, workspace, forge, config=config)
    if plan.action != "release":
        items.append(
            _make_item(
                key, current, "held", plan.reason, plan.worktree_action, workspace
            )
        )
        return None, 0
    success, reason = _apply_release(
        key, current, plan, raw, workspace, task, config=config
    )
    return _record_collection_result(
        key, current, plan, success, reason, workspace, items, receipts
    )


def _record_collection_result(
    key: str,
    current: ActiveWorktree,
    plan: HandoffPlan,
    success: bool,
    reason: str,
    workspace: ClaimWorkspace,
    items: list[GcItemResult],
    receipts: list[CompletionReceipt],
) -> tuple[int | None, int]:
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
        receipts.append(CompletionReceipt(issue_number=current.core.issue_number))
    return None, 0


def _inspect_or_hold(
    active: ActiveWorktree,
    workspace: ClaimWorkspace,
    forge: HandoffForge | None,
    *,
    config: DispatcherConfig | None = None,
) -> HandoffPlan:
    core = active.core
    completion = active.completion
    if not _is_handoff_ready(active):
        return HandoffPlan(
            key=str(core.issue_number),
            issue_number=core.issue_number,
            result=completion.completion_result,
            action="hold",
            reason="legacy_unverified"
            if completion.completion_handoff_ready
            or completion.completion_stage == "handed_off_to_gc"
            else "not_handoff_ready",
            worktree_action="retain",
        )
    if forge is None:
        return HandoffPlan(
            key=str(core.issue_number),
            issue_number=core.issue_number,
            result=completion.completion_result,
            action="hold",
            reason="outcome_unknown",
            worktree_action="retain",
        )
    try:
        return inspect_handoff(active, workspace, forge, config=config)
    except Exception:
        return HandoffPlan(
            key=str(core.issue_number),
            issue_number=core.issue_number,
            result=completion.completion_result,
            action="hold",
            reason="inspection_failed",
            worktree_action="retain",
        )


def _handoff_contract_hold(
    key: str, active: ActiveWorktree, workspace: ClaimWorkspace
) -> GcItemResult | None:
    if _is_handoff_ready(active) and not completion_handoff_matches_active(
        load_run_state_readonly(workspace.run_state_path), active
    ):
        return _make_item(
            key, active, "held", "completion_evidence_mismatch", "retain", workspace
        )
    return None

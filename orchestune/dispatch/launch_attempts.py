"""Fail-closed launch and recovery for cloud implementation workers (#818)."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from orchestune.dispatch.attempt_record import (
    LaunchAttempt,
    read_attempt,
    write_attempt,
)
from orchestune.dispatch.escalation import apply_human_review_escalation
from orchestune.dispatch.execution_profiles import resolve_task_execution_selection
from orchestune.dispatch.labels import (
    PRIMARY_STATUS_LABELS,
    TERMINAL_ESCALATION_LABELS,
    transition_status_label,
)
from orchestune.dispatch.state import (
    ActiveWorktree,
    RunState,
    load_run_state,
    save_run_state,
)
from orchestune.dispatch.targets import DispatchHandle, DispatchTarget
from orchestune.issue_parsing import recovery_counters_from_body
from orchestune.labels import StatusLabel
from orchestune.task_metadata import TaskMetadata

if TYPE_CHECKING:
    from orchestune.dispatch.config import DispatcherConfig
    from orchestune.dispatch.execution_profiles import ExecutionSelection
    from orchestune.forge import Forge


class LaunchOutcomeUnknown(RuntimeError):
    """Do not clean up a worktree or release quota after a possible launch."""


class LaunchPlan(Protocol):
    @property
    def task(self) -> TaskMetadata: ...

    branch_name: str
    base_branch_for_state: str


def _recovery_allowed(task: TaskMetadata, config: DispatcherConfig) -> bool:
    issue = config.resolved_forge.get_issue(task.issue_number)
    terminal = (*TERMINAL_ESCALATION_LABELS, StatusLabel.DONE, StatusLabel.NOT_NEEDED)
    return (
        issue is not None
        and issue.state == "OPEN"
        and not any(label in issue.labels for label in terminal)
    )


def active_from_attempt(
    attempt: LaunchAttempt, task: TaskMetadata, config: DispatcherConfig
) -> ActiveWorktree:
    issue = config.resolved_forge.get_issue(task.issue_number)
    count, serial = recovery_counters_from_body(issue.body) if issue else (0, False)
    selection = resolve_task_execution_selection(task, config)
    return ActiveWorktree(
        issue_number=task.issue_number,
        branch=attempt.branch,
        worktree_path=str(
            Path(config.worktree_root) / attempt.branch.replace("/", "-")
        ),
        pid=None,
        started_at=attempt.started_at,
        declared_footprint=task.footprint,
        external_id=attempt.external_id,
        external_url=attempt.external_url,
        base_branch=attempt.base_branch,
        launch_attempt_id=attempt.attempt_id,
        launch_phase=attempt.phase,
        recompute_count=count,
        forced_serial=serial or StatusLabel.FORCE_SERIAL in task.status_labels,
        profile=selection.profile,
        model=selection.model,
        reasoning_effort=selection.reasoning_effort,
        selection_reason=selection.reason,
    )


def restored_active_record(
    *,
    issue_number: int,
    branch: str,
    worktree_path: str,
    footprint: tuple[str, ...],
    recompute_count: int,
    forced_serial: bool,
    external_id: str | None,
    external_url: str | None,
    base_branch: str,
    profile: str | None,
    model: str | None,
    reasoning_effort: str | None,
    selection_reason: str | None,
    owner_kind: str,
    claim_id: str,
    reservation_kind: str,
    repository_id: str,
    owner_token_digest: str,
) -> ActiveWorktree:
    """Construct a non-resumable record recovered without an owner secret."""
    return ActiveWorktree(
        issue_number=issue_number,
        branch=branch,
        worktree_path=worktree_path,
        pid=None,
        started_at=None,
        declared_footprint=footprint,
        recompute_count=recompute_count,
        forced_serial=forced_serial,
        external_id=external_id,
        external_url=external_url,
        base_branch=base_branch,
        profile=profile,
        model=model,
        reasoning_effort=reasoning_effort,
        selection_reason=selection_reason,
        owner_kind=owner_kind,
        claim_id=claim_id,
        claim_stage="completed",
        base_ref=base_branch,
        base_sha=None,
        reservation_kind=reservation_kind,
        repository_id=repository_id,
        claimed_at=0.0,
        owner_token_digest=owner_token_digest,
    )


def _recovered_active_from_attempt(
    attempt: LaunchAttempt, task: TaskMetadata, config: DispatcherConfig
) -> ActiveWorktree:
    """Restore a cloud handle without granting ownership of an absent claim."""
    claim_id = f"recovered-{attempt.attempt_id}"
    return replace(
        active_from_attempt(attempt, task, config),
        owner_kind="dispatch",
        claim_id=claim_id,
        claim_stage="completed",
        base_ref=attempt.base_branch,
        base_sha=None,
        reservation_kind="footprint",
        repository_id=str(config.run_state_path.resolve().parent),
        claimed_at=attempt.started_at,
        owner_token_digest=sha256(
            f"recovered-unverifiable:{claim_id}".encode()
        ).hexdigest(),
    )


def _hold(task: TaskMetadata, config: DispatcherConfig, reason: str) -> None:
    print(
        f"Holding cloud launch for issue #{task.issue_number}: {reason}",
        file=sys.stderr,
    )
    labels = config.resolved_forge.get_issue_labels(task.issue_number)
    if StatusLabel.BLOCKED_HUMAN_REVIEW not in labels:
        apply_human_review_escalation(
            task.issue_number,
            labels,
            f"クラウド起動試行を保留しました: {reason}。既存実行の照合・終了確認が必要です。"
            " queuedへの変更だけでは再起動しません。",
            forge=config.resolved_forge,
        )


def _lookup_attempt(
    attempt: LaunchAttempt, task: TaskMetadata, config: DispatcherConfig
) -> LaunchAttempt:
    target = config.dispatch_target
    assert target is not None
    if attempt.phase == "unknown" and target.launch_capabilities.lookup_by_attempt:
        handle = target.lookup_launch_attempt(attempt.attempt_id)
        if handle is not None and handle.external_id:
            resolved = replace(
                attempt,
                phase="launched",
                external_id=handle.external_id,
                external_url=handle.external_url,
            )
            write_attempt(
                config.resolved_forge, task.issue_number, resolved, expected=attempt
            )
            return resolved
    return attempt


def _adopt_confirmed_attempt(
    attempt: LaunchAttempt,
    task: TaskMetadata,
    config: DispatcherConfig,
    existing: ActiveWorktree,
) -> ActiveWorktree:
    """確認済みattemptから新規`ActiveWorktree`を組み立てる。

    #943レビュー対応(Codex P1, round3): `existing`（claim_task由来の
    プレースホルダー）がある場合、`active_from_attempt`は持たないclaim由来の
    所有権・予約範囲（`owner_kind`/`claim_id`/`reservation_kind`。footprintが
    空のissueは`reservation_kind="repository"`＝全面予約になり得る）を
    引き継ぐ。引き継がないと全面予約が既定の"footprint"へ黙って縮小し、
    以後の同時実行排他が緩んでしまう。
    """
    adopted = active_from_attempt(attempt, task, config)
    return replace(
        adopted,
        owner_kind=existing.owner_kind,
        claim_id=existing.claim_id,
        claim_stage=existing.claim_stage,
        reservation_kind=existing.reservation_kind,
        base_ref=existing.base_ref,
        base_sha=existing.base_sha,
        repository_id=existing.repository_id,
        claimed_at=existing.claimed_at,
        owner_token_digest=existing.owner_token_digest,
    )


def _load_or_recover_active(
    attempt: LaunchAttempt,
    task: TaskMetadata,
    state: RunState,
    config: DispatcherConfig,
) -> ActiveWorktree:
    key = str(task.issue_number)
    existing = state.active_worktrees.get(key)
    if existing is None:
        existing = load_run_state(config.run_state_path).active_worktrees.get(key)
        if existing is not None:
            state.active_worktrees[key] = existing
    if existing is None:
        existing = _recovered_active_from_attempt(attempt, task, config)
        state.active_worktrees[key] = existing
    return existing


def reconcile_attempt(
    attempt: LaunchAttempt,
    task: TaskMetadata,
    state: RunState,
    config: DispatcherConfig,
) -> bool:
    """True means the journal consumed the task; never launch it as queued."""
    if not _recovery_allowed(task, config):
        return True
    target = config.dispatch_target
    if target is None or attempt.target != target.target_name:
        _hold(task, config, f"attempt {attempt.attempt_id}: provider changed")
        return True
    if attempt.phase == "prepared":
        return False
    try:
        attempt = _lookup_attempt(attempt, task, config)
    except Exception as exc:
        _hold(task, config, f"attempt {attempt.attempt_id}: lookup failed ({exc})")
        return True
    if attempt.phase != "launched":
        _hold(
            task,
            config,
            f"attempt {attempt.attempt_id}: unknown launch result; provider cannot reconcile",
        )
        return True
    key = str(task.issue_number)
    existing = _load_or_recover_active(attempt, task, state, config)
    # #943: dispatchの起動がclaim_task経由になったことで、実際の起動より前に
    # claim自身の予約（`launch_attempt_id`未設定のプレースホルダー）が
    # `run_state.active_worktrees`へ同期されるようになった。このプレース
    # ホルダーは別の起動に属するものではないため、既存の「別attemptに属する」
    # 拒否と区別し、確認済みのattemptで採用できるようにする
    # （`_adopt_confirmed_attempt`参照）。
    if existing.launch_attempt_id is None:
        state.active_worktrees[key] = _adopt_confirmed_attempt(
            attempt, task, config, existing
        )
    elif existing.launch_attempt_id != attempt.attempt_id:
        _hold(task, config, "local state belongs to a different launch attempt")
        return True
    save_run_state(
        state, config.run_state_path, launch_window_seconds=config.window_seconds
    )
    labels = config.resolved_forge.get_issue_labels(task.issue_number)
    transition_status_label(
        config.resolved_forge,
        task.issue_number,
        StatusLabel.IN_PROGRESS,
        (label for label in PRIMARY_STATUS_LABELS if label in labels),
    )
    return True


class JournaledDispatchTarget(DispatchTarget):
    """Journal immediately around the provider boundary, after worktree setup."""

    def __init__(
        self,
        target: DispatchTarget,
        attempt: LaunchAttempt,
        forge: Forge,
        commit: Callable[[], None],
    ):
        self.target = target
        self.attempt = attempt
        self.forge = forge
        self.commit = commit

    def launch(
        self,
        task: TaskMetadata,
        branch_name: str,
        worktree_path: Path,
        *,
        force_push: bool = False,
        execution_selection: ExecutionSelection | None = None,
        base_branch: str | None = None,
    ) -> DispatchHandle:
        unknown = replace(self.attempt, phase="unknown", started_at=time.time())
        # A write may have committed even if its response was lost. Preserve quota.
        self.commit()
        try:
            write_attempt(self.forge, task.issue_number, unknown, expected=self.attempt)
            handle = self.target.launch_attempt(
                unknown.attempt_id,
                task,
                branch_name,
                worktree_path,
                force_push=force_push,
                execution_selection=execution_selection,
                base_branch=base_branch,
            )
            if not handle.external_id:
                raise ValueError("provider returned no execution handle")
            launched = replace(
                unknown,
                phase="launched",
                external_id=handle.external_id,
                external_url=handle.external_url,
            )
            write_attempt(self.forge, task.issue_number, launched, expected=unknown)
        except Exception as exc:
            raise LaunchOutcomeUnknown(str(exc)) from exc
        return replace(handle, launch_attempt_id=launched.attempt_id)

    def is_complete(self, handle: DispatchHandle, forge: Forge | None = None) -> bool:
        return self.target.is_complete(handle, forge=forge)


def prepare_journaled_target(
    plan: LaunchPlan,
    state: RunState,
    now: float,
    config: DispatcherConfig,
    commit: Callable[[], None],
) -> DispatchTarget | None:
    target = config.dispatch_target
    assert target is not None
    if not target.launch_capabilities.durable_attempt:
        return target
    if not _recovery_allowed(plan.task, config):
        return None
    try:
        attempt = read_attempt(config.resolved_forge, plan.task.issue_number)
    except ValueError as exc:
        _hold(plan.task, config, str(exc))
        return None
    if attempt is not None:
        if reconcile_attempt(attempt, plan.task, state, config):
            return None
        if (
            attempt.branch != plan.branch_name
            or attempt.base_branch != plan.base_branch_for_state
        ):
            _hold(plan.task, config, "prepared attempt branch changed")
            return None
    else:
        attempt = LaunchAttempt(
            str(uuid4()),
            "prepared",
            target.target_name or "unknown",
            plan.branch_name,
            plan.base_branch_for_state,
            now,
        )
        write_attempt(
            config.resolved_forge, plan.task.issue_number, attempt, expected=None
        )
    return JournaledDispatchTarget(target, attempt, config.resolved_forge, commit)

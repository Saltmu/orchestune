"""Shared durable downstream policy runner; physical worktree GC is separate."""

from __future__ import annotations

import copy
import hashlib
import json
import time
from contextlib import nullcontext
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Literal

from orchestune.claim.workspace import resolve_claim_workspace
from orchestune.complete.contracts import DownstreamPolicyRecord
from orchestune.complete.journal_models import CompletionJournalRecord
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.config_values import completion_policy_overrides
from orchestune.dispatch.cycle_events import (
    PolicyHoldCompletion,
    PolicyProgressCompletion,
)
from orchestune.dispatch.gc.outcome_decision import read_retry_state, write_retry_state
from orchestune.dispatch.gc.policy_discovery import (
    ensure_policy,
    journal_outcome,
    policy_candidates,
    policy_kind,
    update_policy,
    update_record,
)
from orchestune.dispatch.gc.policy_effects import apply_effects
from orchestune.dispatch.gc.policy_review import reconcile_review
from orchestune.dispatch.retry_policy import (
    RetryDisposition,
    plan_retry,
    review_timeout_policy,
)
from orchestune.infra.json_state import write_json_atomic
from orchestune.infra.process_utils import assert_run_state_lock_held, run_state_lock
from orchestune.infra.repository_config import find_and_load_config_file
from orchestune.labels import StatusLabel
from orchestune.ledger.run_state import (
    RunState,
    TaskReclaimRecord,
    load_run_state_readonly,
)
from orchestune.targets.cloud_routine import (
    ROUTINE_ID_ENV_VAR,
    ROUTINE_TOKEN_ENV_VAR,
    ClaudeCodeCloudRoutineDispatchTarget,
)


def save_run_state(state: RunState, path: Path) -> None:
    """Persist policy maps atomically without dropping unrelated state extensions."""
    assert_run_state_lock_held(path.with_suffix(".lock"))
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.update(
        completion_journal=state.completion_journal,
        completion_replay_receipts=state.completion_replay_receipts,
    )
    previous = load_run_state_readonly(path).task_reclaim_counts
    counts = raw.setdefault("task_reclaim_counts", {})
    for issue, retry in state.task_reclaim_counts.items():
        if previous.get(issue) != retry:
            counts[str(issue)] = asdict(retry)
    write_json_atomic(path, raw)


def _verified_outcome(record: CompletionJournalRecord, forge: Any) -> bool:
    evidence = record.posting_evidence or {}
    body = record.outcome_payload.get("body", record.outcome_payload.get("outcome"))
    return any(
        str(c.get("id")) == evidence.get("comment_id")
        and c.get("html_url") == evidence.get("comment_url")
        and c.get("body") == body
        for c in forge.list_all_issue_comments(record.issue_number)
    )


def _reserve_retry(
    state: RunState, config: DispatcherConfig, issue: int, now: float
) -> dict[str, Any]:
    previous = state.task_reclaim_counts.get(issue)
    plan = plan_retry(
        review_timeout_policy(
            config.max_review_timeout_retries, config.review_timeout_backoff_seconds
        ),
        read_retry_state(previous, "review_timeout"),
        now=now,
    )
    if plan.disposition is RetryDisposition.EXHAUSTED:
        return {
            "target_label": StatusLabel.BLOCKED_HUMAN_REVIEW,
            "retry_count": plan.state.count,
        }
    retry = copy.deepcopy(previous) if previous else TaskReclaimRecord()
    write_retry_state(retry, "review_timeout", plan.state)
    state.task_reclaim_counts[issue] = retry
    return {
        "target_label": StatusLabel.QUEUED,
        "retry_count": plan.state.count,
        "retry_at": plan.state.retry_at,
    }


def _prepare(
    state: RunState,
    config: DispatcherConfig,
    record: CompletionJournalRecord,
    now: float,
) -> CompletionJournalRecord:
    outcome = journal_outcome(record)
    if outcome is None:
        raise ValueError("journal outcome identity mismatch")
    kind = policy_kind(record, outcome)
    if kind is None:
        return record
    if any(p.policy_kind != kind for p in record.downstream_policy_records):
        raise ValueError("downstream policy does not match completion result/context")
    record = ensure_policy(record, kind)
    policy = next(p for p in record.downstream_policy_records if p.policy_kind == kind)
    if policy.status == "applied" or "operation_id" in policy.metadata:
        return record
    metadata: dict[str, Any] = {
        "operation_id": hashlib.sha256(policy.policy_key.encode()).hexdigest(),
        "reserved_at": now,
    }
    if kind == "review-timeout":
        metadata.update(_reserve_retry(state, config, record.issue_number, now))
    if kind == "base-branch-red":
        attempt = outcome.attempt or 1
        metadata.update(
            attempt=attempt,
            target_label=StatusLabel.BLOCKED_HUMAN_REVIEW
            if attempt >= 3
            else StatusLabel.BLOCKED,
        )
    if kind == "not-needed-review":
        metadata["review_timeout_seconds"] = config.not_needed_review_timeout_seconds
    return update_policy(record, policy, **metadata)


def _apply_policy(
    state: RunState,
    config: DispatcherConfig,
    record: CompletionJournalRecord,
    policy: DownstreamPolicyRecord,
    now: float,
) -> CompletionJournalRecord:
    def save(metadata: dict[str, Any]) -> None:
        nonlocal record, policy
        record = update_policy(record, policy, **metadata)
        policy = next(
            p
            for p in record.downstream_policy_records
            if p.policy_kind == policy.policy_kind
        )
        update_record(state, record)
        save_run_state(state, config.run_state_path)

    if policy.policy_kind == "not-needed-review":
        if not reconcile_review(
            config.resolved_forge,
            record.issue_number,
            policy,
            config.dispatch_target,
            save,
            now,
        ):
            return record
    else:
        apply_effects(config.resolved_forge, record.issue_number, policy)
    if policy.policy_kind == "review-timeout":
        retry = state.task_reclaim_counts.get(record.issue_number)
        if retry is not None:
            retry.review_timeout_retry_pending = False
    applied = replace(policy, status="applied")
    record = replace(
        record,
        downstream_policy_records=tuple(
            applied if p.policy_kind == policy.policy_kind else p
            for p in record.downstream_policy_records
        ),
    )
    update_record(state, record)
    save_run_state(state, config.run_state_path)
    return record


def _process_one(
    state: RunState,
    config: DispatcherConfig,
    record: CompletionJournalRecord,
    now: float,
) -> PolicyProgressCompletion:
    action: Literal["completion_policy_pending", "completion_policy_applied"] = (
        "completion_policy_pending"
    )
    outcome = journal_outcome(record)
    if outcome is None or not _verified_outcome(record, config.resolved_forge):
        raise ValueError("completion evidence mismatch")
    if (
        record.result == "done"
        and (record.prepublication_policy_evidence or {}).get("decision") != "allowed"
    ):
        raise ValueError("done publication policy evidence missing")
    if not config.apply:
        return _policy_progress_event(record, action, reason="preview")
    if (
        policy_kind(record, outcome) is None and not record.downstream_policy_records
    ) or (
        record.downstream_policy_records
        and all(p.status == "applied" for p in record.downstream_policy_records)
    ):
        return _policy_progress_event(record, "completion_policy_applied")
    record = _prepare(state, config, record, now)
    # Reserve intent and retry count before any external effect or physical GC.
    update_record(state, record)
    save_run_state(state, config.run_state_path)
    for policy in record.downstream_policy_records:
        if policy.status != "applied":
            record = _apply_policy(state, config, record, policy, now)
    if all(p.status == "applied" for p in record.downstream_policy_records):
        action = "completion_policy_applied"
    return _policy_progress_event(record, action)


def _policy_progress_event(
    record: CompletionJournalRecord,
    action: Literal["completion_policy_pending", "completion_policy_applied"],
    *,
    reason: str | None = None,
) -> PolicyProgressCompletion:
    return PolicyProgressCompletion(
        issue_number=record.issue_number,
        completion_id=record.completion_id,
        generation_id=record.generation_id,
        action=action,
        reason=reason,
    )


def process_completion_policies(
    state: RunState,
    config: DispatcherConfig,
    *,
    now: float | None = None,
    repository_id: str | None = None,
) -> list[PolicyProgressCompletion | PolicyHoldCompletion]:
    """Run only matching, label-confirmed journal/receipt generations under one lock."""
    events: list[PolicyProgressCompletion | PolicyHoldCompletion] = []
    observed = time.time() if now is None else now
    with (
        run_state_lock(config.run_state_path.with_suffix(".lock"))
        if config.apply
        else nullcontext()
    ):
        fresh = load_run_state_readonly(config.run_state_path)
        state.completion_journal = fresh.completion_journal
        state.completion_replay_receipts = fresh.completion_replay_receipts
        state.task_reclaim_counts = fresh.task_reclaim_counts
        state.completion_reservations = fresh.completion_reservations
        for record in policy_candidates(fresh):
            before = copy.deepcopy(
                (
                    state.completion_journal,
                    state.completion_replay_receipts,
                    state.task_reclaim_counts,
                )
            )
            try:
                expected_repository = (
                    repository_id
                    or resolve_claim_workspace(
                        config.worktree_root.parent,
                        explicit_state_path=config.run_state_path,
                    ).repository_identity
                )
                if record.repository_id != expected_repository:
                    raise ValueError("repository_mismatch")
                events.append(_process_one(state, config, record, observed))
            except Exception as error:
                _restore_after_failure(state, config, before)
                events.append(
                    PolicyHoldCompletion(
                        issue_number=record.issue_number,
                        completion_id=record.completion_id,
                        reason=str(error),
                    )
                )
    return events


def standalone_policies(
    workspace: Any, forge: Any, *, apply: bool
) -> list[dict[str, Any]]:
    """Resolve local configuration without requiring a running Dispatcher."""
    import os

    state = load_run_state_readonly(workspace.run_state_path)
    if not policy_candidates(state):
        return []
    raw = find_and_load_config_file(workspace.repository_root)
    raw = {key.replace("-", "_"): value for key, value in raw.items()}
    target = None
    routine, token = (
        os.environ.get(ROUTINE_ID_ENV_VAR),
        os.environ.get(ROUTINE_TOKEN_ENV_VAR),
    )
    if apply and routine and token:
        target = ClaudeCodeCloudRoutineDispatchTarget(routine, token)
    config = DispatcherConfig(
        parent_issue_number=raw.get("parent_issue_number", 0),
        apply=apply,
        run_state_path=workspace.run_state_path,
        worktree_root=workspace.worktree_root,
        events_log_path=workspace.repository_root / "events.jsonl",
        log_dir=workspace.repository_root / "logs",
        not_needed_review_state_path=workspace.repository_root
        / "not_needed_review_state.json",
        forge=forge,
        dispatch_target=target,
        **completion_policy_overrides(raw),
    )
    return [
        event.to_dict()
        for event in process_completion_policies(
            state, config, repository_id=workspace.repository_identity
        )
    ]


def _restore_after_failure(
    state: RunState, config: DispatcherConfig, before: Any
) -> None:
    # Never let the outer Dispatcher persist an intent whose save failed.
    try:
        persisted = load_run_state_readonly(config.run_state_path)
        state.completion_journal = persisted.completion_journal
        state.completion_replay_receipts = persisted.completion_replay_receipts
        state.task_reclaim_counts = persisted.task_reclaim_counts
    except (OSError, ValueError):
        (
            state.completion_journal,
            state.completion_replay_receipts,
            state.task_reclaim_counts,
        ) = before

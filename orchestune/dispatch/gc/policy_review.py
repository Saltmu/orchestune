"""Persist launch intent before starting a generation-specific independent review."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from orchestune.complete.contracts import DownstreamPolicyRecord
from orchestune.dispatch.gc.policy_effects import (
    reconcile_close,
    reconcile_comment,
    reconcile_labels,
)
from orchestune.labels import StatusLabel
from orchestune.targets.cloud_routine import ClaudeCodeCloudRoutineDispatchTarget


def review_verdict(forge: Any, issue: int, operation_id: str) -> str | None:
    comments = forge.list_all_issue_comments(issue)
    for verdict in ("failed", "passed"):
        marker = f"<!-- orchestune:policy-review {operation_id} verdict={verdict} -->"
        if any(marker in str(c.get("body", "")) for c in comments):
            return verdict
    return None


def reconcile_review(
    forge: Any,
    issue: int,
    policy: DownstreamPolicyRecord,
    target: Any,
    save: Any,
    now: float,
) -> bool:
    meta = policy.metadata
    operation = meta["operation_id"]
    if meta.get("review_rejected") or meta.get("review_timed_out"):
        return False
    verdict = (
        review_verdict(forge, issue, operation)
        if meta.get("launch_state") in {"launching", "launched"}
        else None
    )
    if verdict == "passed":
        reconcile_comment(
            forge,
            issue,
            policy,
            "独立レビューでも対応不要と確認されたためクローズします。",
        )
        reconcile_close(forge, issue)
        meta["verdict"] = verdict
        return True
    if verdict == "failed":
        reconcile_labels(
            forge, issue, StatusLabel.QUEUED, remove=(StatusLabel.NOT_NEEDED,)
        )
        meta["verdict"] = verdict
        # A rejected completion must never satisfy the dependency gate.
        save({"review_rejected": True})
        return False
    if meta.get("launch_state") == "launched":
        _check_timeout(forge, issue, policy, save, now)
        return False
    return _launch_review(forge, issue, policy, target, save, now)


def _launch_review(
    forge: Any,
    issue: int,
    policy: DownstreamPolicyRecord,
    target: Any,
    save: Any,
    now: float,
) -> bool:
    meta = policy.metadata
    operation = meta["operation_id"]
    if meta.get("launch_state") == "launching":
        return _recover_review_launch(forge, issue, policy, target, save, now)
    if target is None or not callable(getattr(target, "fire_text", None)):
        return False
    target_name = getattr(target, "target_name", None)
    target_name = target_name if isinstance(target_name, str) else type(target).__name__
    save(
        {
            "launch_state": "launching",
            "launch_requested_at": now,
            "launch_target": target_name,
        }
    )
    prompt = (
        f"Independently review the not-needed completion for Issue #{issue}. "
        f"Generation: {policy.generation_id}; completion: {policy.completion_id}. "
        "Read the exact Outcome and verify the task is unnecessary. Post an Issue comment containing "
        f"<!-- orchestune:policy-review {operation} verdict=passed --> if approved, or "
        f"<!-- orchestune:policy-review {operation} verdict=failed --> if rejected, with evidence. "
        "Do not change labels or close the Issue; Orchestune will apply the verdict."
    )
    handle = (
        target.fire_text_once(prompt)
        if isinstance(target, ClaudeCodeCloudRoutineDispatchTarget)
        else target.fire_text(prompt)
    )
    if not handle.external_id and not handle.pid:
        raise RuntimeError("review launch result has no execution identifier")
    save({"launch_state": "launched", "handle": asdict(handle), "launched_at": now})
    return False


def _recover_review_launch(
    forge: Any,
    issue: int,
    policy: DownstreamPolicyRecord,
    target: Any,
    save: Any,
    now: float,
) -> bool:
    meta = policy.metadata
    target_name = getattr(target, "target_name", None)
    target_name = target_name if isinstance(target_name, str) else type(target).__name__
    handle = None
    if target is not None and meta.get("launch_target") == target_name:
        try:
            handle = target.lookup_launch_attempt(meta["operation_id"])
        except Exception:
            # An unavailable lookup never proves that the original POST failed.
            handle = None
    if handle is None:
        _check_timeout(forge, issue, policy, save, now)
    else:
        save(
            {
                "launch_state": "launched",
                "handle": asdict(handle),
                "launched_at": meta["launch_requested_at"],
            }
        )
    return False


def _check_timeout(
    forge: Any, issue: int, policy: DownstreamPolicyRecord, save: Any, now: float
) -> None:
    meta = policy.metadata
    if now - meta["launch_requested_at"] >= meta["review_timeout_seconds"]:
        reconcile_labels(
            forge,
            issue,
            StatusLabel.BLOCKED_HUMAN_REVIEW,
            remove=(StatusLabel.NOT_NEEDED,),
        )
        reconcile_comment(
            forge,
            issue,
            policy,
            "対応不要の独立レビューの起動結果を確認できません。人間による確認が必要です。"
            if meta.get("launch_state") == "launching"
            else "対応不要の独立レビューがタイムアウトしました。人間による確認が必要です。",
        )
        save({"review_timed_out": True})

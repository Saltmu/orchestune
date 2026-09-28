"""Live reconciliation for resumable downstream labels, comments and closes."""

from __future__ import annotations

from typing import Any

from orchestune.complete.contracts import DownstreamPolicyRecord
from orchestune.labels import StatusLabel
from orchestune.ledger.status_labels import PRIMARY_STATUS_LABELS


def reconcile_labels(
    forge: Any,
    issue: int,
    target: str,
    *,
    add: tuple[str, ...] = (),
    remove: tuple[str, ...] = (),
) -> None:
    labels = set(forge.get_issue_labels(issue))
    for label in (target, *add):
        if label not in labels:
            forge.add_label(issue, label)
    for label in (*PRIMARY_STATUS_LABELS, *remove):
        if label != target and label in labels:
            forge.remove_label(issue, label)
    observed = set(forge.get_issue_labels(issue))
    stale = (set(PRIMARY_STATUS_LABELS) - {target}) | set(remove)
    if not {target, *add}.issubset(observed) or stale.intersection(observed):
        raise RuntimeError("policy labels not confirmed")


def reconcile_comment(
    forge: Any, issue: int, policy: DownstreamPolicyRecord, message: str
) -> None:
    marker = f"<!-- orchestune:completion-policy {policy.metadata['operation_id']} -->"
    if not any(
        marker in str(c.get("body", "")) for c in forge.list_all_issue_comments(issue)
    ):
        forge.add_comment(issue, f"{marker}\n{message}")
    if not any(
        marker in str(c.get("body", "")) for c in forge.list_all_issue_comments(issue)
    ):
        raise RuntimeError("policy comment not confirmed")


def reconcile_close(forge: Any, issue: int) -> None:
    if forge.get_issue_state(issue).upper() == "OPEN":
        # The separate marker comment is recovered independently of the close.
        forge.close_issue(issue, "not planned")
    if forge.get_issue_state(issue).upper() != "CLOSED":
        raise RuntimeError("policy close not confirmed")


def apply_effects(forge: Any, issue: int, policy: DownstreamPolicyRecord) -> None:
    meta = policy.metadata
    if policy.policy_kind == "not-needed-close":
        reconcile_comment(
            forge,
            issue,
            policy,
            "対応不要（status:not-needed）と判定されたため、Orchestuneがクローズします。",
        )
        reconcile_close(forge, issue)
        return
    if policy.policy_kind == "review-timeout":
        target = meta["target_label"]
        reconcile_labels(forge, issue, target)
        message = (
            f"AIレビュー待機タイムアウトのため自動再投入します（{meta['retry_count']}回目）。次回起動: {meta['retry_at']:.0f}。"
            if target == StatusLabel.QUEUED
            else "AIレビュー待機タイムアウトが上限に達しました。GitHub Actions の actor、job conclusion、認可エラーを確認してください。"
        )
    else:
        escalated = meta["attempt"] >= 3
        reconcile_labels(
            forge,
            issue,
            meta["target_label"],
            add=() if escalated else ("ci:base-branch-red",),
            remove=("ci:base-branch-red",) if escalated else (),
        )
        message = (
            f"ベースブランチ由来のCI失敗を検知しました（試行回数: {meta['attempt']}/3）。"
            + (
                "人間の確認へエスカレーションしました。"
                if escalated
                else "ci:base-branch-red を付与し、ベースブランチの前進まで保留します。"
            )
        )
    reconcile_comment(forge, issue, policy, message)

"""Durable evidence used to finalize integrated child tasks safely."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from orchestune.forge import Forge
from orchestune.integrator.proofs import TaskIntegrationProof
from orchestune.pr_link_notice import NoticeDelivery, ensure_pr_merged_notice
from orchestune.task_branch_resolution import ResolutionSource, is_commit_oid

RECEIPT_MARKER = "<!-- orchestune:task-integration-proof -->"
_UNSET = object()


@dataclass(frozen=True)
class IntegrationNoticeTarget:
    """Receipt target information used to verify and retry notice delivery."""

    proof: TaskIntegrationProof
    has_integration_pr_key: bool
    integration_pr_number: int | None
    notice_base_branch: str | None


def ensure_integration_receipt(
    forge: Forge,
    proof: TaskIntegrationProof,
    base_branch: str,
    *,
    integration_pr_number: int | None | object = _UNSET,
    notice_base_branch: str | None = None,
) -> bool:
    """Persist the proof before attempting branch deletion, without duplicates."""
    receipt = _receipt_payload(
        proof,
        base_branch,
        integration_pr_number=integration_pr_number,
        notice_base_branch=notice_base_branch,
    )
    try:
        trusted_author = forge.get_authenticated_user()
        comments = forge.list_comments(proof.issue_number)
    except Exception:
        return False
    if any(_matches_receipt(comment, receipt, trusted_author) for comment in comments):
        return True
    try:
        forge.add_comment(
            proof.issue_number,
            render_integration_receipt(
                proof,
                base_branch,
                integration_pr_number=integration_pr_number,
                notice_base_branch=notice_base_branch,
            ),
        )
    except Exception:
        return False
    return True


def _receipt_payload(
    proof: TaskIntegrationProof,
    base_branch: str,
    integration_pr_number: int | None | object = _UNSET,
    notice_base_branch: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "issue_number": proof.issue_number,
        "subtask_id": proof.subtask_id,
        "branch_name": proof.branch_name,
        "source_sha": proof.source_sha,
        "source": proof.source.value,
        "base_branch": base_branch.removeprefix("origin/"),
    }
    if integration_pr_number is not _UNSET:
        payload["integration_pr_number"] = integration_pr_number
        payload["notice_base_branch"] = notice_base_branch or base_branch.removeprefix(
            "origin/"
        )
    return payload


def _render_receipt(receipt: dict[str, Any]) -> str:
    return f"{RECEIPT_MARKER}\n```json\n{json.dumps(receipt, sort_keys=True)}\n```"


def render_integration_receipt(
    proof: TaskIntegrationProof,
    base_branch: str,
    *,
    integration_pr_number: int | None | object = _UNSET,
    notice_base_branch: str | None = None,
) -> str:
    """Render one canonical receipt body for storage and recovery tests."""
    return _render_receipt(
        _receipt_payload(
            proof,
            base_branch,
            integration_pr_number=integration_pr_number,
            notice_base_branch=notice_base_branch,
        )
    )


def _parse_matching_proofs(
    comments: list[dict[str, Any]],
    trusted_author: str,
    issue_number: int,
    subtask_id: str,
    base_branch: str,
) -> list[tuple[TaskIntegrationProof, dict[str, Any]]]:
    clean_base = base_branch.removeprefix("origin/")
    results: list[tuple[TaskIntegrationProof, dict[str, Any]]] = []
    for comment in comments:
        payload = _parse_receipt(comment, trusted_author)
        if not isinstance(payload, dict):
            continue
        if (
            payload.get("issue_number") != issue_number
            or payload.get("subtask_id") != subtask_id
            or payload.get("base_branch") != clean_base
        ):
            continue
        source_sha = payload.get("source_sha")
        if is_commit_oid(source_sha):
            stored_branch = payload.get("branch_name")
            if not isinstance(stored_branch, str) or not stored_branch:
                continue
            try:
                source = ResolutionSource(
                    payload.get("source", ResolutionSource.CANONICAL.value)
                )
            except ValueError:
                continue
            proof = TaskIntegrationProof(
                issue_number=issue_number,
                subtask_id=subtask_id,
                branch_name=stored_branch,
                source_sha=source_sha,
                source=source,
            )
            results.append((proof, payload))
    return results


def find_integration_receipt(
    forge: Forge,
    issue_number: int,
    subtask_id: str,
    base_branch: str,
) -> TaskIntegrationProof | None:
    """Return an exact, syntactically valid proof receipt for a child Issue."""
    try:
        trusted_author = forge.get_authenticated_user()
        comments = forge.list_comments(issue_number)
    except Exception:
        return None
    matching = _parse_matching_proofs(
        comments, trusted_author, issue_number, subtask_id, base_branch
    )
    return matching[0][0] if matching else None


def find_latest_integration_receipt(
    forge: Forge,
    issue_number: int,
    subtask_id: str,
    base_branch: str,
) -> IntegrationNoticeTarget | None:
    """Return the newest matching proof receipt, preferring one with integration_pr_number."""
    try:
        trusted_author = forge.get_authenticated_user()
        comments = forge.list_comments(issue_number)
    except Exception:
        return None

    matching_proofs = _parse_matching_proofs(
        comments, trusted_author, issue_number, subtask_id, base_branch
    )
    if not matching_proofs:
        return None

    # integration_pr_number キーを持つ receipt を優先
    has_pr_key_proofs = [
        (proof, payload)
        for proof, payload in matching_proofs
        if "integration_pr_number" in payload
    ]
    if has_pr_key_proofs:
        proof, payload = has_pr_key_proofs[-1]
        return IntegrationNoticeTarget(
            proof=proof,
            has_integration_pr_key=True,
            integration_pr_number=payload.get("integration_pr_number"),
            notice_base_branch=payload.get("notice_base_branch"),
        )

    # キーを持つ receipt が1件もない場合だけ旧形式の最新を採用
    proof, payload = matching_proofs[-1]
    return IntegrationNoticeTarget(
        proof=proof,
        has_integration_pr_key=False,
        integration_pr_number=None,
        notice_base_branch=None,
    )


def _matches_receipt(
    comment: dict[str, Any], receipt: dict[str, Any], trusted_author: str
) -> bool:
    return _parse_receipt(comment, trusted_author) == receipt


def _parse_receipt(
    comment: dict[str, Any], trusted_author: str
) -> dict[str, Any] | None:
    if comment.get("author") != trusted_author:
        return None
    body = comment.get("body")
    if not isinstance(body, str) or RECEIPT_MARKER not in body:
        return None
    try:
        payload = (
            body.split(RECEIPT_MARKER, 1)[1].split("```json", 1)[1].split("```", 1)[0]
        )
        parsed = json.loads(payload)
        if not isinstance(parsed, dict) or body != _render_receipt(parsed):
            return None
        return parsed
    except (IndexError, json.JSONDecodeError):
        return None


def resolve_merged_close_delivery(
    forge: Forge,
    issue_number: int,
    integration_pr_number: int | None,
    notice_base_branch: str,
) -> tuple[NoticeDelivery, str | None]:
    """AutoMergeChildIntegrationStep用のマージ通知試行とクローズ判定。"""
    if integration_pr_number is not None:
        delivery = ensure_pr_merged_notice(
            forge, issue_number, integration_pr_number, notice_base_branch
        )
        return delivery, None
    return NoticeDelivery.ALREADY_PRESENT, (
        "Integratorが親ブランチへの自動マージを完了したため、"
        "このIssueを自動的にクローズしました。"
    )


def resolve_retry_close_delivery(
    forge: Forge,
    issue_number: int,
    subtask_id: str,
    base_branch: str,
) -> tuple[NoticeDelivery, str | None]:
    """RetryChildIssueCloseStep用の通知再試行とクローズ判定。"""
    target = find_latest_integration_receipt(
        forge, issue_number, subtask_id, base_branch
    )
    if (
        target is not None
        and target.has_integration_pr_key
        and target.integration_pr_number is not None
    ):
        notice_base = target.notice_base_branch or base_branch.removeprefix("origin/")
        delivery = ensure_pr_merged_notice(
            forge, issue_number, target.integration_pr_number, notice_base
        )
        return delivery, None
    return NoticeDelivery.ALREADY_PRESENT, (
        "Integratorが親ブランチへの自動マージを完了済みですが、"
        "前回のクローズ処理に失敗していたため再試行しました。"
    )


__all__ = [
    "IntegrationNoticeTarget",
    "RECEIPT_MARKER",
    "ensure_integration_receipt",
    "find_integration_receipt",
    "find_latest_integration_receipt",
    "render_integration_receipt",
    "resolve_merged_close_delivery",
    "resolve_retry_close_delivery",
]

"""Durable evidence used to finalize integrated child tasks safely."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from orchestune.forge import Forge
from orchestune.integrator.proofs import TaskIntegrationProof
from orchestune.models import normalize_newlines
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


def _matching_payload(
    comment: dict[str, Any],
    trusted_author: str,
    issue_number: int,
    subtask_id: str,
    base_branch: str,
) -> dict[str, Any] | None:
    payload = _parse_receipt(comment, trusted_author)
    if payload is None or (
        (
            not isinstance(payload.get("issue_number"), int)
            or isinstance(payload.get("issue_number"), bool)
        )
        or payload.get("issue_number") != issue_number
        or payload.get("subtask_id") != subtask_id
        or payload.get("base_branch") != base_branch.removeprefix("origin/")
    ):
        return None
    return payload


def _proof_from_payload(payload: dict[str, Any]) -> TaskIntegrationProof | None:
    source_sha = payload.get("source_sha")
    stored_branch = payload.get("branch_name")
    source_value = payload.get("source", ResolutionSource.CANONICAL.value)
    if (
        not is_commit_oid(source_sha)
        or not isinstance(stored_branch, str)
        or not stored_branch
        or not isinstance(source_value, str)
    ):
        return None
    try:
        source = ResolutionSource(source_value)
    except ValueError:
        return None
    return TaskIntegrationProof(
        issue_number=payload["issue_number"],
        subtask_id=payload["subtask_id"],
        branch_name=stored_branch,
        source_sha=source_sha,
        source=source,
    )


def _parse_matching_proofs(
    comments: list[dict[str, Any]],
    trusted_author: str,
    issue_number: int,
    subtask_id: str,
    base_branch: str,
) -> list[tuple[TaskIntegrationProof, dict[str, Any]]]:
    results = []
    for comment in comments:
        payload = _matching_payload(
            comment, trusted_author, issue_number, subtask_id, base_branch
        )
        if payload is not None:
            proof = _proof_from_payload(payload)
            if proof is not None:
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


class ReceiptLookupStatus(StrEnum):
    FOUND = "FOUND"
    ABSENT = "ABSENT"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class ReceiptLookupResult:
    """Distinguish missing evidence from evidence that cannot safely be used."""

    status: ReceiptLookupStatus
    receipt: IntegrationNoticeTarget | None = None
    reason: str | None = None


@dataclass(frozen=True)
class _ReceiptCandidate:
    payload: dict[str, Any]
    comment: dict[str, Any]
    target: IntegrationNoticeTarget


def _unavailable(reason: str) -> ReceiptLookupResult:
    return ReceiptLookupResult(ReceiptLookupStatus.UNAVAILABLE, reason=reason)


def _notice_fields_valid(payload: dict[str, Any], base_branch: str) -> bool:
    number = payload.get("integration_pr_number")
    if number is not None and (
        not isinstance(number, int) or isinstance(number, bool) or number <= 0
    ):
        return False
    if "notice_base_branch" in payload:
        base = payload["notice_base_branch"]
        if (
            not isinstance(base, str)
            or not base
            or base != base_branch.removeprefix("origin/")
        ):
            return False
    return True


def _rest_receipt_comment(comment: dict[str, Any]) -> dict[str, Any]:
    user = comment.get("user")
    body = comment.get("body")
    return {
        "author": user.get("login") if isinstance(user, dict) else None,
        "body": normalize_newlines(body) if isinstance(body, str) else body,
    }


def lookup_integration_receipt(
    forge: Forge,
    issue_number: int,
    subtask_id: str,
    base_branch: str,
) -> ReceiptLookupResult:
    """Read all durable receipts once and select a complete proof/notice pair."""
    try:
        author = forge.get_authenticated_user()
        if not isinstance(author, str) or not author:
            return _unavailable("receipt_lookup_failed")
        comments = forge.list_all_issue_comments(issue_number)
    except Exception:
        return _unavailable("receipt_lookup_failed")
    candidates = []
    for comment in comments:
        payload = _matching_payload(
            _rest_receipt_comment(comment),
            author,
            issue_number,
            subtask_id,
            base_branch,
        )
        if payload is None:
            continue
        proof = _proof_from_payload(payload)
        if proof is None or not _notice_fields_valid(payload, base_branch):
            return _unavailable("receipt_invalid")
        target = IntegrationNoticeTarget(
            proof=proof,
            has_integration_pr_key="integration_pr_number" in payload,
            integration_pr_number=payload.get("integration_pr_number"),
            notice_base_branch=payload.get("notice_base_branch"),
        )
        candidates.append(_ReceiptCandidate(payload, comment, target))
    if not candidates:
        return ReceiptLookupResult(ReceiptLookupStatus.ABSENT)
    keyed = [c for c in candidates if c.target.has_integration_pr_key]
    return _select_receipt(keyed or candidates)


_RECEIPT_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


def _comment_time(comment: dict[str, Any]) -> datetime | None:
    value = comment.get("created_at")
    if not isinstance(value, str) or _RECEIPT_TIMESTAMP.fullmatch(value) is None:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _comment_id(comment: dict[str, Any]) -> int | None:
    value = comment.get("id")
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else None
    )


def _duplicate_ids_conflict(candidates: list[_ReceiptCandidate]) -> bool:
    seen: dict[int, _ReceiptCandidate] = {}
    for candidate in candidates:
        comment_id = _comment_id(candidate.comment)
        if comment_id is None:
            continue
        previous = seen.get(comment_id)
        if previous is not None and (
            previous.payload != candidate.payload
            or _comment_time(previous.comment) != _comment_time(candidate.comment)
        ):
            return True
        seen[comment_id] = candidate
    return False


def _select_receipt(candidates: list[_ReceiptCandidate]) -> ReceiptLookupResult:
    if _duplicate_ids_conflict(candidates):
        return _unavailable("receipt_order_unavailable")
    first = candidates[0]
    if all(c.payload == first.payload for c in candidates):
        return ReceiptLookupResult(ReceiptLookupStatus.FOUND, first.target)
    ordered = []
    for candidate in candidates:
        created_at = _comment_time(candidate.comment)
        comment_id = _comment_id(candidate.comment)
        if created_at is None or comment_id is None:
            return _unavailable("receipt_order_unavailable")
        ordered.append(((created_at, comment_id), candidate))
    latest = max(ordered, key=lambda entry: entry[0])[1]
    return ReceiptLookupResult(ReceiptLookupStatus.FOUND, latest.target)


def find_latest_integration_receipt(
    forge: Forge,
    issue_number: int,
    subtask_id: str,
    base_branch: str,
) -> IntegrationNoticeTarget | None:
    """Compatibility wrapper; retry callers must retain the three-state result."""
    return lookup_integration_receipt(
        forge, issue_number, subtask_id, base_branch
    ).receipt


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
    *,
    lookup: ReceiptLookupResult | None = None,
) -> tuple[NoticeDelivery, str | None]:
    """RetryChildIssueCloseStep用の通知再試行とクローズ判定。"""
    result = (
        lookup
        if lookup is not None
        else lookup_integration_receipt(forge, issue_number, subtask_id, base_branch)
    )
    if result.status is ReceiptLookupStatus.UNAVAILABLE:
        return NoticeDelivery.DEFERRED_READ, None
    target = result.receipt
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
    "ReceiptLookupResult",
    "ReceiptLookupStatus",
    "lookup_integration_receipt",
    "ensure_integration_receipt",
    "find_integration_receipt",
    "find_latest_integration_receipt",
    "render_integration_receipt",
    "resolve_merged_close_delivery",
    "resolve_retry_close_delivery",
]

"""Durable evidence used to finalize integrated child tasks safely."""

from __future__ import annotations

import json
import re
from typing import Any

from orchestune.forge import Forge
from orchestune.integrator.proofs import TaskIntegrationProof

RECEIPT_MARKER = "<!-- orchestune:task-integration-proof -->"
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def ensure_integration_receipt(
    forge: Forge, proof: TaskIntegrationProof, base_branch: str
) -> bool:
    """Persist the proof before attempting branch deletion, without duplicates."""
    receipt = _receipt_payload(proof, base_branch)
    try:
        trusted_author = forge.get_authenticated_user()
        comments = forge.list_comments(proof.issue_number)
    except Exception:
        return False
    if any(_matches_receipt(comment, receipt, trusted_author) for comment in comments):
        return True
    try:
        forge.add_comment(
            proof.issue_number, render_integration_receipt(proof, base_branch)
        )
    except Exception:
        return False
    return True


def _receipt_payload(proof: TaskIntegrationProof, base_branch: str) -> dict[str, Any]:
    return {
        "issue_number": proof.issue_number,
        "subtask_id": proof.subtask_id,
        "branch_name": proof.branch_name,
        "source_sha": proof.source_sha,
        "base_branch": base_branch.removeprefix("origin/"),
    }


def _render_receipt(receipt: dict[str, Any]) -> str:
    return f"{RECEIPT_MARKER}\n```json\n{json.dumps(receipt, sort_keys=True)}\n```"


def render_integration_receipt(proof: TaskIntegrationProof, base_branch: str) -> str:
    """Render one canonical receipt body for storage and recovery tests."""
    return _render_receipt(_receipt_payload(proof, base_branch))


def find_integration_receipt(
    forge: Forge,
    issue_number: int,
    subtask_id: str,
    branch_name: str,
    base_branch: str,
) -> TaskIntegrationProof | None:
    """Return an exact, syntactically valid proof receipt for a child Issue."""
    try:
        trusted_author = forge.get_authenticated_user()
        comments = forge.list_comments(issue_number)
    except Exception:
        return None
    for comment in comments:
        payload = _parse_receipt(comment, trusted_author)
        if not isinstance(payload, dict):
            continue
        if (
            payload.get("issue_number") != issue_number
            or payload.get("subtask_id") != subtask_id
            or payload.get("branch_name") != branch_name
            or payload.get("base_branch") != base_branch.removeprefix("origin/")
        ):
            continue
        source_sha = payload.get("source_sha")
        if isinstance(source_sha, str) and _SHA_PATTERN.fullmatch(source_sha):
            return TaskIntegrationProof(
                issue_number=issue_number,
                subtask_id=subtask_id,
                branch_name=branch_name,
                source_sha=source_sha,
            )
    return None


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
        return parsed if isinstance(parsed, dict) else None
    except (IndexError, json.JSONDecodeError):
        return None

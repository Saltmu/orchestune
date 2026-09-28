"""Idempotent token escalation under the completion publication lock."""

from __future__ import annotations

from typing import Any

from orchestune.complete.posting import OutcomeLookupUnknownError
from orchestune.labels import StatusLabel
from orchestune.ledger.escalation import apply_human_review_escalation


class _EscalationForge:
    def __init__(self, forge: Any, body: str):
        self.forge, self.body = forge, body

    def __getattr__(self, name: str) -> Any:
        return getattr(self.forge, name)

    def add_comment(self, issue: int | str, body: str) -> None:
        comments = self.forge.list_all_issue_comments(issue)
        if any(comment.get("body") == body for comment in comments):
            return
        try:
            self.forge.create_issue_comment(issue, body)
        except Exception as error:
            comments = self.forge.list_all_issue_comments(issue)
            if not any(comment.get("body") == body for comment in comments):
                raise OutcomeLookupUnknownError(
                    "Token escalation comment could not be confirmed"
                ) from error


def escalate_token_limit_locked(
    forge: Any, issue: int, completion_id: str, policy: dict[str, Any]
) -> dict[str, Any]:
    body = f"<!-- orchestune:completion-token-limit {completion_id} -->\nToken usage exceeded max_tokens_per_task={policy.get('limit')}; completion is held for human review."
    proxy = _EscalationForge(forge, body)
    labels = tuple(forge.get_issue_labels(issue))
    apply_human_review_escalation(issue, labels, body, forge=proxy)
    if StatusLabel.BLOCKED_HUMAN_REVIEW not in forge.get_issue_labels(issue):
        raise OutcomeLookupUnknownError("Token escalation label could not be confirmed")
    return {
        "label": StatusLabel.BLOCKED_HUMAN_REVIEW,
        "confirmed": True,
        "comment_marker": completion_id,
    }

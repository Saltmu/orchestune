"""Deterministic audit markers and summaries for replan replacement."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from orchestune.replan.models import PlanRevision, ReplacementResult


class AuditCommentForge(Protocol):
    def list_comments(self, issue_number: int | str) -> list[dict[str, Any]]: ...


def retirement_marker(plan_revision: PlanRevision | str) -> str:
    """Return the unique marker for retiring one old generation."""

    revision = PlanRevision(str(plan_revision))
    return f"<!-- orchestune:replan-retirement plan_revision={revision} -->"


def replan_audit_marker(plan_revision: PlanRevision | str) -> str:
    """Return the unique parent audit marker for one replacement revision."""

    revision = PlanRevision(str(plan_revision))
    return f"<!-- orchestune:replan-audit plan_revision={revision} -->"


def _numbers(values: Sequence[int]) -> str:
    return ", ".join(f"#{number}" for number in values) if values else "none"


def render_retirement_audit(
    plan_revision: PlanRevision | str, *, replacement_issue_numbers: Sequence[int]
) -> str:
    """Render the stable reason comment placed on every retired Issue."""

    revision = PlanRevision(str(plan_revision))
    return (
        f"{retirement_marker(revision)}\n"
        "This unstarted Issue belongs to the superseded decomposition generation. "
        f"It was retired after replacement Issues {_numbers(replacement_issue_numbers)} "
        "were prepared; its original title, body, and Footprint remain unchanged."
    )


def render_replan_audit(result: ReplacementResult) -> str:
    """Render a deterministic parent summary for a completed replacement."""

    return "\n".join(
        (
            replan_audit_marker(result.plan_revision),
            "## Replan generation replacement",
            f"- Plan revision: `{result.plan_revision}`",
            f"- Created new-generation Issues: {_numbers(result.created_issue_numbers)}",
            f"- Reused new-generation Issues: {_numbers(result.reused_issue_numbers)}",
            f"- Retired old-generation Issues: {_numbers(result.retired_issue_numbers)}",
            f"- Native relationship result: {'degraded' if result.degraded else 'complete'}",
        )
    )


def comments_contain_marker(
    comments: Sequence[Mapping[str, object]], marker: str
) -> bool:
    return any(marker in str(comment.get("body", "")) for comment in comments)


def find_existing_replan_audit(
    forge: AuditCommentForge,
    parent_issue_number: int,
    plan_revision: PlanRevision | str,
) -> bool:
    """Return whether the parent already holds this revision's audit comment."""

    return comments_contain_marker(
        forge.list_comments(parent_issue_number), replan_audit_marker(plan_revision)
    )


__all__ = [
    "find_existing_replan_audit",
    "render_replan_audit",
    "replan_audit_marker",
    "retirement_marker",
]

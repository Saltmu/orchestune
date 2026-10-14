"""Strict Issue identity of a merged pull request (`merge_evidence`)."""

from __future__ import annotations

from orchestune.merge_evidence import identity_status
from orchestune.models import PrRecord


def _pr(**changes: object) -> PrRecord:
    values: dict[str, object] = {
        "number": 1,
        "head_ref": "claude/issue-42-task",
        "changed_files": (),
        "closes_issue_numbers": (42,),
    }
    values.update(changes)
    return PrRecord(**values)  # type: ignore[arg-type]


def test_known_closing_references_keep_the_existing_verdicts():
    assert identity_status(_pr(), 42, "task") == "match"
    assert (
        identity_status(_pr(head_ref="fix/x", closes_issue_numbers=()), 42, "task")
        == "none"
    )
    assert identity_status(_pr(closes_issue_numbers=(7,)), 42, "task") == "conflict"


def test_unknown_closing_references_without_the_issue_stay_unknown():
    """#1336/#1341: without the full list, an Issue missing from the verified
    subset proves neither a match nor its absence."""
    unknown = {"closing_references_known": False, "closes_issue_numbers": ()}
    assert identity_status(_pr(**unknown), 42, "task") == "unknown"
    assert identity_status(_pr(head_ref="fix/x", **unknown), 42, "task") == "unknown"
    assert (
        identity_status(
            _pr(closing_references_known=False, closes_issue_numbers=(7,)), 42, "task"
        )
        == "unknown"
    )


def test_verified_closing_reference_decides_without_the_full_list():
    """#1341: REST proved that the merge closed the Issue, so the branch name
    alone decides between match and conflict."""
    assert identity_status(_pr(closing_references_known=False), 42, "task") == "match"
    assert (
        identity_status(
            _pr(head_ref="fix/x", closing_references_known=False), 42, "task"
        )
        == "match"
    )
    assert (
        identity_status(
            _pr(head_ref="claude/issue-7-other", closing_references_known=False),
            42,
            "task",
        )
        == "conflict"
    )

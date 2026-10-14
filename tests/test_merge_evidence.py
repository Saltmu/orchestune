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


def test_unknown_closing_references_are_never_a_match():
    """#1336: REST cannot report closing references, so identity is unknown
    even for a canonical head."""
    assert identity_status(_pr(closing_references_known=False), 42, "task") == "unknown"
    assert (
        identity_status(
            _pr(
                head_ref="fix/x",
                closes_issue_numbers=(),
                closing_references_known=False,
            ),
            42,
            "task",
        )
        == "unknown"
    )

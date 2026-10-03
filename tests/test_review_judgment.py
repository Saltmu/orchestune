"""Reusable review judgment contract tests."""

import pytest
import yaml

from orchestune.review.judgment import parse_judgments, validate_coverage
from orchestune.review.markers import (
    parse_head_marker,
    review_head_marker,
    review_selection_marker,
)


def document(findings=None, round_num=1):
    return (
        "```orchestune-review-judgments\n"
        + yaml.safe_dump({"round": round_num, "findings": findings or []})
        + "```\n"
    )


def finding(**updates):
    return dict(
        source="inline_comment:12",
        location="app.py:8",
        judgment="adopt",
        status="resolved",
        basis="contract regression",
        evidence="commit abc",
        **updates,
    )


def test_valid_judgments_and_zero_findings():
    assert parse_judgments(document([finding()]))["round"] == 1
    assert parse_judgments(document())["findings"] == []


@pytest.mark.parametrize(
    "changes",
    [
        {"judgment": "pass"},
        {"status": "done"},
        {"basis": ""},
        {"source": ""},
        {"evidence": []},
    ],
)
def test_invalid_judgment_fields(changes):
    row = finding()
    row.update(changes)
    with pytest.raises(ValueError):
        parse_judgments(document([row]))


def test_missing_and_ambiguous_blocks_rejected():
    with pytest.raises(ValueError):
        parse_judgments("LGTM")
    with pytest.raises(ValueError):
        parse_judgments(document() + document())


def test_coverage_checks_current_sources_and_round():
    result = {
        "round": 1,
        "review_items": [
            {
                "id": 7,
                "kind": "issue_comment",
                "body": "finding",
                "provenance": "current",
            },
            {"id": 8, "kind": "review", "body": "old", "provenance": "historical"},
        ],
        "inline_comments": [
            {"id": 12, "body": "bug", "provenance": "current"},
        ],
    }
    with pytest.raises(ValueError, match="issue_comment:7"):
        validate_coverage(parse_judgments(document([finding()])), result)
    rows = [
        finding(),
        {
            **finding(),
            "source": "issue_comment:7",
            "judgment": "decline",
            "status": "declined",
        },
    ]
    validate_coverage(parse_judgments(document(rows)), result)
    with pytest.raises(ValueError, match="round"):
        validate_coverage(parse_judgments(document(rows, 2)), result)


def test_markers_validate_and_roundtrip_head():
    sha = "a" * 40
    assert parse_head_marker(review_head_marker(sha)) == sha
    assert parse_head_marker("legacy trigger") is None
    assert (
        review_selection_marker("skip", sha)
        == f"<!-- orchestune:review-selection reviewer=skip head={sha} -->"
    )
    with pytest.raises(ValueError):
        review_head_marker("unknown")


@pytest.mark.parametrize("round_num", [0, -1, True, "1", None])
def test_invalid_rounds(round_num):
    with pytest.raises(ValueError):
        parse_judgments(document(round_num=round_num))


def test_deferred_requires_basis_and_fields():
    row = finding()
    row.update(status="deferred", judgment="decline", basis="")
    with pytest.raises(ValueError, match="basis"):
        parse_judgments(document([row]))
    row["basis"] = "outside acceptance criteria; follow-up issue 123"
    del row["location"]
    with pytest.raises(ValueError, match="location"):
        parse_judgments(document([row]))


def test_multiple_findings_in_same_source_are_allowed():
    first = finding()
    second = {
        **finding(),
        "location": "app.py:19",
        "basis": "second independent regression",
    }
    parsed = parse_judgments(document([first, second]))
    assert len(parsed["findings"]) == 2


def test_conflicting_review_commits_do_not_fall_back_to_trigger():
    from orchestune.review.markers import derive_review_target

    items = [
        {"kind": "review", "provenance": "current", "commit_id": sha * 40}
        for sha in ("a", "b")
    ]
    assert derive_review_target(items, "a" * 40, "a" * 40) == (None, "unknown")

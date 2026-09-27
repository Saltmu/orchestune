from __future__ import annotations

from scripts.review_verdict import (
    ACQUISITION_ACQUIRED,
    ACQUISITION_IN_PROGRESS,
    ACQUISITION_UNAVAILABLE,
    SCHEMA_VERSION,
    collect_review_state,
    extract_review_result,
    normalize_review_state,
)


def test_normalize_review_state_rejects_non_list_sections():
    import pytest

    with pytest.raises(ValueError, match="reviews"):
        normalize_review_state({"reviews": {"id": 1}})


def test_collect_review_state_unavailable_when_no_bot_activity():
    result = collect_review_state({}, bot_name="codex")
    assert result == {
        "schema_version": SCHEMA_VERSION,
        "acquisition_status": ACQUISITION_UNAVAILABLE,
        "reason": "no @codex activity found in the supplied review state",
        "review_items": [],
        "review_body": "",
        "inline_comments": [],
        "timestamp": "",
    }


def test_collect_review_state_acquired_carries_no_verdict_field():
    state = {
        "reviews": [
            {
                "id": 1,
                "user": {"login": "codex[bot]"},
                "submitted_at": "2026-08-20T10:00:00Z",
                "body": "Didn't find any major issues.",
            }
        ],
    }

    result = collect_review_state(state, bot_name="codex")

    assert result["acquisition_status"] == ACQUISITION_ACQUIRED
    assert "verdict" not in result
    assert result["review_body"] == "Didn't find any major issues."


def test_extract_review_result_keeps_all_current_round_bodies_not_just_latest():
    """A body-only summary and a separate findings review in the same round must
    both survive — collapsing to a single "latest" item was the bug the issue
    reports (comment/review multiplicity loses content)."""
    state = normalize_review_state(
        {
            "issue_comments": [
                {
                    "id": 1,
                    "user": {"login": "claude[bot]"},
                    "created_at": "2026-08-20T10:00:00Z",
                    "body": "LGTM overall.",
                }
            ],
            "reviews": [
                {
                    "id": 2,
                    "user": {"login": "claude[bot]"},
                    "submitted_at": "2026-08-20T10:05:00Z",
                    "body": "### Findings\n- Bug: validate the input.",
                    "state": "CHANGES_REQUESTED",
                }
            ],
        }
    )

    result = extract_review_result(state, "claude")

    assert result is not None
    assert len(result["review_items"]) == 2
    assert result["review_body"] == (
        "LGTM overall.\n\n---\n\n### Findings\n- Bug: validate the input."
    )
    assert all(item["provenance"] == "current" for item in result["review_items"])


def test_extract_review_result_tags_items_before_round_start_as_historical():
    state = normalize_review_state(
        {
            "reviews": [
                {
                    "id": 1,
                    "user": {"login": "claude[bot]"},
                    "submitted_at": "2026-08-20T09:00:00Z",
                    "body": "old round findings",
                },
                {
                    "id": 2,
                    "user": {"login": "claude[bot]"},
                    "submitted_at": "2026-08-20T10:00:00Z",
                    "body": "new round findings",
                },
            ],
        }
    )

    result = extract_review_result(
        state, "claude", round_started_at="2026-08-20T09:30:00Z"
    )

    assert result is not None
    by_id = {item["id"]: item for item in result["review_items"]}
    assert by_id[1]["provenance"] == "historical"
    assert by_id[2]["provenance"] == "current"
    # Historical content is not discarded, only excluded from the round's body.
    assert result["review_body"] == "new round findings"


def test_extract_review_result_tags_missing_timestamp_as_unassociated():
    state = normalize_review_state(
        {
            "reviews": [
                {
                    "id": 1,
                    "user": {"login": "claude[bot]"},
                    "submitted_at": "2026-08-20T10:00:00Z",
                    "body": "current round findings",
                }
            ],
            "inline_comments": [
                {
                    "id": 9,
                    "user": {"login": "claude[bot]"},
                    "path": "a.py",
                    "line": 1,
                    "body": "no timestamp at all",
                }
            ],
        }
    )

    result = extract_review_result(
        state, "claude", round_started_at="2026-08-20T09:30:00Z"
    )

    assert result is not None
    assert result["inline_comments"][0]["provenance"] == "unassociated"


def test_extract_review_result_returns_none_for_only_unassociated_content():
    """An unassociated-only result (no confirmed current-round content) must
    not be reported as an acquired result for the round (Codex PR #1114
    round 2 finding: only current-provenance content may satisfy a round)."""
    state = normalize_review_state(
        {
            "inline_comments": [
                {
                    "id": 9,
                    "user": {"login": "claude[bot]"},
                    "path": "a.py",
                    "line": 1,
                    "body": "no timestamp at all",
                }
            ],
        }
    )

    result = extract_review_result(
        state, "claude", round_started_at="2026-08-20T09:30:00Z"
    )

    assert result is None


def test_extract_review_result_returns_none_when_only_historical_content_exists():
    """A prior round's real review body must not satisfy a new round whose
    own activity is empty (Codex PR #1114 round 2 finding)."""
    state = normalize_review_state(
        {
            "reviews": [
                {
                    "id": 1,
                    "user": {"login": "claude[bot]"},
                    "submitted_at": "2026-08-20T09:00:00Z",
                    "body": "old round findings",
                }
            ],
        }
    )

    result = extract_review_result(
        state, "claude", round_started_at="2026-08-20T09:30:00Z"
    )

    assert result is None


def test_extract_review_result_excludes_finished_progress_tracker_from_review_items():
    state = normalize_review_state(
        {
            "issue_comments": [
                {
                    "id": 1,
                    "user": {"login": "claude[bot]"},
                    "created_at": "2026-08-20T10:00:00Z",
                    "body": "**Claude finished**\nView job run here",
                },
                {
                    "id": 2,
                    "user": {"login": "claude[bot]"},
                    "created_at": "2026-08-20T10:01:00Z",
                    "body": "Actual review summary.",
                },
            ],
        }
    )

    result = extract_review_result(state, "claude")

    assert result is not None
    assert [item["id"] for item in result["review_items"]] == [2]
    assert result["review_body"] == "Actual review summary."


def test_extract_review_result_empty_body_falls_back_to_current_inline_count():
    state = normalize_review_state(
        {
            "reviews": [
                {
                    "id": 1,
                    "user": {"login": "claude[bot]"},
                    "submitted_at": "2026-08-20T10:00:00Z",
                    "body": "",
                }
            ],
            "inline_comments": [
                {
                    "id": 2,
                    "user": {"login": "claude[bot]"},
                    "created_at": "2026-08-20T10:00:00Z",
                    "path": "a.py",
                    "line": 1,
                    "body": "bug",
                }
            ],
        }
    )

    result = extract_review_result(state, "claude")

    assert result is not None
    assert "1 inline comment(s)" in result["review_body"]


def test_extract_review_result_returns_none_for_empty_review_with_no_inlines():
    """An empty review record (no body, no inline comments) is not review
    content -- distinct from a genuine zero-findings review, which always
    carries real body text (issue #1099)."""
    state = normalize_review_state(
        {
            "reviews": [
                {
                    "id": 1,
                    "user": {"login": "claude[bot]"},
                    "submitted_at": "2026-08-20T10:00:00Z",
                    "body": "",
                    "state": "APPROVED",
                }
            ],
        }
    )
    assert extract_review_result(state, "claude") is None


def test_extract_review_result_returns_none_when_no_bot_activity():
    state = normalize_review_state({"issue_comments": []})
    assert extract_review_result(state, "claude") is None


def test_extract_review_result_returns_none_for_lone_finished_tracker():
    """A "job finished" tracker with no other activity is execution telemetry,
    not review content — must not be reported as a (empty) acquired result."""
    state = normalize_review_state(
        {
            "issue_comments": [
                {
                    "id": 1,
                    "user": {"login": "claude[bot]"},
                    "created_at": "2026-08-20T10:00:00Z",
                    "body": "**Claude finished**\nView job run here",
                }
            ],
        }
    )
    assert extract_review_result(state, "claude") is None


def test_collect_review_state_in_progress_for_explicit_marker():
    """A single snapshot (no polling loop) whose latest activity explicitly
    says the bot is still working must report in_progress, not a final
    acquired-or-unavailable result (issue #1099 Exit 11)."""
    state = {
        "issue_comments": [
            {
                "id": 1,
                "user": {"login": "claude[bot]"},
                "created_at": "2026-08-20T10:00:00Z",
                "updated_at": "2026-08-20T10:00:00Z",
                "body": "### Review in progress\n- [ ] Working...",
            }
        ],
    }
    result = collect_review_state(state, bot_name="claude")
    assert result["acquisition_status"] == ACQUISITION_IN_PROGRESS
    assert "verdict" not in result


def test_collect_review_state_unavailable_for_lone_finished_tracker():
    state = {
        "issue_comments": [
            {
                "id": 1,
                "user": {"login": "claude[bot]"},
                "created_at": "2026-08-20T10:00:00Z",
                "body": "**Claude finished**\nView job run here",
            }
        ],
    }
    result = collect_review_state(state, bot_name="claude")
    assert result["acquisition_status"] == ACQUISITION_UNAVAILABLE


def test_extract_preserves_inline_metadata_and_provenance() -> None:
    metadata = {
        "id": 5,
        "diff_hunk": "@@ -1 +1 @@",
        "side": "LEFT",
        "start_line": 2,
        "start_side": "LEFT",
        "commit_id": "a" * 40,
        "original_commit_id": "b" * 40,
        "original_line": 3,
    }
    state = normalize_review_state(
        {
            "reviews": [{"body": "findings", "user": {"login": "claude"}}],
            "inline_comments": [
                {
                    "body": "bug",
                    "path": "a.py",
                    "line": None,
                    "user": {"login": "claude"},
                    **metadata,
                }
            ],
        }
    )
    result = extract_review_result(state, "claude")
    assert result is not None
    finding = result["inline_comments"][0]
    assert finding["line"] == 3
    assert finding["position_line"] is None
    assert finding["provenance"] == "current"
    for key, value in metadata.items():
        assert finding[key] == value

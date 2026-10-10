from unittest.mock import patch

import pytest

from orchestune.review.progress_tracker import (
    CodexTrackerStatus,
    parse_codex_tracker_status,
)
from scripts.wait_for_review import StalledReviewError, _track_stall, wait_for_review

MARKER = "<!-- codex-pull-request-review-summary -->"


@pytest.fixture(autouse=True)
def _no_network_sha_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scripts.wait_for_review._fetch_pr_head_sha", lambda _: None)
    monkeypatch.setattr("scripts.wait_for_review._fetch_repository_slug", lambda: None)


def tracker(status: str = "Running") -> str:
    return (
        f"{MARKER}\n\n"
        "## Codex Review Summary\n"
        "This comment shows the latest Codex review activity.\n\n"
        "| Review | Status | Commit | Review trigger |\n"
        "| --- | --- | --- | --- |\n"
        f"| 📝 **Code Review** | 🔄 **{status}** since "
        '<relative-time datetime="2026-10-07T02:51:59Z">'
        "2026-10-07T02:51:59Z</relative-time> | `abc1234` | Manual request |\n"
        "\n<details><summary>About Codex</summary>Reviews are running.</details>"
    )


def test_running_tracker_status_comes_from_code_review_row() -> None:
    assert parse_codex_tracker_status(tracker()) is CodexTrackerStatus.IN_PROGRESS


def test_pending_tracker_status_is_in_progress() -> None:
    assert (
        parse_codex_tracker_status(tracker("Pending")) is CodexTrackerStatus.IN_PROGRESS
    )


def test_completed_tracker_status_ignores_running_text_elsewhere() -> None:
    assert (
        parse_codex_tracker_status(tracker("Completed")) is CodexTrackerStatus.COMPLETED
    )


def test_status_from_another_row_does_not_override_code_review() -> None:
    body = tracker("Completed").replace(
        "| --- | --- | --- | --- |",
        "| --- | --- | --- | --- |\n"
        "| Security Review | 🔄 **Running** | `abc1234` | Manual request |",
    )
    assert parse_codex_tracker_status(body) is CodexTrackerStatus.COMPLETED


def test_unknown_and_malformed_trackers_are_not_successful_states() -> None:
    assert parse_codex_tracker_status(tracker("Retrying")) is CodexTrackerStatus.UNKNOWN
    assert parse_codex_tracker_status(MARKER) is CodexTrackerStatus.UNKNOWN


def test_marker_absence_is_not_a_codex_tracker() -> None:
    assert parse_codex_tracker_status("Code Review is Running") is None


def test_unchanged_codex_running_tracker_uses_existing_stall_grace() -> None:
    item = _tracker_comment("Running", updated_at="2026-10-04T03:11:00Z")
    with patch("scripts.wait_for_review.time.time", side_effect=[100.0, 106.0]):
        signature, since = _track_stall(
            item,
            None,
            None,
            stall_grace_seconds=5,
            bot_name="codex",
            pr_number=1271,
            latest_trigger_time="2026-10-04T03:10:00Z",
        )
        with pytest.raises(StalledReviewError, match="PR #1271"):
            _track_stall(
                item,
                signature,
                since,
                stall_grace_seconds=5,
                bot_name="codex",
                pr_number=1271,
                latest_trigger_time="2026-10-04T03:10:00Z",
            )


def _tracker_comment(status: str, *, updated_at: str) -> dict[str, object]:
    return {
        "id": 201,
        "user": {"login": "chatgpt-codex-connector[bot]"},
        "created_at": "2026-10-04T03:11:00Z",
        "updated_at": updated_at,
        "body": tracker(status),
    }


@patch("scripts.wait_for_review._get_pr_data", autospec=True)
@patch("scripts.wait_for_review.post_review_trigger", autospec=True)
def test_online_waits_for_codex_tracker_and_acquires_real_review(
    mock_post, mock_get_data
):
    mock_post.return_value = {
        "id": 200,
        "created_at": "2026-10-04T03:10:00Z",
        "body": "@codex review",
    }
    running = _tracker_comment("Running", updated_at="2026-10-04T03:11:00Z")
    partial_inline = {
        "id": 301,
        "pull_request_review_id": 401,
        "user": {"login": "chatgpt-codex-connector[bot]"},
        "created_at": "2026-10-04T03:12:00Z",
        "body": "Partial finding while the review is running.",
    }
    completed = {
        **running,
        "updated_at": "2026-10-04T03:14:00Z",
        "body": tracker("Completed"),
    }
    real_review = {
        "id": 402,
        "user": {"login": "chatgpt-codex-connector[bot]"},
        "created_at": "2026-10-04T03:15:00Z",
        "updated_at": "2026-10-04T03:15:00Z",
        "body": "### Review complete\nActual review result.",
    }
    empty = {"issue_comments": [], "reviews": [], "inline_comments": []}
    mock_get_data.side_effect = [
        empty,
        {"issue_comments": [running], "reviews": [], "inline_comments": []},
        {
            "issue_comments": [running],
            "reviews": [],
            "inline_comments": [partial_inline],
        },
        {
            "issue_comments": [completed],
            "reviews": [],
            "inline_comments": [partial_inline],
        },
        {
            "issue_comments": [completed, real_review],
            "reviews": [],
            "inline_comments": [partial_inline],
        },
    ]

    result = wait_for_review(
        pr_number=1271,
        timeout=10,
        interval=0,
        bot_name="codex",
        post_trigger=True,
        stall_grace_seconds=1000,
    )

    assert result["review_body"] == "### Review complete\nActual review result."
    assert [item["id"] for item in result["inline_comments"]] == [301]


@patch("scripts.wait_for_review._get_pr_data", autospec=True)
@patch("scripts.wait_for_review.post_review_trigger", autospec=True)
def test_online_acquires_codex_inline_content_after_tracker_completes(
    mock_post, mock_get_data
):
    mock_post.return_value = {
        "id": 200,
        "created_at": "2026-10-04T03:10:00Z",
        "body": "@codex review",
    }
    running = _tracker_comment("Running", updated_at="2026-10-04T03:11:00Z")
    completed = {
        **running,
        "updated_at": "2026-10-04T03:13:00Z",
        "body": tracker("Completed"),
    }
    empty = {"issue_comments": [], "reviews": [], "inline_comments": []}
    review = {
        "id": 401,
        "user": {"login": "chatgpt-codex-connector[bot]"},
        "submitted_at": "2026-10-04T03:14:00Z",
        "body": "",
    }
    inline = {
        "id": 301,
        "pull_request_review_id": 401,
        "user": {"login": "chatgpt-codex-connector[bot]"},
        "created_at": "2026-10-04T03:14:30Z",
        "body": "A real inline finding.",
    }
    mock_get_data.side_effect = [
        empty,
        {"issue_comments": [running], "reviews": [], "inline_comments": []},
        {
            "issue_comments": [completed],
            "reviews": [review],
            "inline_comments": [inline],
        },
    ]

    result = wait_for_review(
        pr_number=1271,
        timeout=10,
        interval=0,
        bot_name="codex",
        post_trigger=True,
        stall_grace_seconds=1000,
    )

    assert "see 1 inline comment(s)" in result["review_body"]
    assert [item["id"] for item in result["inline_comments"]] == [301]

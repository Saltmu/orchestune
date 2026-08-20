from __future__ import annotations

from unittest.mock import patch

import pytest

from scripts.wait_for_review import is_review_completed_comment, wait_for_review


def test_is_review_completed_comment_non_bot():
    comment = {
        "user": {"login": "human_developer"},
        "body": "Re-review complete, looks good!",
    }
    assert is_review_completed_comment(comment, "claude") is False


def test_is_review_completed_comment_in_progress_placeholder():
    comment = {
        "user": {"login": "claude[bot]"},
        "body": "Claude Code is working… <img src=... />",
    }
    assert is_review_completed_comment(comment, "claude") is False

    comment_progress = {
        "user": {"login": "claude[bot]"},
        "body": "### Re-review in progress\n- [ ] Task 1",
    }
    assert is_review_completed_comment(comment_progress, "claude") is False


def test_is_review_completed_comment_bare_todo_checklist_is_false():
    comment = {
        "user": {"login": "claude[bot]"},
        "body": "### Todo\n- [ ] 1. Review changed files\n- [ ] 2. Post feedback",
    }
    assert is_review_completed_comment(comment, "claude") is False


def test_is_review_completed_comment_completed():
    comment = {
        "user": {"login": "claude[bot]"},
        "body": "### Re-review complete\n\n- [x] All checks pass.\n\nI have no further blocking findings.",
    }
    assert is_review_completed_comment(comment, "claude") is True


@patch("scripts.wait_for_review.subprocess.run")
def test_run_gh_api_handles_paginated_slurp_response(mock_run):
    import json

    from scripts.wait_for_review import _run_gh_api

    # Simulated --paginate --slurp response: list of pages (each page is a list of comments)
    page1 = [{"id": 1}, {"id": 2}]
    page2 = [{"id": 3}, {"id": 4}]
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = json.dumps([page1, page2])

    result = _run_gh_api("dummy/endpoint")
    assert len(result) == 4
    assert [c["id"] for c in result] == [1, 2, 3, 4]


@patch("scripts.wait_for_review._get_pr_comments")
@patch("time.sleep")
def test_wait_for_review_detects_new_comment(mock_sleep, mock_get_comments):
    initial = [
        {
            "id": 1,
            "user": {"login": "claude[bot]"},
            "body": "### Re-review complete\nInitial review.",
        }
    ]
    subsequent = [
        {
            "id": 1,
            "user": {"login": "claude[bot]"},
            "body": "### Re-review complete\nInitial review.",
        },
        {
            "id": 2,
            "user": {"login": "claude[bot]"},
            "body": "### Re-review complete\nAll good.",
        },
    ]
    mock_get_comments.side_effect = [initial, subsequent]

    body = wait_for_review(pr_number=537, timeout=30, interval=1, bot_name="claude")
    assert "Re-review complete" in body


@patch("scripts.wait_for_review._get_pr_comments")
@patch("time.sleep")
def test_wait_for_review_times_out(mock_sleep, mock_get_comments):
    mock_get_comments.return_value = []
    with pytest.raises(TimeoutError):
        wait_for_review(pr_number=537, timeout=0, interval=1, bot_name="claude")

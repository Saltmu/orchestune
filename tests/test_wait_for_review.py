from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from scripts.wait_for_review import (
    _build_snapshot,
    _extract_review_result,
    _filter_bot_items,
    _get_item_timestamp,
    _is_bot_user,
    _is_explicitly_in_progress,
    _latest_bot_summary_item,
    _latest_review_trigger_timestamp,
    _run_gh,
    post_review_trigger,
    wait_for_review,
)


def test_filter_bot_items():
    items = [
        {"id": 1, "user": {"login": "claude[bot]"}},
        {"id": 2, "user": {"login": "human"}},
        {"id": 3, "user": {"login": "claude"}},
    ]
    assert _filter_bot_items(items, "claude") == [items[0], items[2]]
    assert _filter_bot_items(items, "claude", exclude_ids={1}) == [items[2]]


def test_extract_review_result_empty_body_fallback_to_inlines():
    current_data = {
        "issue_comments": [],
        "reviews": [
            {
                "id": 1,
                "user": {"login": "claude[bot]"},
                "body": None,
                "created_at": "2026-08-20T10:00:00Z",
            }
        ],
        "inline_comments": [
            {
                "id": 2,
                "user": {"login": "claude[bot]"},
                "path": "app.py",
                "line": 10,
                "body": "Fix null pointer",
                "created_at": "2026-08-20T10:00:00Z",
            }
        ],
    }
    result = _extract_review_result(current_data, "claude")
    assert result is not None
    assert "see 1 inline comment(s)" in result["review_body"]
    assert len(result["inline_comments"]) == 1


def test_is_bot_user():
    assert _is_bot_user("claude[bot]", "claude") is True
    assert _is_bot_user("claude", "claude") is True
    assert _is_bot_user("chatgpt-codex-connector[bot]", "codex") is True
    assert _is_bot_user("codex", "codex") is True
    assert _is_bot_user("human_dev", "claude") is False


def test_is_explicitly_in_progress_uses_only_strong_transient_markers():
    assert _is_explicitly_in_progress({"body": "Claude is working…"}) is True
    assert _is_explicitly_in_progress({"body": "Claude Code is working…"}) is True
    assert _is_explicitly_in_progress({"body": "### Review in progress"}) is True
    assert _is_explicitly_in_progress({"body": "### Claude is reviewing this PR"})
    assert _is_explicitly_in_progress(
        {"body": "### Tasks\n\n- [ ] Run code review\n\n<img src='spinner' />"}
    )
    assert (
        _is_explicitly_in_progress(
            {"body": "### Review complete\n- [ ] Optional follow-up"}
        )
        is False
    )


def test_is_explicitly_in_progress_scans_status_after_a_preamble():
    assert _is_explicitly_in_progress(
        {
            "body": "<!-- transient -->\n\n### Claude is reviewing this PR <img src='spinner' />"
        }
    )


def test_is_explicitly_in_progress_ignores_marker_text_outside_headline():
    completed_review = {
        "body": "### Review complete\n\nThis replaces the old review in progress flow."
    }

    assert _is_explicitly_in_progress(completed_review) is False


def test_is_explicitly_in_progress_ignores_unchecked_item_outside_tasks_section():
    completed_review = {
        "body": (
            "### Tasks\n- [x] Review complete\n\n### Summary\n"
            "The source contains `- [ ] Optional follow-up`."
        )
    }

    assert _is_explicitly_in_progress(completed_review) is False


def test_is_explicitly_in_progress_does_not_match_completed_review_prefix():
    completed_review = {
        "body": "### Codex is reviewing this PR as part of maintenance\n\nFindings: none."
    }

    assert _is_explicitly_in_progress(completed_review) is False


def test_get_item_timestamp():
    assert (
        _get_item_timestamp({"updated_at": "2026-08-20T10:00:00Z"})
        == "2026-08-20T10:00:00Z"
    )
    assert (
        _get_item_timestamp({"submitted_at": "2026-08-20T09:00:00Z"})
        == "2026-08-20T09:00:00Z"
    )
    assert (
        _get_item_timestamp({"created_at": "2026-08-20T08:00:00Z"})
        == "2026-08-20T08:00:00Z"
    )
    assert _get_item_timestamp({}) == ""


def test_latest_review_trigger_timestamp_supports_machine_marker():
    data = {
        "issue_comments": [
            {
                "created_at": "2026-08-20T10:00:00Z",
                "body": "Please check this\n\n<!-- orchestune:review-trigger bot=claude -->",
            }
        ]
    }

    assert _latest_review_trigger_timestamp(data, "claude") == "2026-08-20T10:00:00Z"


def test_latest_review_trigger_timestamp_ignores_the_review_bot_echo():
    data = {
        "issue_comments": [
            {
                "created_at": "2026-08-20T10:00:00Z",
                "user": {"login": "human"},
                "body": "@claude review",
            },
            {
                "created_at": "2026-08-20T10:01:00Z",
                "user": {"login": "claude[bot]"},
                "body": "@claude review",
            },
        ]
    }

    assert _latest_review_trigger_timestamp(data, "claude") == "2026-08-20T10:00:00Z"


def test_latest_bot_summary_item_preserves_review_tiebreak_order():
    issue_comment = {
        "id": 1,
        "user": {"login": "claude[bot]"},
        "updated_at": "2026-08-20T10:00:00Z",
        "body": "Issue comment",
    }
    review = {
        "id": 2,
        "user": {"login": "claude[bot]"},
        "submitted_at": "2026-08-20T10:00:00Z",
        "body": "Review",
    }

    assert (
        _latest_bot_summary_item(
            {"issue_comments": [issue_comment], "reviews": [review]}, "claude"
        )
        == review
    )


def test_latest_bot_summary_item_ignores_finished_tracker_without_summary():
    tracker = {
        "id": 1,
        "user": {"login": "claude[bot]"},
        "created_at": "2026-08-20T10:00:00Z",
        "updated_at": "2026-08-20T10:03:00Z",
        "body": "**Claude finished task** — [View job](https://example.test)",
    }
    review = {
        "id": 2,
        "user": {"login": "claude[bot]"},
        "submitted_at": "2026-08-20T10:02:00Z",
        "body": "### Review complete\nFindings posted inline.",
    }

    assert (
        _latest_bot_summary_item(
            {"issue_comments": [tracker], "reviews": [review]}, "claude"
        )
        == review
    )


def test_latest_bot_summary_item_keeps_tracker_with_non_summary_review_content():
    tracker_with_review = {
        "id": 1,
        "user": {"login": "claude[bot]"},
        "created_at": "2026-08-20T10:00:00Z",
        "updated_at": "2026-08-20T10:03:00Z",
        "body": (
            "**Claude finished task** — [View job](https://example.test)\n\n"
            "---\n### Findings\nNo blocking issues."
        ),
    }
    older_review = {
        "id": 2,
        "user": {"login": "claude[bot]"},
        "submitted_at": "2026-08-20T10:02:00Z",
        "body": "### Review complete\nOlder review.",
    }

    assert (
        _latest_bot_summary_item(
            {"issue_comments": [tracker_with_review], "reviews": [older_review]},
            "claude",
        )
        == tracker_with_review
    )


def test_build_snapshot():
    data = {
        "issue_comments": [
            {
                "id": 1,
                "user": {"login": "claude[bot]"},
                "updated_at": "2026-08-20T09:00:00Z",
                "body": "Hello",
            },
            {
                "id": 2,
                "user": {"login": "human"},
                "body": "Skip me",
            },
        ],
        "reviews": [
            {
                "id": 10,
                "user": {"login": "claude[bot]"},
                "submitted_at": "2026-08-20T09:05:00Z",
                "body": "Review body",
            }
        ],
        "inline_comments": [
            {
                "id": 100,
                "user": {"login": "claude[bot]"},
                "created_at": "2026-08-20T09:05:00Z",
                "body": "Inline",
            }
        ],
    }
    snapshot = _build_snapshot(data, "claude")
    assert snapshot == {
        "comment_1": "2026-08-20T09:00:00Z:5",
        "review_10": "2026-08-20T09:05:00Z:11",
        "inline_100": "2026-08-20T09:05:00Z",
    }


@patch("scripts.wait_for_review.subprocess.run")
def test_post_review_trigger_default(mock_run):
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = json.dumps(
        {
            "id": 12345,
            "created_at": "2026-08-20T07:44:44Z",
            "body": "@claude review",
        }
    )

    result = post_review_trigger(pr_number=540, bot_name="claude")
    assert result["id"] == 12345
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert "gh" in cmd[0]
    assert cmd[-1].startswith("body=@claude review")
    assert "<!-- orchestune:review-trigger bot=claude -->" in cmd[-1]


@patch("scripts.wait_for_review.subprocess.run")
def test_post_review_trigger_marks_custom_bodies_for_no_post_detection(mock_run):
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = json.dumps({"id": 12347})

    post_review_trigger(pr_number=540, bot_name="claude", body="Please check this")

    cmd = mock_run.call_args[0][0]
    assert "<!-- orchestune:review-trigger bot=claude -->" in cmd[-1]


@patch("scripts.wait_for_review.subprocess.run")
def test_post_review_trigger_custom_body(mock_run):
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = json.dumps(
        {
            "id": 12346,
            "created_at": "2026-08-20T08:00:00Z",
            "body": "## レビュー指摘への対応\n@claude review",
        }
    )

    body_text = "## レビュー指摘への対応\n@claude review"
    result = post_review_trigger(pr_number=540, bot_name="claude", body=body_text)
    assert result["id"] == 12346
    cmd = mock_run.call_args[0][0]
    assert cmd[-1].startswith(f"body={body_text}")
    assert "<!-- orchestune:review-trigger bot=claude -->" in cmd[-1]


def test_post_review_trigger_with_body_file(tmp_path):
    body_file = tmp_path / "reply.md"
    body_file.write_text("## Reply content\n@claude review", encoding="utf-8")

    with patch("scripts.wait_for_review.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = json.dumps(
            {"id": 999, "created_at": "2026-08-20T00:00:00Z"}
        )

        result = post_review_trigger(
            pr_number=540, bot_name="claude", body_file=str(body_file)
        )
        assert result["id"] == 999
        cmd = mock_run.call_args[0][0]
        assert cmd[-1].startswith("body=## Reply content\n@claude review")
        assert "<!-- orchestune:review-trigger bot=claude -->" in cmd[-1]


def test_post_review_trigger_failure():
    with patch("scripts.wait_for_review.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        mock_run.return_value.stderr = "Not authorized"

        with pytest.raises(RuntimeError, match="gh command failed: Not authorized"):
            post_review_trigger(pr_number=540, bot_name="claude")


@patch("scripts.wait_for_review.subprocess.run")
def test_run_gh_api_handles_paginated_slurp_response(mock_run):
    from scripts.wait_for_review import _run_gh_api

    page1 = [{"id": 1}, {"id": 2}]
    page2 = [{"id": 3}, {"id": 4}]
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = json.dumps([page1, page2])

    result = _run_gh_api("dummy/endpoint")
    assert len(result) == 4
    assert [c["id"] for c in result] == [1, 2, 3, 4]


@patch("scripts.wait_for_review.subprocess.run")
def test_run_gh_api_handles_concatenated_paginated_response(mock_run):
    from scripts.wait_for_review import _run_gh_api

    page1 = [{"id": 1}, {"id": 2}]
    page2 = [{"id": 3}, {"id": 4}]
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = json.dumps(page1) + "\n" + json.dumps(page2)

    result = _run_gh_api("dummy/endpoint")
    assert len(result) == 4
    assert [c["id"] for c in result] == [1, 2, 3, 4]


@patch("scripts.wait_for_review.subprocess.run")
def test_run_gh_api_handles_empty_stdout(mock_run):
    from scripts.wait_for_review import _run_gh_api

    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = "   \n"

    result = _run_gh_api("dummy/endpoint")
    assert result == []


def test_run_gh_api_failure():
    from scripts.wait_for_review import _run_gh_api

    with patch("scripts.wait_for_review.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        mock_run.return_value.stderr = "404 Not Found"

        with pytest.raises(RuntimeError, match="gh command failed: 404 Not Found"):
            _run_gh_api("dummy")


def test_run_gh_api_single_dict():
    from scripts.wait_for_review import _run_gh_api

    with patch("scripts.wait_for_review.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = json.dumps({"id": 42})

        result = _run_gh_api("dummy")
        assert result == [{"id": 42}]


def test_run_gh_converts_subprocess_timeout_to_bounded_error():
    with patch(
        "scripts.wait_for_review.subprocess.run",
        side_effect=subprocess.TimeoutExpired(["gh", "api"], timeout=30),
    ) as mock_run:
        with pytest.raises(RuntimeError, match="timed out after 30s"):
            _run_gh(["api", "dummy"])

    assert mock_run.call_args.kwargs["timeout"] == 30


@patch("scripts.wait_for_review._get_pr_data")
@patch("scripts.wait_for_review.post_review_trigger")
def test_wait_for_review_detects_new_comment(mock_post, mock_get_data):
    mock_post.return_value = {
        "id": 100,
        "created_at": "2026-08-20T07:44:44Z",
        "body": "@claude review",
    }
    # First call: initial state (empty)
    # Second call: new comment from claude
    mock_get_data.side_effect = [
        {"issue_comments": [], "reviews": [], "inline_comments": []},
        {
            "issue_comments": [
                {
                    "id": 101,
                    "user": {"login": "claude[bot]"},
                    "created_at": "2026-08-20T07:45:00Z",
                    "body": "### Review complete\nLooks good!",
                }
            ],
            "reviews": [],
            "inline_comments": [],
        },
    ]

    result = wait_for_review(
        pr_number=540,
        timeout=10,
        interval=0,
        bot_name="claude",
        post_trigger=True,
    )
    assert "### Review complete" in result["review_body"]
    assert result["timestamp"] == "2026-08-20T07:45:00Z"


@patch("scripts.wait_for_review._get_pr_data")
def test_wait_for_review_detects_updated_comment_inplace(mock_get_data):
    # Initial state: in-progress comment
    initial_comment = {
        "id": 101,
        "user": {"login": "claude[bot]"},
        "created_at": "2026-08-20T07:45:00Z",
        "updated_at": "2026-08-20T07:45:00Z",
        "body": "### Review in progress\n- [ ] Working...",
    }
    # Updated state: completed comment
    updated_comment = {
        "id": 101,
        "user": {"login": "claude[bot]"},
        "created_at": "2026-08-20T07:45:00Z",
        "updated_at": "2026-08-20T07:48:00Z",
        "body": "### Review complete\nAll checks passed.",
    }

    mock_get_data.side_effect = [
        {"issue_comments": [initial_comment], "reviews": [], "inline_comments": []},
        {"issue_comments": [updated_comment], "reviews": [], "inline_comments": []},
    ]

    result = wait_for_review(
        pr_number=540,
        timeout=10,
        interval=0,
        bot_name="claude",
        post_trigger=False,
    )
    assert "### Review complete" in result["review_body"]
    assert result["timestamp"] == "2026-08-20T07:48:00Z"


@patch("scripts.wait_for_review._get_pr_data")
@patch("scripts.wait_for_review.post_review_trigger")
def test_wait_for_review_keeps_waiting_past_in_progress_activity(
    mock_post, mock_get_data
):
    mock_post.return_value = {
        "id": 100,
        "created_at": "2026-08-20T07:44:44Z",
        "body": "@claude review",
    }
    in_progress = {
        "id": 101,
        "user": {"login": "claude[bot]"},
        "created_at": "2026-08-20T07:45:00Z",
        "updated_at": "2026-08-20T07:45:00Z",
        "body": "Claude is working… <img src='spinner.gif' />",
    }
    completed = {
        **in_progress,
        "updated_at": "2026-08-20T07:48:00Z",
        "body": "### Review complete\nAll checks passed.",
    }
    mock_get_data.side_effect = [
        {"issue_comments": [], "reviews": [], "inline_comments": []},
        {"issue_comments": [in_progress], "reviews": [], "inline_comments": []},
        {"issue_comments": [completed], "reviews": [], "inline_comments": []},
    ]

    result = wait_for_review(
        pr_number=540,
        timeout=10,
        interval=0,
        bot_name="claude",
        post_trigger=True,
    )

    assert "### Review complete" in result["review_body"]
    assert result["timestamp"] == "2026-08-20T07:48:00Z"


@patch("scripts.wait_for_review._get_pr_data")
@patch("scripts.wait_for_review.post_review_trigger")
def test_wait_for_review_does_not_return_old_summary_after_new_inline_activity(
    mock_post, mock_get_data
):
    mock_post.return_value = {
        "id": 100,
        "created_at": "2026-08-20T07:44:44Z",
        "body": "@claude review",
    }
    old_summary = {
        "id": 99,
        "user": {"login": "claude[bot]"},
        "created_at": "2026-08-20T07:40:00Z",
        "updated_at": "2026-08-20T07:40:00Z",
        "body": "### Previous review complete",
    }
    new_summary = {
        "id": 102,
        "user": {"login": "claude[bot]"},
        "created_at": "2026-08-20T07:46:00Z",
        "body": "### Review complete\nCurrent round result.",
    }
    mock_get_data.side_effect = [
        {"issue_comments": [old_summary], "reviews": [], "inline_comments": []},
        {
            "issue_comments": [old_summary],
            "reviews": [],
            "inline_comments": [
                {
                    "id": 101,
                    "user": {"login": "claude[bot]"},
                    "created_at": "2026-08-20T07:45:00Z",
                    "body": "New inline finding",
                }
            ],
        },
        {
            "issue_comments": [old_summary, new_summary],
            "reviews": [],
            "inline_comments": [],
        },
    ]

    result = wait_for_review(
        pr_number=540,
        timeout=10,
        interval=0,
        bot_name="claude",
        post_trigger=True,
    )

    assert result["review_body"] == "### Review complete\nCurrent round result."


@patch("scripts.wait_for_review._get_pr_data")
def test_wait_for_review_no_post_returns_completed_reply_after_latest_trigger(
    mock_get_data,
):
    completed_data = {
        "issue_comments": [
            {
                "id": 100,
                "user": {"login": "human"},
                "created_at": "2026-08-20T07:44:44Z",
                "updated_at": "2026-08-20T07:44:44Z",
                "body": "## Review response\n@claude review",
            },
            {
                "id": 101,
                "user": {"login": "claude[bot]"},
                "created_at": "2026-08-20T07:45:00Z",
                "updated_at": "2026-08-20T07:48:00Z",
                "body": "### Review complete\nAll checks passed.",
            },
        ],
        "reviews": [],
        "inline_comments": [],
    }
    mock_get_data.return_value = completed_data

    result = wait_for_review(
        pr_number=540,
        timeout=0,
        interval=0,
        bot_name="claude",
        post_trigger=False,
    )

    assert "### Review complete" in result["review_body"]
    assert result["timestamp"] == "2026-08-20T07:48:00Z"


@patch("scripts.wait_for_review._get_pr_data")
def test_wait_for_review_no_post_returns_reply_created_in_trigger_second(mock_get_data):
    completed_data = {
        "issue_comments": [
            {
                "id": 100,
                "user": {"login": "human"},
                "created_at": "2026-08-20T07:44:44Z",
                "body": "@claude review",
            },
            {
                "id": 101,
                "user": {"login": "claude[bot]"},
                "created_at": "2026-08-20T07:44:44Z",
                "body": "### Review complete\nAll checks passed.",
            },
        ],
        "reviews": [],
        "inline_comments": [],
    }
    mock_get_data.return_value = completed_data

    result = wait_for_review(
        pr_number=540,
        timeout=0,
        interval=0,
        bot_name="claude",
        post_trigger=False,
    )

    assert "### Review complete" in result["review_body"]


@patch("scripts.wait_for_review._get_pr_data")
def test_wait_for_review_no_post_does_not_return_reply_older_than_latest_trigger(
    mock_get_data,
):
    stale_data = {
        "issue_comments": [
            {
                "id": 99,
                "user": {"login": "claude[bot]"},
                "created_at": "2026-08-20T07:40:00Z",
                "updated_at": "2026-08-20T07:40:00Z",
                "body": "### Previous review complete\nAll checks passed.",
            },
            {
                "id": 100,
                "user": {"login": "human"},
                "created_at": "2026-08-20T07:44:44Z",
                "updated_at": "2026-08-20T07:44:44Z",
                "body": "@claude review",
            },
        ],
        "reviews": [],
        "inline_comments": [],
    }
    mock_get_data.return_value = stale_data

    with pytest.raises(TimeoutError):
        wait_for_review(
            pr_number=540,
            timeout=0,
            interval=0,
            bot_name="claude",
            post_trigger=False,
        )


@patch("scripts.wait_for_review._get_pr_data")
def test_wait_for_review_no_post_does_not_use_late_edit_of_old_reply(
    mock_get_data,
):
    stale_data = {
        "issue_comments": [
            {
                "id": 99,
                "user": {"login": "claude[bot]"},
                "created_at": "2026-08-20T07:40:00Z",
                "updated_at": "2026-08-20T07:50:00Z",
                "body": "### Previous review complete\nAll checks passed.",
            },
            {
                "id": 100,
                "user": {"login": "human"},
                "created_at": "2026-08-20T07:44:44Z",
                "body": "@claude review",
            },
        ],
        "reviews": [],
        "inline_comments": [],
    }
    mock_get_data.return_value = stale_data

    with pytest.raises(TimeoutError):
        wait_for_review(
            pr_number=540,
            timeout=0,
            interval=0,
            bot_name="claude",
            post_trigger=False,
        )


@patch("scripts.wait_for_review._get_pr_data")
@patch("scripts.wait_for_review.post_review_trigger")
def test_wait_for_review_detects_codex_review_and_inlines(mock_post, mock_get_data):
    mock_post.return_value = {"id": 200, "created_at": "2026-08-18T07:00:00Z"}
    mock_get_data.side_effect = [
        {"issue_comments": [], "reviews": [], "inline_comments": []},
        {
            "issue_comments": [],
            "reviews": [
                {
                    "id": 201,
                    "user": {"login": "chatgpt-codex-connector[bot]"},
                    "submitted_at": "2026-08-18T07:01:31Z",
                    "body": "### 💡 Codex Review",
                }
            ],
            "inline_comments": [
                {
                    "id": 202,
                    "user": {"login": "chatgpt-codex-connector[bot]"},
                    "created_at": "2026-08-18T07:01:31Z",
                    "path": "test.py",
                    "line": 10,
                    "body": "Fix this line",
                }
            ],
        },
    ]

    result = wait_for_review(
        pr_number=510,
        timeout=10,
        interval=0,
        bot_name="codex",
        post_trigger=True,
    )
    assert "💡 Codex Review" in result["review_body"]
    assert len(result["inline_comments"]) == 1
    assert result["inline_comments"][0]["path"] == "test.py"


@patch("scripts.wait_for_review._get_pr_data")
@patch("scripts.wait_for_review.post_review_trigger")
def test_wait_for_review_times_out(mock_post, mock_get_data):
    mock_post.return_value = {"id": 300, "created_at": "2026-08-20T07:00:00Z"}
    mock_get_data.return_value = {
        "issue_comments": [],
        "reviews": [],
        "inline_comments": [],
    }
    with pytest.raises(TimeoutError):
        wait_for_review(
            pr_number=540,
            timeout=0,
            interval=1,
            bot_name="claude",
            post_trigger=True,
        )


def test_wait_for_review_polling_catches_exception_and_continues():
    with patch("scripts.wait_for_review._get_pr_data") as mock_get_data:
        mock_get_data.side_effect = [
            {"issue_comments": [], "reviews": [], "inline_comments": []},
            RuntimeError("Network hiccup"),
            {
                "issue_comments": [
                    {
                        "id": 1,
                        "user": {"login": "claude[bot]"},
                        "created_at": "2026-08-20T00:01:00Z",
                        "body": "### Review complete\nLGTM!",
                    }
                ],
                "reviews": [],
                "inline_comments": [],
            },
        ]

        result = wait_for_review(
            pr_number=540,
            timeout=10,
            interval=0,
            bot_name="claude",
            post_trigger=False,
        )
        assert "LGTM!" in result["review_body"]


def test_wait_for_review_retries_initial_fetch_failure():
    completed_data = {
        "issue_comments": [
            {
                "id": 100,
                "user": {"login": "human"},
                "created_at": "2026-08-20T07:44:44Z",
                "body": "@claude review",
            },
            {
                "id": 101,
                "user": {"login": "claude[bot]"},
                "created_at": "2026-08-20T07:45:00Z",
                "body": "### Review complete\nLGTM!",
            },
        ],
        "reviews": [],
        "inline_comments": [],
    }

    with patch("scripts.wait_for_review._get_pr_data") as mock_get_data:
        mock_get_data.side_effect = [RuntimeError("Network hiccup"), completed_data]

        result = wait_for_review(
            pr_number=540,
            timeout=10,
            interval=0,
            bot_name="claude",
            post_trigger=False,
        )

    assert "LGTM!" in result["review_body"]


def test_get_pr_data_does_not_return_partial_data_when_an_endpoint_fails():
    from scripts.wait_for_review import _get_pr_data

    with patch("scripts.wait_for_review._run_gh_api") as mock_api:

        def side_effect(endpoint, *args):
            if "issues" in endpoint:
                return [{"id": 1}]
            raise RuntimeError("API disabled")

        mock_api.side_effect = side_effect
        with pytest.raises(RuntimeError, match="API disabled"):
            _get_pr_data(540)


def test_main_cli_success():
    from scripts.wait_for_review import main

    with patch("sys.argv", ["wait_for_review.py", "--pr", "540", "--no-post"]):
        with patch("scripts.wait_for_review.wait_for_review") as mock_wait:
            mock_wait.return_value = {"review_body": "LGTM"}
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 0


def test_main_cli_timeout():
    from scripts.wait_for_review import main

    with patch("sys.argv", ["wait_for_review.py", "--pr", "540", "--no-post"]):
        with patch(
            "scripts.wait_for_review.wait_for_review",
            side_effect=TimeoutError("Timed out"),
        ):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 1


def test_main_cli_unexpected_error():
    from scripts.wait_for_review import main

    with patch("sys.argv", ["wait_for_review.py", "--pr", "540", "--no-post"]):
        with patch(
            "scripts.wait_for_review.wait_for_review", side_effect=ValueError("Boom")
        ):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 2


@patch("scripts.wait_for_review._get_pr_data")
@patch("scripts.wait_for_review.post_review_trigger")
def test_wait_for_review_does_not_self_trigger_if_poster_is_bot(
    mock_post, mock_get_data
):
    # If the user running this script is claude[bot], its own posted trigger comment (id: 100)
    # should NOT be returned as the completed review on the first poll.
    mock_post.return_value = {
        "id": 100,
        "created_at": "2026-08-20T07:44:44Z",
        "body": "@claude review",
        "user": {"login": "claude[bot]"},
    }

    # First poll returns only the trigger comment.
    # Second poll returns the real review comment from the reviewer (id: 101).
    mock_get_data.side_effect = [
        {"issue_comments": [], "reviews": [], "inline_comments": []},
        {
            "issue_comments": [
                {
                    "id": 100,
                    "user": {"login": "claude[bot]"},
                    "created_at": "2026-08-20T07:44:44Z",
                    "body": "@claude review",
                }
            ],
            "reviews": [],
            "inline_comments": [],
        },
        {
            "issue_comments": [
                {
                    "id": 100,
                    "user": {"login": "claude[bot]"},
                    "created_at": "2026-08-20T07:44:44Z",
                    "body": "@claude review",
                },
                {
                    "id": 101,
                    "user": {"login": "claude[bot]"},
                    "created_at": "2026-08-20T07:46:00Z",
                    "body": "### Review complete\nAll clear!",
                },
            ],
            "reviews": [],
            "inline_comments": [],
        },
    ]

    result = wait_for_review(
        pr_number=540,
        timeout=10,
        interval=0,
        bot_name="claude",
        post_trigger=True,
    )
    # Must pick the real review (id: 101), not the trigger comment (id: 100)
    assert "### Review complete" in result["review_body"]
    assert result["timestamp"] == "2026-08-20T07:46:00Z"

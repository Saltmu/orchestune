from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from scripts.wait_for_review import (
    GH_SUBPROCESS_TIMEOUT_SECONDS,
    _build_snapshot,
    _extract_review_result,
    _filter_bot_items,
    _find_latest_trigger_comment,
    _get_item_timestamp,
    _is_bot_user,
    _is_in_progress_body,
    _latest_post_trigger_bot_item,
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
    assert "body=@claude review" in cmd


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
    assert f"body={body_text}" in cmd


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
        assert "body=## Reply content\n@claude review" in cmd


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


def test_get_pr_data_endpoint_fallbacks():
    from scripts.wait_for_review import _get_pr_data

    with patch("scripts.wait_for_review._run_gh_api") as mock_api:

        def side_effect(endpoint, *args):
            if "issues" in endpoint:
                return [{"id": 1}]
            raise RuntimeError("API disabled")

        mock_api.side_effect = side_effect
        data = _get_pr_data(540)
        assert len(data["issue_comments"]) == 1
        assert data["reviews"] == []
        assert data["inline_comments"] == []


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


def test_is_in_progress_body():
    assert _is_in_progress_body("### Review in progress\n- [ ] Working...") is True
    assert _is_in_progress_body("- [ ] still open item") is True
    assert (
        _is_in_progress_body(
            '<img src="https://.../5ac382c7-e004-429b-8e35-7feb3e8f9c6f" />'
        )
        is True
    )
    assert _is_in_progress_body("進行中の対応です") is True
    assert _is_in_progress_body("### Review complete\nAll checks passed.") is False
    assert _is_in_progress_body("") is False


def test_find_latest_trigger_comment():
    items = [
        {"id": 1, "created_at": "2026-08-20T07:00:00Z", "body": "unrelated comment"},
        {"id": 2, "created_at": "2026-08-20T07:10:00Z", "body": "@claude review"},
        {"id": 3, "created_at": "2026-08-20T07:20:00Z", "body": "## reply\n@claude review"},
    ]
    result = _find_latest_trigger_comment(items, "claude")
    assert result is not None
    assert result["id"] == 3
    assert _find_latest_trigger_comment(items, "codex") is None
    assert _find_latest_trigger_comment([], "claude") is None


def test_latest_post_trigger_bot_item():
    data = {
        "issue_comments": [
            {
                "id": 1,
                "user": {"login": "claude[bot]"},
                "created_at": "2026-08-20T07:00:00Z",
                "body": "before trigger",
            },
            {
                "id": 2,
                "user": {"login": "claude[bot]"},
                "created_at": "2026-08-20T07:20:00Z",
                "body": "after trigger",
            },
        ],
        "reviews": [],
        "inline_comments": [],
    }
    result = _latest_post_trigger_bot_item(
        data, "claude", trigger_time="2026-08-20T07:10:00Z"
    )
    assert result is not None
    assert result["id"] == 2
    assert (
        _latest_post_trigger_bot_item(
            data, "claude", trigger_time="2026-08-20T09:00:00Z"
        )
        is None
    )


@patch("scripts.wait_for_review._get_pr_data")
def test_wait_for_review_no_post_returns_lost_update_completed_response(
    mock_get_data,
):
    """Regression test for Issue #564: a bot response that finished
    between a previous invocation and this --no-post resume must be
    detected immediately instead of waiting for a further change."""
    trigger_comment = {
        "id": 500,
        "user": {"login": "human_dev"},
        "created_at": "2026-08-21T12:40:00Z",
        "body": "@claude review",
    }
    completed_comment = {
        "id": 501,
        "user": {"login": "claude[bot]"},
        "created_at": "2026-08-21T12:49:08Z",
        "updated_at": "2026-08-21T12:52:26Z",
        "body": "### Review complete\nAll checks passed.",
    }
    mock_get_data.return_value = {
        "issue_comments": [trigger_comment, completed_comment],
        "reviews": [],
        "inline_comments": [],
    }

    result = wait_for_review(
        pr_number=562,
        timeout=10,
        interval=0,
        bot_name="claude",
        post_trigger=False,
    )
    assert "### Review complete" in result["review_body"]
    # Must return on the very first fetch, without polling further.
    assert mock_get_data.call_count == 1


@patch("scripts.wait_for_review._get_pr_data")
def test_wait_for_review_no_post_waits_for_explicit_in_progress_response(
    mock_get_data,
):
    trigger_comment = {
        "id": 500,
        "user": {"login": "human_dev"},
        "created_at": "2026-08-21T12:40:00Z",
        "body": "@claude review",
    }
    in_progress_comment = {
        "id": 501,
        "user": {"login": "claude[bot]"},
        "created_at": "2026-08-21T12:41:00Z",
        "updated_at": "2026-08-21T12:41:00Z",
        "body": "### Review in progress\n- [ ] Working...",
    }
    completed_comment = {
        **in_progress_comment,
        "updated_at": "2026-08-21T12:52:26Z",
        "body": "### Review complete\nAll checks passed.",
    }

    mock_get_data.side_effect = [
        {
            "issue_comments": [trigger_comment, in_progress_comment],
            "reviews": [],
            "inline_comments": [],
        },
        {
            "issue_comments": [trigger_comment, completed_comment],
            "reviews": [],
            "inline_comments": [],
        },
    ]

    result = wait_for_review(
        pr_number=562,
        timeout=10,
        interval=0,
        bot_name="claude",
        post_trigger=False,
    )
    assert "### Review complete" in result["review_body"]
    assert mock_get_data.call_count == 2


@patch("scripts.wait_for_review.subprocess.run")
def test_run_gh_passes_subprocess_timeout(mock_run):
    from scripts.wait_for_review import _run_gh_api

    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = json.dumps({"id": 1})

    _run_gh_api("dummy")
    assert mock_run.call_args.kwargs["timeout"] == GH_SUBPROCESS_TIMEOUT_SECONDS


def test_run_gh_subprocess_hang_raises_runtime_error():
    from scripts.wait_for_review import _run_gh_api

    with patch("scripts.wait_for_review.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["gh", "api", "dummy"], timeout=GH_SUBPROCESS_TIMEOUT_SECONDS
        )
        with pytest.raises(RuntimeError, match="gh command timed out"):
            _run_gh_api("dummy")

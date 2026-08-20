from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from scripts.wait_for_review import (
    analyze_review_findings,
    is_review_completed_comment,
    post_review_trigger,
    wait_for_review,
)


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


def test_is_review_completed_comment_completed_claude():
    comment = {
        "user": {"login": "claude[bot]"},
        "body": "### Re-review complete\n\n- [x] All checks pass.\n\nI have no further blocking findings.",
    }
    assert is_review_completed_comment(comment, "claude") is True


def test_is_review_completed_comment_completed_codex():
    comment = {
        "user": {"login": "chatgpt-codex-connector[bot]"},
        "body": "### 💡 Codex Review\n\nHere are some automated review suggestions for this pull request.",
    }
    assert is_review_completed_comment(comment, "codex") is True


def test_analyze_review_findings_claude_with_findings():
    body = (
        "**Claude finished @Saltmu's task in 1m 4s** —— [View job](...)\n\n"
        "- [x] Gather context\n"
        "- [x] Post findings\n\n"
        "### Findings\n\n"
        "**🔴 Still open — Bug: first-run frontmatter-only adoption of a raw, never-touched issue fails**\n\n"
        "`orchestune/provisioning.py:531-550`\n"
    )
    result = analyze_review_findings(review_body=body, inline_comments=[])
    assert result["has_findings"] is True
    assert result["status"] == "FINDINGS_DETECTED"
    assert "🔴 Still open — Bug" in result["summary"]


def test_analyze_review_findings_codex_with_inline_findings():
    review_body = "### 💡 Codex Review\n\nHere are some automated review suggestions for this pull request."
    inline_comments = [
        {
            "path": "docs/en/architecture.md",
            "line": 42,
            "body": "<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow?style=flat)</sub></sub> Scope bounded-loop claims to persistent run state\n\nDetails...",
        },
        {
            "path": "docs/en/architecture.md",
            "line": 152,
            "body": "<sub><sub>![P3 Badge](https://img.shields.io/badge/P3-lightgrey?style=flat)</sub></sub> Count all three non-terminal gaps\n\nDetails...",
        },
    ]
    result = analyze_review_findings(
        review_body=review_body, inline_comments=inline_comments
    )
    assert result["has_findings"] is True
    assert result["status"] == "FINDINGS_DETECTED"
    assert len(result["inline_findings"]) == 2
    assert "P2" in result["summary"]


def test_analyze_review_findings_all_clear_lgtm():
    body = (
        "**Claude finished @Saltmu's task in 1m 4s**\n\n"
        "### Summary\n\n"
        "All checks passed. LGTM! I have no further blocking findings."
    )
    result = analyze_review_findings(review_body=body, inline_comments=[])
    assert result["has_findings"] is False
    assert result["status"] == "ALL_CLEAR"


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
            "body": "## レビュー指摘への対応 (Round 1/5)\n- Fixed bug\n\n@claude review",
        }
    )

    body_text = "## レビュー指摘への対応 (Round 1/5)\n- Fixed bug\n\n@claude review"
    result = post_review_trigger(pr_number=540, bot_name="claude", body=body_text)
    assert result["id"] == 12346
    cmd = mock_run.call_args[0][0]
    assert f"body={body_text}" in cmd


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


@patch("scripts.wait_for_review._get_pr_data")
@patch("scripts.wait_for_review.post_review_trigger")
@patch("time.sleep")
def test_wait_for_review_detects_already_present_review_after_post(
    mock_sleep, mock_post, mock_get_data
):
    # Reproducer test: Even if the bot review was already posted immediately after the trigger,
    # wait_for_review should catch it right away based on the trigger timestamp.
    mock_post.return_value = {
        "id": 100,
        "created_at": "2026-08-20T07:44:44Z",
        "body": "@claude review",
    }
    mock_get_data.return_value = {
        "issue_comments": [
            {
                "id": 101,
                "user": {"login": "claude[bot]"},
                "created_at": "2026-08-20T07:44:58Z",
                "body": "### Re-review complete\n- [x] Task\n### Findings\n🔴 Still open — Bug: xyz",
            }
        ],
        "reviews": [],
        "inline_comments": [],
    }

    result = wait_for_review(
        pr_number=540,
        timeout=30,
        interval=1,
        bot_name="claude",
        post_trigger=True,
    )
    assert result["has_findings"] is True
    assert result["status"] == "FINDINGS_DETECTED"
    assert mock_sleep.call_count == 0


@patch("scripts.wait_for_review._get_pr_data")
@patch("scripts.wait_for_review.post_review_trigger")
@patch("time.sleep")
def test_wait_for_review_codex_with_inline_comments(
    mock_sleep, mock_post, mock_get_data
):
    mock_post.return_value = {
        "id": 200,
        "created_at": "2026-08-18T07:00:00Z",
        "body": "@codex review",
    }
    mock_get_data.return_value = {
        "issue_comments": [],
        "reviews": [
            {
                "id": 201,
                "user": {"login": "chatgpt-codex-connector[bot]"},
                "submitted_at": "2026-08-18T07:01:31Z",
                "body": "### 💡 Codex Review\n\nHere are some automated review suggestions.",
            }
        ],
        "inline_comments": [
            {
                "id": 202,
                "user": {"login": "chatgpt-codex-connector[bot]"},
                "created_at": "2026-08-18T07:01:31Z",
                "path": "docs/en/architecture.md",
                "line": 10,
                "body": "<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow?style=flat)</sub></sub> Fix statement",
            }
        ],
    }

    result = wait_for_review(
        pr_number=510,
        timeout=30,
        interval=1,
        bot_name="codex",
        post_trigger=True,
    )
    assert result["has_findings"] is True
    assert len(result["inline_findings"]) == 1


@patch("scripts.wait_for_review._get_pr_data")
@patch("scripts.wait_for_review.post_review_trigger")
@patch("time.sleep")
def test_wait_for_review_times_out(mock_sleep, mock_post, mock_get_data):
    mock_post.return_value = {
        "id": 300,
        "created_at": "2026-08-20T07:00:00Z",
        "body": "@claude review",
    }
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

        with pytest.raises(RuntimeError, match="Failed to post review comment"):
            post_review_trigger(pr_number=540, bot_name="claude")


def test_run_gh_api_failure():
    from scripts.wait_for_review import _run_gh_api

    with patch("scripts.wait_for_review.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        mock_run.return_value.stderr = "404 Not Found"

        with pytest.raises(RuntimeError, match="gh api dummy failed"):
            _run_gh_api("dummy")


def test_run_gh_api_single_dict():
    from scripts.wait_for_review import _run_gh_api

    with patch("scripts.wait_for_review.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = json.dumps({"id": 42})

        result = _run_gh_api("dummy")
        assert result == [{"id": 42}]


def test_wait_for_review_no_post_finds_recent_trigger(tmp_path):
    with patch("scripts.wait_for_review._get_pr_data") as mock_get_data:
        mock_get_data.return_value = {
            "issue_comments": [
                {
                    "id": 1,
                    "user": {"login": "human"},
                    "created_at": "2026-08-20T07:00:00Z",
                    "body": "@claude review",
                },
                {
                    "id": 2,
                    "user": {"login": "claude[bot]"},
                    "created_at": "2026-08-20T07:01:00Z",
                    "body": "### Review complete\nLGTM! All checks passed.",
                },
            ],
            "reviews": [],
            "inline_comments": [],
        }

        result = wait_for_review(
            pr_number=540, timeout=10, bot_name="claude", post_trigger=False
        )
        assert result["status"] == "ALL_CLEAR"


def test_main_cli_success():
    from scripts.wait_for_review import main

    with patch("sys.argv", ["wait_for_review.py", "--pr", "540", "--no-post"]):
        with patch("scripts.wait_for_review.wait_for_review") as mock_wait:
            mock_wait.return_value = {"status": "ALL_CLEAR"}
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


def test_is_bot_user_generic():
    from scripts.wait_for_review import _is_bot_user

    assert _is_bot_user("my-custom-bot[bot]", "my-custom-bot") is True
    assert _is_bot_user("other-user", "my-custom-bot") is False


def test_analyze_review_findings_fallback_red_circle_lines():
    body = "Review outcome:\n🔴 Critical syntax error in test.py\nNote: not blocking"
    result = analyze_review_findings(review_body=body, inline_comments=[])
    assert result["has_findings"] is True
    assert "🔴 Critical syntax error" in result["summary"]


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


def test_wait_for_review_polling_catches_exception_and_continues():
    with patch("scripts.wait_for_review._get_pr_data") as mock_get_data:
        # First call raises Exception, second call returns success review
        mock_get_data.side_effect = [
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
        assert result["status"] == "ALL_CLEAR"

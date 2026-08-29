from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from scripts.wait_for_review import (
    _ensure_review_trigger_mention,
    _find_existing_trigger_comment,
    _handle_review_trigger,
    _has_review_trigger_mention,
    _resolve_current_round,
    post_review_trigger,
)


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


def test_post_review_trigger_with_body_file_prepends_mention_when_missing(tmp_path):
    body_file = tmp_path / "reply.md"
    body_file.write_text("## Review Round 2/5\n- [Addressed] Fix bug", encoding="utf-8")

    with patch("scripts.wait_for_review.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = json.dumps(
            {"id": 1000, "created_at": "2026-08-20T00:00:00Z"}
        )

        result = post_review_trigger(
            pr_number=540, bot_name="codex", body_file=str(body_file), round_num=2
        )
        assert result["id"] == 1000
        cmd = mock_run.call_args[0][0]
        expected_prefix = (
            "body=@codex review\n\n## Review Round 2/5\n- [Addressed] Fix bug"
        )
        assert cmd[-1].startswith(expected_prefix)
        assert "<!-- orchestune:review-trigger bot=codex -->" in cmd[-1]
        assert "<!-- orchestune:review-round 2 -->" in cmd[-1]


def test_post_review_trigger_with_body_string_prepends_mention_when_missing():
    with patch("scripts.wait_for_review.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = json.dumps(
            {"id": 1001, "created_at": "2026-08-20T00:00:00Z"}
        )

        result = post_review_trigger(
            pr_number=540,
            bot_name="claude",
            body="Please check this update",
            round_num=3,
        )
        assert result["id"] == 1001
        cmd = mock_run.call_args[0][0]
        expected_prefix = "body=@claude review\n\nPlease check this update"
        assert cmd[-1].startswith(expected_prefix)
        assert "<!-- orchestune:review-trigger bot=claude -->" in cmd[-1]
        assert "<!-- orchestune:review-round 3 -->" in cmd[-1]


def test_has_review_trigger_mention():
    assert _has_review_trigger_mention("@claude review", "claude") is True
    assert _has_review_trigger_mention("@Claude Review", "claude") is True
    assert _has_review_trigger_mention("@codex   review", "codex") is True
    assert _has_review_trigger_mention("@codex, review", "codex") is True
    assert _has_review_trigger_mention("@codex: review", "codex") is True
    assert _has_review_trigger_mention("Prefix\n@codex review\nSuffix", "codex") is True
    assert (
        _has_review_trigger_mention(
            "## Review changes\n- Addressed @codex feedback", "codex"
        )
        is False
    )
    # Claude matches flexible workflow mention anywhere with review
    assert (
        _has_review_trigger_mention(
            "## Review changes\n- Addressed @claude feedback", "claude"
        )
        is True
    )
    assert _has_review_trigger_mention("@claude", "claude") is False
    assert _has_review_trigger_mention("Just a general review", "claude") is False
    assert (
        _has_review_trigger_mention(
            "<!-- orchestune:review-trigger bot=claude -->", "claude"
        )
        is False
    )
    assert _has_review_trigger_mention("", "claude") is False


def test_find_existing_trigger_comment_prefers_comment_with_mention():
    data = {
        "issue_comments": [
            {
                "id": 502,
                "created_at": "2026-08-20T10:00:00Z",
                "body": "## Review response\n\n<!-- orchestune:review-trigger bot=codex -->\n<!-- orchestune:review-round 2 -->",
            },
            {
                "id": 503,
                "created_at": "2026-08-20T10:01:00Z",
                "body": "@codex review\n\n## Review response\n\n<!-- orchestune:review-trigger bot=codex -->\n<!-- orchestune:review-round 2 -->",
            },
        ]
    }
    result = _find_existing_trigger_comment(data, "codex", round_num=2)
    assert result is not None
    assert result["id"] == 503


def test_resolve_current_round():
    data_5 = {
        "issue_comments": [
            {
                "id": 505,
                "created_at": "2026-08-20T10:00:00Z",
                "body": "@claude review\n\n<!-- orchestune:review-trigger bot=claude -->\n<!-- orchestune:review-round 5 -->",
            }
        ]
    }
    assert (
        _resolve_current_round(data_5, "claude", round_num=None, post_trigger=True) == 6
    )

    # Explicit round_num override takes precedence
    assert _resolve_current_round(data_5, "claude", round_num=3, post_trigger=True) == 3

    # When post_trigger is False, returns max(1, latest_existing_round)
    assert (
        _resolve_current_round(data_5, "claude", round_num=None, post_trigger=False)
        == 5
    )


def test_ensure_review_trigger_mention():
    assert _ensure_review_trigger_mention("", "claude") == "@claude review"
    assert _ensure_review_trigger_mention("   \n\t", "codex") == "@codex review"
    assert (
        _ensure_review_trigger_mention(
            "## Details\n@claude review\n- Fix bug", "claude"
        )
        == "## Details\n@claude review\n- Fix bug"
    )
    assert (
        _ensure_review_trigger_mention("## Details\n- Fix bug", "claude")
        == "@claude review\n\n## Details\n- Fix bug"
    )


@patch("scripts.wait_for_review.post_review_trigger")
def test_handle_review_trigger_skips_when_existing_trigger_has_mention(mock_post):
    data = {
        "issue_comments": [
            {
                "id": 501,
                "created_at": "2026-08-20T10:00:00Z",
                "body": "@claude review\n\n<!-- orchestune:review-trigger bot=claude -->\n<!-- orchestune:review-round 2 -->",
            }
        ]
    }
    excluded_ids = set()
    initial_snapshot = {}

    timestamp = _handle_review_trigger(
        pr_number=540,
        bot_name="claude",
        initial_data=data,
        initial_snapshot=initial_snapshot,
        excluded_ids=excluded_ids,
        current_round=2,
        max_rounds=5,
        body=None,
        body_file=None,
    )

    assert timestamp == "2026-08-20T10:00:00Z"
    assert 501 in excluded_ids
    mock_post.assert_not_called()


@patch("scripts.wait_for_review.post_review_trigger")
def test_handle_review_trigger_reposts_when_existing_trigger_lacks_mention(mock_post):
    data = {
        "issue_comments": [
            {
                "id": 502,
                "created_at": "2026-08-20T10:00:00Z",
                "body": "## Review response\n\n<!-- orchestune:review-trigger bot=codex -->\n<!-- orchestune:review-round 2 -->",
            }
        ]
    }
    mock_post.return_value = {
        "id": 503,
        "created_at": "2026-08-20T10:01:00Z",
        "body": "@codex review\n\n## Review response\n\n<!-- orchestune:review-trigger bot=codex -->\n<!-- orchestune:review-round 2 -->",
    }
    excluded_ids = set()
    initial_snapshot = {}

    timestamp = _handle_review_trigger(
        pr_number=540,
        bot_name="codex",
        initial_data=data,
        initial_snapshot=initial_snapshot,
        excluded_ids=excluded_ids,
        current_round=2,
        max_rounds=5,
        body="## Review response",
        body_file=None,
    )

    assert timestamp == "2026-08-20T10:01:00Z"
    assert 502 in excluded_ids
    assert 503 in excluded_ids
    mock_post.assert_called_once_with(
        540,
        bot_name="codex",
        body="## Review response",
        body_file=None,
        round_num=2,
    )


def test_post_review_trigger_failure():
    with patch("scripts.wait_for_review.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        mock_run.return_value.stderr = "Not authorized"

        with pytest.raises(RuntimeError, match="gh command failed: Not authorized"):
            post_review_trigger(pr_number=540, bot_name="claude")

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from scripts.wait_for_review import (
    EXIT_FINDINGS_PRESENT,
    EXIT_IN_PROGRESS,
    EXIT_NO_FINDINGS,
    EXIT_UNDETERMINED,
    MaxRoundsExceededError,
    _find_existing_trigger_comment,
    _get_latest_review_round,
    _mark_review_trigger,
    _parse_review_round_marker,
    _review_round_marker,
    evaluate_review_verdict,
    post_review_trigger,
    wait_for_review,
)


def test_main_cli_success():
    from scripts.wait_for_review import main

    with patch("sys.argv", ["wait_for_review.py", "--pr", "540", "--no-post"]):
        with patch("scripts.wait_for_review.wait_for_review") as mock_wait:
            mock_wait.return_value = {"review_body": "LGTM", "inline_comments": []}
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 0


def test_main_cli_findings():
    from scripts.wait_for_review import main

    with patch("sys.argv", ["wait_for_review.py", "--pr", "540", "--no-post"]):
        with patch("scripts.wait_for_review.wait_for_review") as mock_wait:
            mock_wait.return_value = {
                "review_body": "### Findings\n🔴 blocking bug",
                "inline_comments": [],
            }
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 10


def test_main_cli_undetermined():
    from scripts.wait_for_review import main

    with patch("sys.argv", ["wait_for_review.py", "--pr", "540", "--no-post"]):
        with patch("scripts.wait_for_review.wait_for_review") as mock_wait:
            mock_wait.return_value = {
                "review_body": "### Note\nAmbiguous comment",
                "inline_comments": [],
            }
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 30


def test_main_cli_timeout():
    from scripts.wait_for_review import main

    with patch("sys.argv", ["wait_for_review.py", "--pr", "540", "--no-post"]):
        with patch(
            "scripts.wait_for_review.wait_for_review",
            side_effect=TimeoutError("Timed out"),
        ):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 20


def test_main_cli_max_rounds():
    from scripts.wait_for_review import main

    with patch("sys.argv", ["wait_for_review.py", "--pr", "540", "--no-post"]):
        with patch(
            "scripts.wait_for_review.wait_for_review",
            side_effect=MaxRoundsExceededError("Max rounds reached"),
        ):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 12


def test_main_cli_unexpected_error():
    from scripts.wait_for_review import main

    with patch("sys.argv", ["wait_for_review.py", "--pr", "540", "--no-post"]):
        with patch(
            "scripts.wait_for_review.wait_for_review", side_effect=ValueError("Boom")
        ):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 2


def test_main_cli_arguments_parsing():
    from scripts.wait_for_review import main

    with patch(
        "sys.argv",
        [
            "wait_for_review.py",
            "--pr",
            "540",
            "--max-rounds",
            "3",
            "--max-retries",
            "2",
            "--round",
            "2",
        ],
    ):
        with patch("scripts.wait_for_review.wait_for_review") as mock_wait:
            mock_wait.return_value = {"review_body": "LGTM", "inline_comments": []}
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 0
            mock_wait.assert_called_once_with(
                540,
                timeout=300,
                interval=5,
                bot_name="claude",
                post_trigger=True,
                body=None,
                body_file=None,
                max_rounds=3,
                max_retries=2,
                round_num=2,
            )


def test_review_round_marker():
    assert _review_round_marker(1) == "<!-- orchestune:review-round 1 -->"
    assert _review_round_marker(5) == "<!-- orchestune:review-round 5 -->"


def test_parse_review_round_marker():
    assert (
        _parse_review_round_marker(
            "Some text\n<!-- orchestune:review-round 2 -->\nmore"
        )
        == 2
    )
    assert _parse_review_round_marker("<!-- orchestune:review-round 10 -->") == 10
    assert _parse_review_round_marker("No marker here") is None


def test_get_latest_review_round():
    data = {
        "issue_comments": [
            {
                "id": 1,
                "user": {"login": "dev"},
                "body": "Fix bug\n\n<!-- orchestune:review-trigger bot=claude -->\n<!-- orchestune:review-round 1 -->",
            },
            {
                "id": 2,
                "user": {"login": "claude[bot]"},
                "body": "Review comment",
            },
            {
                "id": 3,
                "user": {"login": "dev"},
                "body": "Fix round 2\n\n<!-- orchestune:review-trigger bot=claude -->\n<!-- orchestune:review-round 2 -->",
            },
        ]
    }
    assert _get_latest_review_round(data, "claude") == 2
    assert _get_latest_review_round(data, "codex") == 0

    empty_data = {"issue_comments": []}
    assert _get_latest_review_round(empty_data, "claude") == 0


def test_find_existing_trigger_comment():
    data = {
        "issue_comments": [
            {
                "id": 10,
                "user": {"login": "dev"},
                "created_at": "2026-08-20T10:00:00Z",
                "body": "Round 1 trigger\n\n<!-- orchestune:review-trigger bot=claude -->\n<!-- orchestune:review-round 1 -->",
            },
            {
                "id": 11,
                "user": {"login": "claude[bot]"},
                "created_at": "2026-08-20T10:01:00Z",
                "body": "<!-- orchestune:review-trigger bot=claude -->\n<!-- orchestune:review-round 1 -->",
            },
        ]
    }
    # Should find human comment with round 1
    found = _find_existing_trigger_comment(data, "claude", 1)
    assert found is not None
    assert found["id"] == 10

    # Should not find round 2
    assert _find_existing_trigger_comment(data, "claude", 2) is None
    # Should not find for codex
    assert _find_existing_trigger_comment(data, "codex", 1) is None


def test_mark_review_trigger_with_round():
    body = "@claude review"
    marked = _mark_review_trigger(body, "claude", round_num=1)
    assert "<!-- orchestune:review-trigger bot=claude -->" in marked
    assert "<!-- orchestune:review-round 1 -->" in marked

    # Idempotent: already marked
    re_marked = _mark_review_trigger(marked, "claude", round_num=1)
    assert re_marked.count("<!-- orchestune:review-round 1 -->") == 1
    assert re_marked.count("<!-- orchestune:review-trigger bot=claude -->") == 1


@patch("scripts.wait_for_review.subprocess.run")
def test_post_review_trigger_includes_round_marker(mock_run):
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = json.dumps(
        {
            "id": 12345,
            "created_at": "2026-08-20T07:44:44Z",
            "body": "@claude review",
        }
    )

    result = post_review_trigger(pr_number=540, bot_name="claude", round_num=3)
    assert result["id"] == 12345
    cmd = mock_run.call_args[0][0]
    assert "<!-- orchestune:review-round 3 -->" in cmd[-1]


@patch("scripts.wait_for_review._get_pr_data")
@patch("scripts.wait_for_review.post_review_trigger")
def test_wait_for_review_idempotent_skips_post_if_same_round_exists(
    mock_post, mock_get_data
):
    existing_trigger = {
        "id": 100,
        "user": {"login": "human"},
        "created_at": "2026-08-20T07:44:44Z",
        "body": "@claude review\n\n<!-- orchestune:review-trigger bot=claude -->\n<!-- orchestune:review-round 1 -->",
    }
    completed_review = {
        "id": 101,
        "user": {"login": "claude[bot]"},
        "created_at": "2026-08-20T07:45:00Z",
        "body": "### Review complete\nLooks good!",
    }

    mock_get_data.side_effect = [
        {
            "issue_comments": [existing_trigger],
            "reviews": [],
            "inline_comments": [],
        },
        {
            "issue_comments": [existing_trigger, completed_review],
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
        round_num=1,
    )

    mock_post.assert_not_called()
    assert "### Review complete" in result["review_body"]


@patch("scripts.wait_for_review._get_pr_data")
def test_wait_for_review_max_rounds_exceeded(mock_get_data):
    data = {
        "issue_comments": [
            {
                "id": 10,
                "user": {"login": "human"},
                "body": "@claude review\n\nTrigger\n\n<!-- orchestune:review-trigger bot=claude -->\n<!-- orchestune:review-round 5 -->",
            }
        ],
        "reviews": [],
        "inline_comments": [],
    }
    mock_get_data.return_value = data

    with pytest.raises(MaxRoundsExceededError):
        wait_for_review(
            pr_number=540,
            timeout=10,
            interval=0,
            bot_name="claude",
            post_trigger=True,
            max_rounds=5,
        )


def test_evaluate_verdict_layer1_html_marker_pass():
    body = "Review body with random text <!-- orchestune:verdict pass --> more text"
    assert evaluate_review_verdict(body, [], "claude") == EXIT_NO_FINDINGS


def test_evaluate_verdict_layer1_html_marker_fail():
    body = "Review body <!-- orchestune:verdict fail --> despite other words"
    assert evaluate_review_verdict(body, [], "claude") == EXIT_FINDINGS_PRESENT


def test_evaluate_verdict_layer2_codex_clean():
    body = "Codex Review: Didn't find any major issues. Nice work!"
    assert evaluate_review_verdict(body, [], "codex") == EXIT_NO_FINDINGS


def test_evaluate_verdict_layer2_codex_inlines():
    body = "### 💡 Codex Review\nHere are some suggestions."
    inlines = [{"path": "main.py", "line": 10, "body": "Fix bug"}]
    assert evaluate_review_verdict(body, inlines, "codex") == EXIT_FINDINGS_PRESENT


def test_evaluate_verdict_layer2_codex_badge_in_body():
    body = "### 💡 Codex Review\nHere are some suggestions: P1 Badge: Security flaw"
    assert evaluate_review_verdict(body, [], "codex") == EXIT_FINDINGS_PRESENT


def test_evaluate_verdict_layer2_claude_clean():
    body = "No blocking issues found. This is a safe, self-contained update."
    assert evaluate_review_verdict(body, [], "claude") == EXIT_NO_FINDINGS

    body2 = "### Review complete\nAll checks passed."
    assert evaluate_review_verdict(body2, [], "claude") == EXIT_NO_FINDINGS

    body3 = "### Review complete\nLooks good!"
    assert evaluate_review_verdict(body3, [], "claude") == EXIT_NO_FINDINGS


def test_evaluate_verdict_layer2_claude_blocking_findings():
    body = "### Findings\nblocking bugs as written:\n- Issue 1"
    assert evaluate_review_verdict(body, [], "claude") == EXIT_FINDINGS_PRESENT

    body2 = "### Review complete\nmarking this **fail** due to errors."
    assert evaluate_review_verdict(body2, [], "claude") == EXIT_FINDINGS_PRESENT

    body3 = "This PR should not be merged in its current form."
    assert evaluate_review_verdict(body3, [], "claude") == EXIT_FINDINGS_PRESENT

    body4 = "### Findings\n\n- [ ] Vulnerability in parser\n- [x] Style fixed"
    assert evaluate_review_verdict(body4, [], "claude") == EXIT_FINDINGS_PRESENT


def test_evaluate_verdict_layer2_claude_inlines():
    body = "### Review complete\nSee comments."
    inlines = [{"path": "main.py", "line": 5, "body": "Fix this"}]
    assert evaluate_review_verdict(body, inlines, "claude") == EXIT_FINDINGS_PRESENT


def test_evaluate_verdict_layer3_undetermined_fallback():
    body = "### Review note\nJust some comments about architecture without clear pass/fail status."
    assert evaluate_review_verdict(body, [], "claude") == EXIT_UNDETERMINED
    assert evaluate_review_verdict(body, [], "codex") == EXIT_UNDETERMINED
    assert evaluate_review_verdict("", [], "other_bot") == EXIT_UNDETERMINED


def test_evaluate_verdict_in_progress_state():
    body = "### Review in progress\n- [ ] Working on checks..."
    assert evaluate_review_verdict(body, [], "claude") == EXIT_IN_PROGRESS
    assert evaluate_review_verdict(body, [], "codex") == EXIT_IN_PROGRESS


def test_evaluate_verdict_generic_bot():
    body = "Review complete: All checks passed. Looks good."
    assert evaluate_review_verdict(body, [], "custom-bot") == EXIT_NO_FINDINGS

    body_inlines = "Review complete: Some comments."
    inlines = [{"path": "app.py", "line": 1, "body": "fix"}]
    assert (
        evaluate_review_verdict(body_inlines, inlines, "custom-bot")
        == EXIT_FINDINGS_PRESENT
    )


def test_get_latest_review_round_and_idempotency_when_poster_is_bot():
    data = {
        "issue_comments": [
            {
                "id": 50,
                "user": {"login": "claude[bot]"},
                "created_at": "2026-08-22T04:46:33Z",
                "body": "@claude review\n\n<!-- orchestune:review-trigger bot=claude -->\n<!-- orchestune:review-round 1 -->",
            }
        ]
    }
    assert _get_latest_review_round(data, "claude") == 1
    found = _find_existing_trigger_comment(data, "claude", 1)
    assert found is not None
    assert found["id"] == 50


def test_evaluate_verdict_layer2_claude_no_major_blocking_issues():
    body1 = "No major blocking issues found. Looks great overall."
    assert evaluate_review_verdict(body1, [], "claude") == EXIT_NO_FINDINGS

    body2 = "There are no other blocking bugs in this patch."
    assert evaluate_review_verdict(body2, [], "claude") == EXIT_NO_FINDINGS


@patch("scripts.wait_for_review._get_pr_data")
def test_wait_for_review_round_number_immediate_no_post(mock_get_data):
    trigger = {
        "id": 100,
        "user": {"login": "dev"},
        "created_at": "2026-08-20T07:44:44Z",
        "body": "@claude review\n\n<!-- orchestune:review-trigger bot=claude -->\n<!-- orchestune:review-round 1 -->",
    }
    review = {
        "id": 101,
        "user": {"login": "claude[bot]"},
        "created_at": "2026-08-20T07:45:00Z",
        "body": "### Review complete\nLooks good!",
    }
    mock_get_data.return_value = {
        "issue_comments": [trigger, review],
        "reviews": [],
        "inline_comments": [],
    }

    result = wait_for_review(
        pr_number=540,
        timeout=10,
        interval=0,
        bot_name="claude",
        post_trigger=False,
    )
    assert result["round"] == 1


@patch("scripts.wait_for_review._get_pr_data")
def test_wait_for_review_max_retries_exceeded_polling(mock_get_data):
    mock_get_data.side_effect = [
        {"issue_comments": [], "reviews": [], "inline_comments": []},
        RuntimeError("Transient API fail 1"),
        RuntimeError("Transient API fail 2"),
    ]
    with pytest.raises(RuntimeError, match="Exceeded maximum retries"):
        wait_for_review(
            pr_number=540,
            timeout=10,
            interval=0,
            bot_name="claude",
            post_trigger=False,
            max_retries=1,
        )


def test_get_initial_pr_data_max_retries_exceeded():
    from concurrent.futures import ThreadPoolExecutor

    from scripts.wait_for_review import _get_initial_pr_data

    with ThreadPoolExecutor(max_workers=1) as executor:
        with patch(
            "scripts.wait_for_review._get_pr_data",
            side_effect=RuntimeError("API down"),
        ):
            with pytest.raises(TimeoutError, match="retry attempts"):
                _get_initial_pr_data(
                    pr_number=540,
                    executor=executor,
                    timeout=10,
                    interval=0,
                    max_retries=2,
                )


@patch("scripts.wait_for_review._get_pr_data")
@patch("scripts.wait_for_review.post_review_trigger")
def test_wait_for_review_does_not_self_trigger_if_poster_is_bot(
    mock_post, mock_get_data
):
    mock_post.return_value = {
        "id": 100,
        "created_at": "2026-08-20T07:44:44Z",
        "body": "@claude review\n\n<!-- orchestune:review-trigger bot=claude -->\n<!-- orchestune:review-round 1 -->",
        "user": {"login": "claude[bot]"},
    }

    mock_get_data.side_effect = [
        {"issue_comments": [], "reviews": [], "inline_comments": []},
        {
            "issue_comments": [
                {
                    "id": 100,
                    "user": {"login": "claude[bot]"},
                    "created_at": "2026-08-20T07:44:44Z",
                    "body": "@claude review\n\n<!-- orchestune:review-trigger bot=claude -->\n<!-- orchestune:review-round 1 -->",
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
                    "body": "@claude review\n\n<!-- orchestune:review-trigger bot=claude -->\n<!-- orchestune:review-round 1 -->",
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
    assert "### Review complete" in result["review_body"]
    assert result["timestamp"] == "2026-08-20T07:46:00Z"
    assert result["round"] == 1

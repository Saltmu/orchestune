from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from scripts.wait_for_review import (
    EXIT_ACQUIRED,
    MaxRoundsExceededError,
    _find_existing_trigger_comment,
    _get_latest_review_round,
    _mark_review_trigger,
    _parse_review_round_marker,
    _review_round_marker,
    post_review_trigger,
    wait_for_review,
)


@pytest.fixture(autouse=True)
def _no_network_sha_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    """`wait_for_review()`/`_extract_review_result()` best-effort fetch PR head
    SHA / repo slug via `gh` for the acquisition-contract fields; keep these
    tests hermetic unless a specific test overrides the lookups."""
    monkeypatch.setattr(
        "scripts.wait_for_review._fetch_pr_head_sha", lambda pr_number: None
    )
    monkeypatch.setattr("scripts.wait_for_review._fetch_repository_slug", lambda: None)


@pytest.mark.parametrize(
    "side_effect, expected_exit",
    [
        pytest.param(None, 0, id="acquired"),
        pytest.param(ValueError("boom"), 2, id="internal-error"),
        pytest.param(MaxRoundsExceededError("max"), 12, id="round-limit"),
        pytest.param(TimeoutError("timed out"), 20, id="timed-out"),
    ],
)
def test_exit_code_table_for_online_acquisition_control_paths(
    side_effect, expected_exit
):
    """Exit codes here are pure acquisition/wait control, verified against the
    full table (issue #1099 Exit table); StalledReviewError=21 is covered by
    test_main_cli_stalled since it needs its own import."""
    from scripts.wait_for_review import main

    with patch("sys.argv", ["wait_for_review.py", "--pr", "540", "--no-post"]):
        with patch(
            "scripts.wait_for_review.wait_for_review",
            autospec=True,
            side_effect=side_effect,
            return_value={"review_body": "LGTM", "inline_comments": []},
        ):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == expected_exit


@pytest.mark.parametrize(
    "state, expected_exit",
    [
        pytest.param(
            {
                "reviews": [
                    {
                        "id": 1,
                        "user": {"login": "claude[bot]"},
                        "submitted_at": "2026-08-20T10:00:00Z",
                        "body": "LGTM",
                    }
                ]
            },
            0,
            id="acquired",
        ),
        pytest.param(
            {
                "issue_comments": [
                    {
                        "id": 1,
                        "user": {"login": "claude[bot]"},
                        "created_at": "2026-08-20T10:00:00Z",
                        "body": "### Review in progress\n- [ ] Working...",
                    }
                ]
            },
            11,
            id="in-progress",
        ),
        pytest.param({}, 30, id="no-result"),
    ],
)
def test_exit_code_table_for_offline_single_snapshot_paths(
    state, expected_exit, tmp_path
):
    """The offline `--review-state-file` path is the only one that can produce
    Exit 11/30 directly from acquisition_status (issue #1099 Exit table)."""
    from scripts.wait_for_review import main

    path = tmp_path / "state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    with patch("sys.argv", ["wait_for_review.py", "--review-state-file", str(path)]):
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == expected_exit


def test_main_cli_success():
    from scripts.wait_for_review import main

    with patch("sys.argv", ["wait_for_review.py", "--pr", "540", "--no-post"]):
        with patch(
            "scripts.wait_for_review.wait_for_review", autospec=True
        ) as mock_wait:
            mock_wait.return_value = {
                "review_body": "### Findings\n🔴 blocking bug",
                "inline_comments": [],
            }
            with pytest.raises(SystemExit) as exc:
                main()
            # Content no longer decides the exit code: any acquired result is
            # Exit 0 (Exit 10 / findings-present is abolished, issue #1099).
            assert exc.value.code == EXIT_ACQUIRED


def test_main_cli_writes_output_file(tmp_path):
    from scripts.wait_for_review import main

    output_path = tmp_path / "review-result.json"
    with patch(
        "sys.argv",
        [
            "wait_for_review.py",
            "--pr",
            "540",
            "--no-post",
            "--output-file",
            str(output_path),
        ],
    ):
        with patch(
            "scripts.wait_for_review.wait_for_review", autospec=True
        ) as mock_wait:
            mock_wait.return_value = {
                "acquisition_status": "acquired",
                "review_body": "LGTM",
                "inline_comments": [],
            }
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == EXIT_ACQUIRED
    saved = json.loads(output_path.read_text(encoding="utf-8"))
    assert saved["review_body"] == "LGTM"


def test_main_cli_output_file_write_failure_is_not_a_silent_success(tmp_path):
    from scripts.wait_for_review import main

    unwritable_path = tmp_path / "no-such-dir" / "review-result.json"
    with patch(
        "sys.argv",
        [
            "wait_for_review.py",
            "--pr",
            "540",
            "--no-post",
            "--output-file",
            str(unwritable_path),
        ],
    ):
        with patch(
            "scripts.wait_for_review.wait_for_review", autospec=True
        ) as mock_wait:
            mock_wait.return_value = {"review_body": "LGTM", "inline_comments": []}
            with pytest.raises(SystemExit) as exc:
                main()
            # A write failure must not exit as if acquisition succeeded.
            assert exc.value.code != EXIT_ACQUIRED


def test_main_cli_timeout():
    from scripts.wait_for_review import main

    with patch("sys.argv", ["wait_for_review.py", "--pr", "540", "--no-post"]):
        with patch(
            "scripts.wait_for_review.wait_for_review",
            autospec=True,
            side_effect=TimeoutError("Timed out"),
        ):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 20


def test_main_cli_stalled():
    from scripts.wait_for_review import StalledReviewError, main

    with patch("sys.argv", ["wait_for_review.py", "--pr", "540", "--no-post"]):
        with patch(
            "scripts.wait_for_review.wait_for_review",
            autospec=True,
            side_effect=StalledReviewError("Tracker comment stopped changing"),
        ):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 21


def test_main_cli_stall_grace_argument_parsing():
    from scripts.wait_for_review import main

    with patch(
        "sys.argv",
        ["wait_for_review.py", "--pr", "540", "--no-post", "--stall-grace", "120"],
    ):
        with patch(
            "scripts.wait_for_review.wait_for_review", autospec=True
        ) as mock_wait:
            mock_wait.return_value = {"review_body": "LGTM", "inline_comments": []}
            with pytest.raises(SystemExit):
                main()
            assert mock_wait.call_args.kwargs["stall_grace_seconds"] == 120


def test_main_cli_max_rounds():
    from scripts.wait_for_review import main

    with patch("sys.argv", ["wait_for_review.py", "--pr", "540", "--no-post"]):
        with patch(
            "scripts.wait_for_review.wait_for_review",
            autospec=True,
            side_effect=MaxRoundsExceededError("Max rounds reached"),
        ):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 12


def test_main_cli_unexpected_error():
    from scripts.wait_for_review import main

    with patch("sys.argv", ["wait_for_review.py", "--pr", "540", "--no-post"]):
        with patch(
            "scripts.wait_for_review.wait_for_review",
            autospec=True,
            side_effect=ValueError("Boom"),
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
        with patch(
            "scripts.wait_for_review.wait_for_review", autospec=True
        ) as mock_wait:
            mock_wait.return_value = {"review_body": "LGTM", "inline_comments": []}
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == EXIT_ACQUIRED
            mock_wait.assert_called_once_with(
                540,
                timeout=1800,
                interval=5,
                bot_name="claude",
                post_trigger=True,
                body=None,
                body_file=None,
                max_rounds=3,
                max_retries=2,
                round_num=2,
                stall_grace_seconds=600,
            )


def test_main_cli_arguments_parsing_with_jev_threshold():
    from scripts.wait_for_review import main

    with patch(
        "sys.argv",
        [
            "wait_for_review.py",
            "--pr",
            "540",
            "--jev-threshold",
            "0.85",
        ],
    ):
        with patch(
            "scripts.wait_for_review.wait_for_review", autospec=True
        ) as mock_wait:
            mock_wait.return_value = {"review_body": "LGTM", "inline_comments": []}
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == EXIT_ACQUIRED
            mock_wait.assert_called_once_with(
                540,
                timeout=1800,
                interval=5,
                bot_name="claude",
                post_trigger=True,
                body=None,
                body_file=None,
                max_rounds=5,
                max_retries=1,
                round_num=None,
                stall_grace_seconds=600,
                jev_threshold=0.85,
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


@patch("scripts.wait_for_review._get_pr_data", autospec=True)
@patch("scripts.wait_for_review.post_review_trigger", autospec=True)
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
    assert result["round"] == 1
    assert result["trigger_id"] == 100


@patch("scripts.wait_for_review._get_pr_data", autospec=True)
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


@patch("scripts.wait_for_review._get_pr_data", autospec=True)
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
    assert result["trigger_id"] == 100
    assert result["acquisition_status"] == "acquired"


@patch("scripts.wait_for_review._get_pr_data", autospec=True)
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
            autospec=True,
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


@patch("scripts.wait_for_review._get_pr_data", autospec=True)
@patch("scripts.wait_for_review.post_review_trigger", autospec=True)
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


def test_context_is_lazy_cached_and_disabled_without_key(monkeypatch):
    from scripts.jev_context import JevReviewContext
    from scripts.wait_for_review import _extract_review_result

    state = {
        "reviews": [{"body": "bug", "user": {"login": "claude"}}],
        "inline_comments": [
            {"body": "bug", "path": "a.py", "line": 1, "user": {"login": "claude"}}
        ],
    }
    cache = {}
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    with patch("scripts.wait_for_review.collect_review_context") as collect:
        _extract_review_result(state, "claude", pr_number=3, context_cache=cache)
        collect.assert_not_called()
    monkeypatch.setenv("JEV_API_KEY", "test-key")
    with (
        patch(
            "scripts.wait_for_review.collect_review_context",
            return_value=JevReviewContext(),
        ) as collect,
        patch(
            "scripts.wait_for_review.evaluate_review_findings",
            side_effect=lambda items, **kwargs: {"kept": items, "jev_evaluations": []},
        ) as filtering,
    ):
        _extract_review_result(state, "claude", pr_number=3, context_cache=cache)
        _extract_review_result(state, "claude", pr_number=3, context_cache=cache)
        collect.assert_called_once_with(3)
        assert filtering.call_args.kwargs["context"] is cache[3]


def _jev_state():
    sha = "a" * 40
    return {
        "issue_comments": [],
        "reviews": [
            {
                "id": 1,
                "body": "blocking bug",
                "submitted_at": "2026-09-27T00:01:00Z",
                "user": {"login": "claude"},
            }
        ],
        "inline_comments": [
            {
                "id": 2,
                "body": "Only future external callers could trigger this defect",
                "path": "scripts/a.py",
                "line": 2,
                "side": "RIGHT",
                "commit_id": sha,
                "user": {"login": "claude"},
                "created_at": "2026-09-27T00:01:00Z",
            }
        ],
    }


def _jev_offline_context():
    return {
        "pr": {"base_sha": "b" * 40},
        "code": {
            "source": "git_blob",
            "commit_sha": "a" * 40,
            "side": "RIGHT",
            "start_line": 1,
            "text": "pass",
            "status": "available",
        },
        "execution": {
            "input_trust": "unknown",
            "evidence": [
                {
                    "source": "module_description",
                    "commit_sha": "a" * 40,
                    "text": "Only documented local trusted config inputs are used.",
                }
            ],
        },
        "repository_rules": {
            "source": ".agents/AGENTS.md",
            "commit_sha": "b" * 40,
            "text": "Check actual external inputs",
            "status": "available",
        },
    }


@pytest.mark.parametrize("route", ["immediate", "polling", "offline"])
def test_three_routes_use_same_context_policy_and_never_drop_the_finding(
    route, monkeypatch, tmp_path
):
    """All three acquisition routes must run the same Jev context policy AND
    must never remove the finding from the contract just because Jev marked
    it speculative -- only `jev_evaluations` records that decision."""
    from scripts.jev_context import JevReviewContext
    from scripts.jev_filter import JevFindingEvaluation
    from scripts.wait_for_review import _check_immediate_review_result, main

    monkeypatch.setenv("JEV_API_KEY", "test-key")
    monkeypatch.setenv("JEV_LOG_PATH", str(tmp_path / "jev.jsonl"))
    state = _jev_state()
    review = JevReviewContext(pr={"head_sha": "a" * 40, "base_sha": "b" * 40})
    evaluation = JevFindingEvaluation(
        0.99, "HIGH", applicability="SPECULATIVE", applicability_confidence=0.95
    )
    with (
        patch(
            "scripts.jev_filter.evaluate_finding_with_jev", return_value=evaluation
        ) as evaluate,
        patch(
            "scripts.jev_context._read_blob",
            return_value='"""Only documented trusted local config inputs are used."""\npass',
        ),
        patch(
            "scripts.wait_for_review.collect_review_context", return_value=review
        ) as collect,
    ):
        if route == "immediate":
            result = _check_immediate_review_result(
                state, "claude", "2026-09-27T00:00:00Z", 1, pr_number=5
            )
            assert result is not None
            assert result["acquisition_status"] == "acquired"
            assert len(result["inline_comments"]) == 1
            assert result["jev_evaluations"][0]["decision"] == "filtered"
            assert result["jev_evaluations"][0]["decision_reason"] == "speculative"
        elif route == "polling":
            with (
                patch(
                    "scripts.wait_for_review._get_initial_pr_data",
                    return_value={
                        "issue_comments": [],
                        "reviews": [],
                        "inline_comments": [],
                    },
                ),
                patch("scripts.wait_for_review._get_pr_data", return_value=state),
            ):
                result = wait_for_review(5, post_trigger=False)
            assert result["acquisition_status"] == "acquired"
            assert len(result["inline_comments"]) == 1
            assert result["jev_evaluations"][0]["decision"] == "filtered"
        else:
            state["context"] = _jev_offline_context()
            path = tmp_path / "state.json"
            path.write_text(json.dumps(state))
            with (
                patch("sys.argv", ["wait", "--review-state-file", str(path)]),
                pytest.raises(SystemExit) as exc,
            ):
                main()
            # Findings are never dropped, so acquisition is still Exit 0 even
            # though Jev marked the sole finding filtered.
            assert exc.value.code == EXIT_ACQUIRED
            collect.assert_not_called()
        assert evaluate.call_args.kwargs["context"]["code"]["commit_sha"] == "a" * 40
        record = json.loads((tmp_path / "jev.jsonl").read_text())
        assert record["decision_reason"] == "speculative"


def test_offline_with_key_never_fetches_missing_context(monkeypatch, tmp_path):
    from scripts.jev_filter import JevFindingEvaluation
    from scripts.wait_for_review import main

    monkeypatch.setenv("JEV_API_KEY", "test-key")
    monkeypatch.setenv("JEV_LOG_PATH", str(tmp_path / "jev.jsonl"))
    output_path = tmp_path / "review-result.json"
    path = tmp_path / "state.json"
    path.write_text(json.dumps(_jev_state()))
    evaluation = JevFindingEvaluation(
        0.99, "HIGH", applicability="SPECULATIVE", applicability_confidence=0.95
    )
    with (
        patch("scripts.jev_filter.evaluate_finding_with_jev", return_value=evaluation),
        patch("scripts.jev_context.subprocess.run") as run,
        patch(
            "sys.argv",
            [
                "wait",
                "--review-state-file",
                str(path),
                "--output-file",
                str(output_path),
            ],
        ),
        pytest.raises(SystemExit) as exc,
    ):
        main()
    # No context was supplied for this finding, so SPECULATIVE exclusion's
    # provenance requirements are unmet and Jev falls back to validity/impact
    # (HIGH, validity 0.99 >= threshold) -> kept. Acquisition is Exit 0 either
    # way; what must hold is that offline never shells out to git for context.
    assert exc.value.code == EXIT_ACQUIRED
    run.assert_not_called()
    saved = json.loads(output_path.read_text(encoding="utf-8"))
    assert saved["jev_evaluations"][0]["decision"] == "kept"
    assert saved["jev_evaluations"][0]["decision_reason"] == "accepted"


def test_offline_explicit_incomplete_section_is_not_exit_0(tmp_path):
    """A partial MCP/App snapshot that explicitly declares a section
    missing/error/truncated must not report a trustworthy acquired result,
    even though some content was found (Codex PR #1114 round 1 finding)."""
    from scripts.wait_for_review import EXIT_NO_RESULT, main

    state = _jev_state()
    state["completeness"] = {
        "issue_comments": "complete",
        "reviews": "complete",
        "inline_comments": "truncated",
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state), encoding="utf-8")

    with (
        patch("sys.argv", ["wait", "--review-state-file", str(path)]),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    assert exc.value.code == EXIT_NO_RESULT


def test_offline_unknown_completeness_still_exits_acquired(tmp_path):
    """The default "unknown" completeness (no metadata supplied) must keep
    working for legacy callers -- only an *explicit* incomplete declaration
    downgrades the result, per issue #1099's "旧入力形式の読み込みは維持する"."""
    from scripts.wait_for_review import main

    state = _jev_state()
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state), encoding="utf-8")

    with (
        patch("sys.argv", ["wait", "--review-state-file", str(path)]),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    assert exc.value.code == EXIT_ACQUIRED

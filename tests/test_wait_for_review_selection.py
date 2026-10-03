"""Hard gates for reviewer selection and re-review evidence."""

import json
from unittest.mock import patch

import pytest

from scripts.wait_for_review import _extract_review_result, main, wait_for_review

SHA = "a" * 40


@pytest.fixture(autouse=True)
def hermetic_metadata(monkeypatch):
    monkeypatch.setattr("scripts.wait_for_review._fetch_pr_head_sha", lambda _: SHA)
    monkeypatch.setattr(
        "scripts.wait_for_review._fetch_repository_slug", lambda: "org/repo"
    )


def previous_round(bot="claude"):
    return {
        "issue_comments": [
            {
                "id": 1,
                "created_at": "2026-01-01T00:00:00Z",
                "body": f"@{bot} review\n<!-- orchestune:review-trigger bot={bot} -->\n<!-- orchestune:review-round 1 -->\n<!-- orchestune:review-head {SHA} -->",
            },
            {
                "id": 2,
                "created_at": "2026-01-01T00:01:00Z",
                "body": "Contract bug",
                "user": {"login": bot},
            },
        ],
        "reviews": [],
        "inline_comments": [],
    }


def write_reply(tmp_path, findings):
    import yaml

    path = tmp_path / "reply.md"
    path.write_text(
        "```orchestune-review-judgments\n"
        + yaml.safe_dump({"round": 1, "findings": findings})
        + "```\n",
        encoding="utf-8",
    )
    return str(path)


def judgment(source="issue_comment:2"):
    return {
        "source": source,
        "location": "app.py:8",
        "judgment": "adopt",
        "status": "resolved",
        "basis": "fixed interface bug",
        "evidence": "commit and regression test",
    }


def test_valid_re_review_posts_and_switch_preserves_global_round(tmp_path):
    data = previous_round()
    reply = write_reply(tmp_path, [judgment()])
    completed = {
        "id": 3,
        "created_at": "2026-01-01T00:03:00Z",
        "body": "No remaining findings",
        "user": {"login": "codex"},
    }
    after = {**data, "issue_comments": [*data["issue_comments"], completed]}
    with (
        patch("scripts.wait_for_review._get_initial_pr_data", return_value=data),
        patch("scripts.wait_for_review._get_pr_data", return_value=after),
        patch(
            "scripts.wait_for_review.post_review_trigger",
            return_value={"id": 4, "created_at": "2026-01-01T00:02:00Z"},
        ) as post,
    ):
        result = wait_for_review(
            1,
            bot_name="codex",
            switch_reviewer=True,
            body_file=reply,
            timeout=0,
            interval=0,
        )
    assert result["round"] == 2
    assert post.call_args.kwargs["round_num"] == 2
    assert post.call_args.kwargs["head_sha"] == SHA


@pytest.mark.parametrize("findings", [[], [judgment("inline_comment:99")]])
def test_re_review_rejects_missing_current_sources(tmp_path, findings):
    with (
        patch(
            "scripts.wait_for_review._get_initial_pr_data",
            return_value=previous_round(),
        ),
        patch("scripts.wait_for_review.post_review_trigger") as post,
    ):
        with pytest.raises(ValueError, match="issue_comment:2"):
            wait_for_review(
                1, bot_name="claude", body_file=write_reply(tmp_path, findings)
            )
        post.assert_not_called()


def test_resume_restores_recorded_head_for_no_post():
    with patch(
        "scripts.wait_for_review._get_initial_pr_data", return_value=previous_round()
    ):
        result = wait_for_review(1, bot_name="claude", post_trigger=False)
    assert result["requested_head_sha"] == SHA
    assert result["reviewed_head_sha"] is None
    assert result["review_target_sha"] == SHA


def test_post_trigger_has_separate_head_and_unchanged_workflow_marker():
    from scripts.wait_for_review import post_review_trigger

    with patch("scripts.wait_for_review._run_gh", return_value='{"id": 1}') as gh:
        post_review_trigger(
            1,
            bot_name="claude",
            body=f"old\n<!-- orchestune:review-head {'b' * 40} -->",
        )
    body = gh.call_args.args[0][-1]
    assert (
        "<!-- orchestune:review-trigger bot=claude -->\n<!-- orchestune:review-round 1 -->\n"
        in body
    )
    assert body.endswith(f"<!-- orchestune:review-head {SHA} -->")
    assert body.count("orchestune:review-head") == 1


def test_trigger_resume_uses_original_head_after_push():
    from scripts.wait_for_review import _handle_review_trigger

    with (
        patch("scripts.wait_for_review.post_review_trigger") as post,
        patch("scripts.wait_for_review._fetch_pr_head_sha", return_value="b" * 40),
    ):
        _, _, requested = _handle_review_trigger(
            1, "claude", previous_round(), {}, set(), 1, 5, None, None
        )
    assert requested == SHA
    post.assert_not_called()


def test_new_trigger_recovers_sha_from_posted_marker_when_first_lookup_failed():
    from scripts.wait_for_review import _handle_review_trigger

    posted = {
        "id": 1,
        "body": f"<!-- orchestune:review-head {SHA} -->",
        "created_at": "2026-01-01T00:00:00Z",
    }
    with (
        patch("scripts.wait_for_review.post_review_trigger", return_value=posted),
        patch("scripts.wait_for_review._fetch_pr_head_sha", return_value=None),
    ):
        _, _, requested = _handle_review_trigger(
            1, "claude", {}, {}, set(), 1, 5, None, None
        )
    assert requested == SHA


def test_round_two_no_post_still_requires_judgments():
    data = previous_round()
    data["issue_comments"].extend(
        [
            {
                "id": 3,
                "created_at": "2026-01-01T00:02:00Z",
                "body": f"@claude review\n<!-- orchestune:review-trigger bot=claude -->\n<!-- orchestune:review-round 2 -->\n<!-- orchestune:review-head {SHA} -->",
            },
            {
                "id": 4,
                "created_at": "2026-01-01T00:03:00Z",
                "body": "Clean",
                "user": {"login": "claude"},
            },
        ]
    )
    with patch("scripts.wait_for_review._get_initial_pr_data", return_value=data):
        with pytest.raises(ValueError, match="body-file"):
            wait_for_review(1, bot_name="claude", post_trigger=False)


def test_skip_cli_exits_zero_but_prints_not_pass(capsys):
    state = {"issue_comments": [], "reviews": [], "inline_comments": []}
    with (
        patch("sys.argv", ["wait", "--pr", "1", "--bot-name", "skip"]),
        patch("scripts.wait_for_review._get_initial_pr_data", return_value=state),
        patch("scripts.wait_for_review._run_gh", return_value='{"id":1}'),
    ):
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value.code == 0
    assert "not a review pass" in capsys.readouterr().out


def test_empty_previous_round_can_retry_with_explicit_empty_judgments(tmp_path):
    from scripts.wait_for_review import _validate_review_reply

    data = previous_round()
    data["issue_comments"] = data["issue_comments"][:1]
    _validate_review_reply(data, "claude", 2, write_reply(tmp_path, []))


def test_cli_requires_explicit_reviewer():
    with (
        patch("sys.argv", ["wait", "--pr", "1"]),
        patch("scripts.wait_for_review.wait_for_review") as wait,
    ):
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value.code == 2
    wait.assert_not_called()


def test_skip_records_selection_without_trigger_and_deduplicates():
    marker = f"<!-- orchestune:review-selection reviewer=skip head={SHA} -->"
    state = {"issue_comments": [], "reviews": [], "inline_comments": []}
    with (
        patch("scripts.wait_for_review._fetch_pr_head_sha", return_value=SHA),
        patch(
            "scripts.wait_for_review._fetch_repository_slug", return_value="org/repo"
        ),
        patch("scripts.wait_for_review._get_initial_pr_data", return_value=state),
        patch(
            "scripts.wait_for_review._run_gh", return_value=json.dumps({"id": 1})
        ) as gh,
        patch("scripts.wait_for_review.post_review_trigger") as trigger,
    ):
        result = wait_for_review(1, bot_name="skip")
        assert result["reviewer"] == "skip"
        assert result["acquisition_status"] == "unavailable"
        assert marker in gh.call_args.args[0][-1]
        assert "status:blocked-human-review" in gh.call_args.args[0][-1]
        state["issue_comments"] = [{"body": marker}]
        wait_for_review(1, bot_name="skip")
        assert gh.call_count == 1
        trigger.assert_not_called()


def test_switch_reviewer_requires_explicit_override():
    state = {
        "issue_comments": [
            {
                "body": "<!-- orchestune:review-trigger bot=claude -->\n<!-- orchestune:review-round 1 -->"
            }
        ],
        "reviews": [],
        "inline_comments": [],
    }
    with (
        patch("scripts.wait_for_review._get_initial_pr_data", return_value=state),
        patch("scripts.wait_for_review._fetch_repository_slug", return_value=None),
        patch("scripts.wait_for_review.post_review_trigger") as trigger,
    ):
        with pytest.raises(ValueError, match="switch-reviewer"):
            wait_for_review(1, bot_name="codex")
        trigger.assert_not_called()


@pytest.mark.parametrize(
    "review_sha,head,expected,source",
    [
        (SHA, "b" * 40, SHA, "review_commit"),
        (None, SHA, SHA, "trigger_head_verified"),
        (None, "b" * 40, None, "unknown"),
    ],
)
def test_target_sha_derivation(review_sha, head, expected, source):
    state = {"issue_comments": [], "reviews": [], "inline_comments": []}
    item = {
        "id": 1,
        "body": "review",
        "user": {"login": "claude"},
        "created_at": "2026-01-01T00:00:00Z",
    }
    if review_sha:
        item.update(commit_id=review_sha, submitted_at=item["created_at"])
        state["reviews"].append(item)
    else:
        state["issue_comments"].append(item)
    with patch("scripts.wait_for_review._fetch_pr_head_sha", return_value=head):
        result = _extract_review_result(
            state,
            "claude",
            pr_number=1,
            latest_trigger_time="2025-01-01T00:00:00Z",
            requested_head_sha=SHA,
        )
    assert result["review_target_sha"] == expected
    assert result["review_target_sha_source"] == source


def test_round_two_requires_body_file_before_trigger():
    state = {
        "issue_comments": [
            {
                "body": "<!-- orchestune:review-trigger bot=claude -->\n<!-- orchestune:review-round 1 -->"
            }
        ],
        "reviews": [],
        "inline_comments": [],
    }
    with (
        patch("scripts.wait_for_review._get_initial_pr_data", return_value=state),
        patch("scripts.wait_for_review._fetch_repository_slug", return_value=None),
        patch("scripts.wait_for_review.post_review_trigger") as trigger,
    ):
        with pytest.raises(ValueError, match="body-file"):
            wait_for_review(1, bot_name="claude")
        trigger.assert_not_called()

"""The installed package and historical script share exactly one implementation."""

import copy

import pytest

from orchestune.review import acquisition, markers
from scripts import review_verdict
from scripts.wait_for_review import _build_snapshot, _extract_review_result


def test_script_reexports_packaged_acquisition():
    assert review_verdict.extract_review_result is acquisition.extract_review_result
    assert review_verdict.collect_review_state is acquisition.collect_review_state


def test_current_round_acquisition_ignores_bot_authored_trigger():
    state = {
        "issue_comments": [
            dict(
                id=1,
                body="@claude review",
                user={"login": "claude[bot]"},
                created_at="2026-10-03T00:00:00Z",
            ),
        ]
    }
    result = acquisition.collect_review_state(
        state, "claude", exclude_ids={1}, round_started_at="2026-10-03T00:00:00Z"
    )
    assert result["acquisition_status"] == "unavailable"


# --- #1207: same-bot review replies are not review evidence -------------------

REPLY = markers.review_reply_marker()


def _comment(id_, body, login="claude[bot]", at="2026-10-03T00:05:00Z"):
    return dict(id=id_, body=body, user={"login": login}, created_at=at)


def test_review_reply_marker_is_canonical():
    assert REPLY == "<!-- orchestune:review-reply -->"


@pytest.mark.parametrize(
    "body",
    [
        REPLY,
        f"{REPLY}\nbody",
        f"\n\n  {REPLY}  \nbody",
        f"{REPLY}\r\nbody\r\n",
        f"\r\n \t\r\n{REPLY}\r\n",
    ],
)
def test_is_review_reply_accepts_leading_declaration(body):
    assert markers.is_review_reply(body)


@pytest.mark.parametrize(
    "body",
    [
        None,
        "",
        "   \n ",
        "No marker at all",
        f"intro\n{REPLY}",
        f"> {REPLY}",
        f"`{REPLY}`",
        f"```\n{REPLY}\n```",
        f"```html\n{REPLY}\n```\n{REPLY}",
        "<!-- ORCHESTUNE:REVIEW-REPLY -->",
        "<!--orchestune:review-reply-->",
        "<!-- orchestune:review-reply  -->",
        "<!-- orchestune:review-reply foo=bar -->",
        f"{REPLY} trailing text",
        "<!-- orchestune:review-reply-extra -->",
    ],
)
def test_is_review_reply_rejects_everything_else(body):
    assert not markers.is_review_reply(body)  # type: ignore[arg-type]


def _reviewed_state(extra_comments=()):
    return {
        "issue_comments": [
            _comment(1, "@claude review", at="2026-10-03T00:00:00Z"),
            _comment(2, "No required findings.", at="2026-10-03T00:01:00Z"),
            *extra_comments,
        ],
        "reviews": [],
        "inline_comments": [],
    }


@pytest.mark.parametrize("login", ["claude[bot]", "chatgpt-codex-connector[bot]"])
def test_reply_does_not_change_acquired_review(login):
    bot = "claude" if login.startswith("claude") else "codex"
    base = _reviewed_state()
    for item in base["issue_comments"][1:]:
        item["user"] = {"login": login}
    with_reply = _reviewed_state(
        [_comment(3, f"{REPLY}\nRound 1 judgments", login=login)]
    )
    with_reply["issue_comments"][1]["user"] = {"login": login}
    kwargs = dict(exclude_ids={1}, round_started_at="2026-10-03T00:00:00Z")
    before = acquisition.collect_review_state(base, bot, **kwargs)
    after = acquisition.collect_review_state(with_reply, bot, **kwargs)
    assert before["acquisition_status"] == "acquired"
    assert after == before


def test_reply_only_is_not_an_acquired_review():
    state = {
        "issue_comments": [_comment(3, f"{REPLY}\njudged everything")],
        "reviews": [],
        "inline_comments": [],
    }
    result = acquisition.collect_review_state(state, "claude")
    assert result["acquisition_status"] == "unavailable"
    assert result["review_items"] == []


def test_reply_does_not_hide_in_progress_review():
    state = _reviewed_state()
    state["issue_comments"][1]["body"] = "Claude is working…"
    state["issue_comments"][1]["created_at"] = "2026-10-03T00:01:00Z"
    state["issue_comments"].append(
        _comment(3, f"{REPLY}\nreply", at="2026-10-03T00:09:00Z")
    )
    result = acquisition.collect_review_state(state, "claude", exclude_ids={1})
    assert result["acquisition_status"] == "in_progress"
    latest = acquisition._latest_bot_activity_item(state, "claude", {1})
    assert latest is not None and latest["id"] == 2


def test_reply_is_not_latest_summary_or_activity():
    state = _reviewed_state([_comment(3, f"{REPLY}\nreply", at="2026-10-03T00:09:00Z")])
    summary = acquisition._latest_bot_summary_item(state, "claude", {1})
    assert summary is not None and summary["id"] == 2


def test_reply_changes_do_not_alter_polling_snapshot():
    state = _reviewed_state()
    before = acquisition._build_snapshot(state, "claude", {1})
    state["issue_comments"].append(_comment(3, f"{REPLY}\nv1"))
    state["issue_comments"][-1]["updated_at"] = "2026-10-03T01:00:00Z"
    assert acquisition._build_snapshot(state, "claude", {1}) == before


def test_marker_only_applies_to_issue_comments():
    state = {
        "issue_comments": [],
        "reviews": [
            dict(
                id=5,
                body=f"{REPLY}\nformal review finding",
                user={"login": "claude[bot]"},
                submitted_at="2026-10-03T00:02:00Z",
                state="COMMENTED",
                commit_id="a" * 40,
            )
        ],
        "inline_comments": [
            dict(
                id=6,
                body=f"{REPLY}\ninline finding",
                user={"login": "claude[bot]"},
                path="a.py",
                line=1,
                created_at="2026-10-03T00:02:00Z",
            )
        ],
    }
    result = acquisition.collect_review_state(state, "claude")
    assert result["acquisition_status"] == "acquired"
    assert [item["id"] for item in result["review_items"]] == [5]
    assert [item["id"] for item in result["inline_comments"]] == [6]
    assert acquisition._build_snapshot(state, "claude") == {
        "review_5": "2026-10-03T00:02:00Z:" + str(len(state["reviews"][0]["body"])),
        "inline_6": "2026-10-03T00:02:00Z",
    }


def test_unmarked_bot_comment_is_still_evidence():
    state = _reviewed_state([_comment(3, "Judgments for round 1 (no marker)")])
    result = acquisition.collect_review_state(state, "claude", exclude_ids={1})
    assert [item["id"] for item in result["review_items"]] == [2, 3]


def test_reply_filter_keeps_exclude_ids_and_input_untouched():
    state = _reviewed_state([_comment(3, f"{REPLY}\nreply")])
    state["issue_comments"].append(
        {"body": f"{REPLY}\nno id", "user": {"login": "claude"}}
    )
    state["issue_comments"].append(
        {"id": None, "body": None, "user": {"login": "claude"}}
    )
    snapshot = copy.deepcopy(state)
    kept = acquisition._filter_bot_issue_comments(
        state["issue_comments"], "claude", {"1", 2}
    )
    assert [item.get("body") for item in kept] == [None]
    assert state == snapshot


# --- #1207: wait_for_review paths ignore marked same-bot replies -----------------

_REPLY_BODY = "<!-- orchestune:review-reply -->\nRound 1 judgments"


def _reply_round(bot="claude"):
    return {
        "issue_comments": [
            {
                "id": 1,
                "created_at": "2026-01-01T00:00:00Z",
                "body": f"@{bot} review\n<!-- orchestune:review-trigger bot={bot} -->\n<!-- orchestune:review-round 1 -->\n<!-- orchestune:review-head {'a' * 40} -->",
            },
            {
                "id": 2,
                "created_at": "2026-01-01T00:01:00Z",
                "body": "Contract bug",
                "user": {"login": bot},
            },
            {
                "id": 3,
                "created_at": "2026-01-01T00:03:00Z",
                "body": _REPLY_BODY,
                "user": {"login": bot},
            },
        ],
        "reviews": [],
        "inline_comments": [],
    }


def _write_judgments(tmp_path, sources):
    import yaml

    findings = [
        {
            "source": source,
            "location": "app.py:8",
            "judgment": "adopt",
            "status": "resolved",
            "basis": "fixed interface bug",
            "evidence": "commit and regression test",
        }
        for source in sources
    ]
    path = tmp_path / "reply.md"
    path.write_text(
        "```orchestune-review-judgments\n"
        + yaml.safe_dump({"round": 1, "findings": findings})
        + "```\n",
        encoding="utf-8",
    )
    return str(path)


@pytest.mark.parametrize("bot", ["claude", "codex"])
def test_previous_round_coverage_ignores_marked_reply(tmp_path, bot):
    from scripts.wait_for_review import _validate_review_reply

    data = _reply_round(bot)
    _validate_review_reply(
        data, bot, 2, _write_judgments(tmp_path, ["issue_comment:2"])
    )


def test_previous_round_coverage_still_requires_unmarked_reply(tmp_path):
    from scripts.wait_for_review import _validate_review_reply

    data = _reply_round()
    data["issue_comments"][2]["body"] = "Round 1 judgments (no marker)"
    with pytest.raises(ValueError, match="source"):
        _validate_review_reply(
            data, "claude", 2, _write_judgments(tmp_path, ["issue_comment:2"])
        )


def test_wait_helpers_ignore_marked_reply():
    data = _reply_round()
    kept = {k: v for k, v in data.items()}
    without_reply = {**kept, "issue_comments": data["issue_comments"][:2]}
    assert _build_snapshot(data, "claude", exclude_ids={1}) == _build_snapshot(
        without_reply, "claude", exclude_ids={1}
    )
    assert _extract_review_result(
        data, "claude", exclude_ids={1}
    ) == _extract_review_result(without_reply, "claude", exclude_ids={1})

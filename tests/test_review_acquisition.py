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


# --- #1210: round boundaries, comment-only exclusion, bounded progress ---------

ROUND_START = "2026-10-04T03:10:00Z"
ROUND_END = "2026-10-04T03:20:00Z"


def _bounded(state, **kwargs):
    return acquisition.collect_review_state(
        state,
        "codex",
        round_started_at=ROUND_START,
        round_ended_at=ROUND_END,
        **kwargs,
    )


def _items(result, section="review_items"):
    return {item["id"]: item for item in result[section]}


def test_items_are_current_only_inside_the_round_interval():
    state = {
        "issue_comments": [
            _comment(1, "before", "codex", "2026-10-04T03:09:59Z"),
            _comment(2, "start boundary", "codex", ROUND_START),
            _comment(3, "inside", "codex", "2026-10-04T03:15:00Z"),
            _comment(4, "end boundary", "codex", ROUND_END),
            _comment(5, "after", "codex", "2026-10-04T03:30:00Z"),
        ]
    }
    items = _items(_bounded(state))
    assert {i: items[i]["provenance"] for i in items} == {
        1: "historical",
        2: "current",
        3: "current",
        4: "unassociated",
        5: "unassociated",
    }
    assert items[4]["provenance_reason"] == "after_round_end"
    assert items[5]["provenance_reason"] == "after_round_end"
    assert "provenance_reason" not in items[3]


def test_updated_at_never_promotes_an_old_item_into_the_round():
    old = _comment(1, "old review", "codex", "2026-10-04T03:00:00Z")
    old["updated_at"] = "2026-10-04T03:15:00Z"
    current = _comment(2, "this round", "codex", "2026-10-04T03:12:00Z")
    items = _items(_bounded({"issue_comments": [old, current]}))
    assert items[1]["provenance"] == "historical"
    assert items[2]["provenance"] == "current"


def test_review_after_the_round_end_is_not_current_and_keeps_its_reason():
    review = {
        "id": 8,
        "body": "late",
        "user": {"login": "codex"},
        "submitted_at": "2026-10-04T03:25:00Z",
        "commit_id": "a" * 40,
    }
    current = _comment(2, "this round", "codex", "2026-10-04T03:12:00Z")
    items = _items(_bounded({"issue_comments": [current], "reviews": [review]}))
    assert items[8]["provenance"] == "unassociated"
    assert items[8]["provenance_reason"] == "after_round_end"


def _review(id_, at, body="review"):
    return {
        "id": id_,
        "body": body,
        "user": {"login": "codex"},
        "submitted_at": at,
        "commit_id": "a" * 40,
    }


def _inline(id_, parent, at):
    return {
        "id": id_,
        "body": "finding",
        "user": {"login": "codex"},
        "pull_request_review_id": parent,
        "created_at": at,
        "path": "a.py",
        "line": 1,
    }


def test_inline_follows_a_confirmed_parent_but_not_past_the_round_end():
    state = {
        "reviews": [
            _review(10, "2026-10-04T03:05:00Z"),
            _review(11, "2026-10-04T03:12:00Z"),
        ],
        "inline_comments": [
            _inline(1, 10, "2026-10-04T03:12:30Z"),  # late inline on a past review
            _inline(2, 11, "2026-10-04T03:13:00Z"),
            _inline(3, 11, "2026-10-04T03:21:00Z"),  # parent ok, created after end
            _inline(4, 99, "2026-10-04T03:13:00Z"),  # unknown parent
        ],
    }
    inlines = _items(_bounded(state), "inline_comments")
    assert {i: inlines[i]["provenance"] for i in inlines} == {
        1: "historical",
        2: "current",
        3: "unassociated",
        4: "unassociated",
    }
    assert inlines[3]["provenance_reason"] == "after_round_end"


def test_inline_without_parent_uses_its_own_creation_time():
    inline = _inline(5, None, "2026-10-04T03:30:00Z")
    inline.pop("pull_request_review_id")
    current = _comment(2, "this round", "codex", "2026-10-04T03:12:00Z")
    state = {"issue_comments": [current], "inline_comments": [inline]}
    inlines = _items(_bounded(state), "inline_comments")
    assert inlines[5]["provenance"] == "unassociated"
    assert inlines[5]["provenance_reason"] == "after_round_end"


def test_comment_only_exclusion_does_not_hide_other_sections_with_the_same_id():
    state = {
        "issue_comments": [
            _comment(7, "@codex review", "codex", ROUND_START),
            _comment(2, "real summary", "codex", "2026-10-04T03:12:00Z"),
        ],
        "reviews": [_review(7, "2026-10-04T03:13:00Z", "review seven")],
        "inline_comments": [_inline(7, 7, "2026-10-04T03:13:30Z")],
    }
    result = _bounded(state, exclude_issue_comment_ids={7})
    assert sorted(_items(result)) == [2, 7]
    assert [i["id"] for i in result["inline_comments"]] == [7]
    legacy = _bounded(state, exclude_ids={7})
    assert sorted(_items(legacy)) == [2]  # the old shared set keeps hiding all three


def test_comment_only_exclusion_applies_to_activity_and_snapshot_helpers():
    comment = _comment(7, "Codex is working…", "codex", "2026-10-04T03:12:00Z")
    state = {"issue_comments": [comment], "reviews": [], "inline_comments": []}
    kept = acquisition._latest_bot_activity_item(
        state, "codex", None, exclude_issue_comment_ids={7}
    )
    assert kept is None


def test_old_in_progress_tracker_edited_later_does_not_block_the_round():
    tracker = _comment(1, "Codex is working…", "codex", "2026-10-04T02:00:00Z")
    tracker["updated_at"] = "2026-10-04T03:15:00Z"
    review = _review(9, "2026-10-04T03:14:00Z", "LGTM no issues")
    result = _bounded({"issue_comments": [tracker], "reviews": [review]})
    assert result["acquisition_status"] == "acquired"


def test_in_progress_tracker_inside_the_round_still_reports_in_progress():
    tracker = _comment(1, "Codex is working…", "codex", "2026-10-04T03:12:00Z")
    result = _bounded({"issue_comments": [tracker]})
    assert result["acquisition_status"] == "in_progress"


def test_in_progress_tracker_after_the_round_end_is_not_this_rounds_activity():
    tracker = _comment(1, "Codex is working…", "codex", "2026-10-04T03:30:00Z")
    review = _review(9, "2026-10-04T03:14:00Z", "LGTM no issues")
    result = _bounded({"issue_comments": [tracker], "reviews": [review]})
    assert result["acquisition_status"] == "acquired"


def test_telemetry_only_round_is_not_acquired():
    tracker = _comment(
        1, "**Codex finished** view job", "codex", "2026-10-04T03:12:00Z"
    )
    result = _bounded({"issue_comments": [tracker]})
    assert result["acquisition_status"] == "unavailable"


CODEX_TRACKER = (
    "<!-- codex-pull-request-review-summary -->\n"
    "| Review | Status | Commit | Review trigger |\n"
    "| --- | --- | --- | --- |\n"
    "| 📝 **Code Review** | 🔄 **{status}** since "
    '<relative-time datetime="2026-10-04T03:12:00Z">'
    "2026-10-04T03:12:00Z</relative-time> | `abc1234` | Manual request |\n"
    "<details><summary>About Codex</summary>Reviews are running.</details>"
)


def test_codex_running_tracker_holds_acquisition_open_with_partial_inline():
    tracker = _comment(
        1,
        CODEX_TRACKER.format(status="Running"),
        "chatgpt-codex-connector[bot]",
        "2026-10-04T03:12:00Z",
    )
    partial_inline = _inline(7, 2, "2026-10-04T03:13:00Z")
    partial_review = _review(8, "2026-10-04T03:13:30Z", "Partial review body.")
    result = _bounded(
        {
            "issue_comments": [tracker],
            "reviews": [partial_review],
            "inline_comments": [partial_inline],
        }
    )
    assert result["acquisition_status"] == "in_progress"
    assert [item["id"] for item in result["review_items"]] == [8]
    assert [item["id"] for item in result["inline_comments"]] == [7]


def test_codex_completed_tracker_only_is_not_review_content():
    tracker = _comment(
        1,
        CODEX_TRACKER.format(status="Completed"),
        "chatgpt-codex-connector[bot]",
        "2026-10-04T03:12:00Z",
    )
    result = _bounded({"issue_comments": [tracker]})
    assert result["acquisition_status"] == "unavailable"
    assert result["review_body"] == ""
    assert result["review_items"] == []


CODEX_BOT = "chatgpt-codex-connector[bot]"
SHA_A = "abc1234" + "0" * 33
SHA_B = "def5678" + "0" * 33


def _reused_tracker(status="Running", updated="2026-10-04T03:15:00Z", **kwargs):
    """Round 1's tracker (created long before) edited by a later round (#1274)."""
    tracker = _comment(
        1, CODEX_TRACKER.format(status=status), CODEX_BOT, "2026-10-04T03:00:00Z"
    )
    if updated is not None:
        tracker["updated_at"] = updated
    tracker.update(kwargs)
    return tracker


def test_codex_tracker_updated_at_or_after_the_round_end_does_not_block_it():
    """(a) #1210: a later edit belongs to a later round, not this closed one."""
    tracker = _reused_tracker(updated=ROUND_END)
    review = _review(9, "2026-10-04T03:14:00Z", "Current round review.")
    result = _bounded({"issue_comments": [tracker], "reviews": [review]})
    assert result["acquisition_status"] == "acquired"
    assert result["review_body"] == "Current round review."


def test_codex_tracker_for_another_commit_does_not_block_the_round():
    """(b) An explicitly different commit is not this request's activity."""
    tracker = _reused_tracker()  # `abc1234`
    review = _review(9, "2026-10-04T03:14:00Z", "Current round review.")
    result = _bounded(
        {"issue_comments": [tracker], "reviews": [review]}, requested_head_sha=SHA_B
    )
    assert result["acquisition_status"] == "acquired"


@pytest.mark.parametrize("requested", [SHA_A, None])
def test_codex_tracker_updated_inside_the_round_is_current_progress(requested):
    """(c) Running, commit matching or unknown: still in progress."""
    tracker = _reused_tracker()
    review = _review(9, "2026-10-04T03:14:00Z", "Partial review.")
    result = _bounded(
        {"issue_comments": [tracker], "reviews": [review]}, requested_head_sha=requested
    )
    assert result["acquisition_status"] == "in_progress"
    assert [item["id"] for item in result["review_items"]] == [9]


def test_reused_running_tracker_in_the_open_round_is_in_progress():
    tracker = _reused_tracker(updated="2026-10-04T03:12:00Z")
    result = acquisition.collect_review_state(
        {"issue_comments": [tracker]}, "codex", round_started_at=ROUND_START
    )
    assert result["acquisition_status"] == "in_progress"


def test_untouched_reused_tracker_is_not_progress_of_the_open_round():
    tracker = _reused_tracker(updated="2026-10-04T03:05:00Z")
    result = acquisition.collect_review_state(
        {"issue_comments": [tracker]}, "codex", round_started_at=ROUND_START
    )
    assert result["acquisition_status"] == "unavailable"


def test_reused_completed_tracker_with_a_current_review_is_acquired():
    tracker = _reused_tracker("Completed", updated="2026-10-04T03:16:00Z")
    review = _review(9, "2026-10-04T03:15:30Z", "Real findings.")
    result = acquisition.collect_review_state(
        {"issue_comments": [tracker], "reviews": [review]},
        "codex",
        round_started_at=ROUND_START,
    )
    assert result["acquisition_status"] == "acquired"
    assert result["review_body"] == "Real findings."


def test_reused_completed_tracker_alone_is_unavailable_not_review_content():
    tracker = _reused_tracker("Completed", updated="2026-10-04T03:16:00Z")
    result = acquisition.collect_review_state(
        {"issue_comments": [tracker]}, "codex", round_started_at=ROUND_START
    )
    assert result["acquisition_status"] == "unavailable"
    assert result["review_items"] == [] and result["review_body"] == ""


def test_later_edit_of_an_old_review_is_still_not_current_evidence():
    """The updated_at exception covers the tracker only (#1210)."""
    old = _review(9, "2026-10-04T03:00:00Z", "Old review.")
    old["updated_at"] = "2026-10-04T03:16:00Z"
    result = acquisition.collect_review_state(
        {"issue_comments": [], "reviews": [old]}, "codex", round_started_at=ROUND_START
    )
    assert result["acquisition_status"] == "unavailable"


@pytest.mark.parametrize("with_parent", [False, True])
def test_current_inline_after_a_completed_reused_tracker_is_acquired(with_parent):
    tracker = _reused_tracker("Completed", updated="2026-10-04T03:16:00Z")
    state = {"issue_comments": [tracker]}
    if with_parent:
        parent = _review(2, "2026-10-04T03:15:00Z", "")
        state["reviews"] = [parent]
    state["inline_comments"] = [
        _inline(7, 2 if with_parent else None, "2026-10-04T03:15:00Z")
    ]
    result = acquisition.collect_review_state(
        state, "codex", round_started_at=ROUND_START
    )
    assert result["acquisition_status"] == "acquired"
    assert [item["provenance"] for item in result["inline_comments"]] == ["current"]


def test_snapshot_detects_a_same_length_tracker_edit_without_exposing_the_body():
    running = _reused_tracker("Running")
    pending = _reused_tracker("Pending")
    assert len(running["body"]) == len(pending["body"])
    state = {"issue_comments": [running], "reviews": [], "inline_comments": []}
    other = {"issue_comments": [pending], "reviews": [], "inline_comments": []}
    before = acquisition._build_snapshot(state, "codex")
    after = acquisition._build_snapshot(other, "codex")
    assert before != after
    assert all("Running" not in value for value in before.values())


def test_newer_completed_codex_tracker_replaces_older_running_tracker():
    running = _comment(
        1,
        CODEX_TRACKER.format(status="Running"),
        "chatgpt-codex-connector[bot]",
        "2026-10-04T03:12:00Z",
    )
    completed = _comment(
        2,
        CODEX_TRACKER.format(status="Completed"),
        "chatgpt-codex-connector[bot]",
        "2026-10-04T03:13:00Z",
    )
    result = _bounded({"issue_comments": [running, completed]})
    assert result["acquisition_status"] == "unavailable"
    assert result["review_items"] == []


@pytest.mark.parametrize(
    "body",
    [
        CODEX_TRACKER.format(status="Retrying"),
        "<!-- codex-pull-request-review-summary -->",
    ],
)
def test_unknown_codex_tracker_is_not_review_content(body):
    tracker = _comment(1, body, "chatgpt-codex-connector[bot]", "2026-10-04T03:12:00Z")
    result = _bounded({"issue_comments": [tracker]})
    assert result["acquisition_status"] == "unavailable"
    assert result["review_body"] == ""
    assert result["review_items"] == []


def test_codex_completed_tracker_does_not_hide_real_review_content():
    tracker = _comment(
        1,
        CODEX_TRACKER.format(status="Completed"),
        "chatgpt-codex-connector[bot]",
        "2026-10-04T03:13:00Z",
    )
    real_review = _review(9, "2026-10-04T03:14:00Z", "Actual review findings.")
    result = _bounded({"issue_comments": [tracker], "reviews": [real_review]})
    assert result["acquisition_status"] == "acquired"
    assert result["review_body"] == "Actual review findings."
    assert all(item["id"] != 1 for item in result["review_items"])


def test_codex_completed_tracker_with_real_inline_review_is_acquired():
    tracker = _comment(
        1,
        CODEX_TRACKER.format(status="Completed"),
        "chatgpt-codex-connector[bot]",
        "2026-10-04T03:13:00Z",
    )
    review = _review(9, "2026-10-04T03:14:00Z", "")
    inline = _inline(10, 9, "2026-10-04T03:14:30Z")
    result = _bounded(
        {
            "issue_comments": [tracker],
            "reviews": [review],
            "inline_comments": [inline],
        }
    )
    assert result["acquisition_status"] == "acquired"
    assert "see 1 inline comment(s)" in result["review_body"]
    assert [item["id"] for item in result["inline_comments"]] == [10]


def test_unbounded_collection_is_unchanged_by_the_new_parameters():
    state = _reviewed_state([_comment(3, "Judgments for round 1 (no marker)")])
    plain = acquisition.collect_review_state(state, "claude", exclude_ids={1})
    explicit = acquisition.collect_review_state(
        state,
        "claude",
        exclude_ids={1},
        round_ended_at="",
        exclude_issue_comment_ids=None,
    )
    assert plain == explicit
    assert all("provenance_reason" not in i for i in plain["review_items"])

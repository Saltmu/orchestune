"""Reusable review judgment contract tests."""

import pytest
import yaml

from orchestune.review.judgment import (
    judgment_digest,
    parse_judgments,
    validate_coverage,
    validate_previous_round_reply,
)
from orchestune.review.markers import (
    build_trigger_body,
    derive_review_target,
    parse_head_marker,
    review_head_marker,
    review_selection_marker,
)
from orchestune.review.rounds import previous_round_window


def document(findings=None, round_num=1):
    return (
        "```orchestune-review-judgments\n"
        + yaml.safe_dump({"round": round_num, "findings": findings or []})
        + "```\n"
    )


def finding(**updates):
    return {
        "source": "inline_comment:12",
        "location": "app.py:8",
        "judgment": "adopt",
        "status": "resolved",
        "basis": "contract regression",
        "evidence": "commit abc",
        **updates,
    }


def test_valid_judgments_and_zero_findings():
    assert parse_judgments(document([finding()]))["round"] == 1
    assert parse_judgments(document())["findings"] == []


@pytest.mark.parametrize(
    "changes",
    [
        {"judgment": "pass"},
        {"status": "done"},
        {"basis": ""},
        {"source": ""},
        {"evidence": []},
    ],
)
def test_invalid_judgment_fields(changes):
    row = finding()
    row.update(changes)
    with pytest.raises(ValueError):
        parse_judgments(document([row]))


def test_missing_and_ambiguous_blocks_rejected():
    with pytest.raises(ValueError):
        parse_judgments("LGTM")
    with pytest.raises(ValueError):
        parse_judgments(document() + document())


def test_coverage_checks_current_sources_and_round():
    result = {
        "round": 1,
        "review_items": [
            {
                "id": 7,
                "kind": "issue_comment",
                "body": "finding",
                "provenance": "current",
            },
            {"id": 8, "kind": "review", "body": "old", "provenance": "historical"},
        ],
        "inline_comments": [
            {"id": 12, "body": "bug", "provenance": "current"},
        ],
    }
    with pytest.raises(ValueError, match="issue_comment:7"):
        validate_coverage(parse_judgments(document([finding()])), result)
    rows = [
        finding(),
        {
            **finding(),
            "source": "issue_comment:7",
            "judgment": "decline",
            "status": "declined",
        },
    ]
    validate_coverage(parse_judgments(document(rows)), result)
    with pytest.raises(ValueError, match="round"):
        validate_coverage(parse_judgments(document(rows, 2)), result)


def test_markers_validate_and_roundtrip_head():
    sha = "a" * 40
    assert parse_head_marker(review_head_marker(sha)) == sha
    assert parse_head_marker("legacy trigger") is None
    assert (
        parse_head_marker(review_head_marker(sha) + "\n" + review_head_marker("b" * 40))
        is None
    )
    assert (
        review_selection_marker("skip", sha)
        == f"<!-- orchestune:review-selection reviewer=skip head={sha} -->"
    )
    with pytest.raises(ValueError):
        review_head_marker("unknown")


@pytest.mark.parametrize("round_num", [0, -1, True, "1", None])
def test_invalid_rounds(round_num):
    with pytest.raises(ValueError):
        parse_judgments(document(round_num=round_num))


def test_deferred_requires_basis_and_fields():
    row = finding()
    row.update(status="deferred", judgment="decline", basis="")
    with pytest.raises(ValueError, match="basis"):
        parse_judgments(document([row]))
    row["basis"] = "outside acceptance criteria; follow-up issue 123"
    del row["location"]
    with pytest.raises(ValueError, match="location"):
        parse_judgments(document([row]))


def test_multiple_findings_in_same_source_are_allowed():
    first = finding()
    second = {
        **finding(),
        "location": "app.py:19",
        "basis": "second independent regression",
    }
    parsed = parse_judgments(document([first, second]))
    assert len(parsed["findings"]) == 2


def test_conflicting_review_commits_do_not_fall_back_to_trigger():
    from orchestune.review.markers import derive_review_target

    items = [
        {"kind": "review", "provenance": "current", "commit_id": sha * 40}
        for sha in ("a", "b")
    ]
    assert derive_review_target(items, "a" * 40, "a" * 40) == (None, "unknown")


# --- #1210: target SHA derivation never falls back past bad evidence -----------

HEAD = "a" * 40


def _review(commit, provenance="current"):
    return {"kind": "review", "provenance": provenance, "commit_id": commit}


def _comment_item(provenance="current"):
    return {"kind": "issue_comment", "provenance": provenance}


def test_single_valid_review_commit_is_the_target_even_when_head_moved():
    assert derive_review_target([_review("b" * 40)], HEAD, HEAD) == (
        "b" * 40,
        "review_commit",
    )


def test_review_commit_is_lowercased_and_historical_commits_are_ignored():
    items = [_review("B" * 40), _review("c" * 40, provenance="historical")]
    assert derive_review_target(items, None, None) == ("b" * 40, "review_commit")


@pytest.mark.parametrize(
    "commits", [["abc"], ["g" * 40], ["a" * 40, "abc"], ["a" * 41], [5]]
)
def test_invalid_review_commit_is_unknown_and_never_falls_back(commits):
    items = [_review(commit) for commit in commits]
    items.append(_comment_item())
    assert derive_review_target(items, HEAD, HEAD) == (None, "unknown")


def test_comment_only_round_is_verified_only_by_matching_heads():
    assert derive_review_target([_comment_item()], HEAD, HEAD) == (
        HEAD,
        "trigger_head_verified",
    )
    assert derive_review_target([_comment_item()], HEAD, "b" * 40) == (None, "unknown")
    assert derive_review_target([_comment_item()], None, HEAD) == (None, "unknown")
    assert derive_review_target([_comment_item()], HEAD, None) == (None, "unknown")
    assert derive_review_target([_comment_item()], "abc", "abc") == (None, "unknown")


def test_inline_only_or_empty_round_has_no_target():
    assert derive_review_target([], HEAD, HEAD) == (None, "unknown")
    assert derive_review_target([_review(None)], HEAD, HEAD) == (None, "unknown")
    assert derive_review_target([_comment_item("historical")], HEAD, HEAD) == (
        None,
        "unknown",
    )


# --- #1210: shared previous-round reply validation ------------------------------


def _trigger(id_, round_num, minute, bot="claude"):
    return {
        "id": id_,
        "body": build_trigger_body("", bot, round_num, HEAD),
        "created_at": f"2026-10-04T03:{minute:02d}:00Z",
        "user": {"login": "someone"},
    }


def _bot_comment(id_, minute, body="finding", login="claude[bot]"):
    return {
        "id": id_,
        "body": body,
        "created_at": f"2026-10-04T03:{minute:02d}:00Z",
        "user": {"login": login},
    }


def _state(*comments, reviews=(), inline=()):
    return {
        "issue_comments": list(comments),
        "reviews": list(reviews),
        "inline_comments": list(inline),
    }


def _window(state, next_round=2, strict=True):
    return previous_round_window(state["issue_comments"], next_round, strict=strict)


def test_valid_reply_covers_round_sources_and_reports_digests():
    state = _state(_trigger(1, 1, 0), _bot_comment(2, 5))
    reply = validate_previous_round_reply(
        state, _window(state), document([finding(source="issue_comment:2")])
    )
    assert (reply.previous_round, reply.reviewer) == (1, "claude")
    assert reply.sources == ("issue_comment:2",)
    assert len(reply.source_digest) == len(reply.judgment_digest) == 64
    assert reply.counts == {"adopt": 1, "resolved": 1}


def test_judgment_digest_ignores_row_order_and_whitespace():
    first = finding(source="issue_comment:2")
    second = {**finding(), "source": "issue_comment:3"}
    free_text = {"source", "location", "basis", "evidence"}
    padded = {
        key: f"  {value} " if key in free_text else value
        for key, value in second.items()
    }
    one = judgment_digest(parse_judgments(document([first, second])))
    two = judgment_digest(parse_judgments(document([padded, first])))
    assert one == two


def test_missing_source_coverage_is_rejected_by_name():
    state = _state(_trigger(1, 1, 0), _bot_comment(2, 5), _bot_comment(3, 6))
    with pytest.raises(ValueError, match="issue_comment:3"):
        validate_previous_round_reply(
            state, _window(state), document([finding(source="issue_comment:2")])
        )


def test_clean_summary_still_needs_a_row_and_empty_table_is_only_for_no_content():
    clean = _state(_trigger(1, 1, 0), _bot_comment(2, 5, "No issues found."))
    with pytest.raises(ValueError, match="issue_comment:2"):
        validate_previous_round_reply(clean, _window(clean), document())
    row = finding(source="issue_comment:2", judgment="already_addressed")
    validate_previous_round_reply(clean, _window(clean), document([row]))
    empty = _state(_trigger(1, 1, 0))
    assert (
        validate_previous_round_reply(empty, _window(empty), document()).sources == ()
    )


def test_telemetry_only_previous_round_accepts_an_explicit_empty_table():
    tracker = _bot_comment(2, 5, "**Claude finished** view job")
    state = _state(_trigger(1, 1, 0), tracker)
    validate_previous_round_reply(state, _window(state), document())


def test_wrong_round_missing_or_duplicate_table_is_rejected():
    state = _state(_trigger(1, 1, 0))
    window = _window(state)
    with pytest.raises(ValueError, match="round"):
        validate_previous_round_reply(state, window, document(round_num=2))
    with pytest.raises(ValueError, match="exactly one"):
        validate_previous_round_reply(state, window, "no table")
    with pytest.raises(ValueError, match="exactly one"):
        validate_previous_round_reply(state, window, document() + document())


def test_same_source_may_carry_several_findings():
    state = _state(_trigger(1, 1, 0), _bot_comment(2, 5))
    rows = [
        finding(source="issue_comment:2"),
        {**finding(source="issue_comment:2"), "location": "b.py:1"},
    ]
    reply = validate_previous_round_reply(state, _window(state), document(rows))
    assert reply.sources == ("issue_comment:2",)


def test_later_round_findings_never_leak_into_previous_round_coverage():
    next_trigger = _trigger(102, 2, 10, bot="codex")
    state = _state(
        _trigger(1, 1, 0),
        _bot_comment(2, 5),
        next_trigger,
        _bot_comment(3, 15, "round two finding", login="codex"),
        _bot_comment(4, 16, "late claude finding"),
    )
    window = _window(state)
    assert window.ended_at == "2026-10-04T03:10:00Z"
    reply = validate_previous_round_reply(
        state, window, document([finding(source="issue_comment:2")])
    )
    assert reply.sources == ("issue_comment:2",)


def test_review_reply_marked_comment_is_not_a_required_source():
    marked = "<!-- orchestune:review-reply -->\nRound 1 judgments"
    state = _state(_trigger(1, 1, 0), _bot_comment(2, 5), _bot_comment(3, 6, marked))
    validate_previous_round_reply(
        state, _window(state), document([finding(source="issue_comment:2")])
    )


def test_a_source_without_id_or_url_cannot_be_covered():
    anonymous = {"body": "finding", "created_at": "2026-10-04T03:05:00Z"}
    anonymous["user"] = {"login": "claude[bot]"}
    state = _state(_trigger(1, 1, 0), anonymous)
    with pytest.raises(ValueError, match="source id"):
        validate_previous_round_reply(state, _window(state), document())


def test_reviewer_change_judges_the_previous_reviewers_round():
    state = _state(
        _trigger(1, 1, 0, bot="codex"),
        _bot_comment(2, 5, "codex finding", login="chatgpt-codex-connector[bot]"),
        _bot_comment(3, 6, "claude noise", login="claude[bot]"),
    )
    reply = validate_previous_round_reply(
        state, _window(state), document([finding(source="issue_comment:2")])
    )
    assert reply.reviewer == "codex"


def test_pre_post_validation_uses_everything_up_to_the_snapshot():
    state = _state(_trigger(1, 1, 0), _bot_comment(2, 50))
    assert _window(state).ended_at is None
    with pytest.raises(ValueError, match="issue_comment:2"):
        validate_previous_round_reply(state, _window(state), document())


def test_lenient_window_validates_like_the_online_path():
    state = _state(_trigger(1, 1, 0), _bot_comment(2, 5))
    window = _window(state, strict=False)
    validate_previous_round_reply(
        state, window, document([finding(source="issue_comment:2")])
    )

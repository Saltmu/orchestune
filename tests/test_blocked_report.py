"""Known-marker classification for blocked-human-review comments."""

import re

from orchestune.blocked_report import (
    CATEGORIES,
    BlockedIssueTriage,
    build_triage_entry,
    classify_comment,
    classify_comments,
    recommended_action,
)
from orchestune.infra.cli_guidance import command_is_resolved

_PLACEHOLDER = re.compile(r"<[A-Za-z0-9_-]+>")

_POSTED = {
    "child-review-gate": "<!-- orchestune:child-review-gate digest=abc -->",
    "semantic-review": "<!-- orchestune:semantic-review-state:v1 {} -->",
    "token-limit": "<!-- orchestune:completion-token-limit completion-1 -->",
    "finalization-escalation": (
        "<!-- orchestune:child-branch-finalization-escalation:v1 "
        "issue=9 sha=abc gen=2 -->"
    ),
    "final-acceptance-review": (
        "<!-- orchestune:final-acceptance-review:v1 "
        "head=0123456789abcdef0123456789abcdef01234567 -->"
    ),
    "outcome": "<!-- orchestune:outcome -->",
}


def test_posted_forms_match_their_category():
    assert classify_comment(
        "<!-- orchestune:semantic-review-override:v1 {} -->"
    ) == frozenset({"semantic-review"})
    assert classify_comment(
        "<!-- orchestune:semantic-review-retry:v1 {} -->"
    ) == frozenset({"semantic-review"})
    assert classify_comment("<!-- orchestune:outcome-->") == frozenset({"outcome"})
    for category, line in _POSTED.items():
        assert classify_comment(line) == frozenset({category})


def test_one_comment_counts_each_category_once():
    body = "\n".join(
        (
            _POSTED["outcome"],
            "<!-- orchestune:outcome -->",
            _POSTED["semantic-review"],
            "<!-- orchestune:semantic-review-override:v1 {} -->",
        )
    )
    assert classify_comment(body) == frozenset({"outcome", "semantic-review"})


def test_empty_prose_quotes_code_and_unknown_markers_are_unrecognized():
    samples = [
        "",
        "plain text about orchestune:outcome",
        "> <!-- orchestune:outcome -->",
        "    <!-- orchestune:outcome -->",
        "```\n<!-- orchestune:outcome -->\n```",
        "see <!-- orchestune:outcome --> here",
        "<!-- orchestune:outcome",
        "<!-- orchestune:outcome-extra -->",
        "<!-- orchestune:semantic-review-state:v2 {} -->",
        "<!-- orchestune:child-branch-finalization:v1 -->",
        "- <!-- orchestune:outcome -->",
    ]
    for body in samples:
        assert classify_comment(body) == frozenset({"unrecognized"}), body


def test_crlf_comment_still_matches():
    body = "<!-- orchestune:outcome -->\r\nnext line\r\n"
    assert classify_comment(body) == frozenset({"outcome"})


def test_classify_comments_sums_comments_and_keeps_every_category():
    counts = classify_comments(
        [
            _POSTED["outcome"] + "\n" + _POSTED["token-limit"],
            "not a marker",
            _POSTED["outcome"],
        ]
    )
    assert list(counts) == list(CATEGORIES)
    assert counts["outcome"] == 2
    assert counts["token-limit"] == 1
    assert counts["unrecognized"] == 1
    assert counts["child-review-gate"] == 0
    assert classify_comments(()) == {category: 0 for category in CATEGORIES}


def _counts(**overrides: int) -> dict[str, int]:
    counts = {category: 0 for category in CATEGORIES}
    counts.update(overrides)
    return counts


def test_token_limit_recommends_recover_preview_only():
    command, note = recommended_action(17, _counts(**{"token-limit": 1}))
    assert command == ("orchestune", "recover", "--issue", "17")
    assert "--apply" not in command
    assert "preview" in note
    assert command_is_resolved(command)


def test_token_limit_wins_over_other_categories_and_missing_comments():
    counts = _counts(**{"token-limit": 1, "semantic-review": 3, "outcome": 2})
    command, note = recommended_action(4, counts, has_comments=False)
    assert command[0] == "orchestune"
    assert "token-limit" in note
    assert "secret body" not in note


def test_each_other_category_views_comments_with_its_note():
    expected = {
        "child-review-gate": "child-review-gate",
        "semantic-review": "orchestune review status --pr",
        "finalization-escalation": "finalization",
        "final-acceptance-review": "final-acceptance-review",
        "outcome": "Outcome",
        "unrecognized": "unrecognized",
    }
    for category, needle in expected.items():
        command, note = recommended_action(9, _counts(**{category: 1}))
        assert command == ("gh", "issue", "view", "9", "--comments")
        assert needle in note
        assert command_is_resolved(command)
        assert not any(_PLACEHOLDER.search(part) for part in command)


def test_multiple_non_token_categories_follow_categories_order():
    counts = _counts(**{"outcome": 2, "semantic-review": 1})
    _, note = recommended_action(3, counts)
    assert "orchestune review status --pr" in note


def test_no_comments_and_no_markers_view_comments_without_placeholders():
    empty, empty_note = recommended_action(8, _counts(), has_comments=False)
    unmarked, unmarked_note = recommended_action(8, _counts())
    view = ("gh", "issue", "view", "8", "--comments")
    assert empty == unmarked == view
    assert "コメント" in empty_note
    assert "既知" in unmarked_note
    assert "<" not in empty_note and "<" not in unmarked_note


def test_build_triage_entry_keeps_inputs_and_resolved_command():
    counts = _counts(**{"semantic-review": 1})
    entry = build_triage_entry(11, "title", counts, has_comments=True)
    assert isinstance(entry, BlockedIssueTriage)
    assert entry.issue_number == 11
    assert entry.title == "title"
    assert dict(entry.counts) == counts
    assert entry.has_comments is True
    assert entry.recommended_command == ("gh", "issue", "view", "11", "--comments")
    assert "orchestune review status --pr" in entry.recommended_note
    counts["semantic-review"] = 0
    assert entry.counts["semantic-review"] == 1

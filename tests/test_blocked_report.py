"""Known-marker classification for blocked-human-review comments."""

from orchestune.blocked_report import CATEGORIES, classify_comment, classify_comments

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

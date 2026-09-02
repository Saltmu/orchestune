"""Fixes the behavior of `.github/workflows/claude-code-review.yml`'s job
`if:` condition and `allowed_bots` grant (Issue #692).

`claude-code-action@v1` rejects any non-human actor unless it is present in
`allowed_bots`, and does so by hard-failing the job rather than skipping it.
The workflow's `if:` condition is therefore the only place we can turn an
unauthorized/unmarked bot-authored trigger comment into a clean `skipped`
job instead of a `failure`. This test evaluates the *actual* `if:` expression
string from the YAML file (not a hand-copied duplicate of its logic) against
the input/output table from the Issue's verification plan, so the test
breaks if the expression's real behavior ever drifts from that table.
"""

from __future__ import annotations

import os
import re
from typing import Any, cast

import pytest
import yaml

WORKFLOW_PATH = os.path.join(
    os.path.dirname(__file__), "..", ".github", "workflows", "claude-code-review.yml"
)

ORCHESTUNE_TRIGGER_MARKER = "<!-- orchestune:review-trigger bot=claude -->"


def _load_claude_review_job() -> dict[str, Any]:
    with open(WORKFLOW_PATH, encoding="utf-8") as f:
        workflow = yaml.safe_load(f)
    return cast(dict[str, Any], workflow["jobs"]["claude-review"])


def _load_if_expression() -> str:
    return cast(str, _load_claude_review_job()["if"])


def _gha_contains(haystack: str | None, needle: str) -> bool:
    return needle in (haystack or "")


def _gha_ends_with(value: str | None, suffix: str) -> bool:
    return (value or "").endswith(suffix)


def _evaluate_if_expression(expr: str, context: dict[str, Any]) -> bool:
    """Evaluate a (small, known) subset of GitHub Actions expression syntax:
    `&&`, `||`, `!`, `==`, `!=`, parentheses, string/None literals, and the
    `contains()` / `endsWith()` functions, against dotted `github.*` context
    paths substituted from `context`.
    """
    working = expr.replace("\n", " ")

    # Protect the expression's own single-quoted string literals (e.g. the
    # HTML-comment marker text, which contains `!--`) before doing any
    # operator-level rewrites, so those rewrites only ever touch actual GHA
    # syntax and never literal content.
    literals: list[str] = []

    def _stash_literal(match: re.Match[str]) -> str:
        literals.append(match.group(0))
        return f"__GHA_STR_{len(literals) - 1}__"

    working = re.sub(r"'[^']*'", _stash_literal, working)

    working = re.sub(r"\bnull\b", "None", working)
    # GHA negation is `!expr` (no space); don't touch `!=`.
    working = re.sub(r"!(?!=)", "not ", working)
    working = working.replace("&&", " and ").replace("||", " or ")

    # Substitute dotted context paths with their Python repr, longest path
    # first so e.g. `github.event.comment.body` isn't clobbered by a
    # shorter overlapping prefix substitution.
    for path in sorted(context, key=len, reverse=True):
        # Use a replacement function, not a plain string: re.sub() reinterprets
        # backslash escapes (e.g. `\n`) in a string `repl`, which would mangle
        # repr() output for any body containing a literal newline.
        def _replace_with(
            _match: re.Match[str], _value: str = repr(context[path])
        ) -> str:
            return _value

        working = re.sub(
            rf"(?<![\w.]){re.escape(path)}(?![\w.])", _replace_with, working
        )

    for index, literal in enumerate(literals):
        working = working.replace(f"__GHA_STR_{index}__", literal)

    remaining_paths = re.findall(r"\bgithub(?:\.[\w]+)+\b", working)
    assert not remaining_paths, f"context path(s) not substituted: {remaining_paths}"

    return bool(
        eval(  # noqa: S307 - fixed, test-authored expression text only
            working,
            {"__builtins__": {}},
            {"contains": _gha_contains, "endsWith": _gha_ends_with, "None": None},
        )
    )


def _context(
    *,
    event_name: str,
    actor: str,
    body: str,
    issue_is_pr: bool = True,
) -> dict[str, Any]:
    return {
        "github.event_name": event_name,
        "github.actor": actor,
        "github.event.comment.body": body,
        "github.event.issue.pull_request": {"url": "x"} if issue_is_pr else None,
    }


HUMAN_REVIEW_COMMENT = "@claude review"
BOT_TRIGGER_COMMENT = f"@claude review\n\n{ORCHESTUNE_TRIGGER_MARKER}"
BOT_TRIGGER_COMMENT_NO_MARKER = "@claude review"


@pytest.mark.parametrize(
    ("description", "context", "expected"),
    [
        (
            "human actor + '@claude review' on a PR issue comment runs",
            _context(
                event_name="issue_comment", actor="Saltmu", body=HUMAN_REVIEW_COMMENT
            ),
            True,
        ),
        (
            "human actor + '@claude review' inline PR review comment runs",
            _context(
                event_name="pull_request_review_comment",
                actor="Saltmu",
                body=HUMAN_REVIEW_COMMENT,
            ),
            True,
        ),
        (
            "human actor commenting on a non-PR issue does not run",
            _context(
                event_name="issue_comment",
                actor="Saltmu",
                body=HUMAN_REVIEW_COMMENT,
                issue_is_pr=False,
            ),
            False,
        ),
        (
            "claude[bot] + Orchestune trigger marker on a PR issue comment runs",
            _context(
                event_name="issue_comment",
                actor="claude[bot]",
                body=BOT_TRIGGER_COMMENT,
            ),
            True,
        ),
        (
            "claude[bot] + Orchestune trigger marker inline review comment runs",
            _context(
                event_name="pull_request_review_comment",
                actor="claude[bot]",
                body=BOT_TRIGGER_COMMENT,
            ),
            True,
        ),
        (
            "claude[bot] without the Orchestune trigger marker is skipped",
            _context(
                event_name="issue_comment",
                actor="claude[bot]",
                body=BOT_TRIGGER_COMMENT_NO_MARKER,
            ),
            False,
        ),
        (
            "claude[bot]'s own track_progress/final comment (no marker) is skipped, "
            "preventing self-recursion",
            _context(
                event_name="issue_comment",
                actor="claude[bot]",
                body="Code review complete. @claude review coverage looks good.",
            ),
            False,
        ),
        (
            "claude[bot]'s own inline finding (no marker) is skipped, "
            "preventing self-recursion",
            _context(
                event_name="pull_request_review_comment",
                actor="claude[bot]",
                body="Please have @claude review this follow-up separately.",
            ),
            False,
        ),
        (
            "an unauthorized bot with the trigger marker is skipped",
            _context(
                event_name="issue_comment",
                actor="github-actions[bot]",
                body=BOT_TRIGGER_COMMENT,
            ),
            False,
        ),
        (
            "chatgpt-codex-connector[bot] is not in Claude's allow-list and is skipped",
            _context(
                event_name="issue_comment",
                actor="chatgpt-codex-connector[bot]",
                body=BOT_TRIGGER_COMMENT,
            ),
            False,
        ),
        (
            "an unauthorized bot without the marker is skipped",
            _context(
                event_name="issue_comment",
                actor="github-actions[bot]",
                body=BOT_TRIGGER_COMMENT_NO_MARKER,
            ),
            False,
        ),
        (
            "comment missing the review trigger phrase never runs, human or bot",
            _context(event_name="issue_comment", actor="Saltmu", body="just a comment"),
            False,
        ),
    ],
)
def test_claude_review_if_expression(
    description: str, context: dict[str, Any], expected: bool
) -> None:
    expr = _load_if_expression()
    assert _evaluate_if_expression(expr, context) is expected, description


def test_allowed_bots_is_minimal_claude_only() -> None:
    job = _load_claude_review_job()
    steps = job["steps"]
    action_step = next(
        s
        for s in steps
        if s.get("uses", "").startswith("anthropics/claude-code-action")
    )
    allowed_bots = action_step["with"]["allowed_bots"]

    assert allowed_bots == "claude[bot]", (
        "allowed_bots must be the minimal 'claude[bot]' grant, not '*' or a "
        "broader list (see anthropics/claude-code-action docs/security.md: "
        "allowed bots are not checked for repository write access)"
    )

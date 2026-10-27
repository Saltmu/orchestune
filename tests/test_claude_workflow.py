import os

import yaml

WORKFLOW_PATH = os.path.join(
    os.path.dirname(__file__), "..", ".github", "workflows", "claude.yml"
)


def _load_workflow():
    with open(WORKFLOW_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_claude_workflow_does_not_run_on_pull_request_open():
    triggers = _load_workflow()[True]  # PyYAML parses the bare `on` key as True

    assert "pull_request" not in triggers
    assert "pull_request_target" not in triggers


def test_claude_workflow_accepts_review_mention_on_pull_request_comments():
    condition = _load_workflow()["jobs"]["claude"]["if"]

    # `@claude review` comments must not be excluded (issue #1455): the dedicated
    # review workflow that used to own them was removed in #1418.
    assert "'review'" not in condition
    assert "github.event_name == 'issue_comment'" in condition
    assert "github.event_name == 'pull_request_review_comment'" in condition


REVIEW_TRIGGER_MARKER = "<!-- orchestune:review-trigger bot=claude -->"


def _action_inputs():
    steps = _load_workflow()["jobs"]["claude"]["steps"]
    (step,) = (s for s in steps if s.get("id") == "claude")
    return step["with"]


def test_claude_workflow_allows_only_marked_claude_bot_triggers():
    condition = _load_workflow()["jobs"]["claude"]["if"]

    # Bots are skipped unless they are exactly claude[bot] and carry the marker
    # that wait_for_review.py stamps; the action's own comments never carry it,
    # so they cannot retrigger the job (issue #1457, see #692).
    assert "!endsWith(github.actor, '[bot]')" in condition
    assert "github.actor == 'claude[bot]'" in condition
    assert REVIEW_TRIGGER_MARKER in condition


def test_claude_workflow_allowed_bots_is_limited_to_claude_bot():
    assert _action_inputs()["allowed_bots"] == "claude[bot]"

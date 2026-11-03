import os

import yaml

WORKFLOW_PATH = os.path.join(
    os.path.dirname(__file__), "..", ".github", "workflows", "grok.yml"
)


def _load_workflow():
    with open(WORKFLOW_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _job():
    return _load_workflow()["jobs"]["grok-review"]


def _steps():
    return _job()["steps"]


def test_grok_workflow_triggers_only_on_normal_pr_comment_creation():
    triggers = _load_workflow()[True]  # PyYAML parses the bare `on` key as True

    # Inline review comments are excluded: fork PR review-comment runs get no
    # secrets, so the API step could not run (issue #1599, PR #1608 review).
    assert set(triggers) == {"issue_comment"}
    assert triggers["issue_comment"] == {"types": ["created"]}


def test_grok_workflow_ignores_inline_review_comments():
    condition = _job()["if"]

    assert "pull_request_review_comment" not in str(_load_workflow())
    assert "pull_request_review_comment" not in condition


def test_grok_workflow_does_not_run_on_pull_request_or_push():
    triggers = _load_workflow()[True]

    for name in (
        "pull_request",
        "pull_request_target",
        "pull_request_review",
        "push",
        "issues",
    ):
        assert name not in triggers


def test_grok_workflow_default_permissions_are_empty():
    assert _load_workflow()["permissions"] == {}


def test_grok_job_permissions_grant_only_pull_request_write():
    assert _job()["permissions"] == {"pull-requests": "write"}


def test_grok_job_condition_requires_mention_on_a_pull_request_comment():
    condition = _job()["if"]

    assert "contains(github.event.comment.body, '@grok review')" in condition
    assert "github.event.issue.pull_request" in condition
    assert "!endsWith(github.actor, '[bot]')" in condition
    for association in ("OWNER", "MEMBER", "COLLABORATOR"):
        assert association in condition


def test_grok_workflow_serializes_reviews_per_pull_request():
    concurrency = _load_workflow()["concurrency"]

    assert concurrency["cancel-in-progress"] is False
    assert "github.event.issue.number" in concurrency["group"]


def test_grok_job_has_timeout():
    assert _job()["timeout-minutes"] == 10


def test_grok_workflow_never_checks_out_repository():
    for step in _steps():
        assert "actions/checkout" not in str(step.get("uses", ""))


def test_grok_workflow_passes_event_values_through_env_not_run():
    for step in _steps():
        run = step.get("run", "")
        assert "github.event.comment.body" not in run
        assert "secrets." not in run
        assert "github.event.issue" not in run
        assert "github.event.pull_request" not in run


def test_grok_api_key_is_only_exposed_to_the_api_call_step():
    steps = _steps()
    holders = [
        i
        for i, step in enumerate(steps)
        if "secrets.XAI_API_KEY" in str(step.get("env", {}))
    ]

    assert len(holders) == 1
    api_step = steps[holders[0]]
    assert "curl" in api_step["run"]
    assert "XAI_API_KEY" not in " ".join(
        str(s.get("run", "")) for i, s in enumerate(steps) if i != holders[0]
    )


def test_grok_workflow_checks_write_permission_before_calling_api():
    names = [s.get("name", "") for s in _steps()]
    permission_steps = [i for i, s in enumerate(_steps()) if "collaborators" in str(s)]
    api_steps = [i for i, s in enumerate(_steps()) if "curl" in str(s.get("run", ""))]

    assert permission_steps, "a collaborator permission check step is required"
    assert permission_steps[0] < api_steps[0], names

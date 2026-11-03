"""Final acceptance requests stay advisory, bounded and distinct from review rounds."""

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from orchestune.dispatch.semantic_review.triggers import restore_round_triggers
from orchestune.integrator.final_acceptance_review import (
    PayloadBudgetExceeded,
    extract_acceptance_sections,
    final_acceptance_marker,
    render_final_acceptance_review,
    request_final_acceptance_review,
)
from orchestune.integrator.final_pr_body import ChildSummary, render_final_pr_body
from orchestune.models import CommitCheck, IssueRecord, PrRecord
from orchestune.review.rounds import (
    parse_trigger,
    restore_triggers,
    trigger_comment_ids,
)

SHA = "a" * 40
OTHER = "b" * 40


def issue(number=100, body="## 受け入れ条件\n- [ ] 動作する\n", title="Task"):
    return IssueRecord(number, title, body, (), "", state="CLOSED")


def check(
    name,
    conclusion="success",
    *,
    kind="check_run",
    completed=True,
    url="https://github.com/o/r/runs/1",
):
    return CommitCheck(name, kind, completed, conclusion, url)


def render(parent=None, children=None, summaries=None, **kwargs):
    children = [issue(101)] if children is None else children
    summaries = (
        [ChildSummary(101, "Task", (201,), "done (claude / pass)")]
        if summaries is None
        else summaries
    )
    kwargs.setdefault("ci_checks", (check("ci"),))
    return render_final_acceptance_review(
        parent=parent or issue(),
        children=children,
        summaries=summaries,
        pr_number=500,
        head_sha=SHA,
        reviewer="claude",
        **kwargs,
    )


@pytest.mark.parametrize(
    "heading",
    [
        "## 受け入れ条件",
        "### 受け入れ基準 (Acceptance Criteria)",
        "#### ACCEPTANCE criteria ###",
        "  ## 追加受け入れ条件",
    ],
)
def test_extract_heading_variations(heading):
    body = f"{heading}\r\n- yes\r\n# End\r\nno"
    assert extract_acceptance_sections(body) == (f"{heading}\n- yes",)


def test_extract_all_sections_without_duplicate_nested_acceptance():
    body = "## Acceptance\nparent\n### 追加受け入れ\nnested\n## Other\nx\n### Acceptance\nlast"
    assert extract_acceptance_sections(body) == (
        "## Acceptance\nparent\n### 追加受け入れ\nnested",
        "### Acceptance\nlast",
    )


@pytest.mark.parametrize(
    "code",
    [
        "```md\n## Acceptance\nwrong\n```",
        "~~~~\n## Acceptance\nwrong\n~~~\n~~~~",
        "> ```\n> ## Acceptance\n> wrong\n> ```",
        "- ```\n  ## Acceptance\n  wrong\n  ```",
        "    ## Acceptance\n    wrong",
        "- item\n  ## Acceptance\n  wrong",
    ],
)
def test_code_and_container_headings_are_not_acceptance(code):
    assert extract_acceptance_sections(code + "\n\n## 受け入れ条件\nyes") == (
        "## 受け入れ条件\nyes",
    )


def test_unclosed_fence_and_unsupported_headings():
    assert extract_acceptance_sections("```\n## Acceptance\nwrong") == ()
    assert (
        extract_acceptance_sections("Acceptance\n===\nwrong\n##### Acceptance\nwrong")
        == ()
    )


def test_render_contains_sources_and_is_not_a_normal_review_round():
    payload = render()
    assert payload.body.startswith("@claude review\n")
    assert SHA in payload.body and "#100" in payload.body and "#101" in payload.body
    assert "done (claude / pass)" in payload.body and "#201" in payload.body
    assert payload.body.rstrip().endswith(final_acceptance_marker(SHA))
    comment = {"id": 1, "body": payload.body, "created_at": "2026-11-01T00:00:00Z"}
    assert parse_trigger(comment) is None
    assert restore_triggers([comment]) == []
    assert restore_round_triggers([comment]) == []
    assert trigger_comment_ids([comment]) == {1}
    assert payload.truncated_issue_numbers == ()


def test_render_sanitizes_foreign_markers_mentions_and_unclosed_fences():
    raw = (
        "## Acceptance\n@codex review\n<!-- orchestune:review-head "
        + SHA
        + " -->\n```\nraw"
    )
    payload = render(
        parent=issue(
            body=raw, title="@claude <!-- orchestune:review-trigger bot=claude -->"
        )
    )
    assert payload.body.count("@claude") == 1
    assert "@codex" not in payload.body
    assert "<!-- orchestune:review-" not in payload.body
    assert parse_trigger({"id": 1, "body": payload.body}) is None


def test_render_missing_and_empty_conditions_are_explicit():
    payload = render(
        parent=issue(body="description"), children=[issue(101, "## Acceptance\n")]
    )
    assert "受け入れ条件の節が見つかりません" in payload.body
    assert "受け入れ条件の節はありますが本文が空です" in payload.body


def test_render_budget_is_deterministic_keeps_parent_and_all_child_metadata():
    children = [
        issue(102, "## Acceptance\n" + "あ" * 1000),
        issue(101, "## Acceptance\n" + "い" * 1000),
    ]
    summaries = [ChildSummary(n, "Task", (n + 100,), "done") for n in (102, 101)]
    payload = render(
        parent=issue(body="## Acceptance\nPARENT"),
        children=children,
        summaries=summaries,
        max_bytes=2400,
    )
    assert len(payload.body.encode("utf-8")) <= 2400
    assert (
        "PARENT" in payload.body and "#101" in payload.body and "#102" in payload.body
    )
    assert payload.truncated_issue_numbers == (101, 102)
    assert "一部省略" in payload.body and "本文省略" in payload.body
    assert payload == render(
        parent=issue(body="## Acceptance\nPARENT"),
        children=list(reversed(children)),
        summaries=list(reversed(summaries)),
        max_bytes=2400,
    )


def test_oversize_parent_never_gets_truncated():
    with pytest.raises(PayloadBudgetExceeded):
        render(parent=issue(body="## Acceptance\n" + "親" * 30000))
    with pytest.raises(PayloadBudgetExceeded):
        render(max_bytes=20)


@pytest.fixture
def request_context():
    forge = MagicMock()
    children = [issue(101)]
    summaries = [ChildSummary(101, "Task", (201,), "done")]
    pr = PrRecord(
        500,
        "parent/issue-100",
        (),
        base_ref="main",
        head_sha=SHA,
        is_cross_repository=False,
        body=render_final_pr_body(100, summaries),
    )
    forge.get_pull_request.return_value = pr
    forge.get_issue.return_value = issue()
    forge.list_all_issue_comments.return_value = []
    forge.get_authenticated_user.return_value = "worker"
    forge.list_notice_trusted_authors.return_value = frozenset({"maintainer"})
    forge.create_issue_comment.return_value = {"id": 7}
    forge.list_commit_checks.return_value = (check("ci"),)
    kwargs = dict(
        forge=forge,
        parent_issue_number=100,
        pr_number=500,
        children=children,
        summaries=summaries,
        reviewer="claude",
    )
    return forge, pr, kwargs


def test_posts_once_and_recovers_after_lost_response(request_context):
    forge, _, kwargs = request_context
    forge.create_issue_comment.side_effect = TimeoutError("response lost")
    first = request_final_acceptance_review(**kwargs)
    assert (first["status"], first["reason"]) == (
        "indeterminate",
        "post_outcome_unknown",
    )
    body = forge.create_issue_comment.call_args.args[1]
    forge.list_all_issue_comments.return_value = [
        {"id": 7, "body": body, "user": {"login": "worker"}}
    ]
    second = request_final_acceptance_review(**kwargs)
    assert second["status"] == "already_requested" and second["comment_id"] == 7
    forge.create_issue_comment.assert_called_once()


def test_posted_report_and_recheck(request_context):
    forge, _, kwargs = request_context
    result = request_final_acceptance_review(**kwargs)
    assert result["status"] == "posted" and result["comment_id"] == 7
    assert forge.get_pull_request.call_count == 2
    forge.list_all_issue_comments.assert_called_once_with(500)


@pytest.mark.parametrize(
    "author,expected",
    [
        ("worker", "already_requested"),
        ("maintainer", "already_requested"),
        ("stranger", "posted"),
    ],
)
def test_marker_author_trust(request_context, author, expected):
    forge, _, kwargs = request_context
    forge.list_all_issue_comments.return_value = [
        {"id": 1, "body": final_acceptance_marker(SHA), "author": author}
    ]
    assert request_final_acceptance_review(**kwargs)["status"] == expected


def test_unknown_trust_fails_closed(request_context):
    forge, _, kwargs = request_context
    forge.list_all_issue_comments.return_value = [
        {"id": 1, "body": final_acceptance_marker(SHA)}
    ]
    result = request_final_acceptance_review(**kwargs)
    assert result["status"] == "indeterminate" and result["reason"] == "author_unknown"
    forge.create_issue_comment.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        final_acceptance_marker(OTHER),
        "> " + final_acceptance_marker(SHA),
        "```\n" + final_acceptance_marker(SHA) + "\n```",
    ],
)
def test_other_head_or_quoted_marker_does_not_suppress(request_context, body):
    forge, _, kwargs = request_context
    forge.list_all_issue_comments.return_value = [
        {"id": 1, "body": body, "author": "worker"}
    ]
    assert request_final_acceptance_review(**kwargs)["status"] == "posted"


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_sha", ""),
        ("head_sha", "short"),
        ("state", "MERGED"),
        ("base_ref", "other"),
        ("head_ref", "other"),
        ("is_cross_repository", None),
        ("is_cross_repository", True),
    ],
)
def test_invalid_pr_identity_fails_closed(request_context, field, value):
    forge, pr, kwargs = request_context
    forge.get_pull_request.return_value = replace(pr, **{field: value})
    assert request_final_acceptance_review(**kwargs)["status"] == "indeterminate"
    forge.create_issue_comment.assert_not_called()


@pytest.mark.parametrize(
    "method,reason",
    [
        ("list_all_issue_comments", "comments_unavailable"),
        ("get_issue", "parent_body_unavailable"),
        ("get_pull_request", "pr_identity_unknown"),
    ],
)
def test_read_failure_is_not_absence(request_context, method, reason):
    forge, _, kwargs = request_context
    getattr(forge, method).side_effect = RuntimeError("unavailable")
    result = request_final_acceptance_review(**kwargs)
    assert result["status"] == "indeterminate" and result["reason"] == reason
    forge.create_issue_comment.assert_not_called()


def test_head_change_before_post_defers(request_context):
    forge, pr, kwargs = request_context
    forge.get_pull_request.side_effect = [pr, replace(pr, head_sha=OTHER)]
    assert request_final_acceptance_review(**kwargs)["reason"] == "head_changed"
    forge.create_issue_comment.assert_not_called()


def test_degraded_summaries_or_stale_table_defers(request_context):
    forge, pr, kwargs = request_context
    assert (
        request_final_acceptance_review(**{**kwargs, "summaries": []})["reason"]
        == "child_summaries_unavailable"
    )
    forge.get_pull_request.return_value = replace(pr, body="stale")
    assert (
        request_final_acceptance_review(**kwargs)["reason"]
        == "final_pr_snapshot_mismatch"
    )


@pytest.mark.parametrize("separator", ["- - -", "* * *", "_ _ _"])
def test_thematic_break_does_not_create_or_reset_list_container(separator):
    assert extract_acceptance_sections(separator + "\n  ## Acceptance\nyes") == (
        "  ## Acceptance\nyes",
    )
    assert extract_acceptance_sections(
        "- outer\n  " + separator + "\n  ## Acceptance\n  hidden\n\n## Acceptance\nyes"
    ) == ("## Acceptance\nyes",)


def test_missing_parent_and_oversize_parent(request_context):
    forge, _, kwargs = request_context
    forge.get_issue.return_value = None
    assert (
        request_final_acceptance_review(**kwargs)["reason"] == "parent_body_unavailable"
    )
    forge.get_issue.return_value = issue(body="## Acceptance\n" + "親" * 30000)
    assert (
        request_final_acceptance_review(**kwargs)["reason"] == "payload_budget_exceeded"
    )


@pytest.mark.parametrize(
    "code",
    [
        "```\n    ```\n## Acceptance\nwrong\n```",
        "- ~~~\n  ## Acceptance\n  wrong\n  ~~~",
    ],
)
def test_fence_closers_respect_indentation_and_containers(code):
    assert extract_acceptance_sections(code + "\n\n## Acceptance\nyes") == (
        "## Acceptance\nyes",
    )


def test_more_than_one_comment_page_and_no_legacy_comment_reader(request_context):
    forge, _, kwargs = request_context
    marker = {
        "id": 1,
        "body": final_acceptance_marker(SHA),
        "user": {"login": "worker"},
    }
    forge.list_all_issue_comments.return_value = [marker] + [
        {"id": n, "body": "ordinary"} for n in range(2, 155)
    ]
    forge.list_comments.side_effect = AssertionError("not all pages")
    assert request_final_acceptance_review(**kwargs)["status"] == "already_requested"
    forge.list_comments.assert_not_called()
    forge.get_issue.assert_not_called()


def test_new_head_and_new_pr_can_each_get_a_request(request_context):
    forge, pr, kwargs = request_context
    forge.list_all_issue_comments.return_value = [
        {"id": 1, "body": final_acceptance_marker(SHA), "author": "worker"}
    ]
    forge.get_pull_request.return_value = replace(pr, head_sha=OTHER)
    assert request_final_acceptance_review(**kwargs)["status"] == "posted"
    forge.get_pull_request.return_value = replace(pr, number=501)
    forge.list_all_issue_comments.return_value = []
    assert (
        request_final_acceptance_review(**{**kwargs, "pr_number": 501})["status"]
        == "posted"
    )
    assert forge.create_issue_comment.call_count == 2


@pytest.mark.parametrize(
    ("code", "status", "reason"),
    [
        (403, "failed", "post_rejected"),
        (422, "failed", "post_rejected"),
        (503, "indeterminate", "post_outcome_unknown"),
    ],
)
def test_post_rejection_and_uncertain_failure_are_distinct(
    request_context, code, status, reason
):
    import subprocess

    forge, _, kwargs = request_context
    forge.create_issue_comment.side_effect = subprocess.CalledProcessError(
        1, ["gh", "api"], stderr=f"HTTP {code}"
    )
    result = request_final_acceptance_review(**kwargs)
    assert (result["status"], result["reason"]) == (status, reason)
    forge.create_issue_comment.assert_called_once()


def test_unknown_collaborators_defer_instead_of_treating_author_as_untrusted(
    request_context,
):
    forge, _, kwargs = request_context
    forge.list_all_issue_comments.return_value = [
        {"id": 1, "body": final_acceptance_marker(SHA), "author": {"login": "other"}}
    ]
    forge.list_notice_trusted_authors.side_effect = RuntimeError("denied")
    assert (
        request_final_acceptance_review(**kwargs)["reason"]
        == "collaborators_unavailable"
    )
    forge.create_issue_comment.assert_not_called()


def test_ambiguous_success_response_and_renderer_exception(request_context):
    from unittest.mock import patch

    forge, _, kwargs = request_context
    forge.create_issue_comment.return_value = {}
    assert request_final_acceptance_review(**kwargs)["reason"] == "post_outcome_unknown"
    with patch(
        "orchestune.integrator.final_acceptance_review.render_final_acceptance_review",
        side_effect=ValueError("render"),
    ):
        assert request_final_acceptance_review(**kwargs)["status"] == "failed"


def test_budget_counts_entities_and_unicode_without_breaking_sections():
    child = issue(101, "## Acceptance\n" + "親😀@<&```" * 10000)
    payload = render(children=[child], max_bytes=4000)
    assert len(payload.body.encode("utf-8")) <= 4000
    assert payload.truncated_issue_numbers == (101,)
    assert payload.body.count("@") == 1
    assert payload.body.rstrip().endswith(final_acceptance_marker(SHA))
    assert parse_trigger({"id": 1, "body": payload.body}) is None


def test_extra_rows_in_stale_pr_table_do_not_match_snapshot(request_context):
    forge, pr, kwargs = request_context
    forge.get_pull_request.return_value = replace(
        pr,
        body=render_final_pr_body(
            100, [*kwargs["summaries"], ChildSummary(102, "new")]
        ),
    )
    assert (
        request_final_acceptance_review(**kwargs)["reason"]
        == "final_pr_snapshot_mismatch"
    )


def test_waits_for_pending_checks_then_posts_once(request_context, capsys):
    forge, _, kwargs = request_context
    forge.list_commit_checks.return_value = (
        check("ci"),
        check("b-slow", "", completed=False),
        check("a-slow", "pending", kind="status", completed=False),
    )
    waiting = request_final_acceptance_review(**kwargs)
    assert (waiting["status"], waiting["reason"]) == ("waiting_for_ci", "ci_pending")
    assert waiting["pending_checks"] == ["a-slow", "b-slow"]
    assert waiting["head_sha"] == SHA and waiting["pr_number"] == 500
    forge.create_issue_comment.assert_not_called()
    assert capsys.readouterr().err == ""
    forge.list_commit_checks.assert_called_with(SHA)

    forge.list_commit_checks.return_value = (check("ci"), check("slow"))
    assert request_final_acceptance_review(**kwargs)["status"] == "posted"
    forge.create_issue_comment.assert_called_once()


def test_no_checks_waits_without_warning(request_context, capsys):
    forge, _, kwargs = request_context
    forge.list_commit_checks.return_value = ()
    result = request_final_acceptance_review(**kwargs)
    assert (result["status"], result["reason"]) == ("waiting_for_ci", "no_checks")
    assert result["pending_checks"] == []
    forge.create_issue_comment.assert_not_called()
    assert capsys.readouterr().err == ""


def test_unreadable_checks_are_indeterminate_not_absence(request_context, capsys):
    forge, _, kwargs = request_context
    forge.list_commit_checks.side_effect = ValueError("incomplete")
    result = request_final_acceptance_review(**kwargs)
    assert (result["status"], result["reason"]) == (
        "indeterminate",
        "ci_status_unavailable",
    )
    forge.create_issue_comment.assert_not_called()
    assert "ci_status_unavailable" in capsys.readouterr().err


def test_existing_marker_wins_without_reading_ci(request_context):
    forge, _, kwargs = request_context
    forge.list_all_issue_comments.return_value = [
        {"id": 1, "body": final_acceptance_marker(SHA), "author": "worker"}
    ]
    forge.list_commit_checks.side_effect = RuntimeError("must not be read")
    assert request_final_acceptance_review(**kwargs)["status"] == "already_requested"
    forge.list_commit_checks.assert_not_called()


def test_failed_ci_is_embedded_and_still_requested(request_context):
    forge, _, kwargs = request_context
    forge.list_commit_checks.return_value = (check("ci", "failure"),)
    assert request_final_acceptance_review(**kwargs)["status"] == "posted"
    body = forge.create_issue_comment.call_args.args[1]
    assert f"CI results for head `{SHA}`" in body
    assert "| ci | check_run | failure | https://github.com/o/r/runs/1 |" in body
    assert "All 1 checks completed: 1 failure." in body


def test_ci_section_is_ordered_and_summarized():
    body = render(
        ci_checks=(
            check("z", "success"),
            check("b", "failure", kind="status", url=""),
            check("a", "success"),
            check("b", "success"),
        )
    ).body
    rows = [
        line
        for line in body.splitlines()
        if line.startswith("| ") and "---" not in line
    ]
    assert [row.split(" | ")[0] + row.split(" | ")[1] for row in rows[1:]] == [
        "| acheck_run",
        "| bcheck_run",
        "| bstatus",
        "| zcheck_run",
    ]
    assert "All 4 checks completed: 1 failure, 3 success." in body
    assert body.index("CI results") < body.index("### 親Issue #100")
    assert "—" in body


def test_ci_section_neutralizes_names_conclusions_and_urls():
    body = render(
        ci_checks=(
            check("a|b\nc @team <x>", "su|cc\ness", url="https://ok.example/1"),
            check("ext", url="javascript:alert(1)"),
            check("sp", url="https://bad.example/a b"),
            check("pipe", url="https://bad.example/a|b"),
            check("angle", url="https://bad.example/<a>"),
        )
    ).body
    assert "a\\|b c &#64;team &lt;x&gt;" in body
    assert "su\\|cc ess" in body
    assert "https://ok.example/1" in body
    for bad in ("javascript:", "a b", "a|b\n", "<a>"):
        assert bad not in body.split("### 親Issue")[0].split("| ext |")[1]
    for name in ("ext", "sp", "pipe", "angle"):
        assert f"| {name} | check_run | success | — |" in body


def test_ci_section_survives_child_truncation_and_counts_toward_budget():
    big = issue(101, body="## 受け入れ条件\n" + "子" * 30000)
    full = render(children=[big]).body
    limit = len(full.encode()) // 2
    payload = render(children=[big], max_bytes=limit)
    assert payload.truncated_issue_numbers == (101,)
    assert (
        "CI results for head" in payload.body and "| ci | check_run |" in payload.body
    )
    many = tuple(check(f"check-{i}") for i in range(400))
    with pytest.raises(PayloadBudgetExceeded):
        render(ci_checks=many, max_bytes=len(render().body.encode()) + 100)


def test_intro_asks_for_ci_citation_not_bot_side_test_runs():
    body = render().body
    assert "State tests run and tests you could not run" not in body
    assert "CI results" in body
    assert "advisory" in body and "humans decide the final merge" in body
    assert "not merge approval" in body


def test_backslash_before_pipe_cannot_reopen_the_table_cell():
    body = render(ci_checks=(check("a\\|b"),)).body
    assert "| a\\\\\\|b | check_run |" in body

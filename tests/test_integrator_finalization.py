"""Receipt parsing, compatibility and deterministic selection for #685."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orchestune.integrator import finalization as receipts
from orchestune.integrator.proofs import TaskIntegrationProof
from orchestune.integrator.steps import RetryChildIssueCloseStep
from orchestune.integrator.types import IntegrationContext, IntegratorConfig
from tests.conftest import make_task
from tests.forge_event_support import install_receipt_lookup_adapter

BASE = "parent/issue-100"


def proof(number=1, sha="a" * 40):
    return TaskIntegrationProof(
        number, f"task-{number}", f"claude/issue-{number}-task", sha
    )


def payload(**overrides: Any) -> dict[str, Any]:
    body = receipts.render_integration_receipt(proof(), BASE, integration_pr_number=100)
    result: dict[str, Any] = json.loads(body.split("```json\n")[1].split("\n```")[0])
    result.update(overrides)
    return result


def comment(data=None, *, id: Any = 1, at: Any = "2026-10-18T00:00:00Z", **overrides):
    data = payload() if data is None else data
    result = {
        "body": f"{receipts.RECEIPT_MARKER}\n```json\n{json.dumps(data, sort_keys=True)}\n```",
        "user": {"login": "bot"},
        "id": id,
        "created_at": at,
    }
    result.update(overrides)
    return result


def lookup(forge, comments):
    forge.list_all_issue_comments.side_effect = None
    forge.list_all_issue_comments.return_value = comments
    return receipts.lookup_integration_receipt(forge, 1, "task-1", f"origin/{BASE}")


@pytest.mark.parametrize(
    "key,value",
    [
        ("integration_pr_number", True),
        ("integration_pr_number", 0),
        ("integration_pr_number", -1),
        ("integration_pr_number", "100"),
        ("notice_base_branch", ""),
        ("notice_base_branch", None),
        ("notice_base_branch", "parent/issue-OTHER"),
        ("notice_base_branch", 100),
        ("source_sha", "invalid"),
        ("branch_name", ""),
        ("branch_name", []),
        ("source", "invalid"),
        ("source", []),
    ],
)
@pytest.mark.parametrize("invalid_first", [False, True])
def test_invalid_matching_receipt_holds_even_with_valid_history(
    fake_forge, key, value, invalid_first
):
    comments = [comment(), comment(payload(**{key: value}), id=2)]
    result = lookup(fake_forge, comments if invalid_first else comments[::-1])
    assert result.status.value == "UNAVAILABLE"
    assert result.reason == "receipt_invalid"
    assert result.receipt is None


@pytest.mark.parametrize(
    "mutation",
    [
        {"user": {"login": "other"}},
        {"user": None},
        {"body": "not a receipt"},
        {"body": receipts.RECEIPT_MARKER + "\n```json\nbroken\n```"},
        {"body": "quoted\n" + comment()["body"]},
    ],
)
def test_untrusted_or_noncanonical_comments_are_absent(fake_forge, mutation):
    assert lookup(fake_forge, [comment(**mutation)]).status.value == "ABSENT"


@pytest.mark.parametrize(
    "changed",
    [
        {"issue_number": 2},
        {"issue_number": True},
        {"subtask_id": "task-10"},
        {"base_branch": "parent/issue-1000"},
    ],
)
def test_other_target_is_absent(fake_forge, changed):
    assert lookup(fake_forge, [comment(payload(**changed))]).status.value == "ABSENT"


def test_crlf_and_missing_notice_base_are_compatible(fake_forge):
    data = payload()
    del data["notice_base_branch"]
    original = comment(data)
    original["body"] = original["body"].replace("\n", "\r\n")
    result = lookup(fake_forge, [original])
    assert result.status.value == "FOUND"
    assert result.receipt.integration_pr_number == 100
    assert result.receipt.notice_base_branch is None


@pytest.mark.parametrize("reverse", [False, True])
def test_same_instant_uses_numeric_id_and_one_complete_receipt(fake_forge, reverse):
    first = comment(id=9, at="2026-10-18T09:00:00+09:00")
    second = comment(
        payload(integration_pr_number=200, source_sha="b" * 40),
        id=10,
        at="2026-10-18T00:00:00Z",
    )
    result = lookup(fake_forge, [second, first] if reverse else [first, second])
    assert result.receipt.integration_pr_number == 200
    assert result.receipt.proof.source_sha == "b" * 40


def test_time_precedes_id(fake_forge):
    result = lookup(
        fake_forge,
        [
            comment(id=100),
            comment(
                payload(integration_pr_number=200), id=1, at="2026-10-18T00:00:01Z"
            ),
        ],
    )
    assert result.receipt.integration_pr_number == 200


@pytest.mark.parametrize(
    "key,value",
    [
        ("created_at", None),
        ("created_at", "2026-10-18T00:00:00"),
        ("created_at", "2026-02-30T00:00:00Z"),
        ("created_at", "20261018T000000Z"),
        ("id", None),
        ("id", True),
        ("id", "10"),
        ("id", 0),
    ],
)
def test_order_metadata_must_be_valid_for_distinct_payloads(fake_forge, key, value):
    bad = comment(payload(integration_pr_number=200), id=2)
    bad[key] = value
    result = lookup(fake_forge, [comment(), bad])
    assert result.status.value == "UNAVAILABLE"
    assert result.reason == "receipt_order_unavailable"


def test_equal_payloads_do_not_need_order_metadata(fake_forge):
    result = lookup(fake_forge, [comment(id=None, at=None), comment(id=None, at=None)])
    assert result.status.value == "FOUND"


@pytest.mark.parametrize(
    "different",
    [
        {"data": payload(integration_pr_number=200)},
        {"at": "2026-10-18T00:00:01Z"},
    ],
)
def test_same_id_cannot_describe_conflicting_comments(fake_forge, different):
    result = lookup(fake_forge, [comment(), comment(**different)])
    assert result.reason == "receipt_order_unavailable"


def test_same_id_and_equivalent_utc_time_can_be_repeated(fake_forge):
    result = lookup(fake_forge, [comment(), comment(at="2026-10-18T09:00:00+09:00")])
    assert result.status.value == "FOUND"


def test_new_format_precedes_newer_legacy_and_latest_null_is_explicit(fake_forge):
    legacy = payload()
    del legacy["integration_pr_number"]
    del legacy["notice_base_branch"]
    result = lookup(
        fake_forge,
        [
            comment(legacy, id=30, at="2026-10-19T00:00:00Z"),
            comment(
                payload(integration_pr_number=None), id=20, at="2026-10-18T01:00:00Z"
            ),
            comment(id=10),
        ],
    )
    assert result.receipt.has_integration_pr_key
    assert result.receipt.integration_pr_number is None


def test_legacy_receipts_select_latest_proof(fake_forge):
    first, second = payload(), payload(source_sha="b" * 40)
    for data in (first, second):
        del data["integration_pr_number"]
        del data["notice_base_branch"]
    result = lookup(fake_forge, [comment(second, id=2), comment(first)])
    assert not result.receipt.has_integration_pr_key
    assert result.receipt.proof.source_sha == "b" * 40


def test_legacy_proof_api_keeps_first_match_and_ignores_notice_fields(fake_forge):
    first = comment(payload(integration_pr_number="invalid"))
    second = comment(payload(source_sha="b" * 40))
    fake_forge.list_comments.return_value = [
        {"author": "bot", "body": c["body"]} for c in (first, second)
    ]
    assert receipts.find_integration_receipt(fake_forge, 1, "task-1", BASE) == proof()
    fake_forge.list_all_issue_comments.assert_not_called()


@pytest.mark.parametrize("api", ["get_authenticated_user", "list_all_issue_comments"])
def test_lookup_failure_is_unavailable_and_wrapper_returns_none(fake_forge, api):
    getattr(fake_forge, api).side_effect = RuntimeError("outage")
    result = receipts.lookup_integration_receipt(fake_forge, 1, "task-1", BASE)
    assert result.status.value == "UNAVAILABLE"
    assert result.reason == "receipt_lookup_failed"
    assert (
        receipts.find_latest_integration_receipt(fake_forge, 1, "task-1", BASE) is None
    )


def test_latest_wrapper_returns_existing_type(fake_forge):
    fake_forge.list_all_issue_comments.side_effect = None
    fake_forge.list_all_issue_comments.return_value = [comment()]
    result = receipts.find_latest_integration_receipt(fake_forge, 1, "task-1", BASE)
    assert isinstance(result, receipts.IntegrationNoticeTarget)


def test_save_distinguishes_omitted_null_and_number_without_duplicates(fake_forge):
    comments = []
    fake_forge.list_comments.side_effect = lambda _: list(comments)
    fake_forge.add_comment.side_effect = lambda _, body: comments.append(
        {"author": "bot", "body": body}
    )
    assert receipts.ensure_integration_receipt(fake_forge, proof(), BASE)
    for value in (None, 123, 123):
        assert receipts.ensure_integration_receipt(
            fake_forge, proof(), BASE, integration_pr_number=value
        )
    assert len(comments) == 3
    assert '"integration_pr_number"' not in comments[0]["body"]
    assert '"integration_pr_number": null' in comments[1]["body"]


def test_legacy_proof_api_keeps_none_on_read_failure(fake_forge):
    fake_forge.list_comments.side_effect = RuntimeError("outage")
    assert receipts.find_integration_receipt(fake_forge, 1, "task-1", BASE) is None


def _setup_receipt_comments(fake_forge):
    comments_by_issue: dict[int, list[dict[str, Any]]] = {}

    def add_comment(issue_number, body):
        comments_by_issue.setdefault(int(issue_number), []).append(
            {"body": body, "author": fake_forge.get_authenticated_user()}
        )

    def list_comments(issue_number):
        return list(comments_by_issue.get(int(issue_number), []))

    fake_forge.add_comment.side_effect = add_comment
    fake_forge.list_comments.side_effect = list_comments
    install_receipt_lookup_adapter(fake_forge)


def test_ensure_integration_receipt_persists_pr_number_and_does_not_duplicate(
    fake_forge,
):
    from orchestune.integrator.finalization import (
        ensure_integration_receipt,
        find_latest_integration_receipt,
    )

    _setup_receipt_comments(fake_forge)

    proof = TaskIntegrationProof(
        issue_number=10,
        subtask_id="task-10",
        branch_name="claude/issue-10-task-10",
        source_sha="c" * 40,
    )

    # 初回保存
    assert ensure_integration_receipt(
        fake_forge, proof, "origin/parent/issue-1", integration_pr_number=123
    )
    # 同一PR番号での再保存は重複せず True
    assert ensure_integration_receipt(
        fake_forge, proof, "origin/parent/issue-1", integration_pr_number=123
    )

    comments = fake_forge.list_comments(10)
    assert len(comments) == 1

    target = find_latest_integration_receipt(
        fake_forge, 10, "task-10", "origin/parent/issue-1"
    )
    assert target is not None
    assert target.has_integration_pr_key is True
    assert target.integration_pr_number == 123
    assert target.notice_base_branch == "parent/issue-1"


def test_find_latest_integration_receipt_prefers_new_form_over_old_form(
    fake_forge,
):
    from orchestune.integrator.finalization import (
        ensure_integration_receipt,
        find_latest_integration_receipt,
        render_integration_receipt,
    )

    _setup_receipt_comments(fake_forge)

    proof = TaskIntegrationProof(
        issue_number=10,
        subtask_id="task-10",
        branch_name="claude/issue-10-task-10",
        source_sha="d" * 40,
    )
    # 旧形式 receipt を先に追加
    fake_forge.add_comment(
        10, render_integration_receipt(proof, "origin/parent/issue-1")
    )
    # 新形式 receipt（PR番号付き）を追加
    ensure_integration_receipt(
        fake_forge, proof, "origin/parent/issue-1", integration_pr_number=456
    )

    target = find_latest_integration_receipt(
        fake_forge, 10, "task-10", "origin/parent/issue-1"
    )
    assert target is not None
    assert target.has_integration_pr_key is True
    assert target.integration_pr_number == 456


def test_find_latest_integration_receipt_with_null_pr_number(fake_forge):
    from orchestune.integrator.finalization import (
        ensure_integration_receipt,
        find_latest_integration_receipt,
    )

    _setup_receipt_comments(fake_forge)

    proof = TaskIntegrationProof(
        issue_number=10,
        subtask_id="task-10",
        branch_name="claude/issue-10-task-10",
        source_sha="e" * 40,
    )
    # integration_pr_number が None（null）
    ensure_integration_receipt(
        fake_forge, proof, "origin/parent/issue-1", integration_pr_number=None
    )

    target = find_latest_integration_receipt(
        fake_forge, 10, "task-10", "origin/parent/issue-1"
    )
    assert target is not None
    assert target.has_integration_pr_key is True
    assert target.integration_pr_number is None


def test_retry_close_defers_and_does_not_close_when_notice_is_unknown(
    fake_forge, tmp_path: Path
):
    from orchestune.integrator.finalization import ensure_integration_receipt
    from orchestune.pr_link_notice import render_merged_notice

    _setup_receipt_comments(fake_forge)

    task = make_task(
        10,
        subtask_id="task-10",
        status_labels=("status:done", "integration:included"),
    )
    proof = TaskIntegrationProof(
        issue_number=10,
        subtask_id="task-10",
        branch_name="claude/issue-10-task-10",
        source_sha="f" * 40,
    )
    ensure_integration_receipt(
        fake_forge, proof, "origin/parent/issue-1", integration_pr_number=789
    )
    # 他者によるマーカーがあり、collaborators API が失敗することにより UNKNOWN
    fake_forge.create_issue_comment(
        10, render_merged_notice(789, "parent/issue-1"), author="untrusted_author"
    )
    fake_forge.list_notice_trusted_authors.side_effect = RuntimeError(
        "No collaborators API"
    )

    ctx = IntegrationContext(
        config=IntegratorConfig(
            parent_issue_number=1,
            base_branch="parent/issue-1",
            apply=True,
            forge=fake_forge,
        ),
        repository_root=tmp_path,
        original_root=tmp_path,
        base_branch="origin/parent/issue-1",
        temp_branch="integration/temp-parent-issue-1-test",
        active_done_tasks=[task],
    )

    report = RetryChildIssueCloseStep().execute(ctx)

    assert 10 not in report.get("retried_closed_issues", [])
    assert 10 in report.get("finalization_deferred", [])
    fake_forge.close_issue.assert_not_called()


def test_retry_close_closes_with_generic_comment_when_pr_number_is_null(
    fake_forge, tmp_path: Path
):
    from orchestune.integrator.finalization import ensure_integration_receipt

    _setup_receipt_comments(fake_forge)

    task = make_task(
        10,
        subtask_id="task-10",
        status_labels=("status:done", "integration:included"),
    )
    proof = TaskIntegrationProof(
        issue_number=10,
        subtask_id="task-10",
        branch_name="claude/issue-10-task-10",
        source_sha="1" * 40,
    )
    ensure_integration_receipt(
        fake_forge, proof, "origin/parent/issue-1", integration_pr_number=None
    )

    ctx = IntegrationContext(
        config=IntegratorConfig(
            parent_issue_number=1,
            base_branch="parent/issue-1",
            apply=True,
            forge=fake_forge,
        ),
        repository_root=tmp_path,
        original_root=tmp_path,
        base_branch="origin/parent/issue-1",
        temp_branch="integration/temp-parent-issue-1-test",
        active_done_tasks=[task],
    )

    report = RetryChildIssueCloseStep().execute(ctx)

    assert report.get("retried_closed_issues") == [10]
    fake_forge.close_issue.assert_called_once()
    assert "完了済み" in (fake_forge.close_issue.call_args.kwargs.get("comment") or "")


def test_lookup_uses_real_forge_all_pages_and_native_metadata(
    forge, gh_run, monkeypatch
):
    monkeypatch.setattr(forge, "get_authenticated_user", lambda: "bot")
    unrelated = comment(payload(issue_number=999), id=99)
    gh_run.stdout(json.dumps([[unrelated], [comment()]]))
    result = receipts.lookup_integration_receipt(forge, 1, "task-1", BASE)
    assert result.status.value == "FOUND"
    assert result.receipt.integration_pr_number == 100
    args = gh_run.call_args.args[0]
    assert "--paginate" in args
    assert "--slurp" in args
    assert "repos/{owner}/{repo}/issues/1/comments?per_page=100" in args

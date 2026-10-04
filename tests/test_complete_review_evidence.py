"""Completion derives evidence from fresh PR content, never a claimed verdict."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
import yaml

from orchestune.complete.contracts import CompleteFailureReason, CompleteRequest
from orchestune.complete.journal import CompletionJournalError
from orchestune.complete.review_evidence import verify_review_evidence
from orchestune.review.markers import (
    review_head_marker,
    review_reply_marker,
    review_round_marker,
    review_selection_marker,
    review_trigger_marker,
)

HEAD = "a" * 40
LATER = "b" * 40


class ReviewForge:
    def __init__(self):
        self.comments = [
            dict(
                id=1,
                body="\n".join(
                    (
                        review_trigger_marker("claude"),
                        review_round_marker(1),
                        review_head_marker(HEAD),
                    )
                ),
                created_at="2026-10-03T00:00:00Z",
                user={"login": "worker"},
            ),
            dict(
                id=2,
                body="No required findings.",
                created_at="2026-10-03T00:01:00Z",
                user={"login": "claude[bot]"},
            ),
        ]
        self.reviews = []
        self.inlines = []

    def list_all_issue_comments(self, pr):
        return self.comments

    def list_pull_request_reviews(self, pr):
        return self.reviews

    def list_pull_request_review_comments(self, pr):
        return self.inlines


@pytest.fixture
def evidence(tmp_path):
    path = tmp_path / "review-reply.md"
    judgments = {
        "round": 1,
        "findings": [
            dict(
                source="issue_comment:2",
                location="summary",
                judgment="already_addressed",
                status="resolved",
                basis="No required findings in the acquired summary",
                evidence="current source",
            )
        ],
    }
    path.write_text(
        "```orchestune-review-judgments\n" + yaml.safe_dump(judgments) + "```\n"
    )
    request = CompleteRequest.done(1029, 42, reviewer="claude", review_reply=path)
    return request, ReviewForge(), SimpleNamespace(head_sha=HEAD), judgments


def verify(evidence, head=HEAD):
    request, forge, pr, _ = evidence
    return verify_review_evidence(request, forge, pr, head)


def rewrite(evidence):
    request, _, _, judgments = evidence
    request.payload.review_reply.write_text(
        "```orchestune-review-judgments\n" + yaml.safe_dump(judgments) + "```\n"
    )


def test_pass_is_bound_to_current_sources_and_head(evidence):
    summary = verify(evidence)
    assert (summary.bot, summary.rounds, summary.verdict) == ("claude", 1, "pass")
    assert summary.reviewed_head_sha == HEAD
    assert summary.review_target_sha_source == "trigger_head_verified"
    assert len(summary.judgment_digest) == 64
    assert summary.judgment_counts == {"already_addressed": 1, "resolved": 1}


@pytest.mark.parametrize(
    "mutation",
    [
        "remote_push",
        "local_rebase",
        "old_trigger",
        "trigger_push",
        "review_commit_mismatch",
    ],
)
def test_head_changes_reject_before_outcome(evidence, mutation):
    _, forge, pr, _ = evidence
    head = HEAD
    if mutation == "remote_push":
        pr.head_sha = LATER
    elif mutation == "local_rebase":
        head = LATER
    elif mutation == "old_trigger":
        forge.comments[0]["body"] = (
            review_trigger_marker("claude") + "\n" + review_round_marker(1)
        )
    elif mutation == "trigger_push":
        pr.head_sha = head = LATER
    else:
        forge.reviews = [
            dict(
                id=3,
                body="Review",
                submitted_at="2026-10-03T00:02:00Z",
                commit_id=LATER,
                user={"login": "claude[bot]"},
            )
        ]
    with pytest.raises(CompletionJournalError) as exc:
        verify(evidence, head)
    assert exc.value.reason == CompleteFailureReason.REVIEW_HEAD_MISMATCH
    assert "Step 11" in str(exc.value)


@pytest.mark.parametrize(
    "mutation",
    [
        "reviewer",
        "round",
        "coverage",
        "unresolved",
        "needs_information",
        "unavailable",
        "in_progress",
        "malformed",
    ],
)
def test_invalid_review_and_judgments_fail_closed(evidence, mutation):
    request, forge, _, judgments = evidence
    if mutation == "reviewer":
        forge.comments[0]["body"] = forge.comments[0]["body"].replace("claude", "codex")
    elif mutation == "round":
        judgments["round"] = 2
    elif mutation == "coverage":
        judgments["findings"] = []
    elif mutation == "unresolved":
        judgments["findings"][0]["status"] = "unresolved"
    elif mutation == "needs_information":
        judgments["findings"][0]["judgment"] = "needs_information"
    elif mutation == "unavailable":
        forge.comments.pop()
    elif mutation == "in_progress":
        forge.comments.append(
            dict(
                id=4,
                body="Review in progress",
                created_at="2026-10-03T00:02:00Z",
                user={"login": "claude[bot]"},
            )
        )
    else:
        request.payload.review_reply.write_text("invalid")
    if mutation != "malformed":
        rewrite(evidence)
    with pytest.raises(CompletionJournalError) as exc:
        verify(evidence)
    assert exc.value.reason == CompleteFailureReason.REVIEW_EVIDENCE_INVALID


def test_fetch_failure_is_evidence_missing(evidence, monkeypatch):
    def fail(_):
        raise OSError("unavailable")

    monkeypatch.setattr(evidence[1], "list_pull_request_reviews", fail)
    with pytest.raises(CompletionJournalError) as exc:
        verify(evidence)
    assert exc.value.reason == CompleteFailureReason.EVIDENCE_MISSING


def test_skip_requires_selection_for_current_head(evidence):
    request, forge, pr, _ = evidence
    request = replace(
        request, payload=replace(request.payload, reviewer="skip", review_reply=None)
    )
    with pytest.raises(CompletionJournalError):
        verify_review_evidence(request, forge, pr, HEAD)
    forge.comments.append(dict(body=review_selection_marker("skip", HEAD)))
    summary = verify_review_evidence(request, forge, pr, HEAD)
    assert summary.verdict == "skipped"
    assert summary.bot == "skip"


def test_judgment_digest_is_stable_across_yaml_formatting(evidence):
    before = verify(evidence).judgment_digest
    rewrite(evidence)
    assert verify(evidence).judgment_digest == before


def test_incomplete_snapshot_is_rejected(evidence, monkeypatch):
    from orchestune.complete import review_evidence

    fetch = review_evidence._fetch

    def incomplete(*args):
        state = fetch(*args)
        state["completeness"]["inline_comments"] = "unknown"
        return state

    monkeypatch.setattr(review_evidence, "_fetch", incomplete)
    with pytest.raises(CompletionJournalError) as exc:
        verify(evidence)
    assert exc.value.reason == CompleteFailureReason.REVIEW_EVIDENCE_INVALID


def test_deferred_requires_basis_and_cannot_be_required(evidence):
    row = evidence[3]["findings"][0]
    row.update(
        judgment="decline",
        status="deferred",
        basis="Follow-up is outside acceptance criteria",
    )
    rewrite(evidence)
    assert verify(evidence).judgment_counts["deferred"] == 1
    row["basis"] = ""
    rewrite(evidence)
    with pytest.raises(CompletionJournalError):
        verify(evidence)


def test_codex_review_commit_and_inline_coverage(evidence):
    request, forge, pr, table = evidence
    request = replace(request, payload=replace(request.payload, reviewer="codex"))
    forge.comments = forge.comments[:1]
    forge.comments[0]["body"] = forge.comments[0]["body"].replace("claude", "codex")
    forge.reviews = [
        dict(
            id=5,
            body="Reviewed",
            commit_id=HEAD,
            submitted_at="2026-10-03T00:01:00Z",
            user={"login": "chatgpt-codex-connector[bot]"},
        )
    ]
    forge.inlines = [
        dict(
            id=6,
            body="Finding",
            pull_request_review_id=5,
            created_at="2026-10-03T00:01:00Z",
            user={"login": "chatgpt-codex-connector[bot]"},
        )
    ]
    table["findings"][0]["source"] = "review:5"
    table["findings"].append({**table["findings"][0], "source": "inline_comment:6"})
    rewrite(evidence)
    summary = verify_review_evidence(request, forge, pr, HEAD)
    assert summary.review_target_sha_source == "review_commit"
    assert summary.bot == "codex"


def publication_with_evidence(tmp_path, monkeypatch, evidence):
    from complete_lifecycle_test_support import lifecycle_environment

    request, forge, _ = lifecycle_environment(tmp_path, monkeypatch, "done")
    evidence_request, remote, _, _ = evidence
    request = replace(
        request,
        payload=replace(
            request.payload,
            reviewer="claude",
            review_reply=evidence_request.payload.review_reply,
        ),
    )
    existing_comments = forge.list_all_issue_comments
    forge.list_all_issue_comments = (
        lambda number: remote.comments if number == 42 else existing_comments(number)
    )
    forge.list_pull_request_reviews = remote.list_pull_request_reviews
    forge.list_pull_request_review_comments = remote.list_pull_request_review_comments
    return request, forge


def test_publication_persists_verified_summary(tmp_path, monkeypatch, evidence):
    from orchestune.complete.service import complete_task

    request, forge = publication_with_evidence(tmp_path, monkeypatch, evidence)
    result = complete_task(request, forge=forge)
    assert result.success, result.failure
    assert result.outcome_record.review.verdict == "pass"
    assert (
        result.outcome_record.review.reviewed_head_sha == result.outcome_record.head_sha
    )
    assert len(forge.comments) == 1


def test_invalid_review_never_reserves_or_posts(tmp_path, monkeypatch, evidence):
    from orchestune.complete.service import complete_task
    from orchestune.ledger.run_state import load_run_state_readonly

    request, forge = publication_with_evidence(tmp_path, monkeypatch, evidence)
    evidence[3]["findings"] = []
    rewrite(evidence)
    result = complete_task(request, forge=forge)
    assert result.failure.reason == CompleteFailureReason.REVIEW_EVIDENCE_INVALID
    assert not forge.comments
    assert not load_run_state_readonly(request.state_path).completion_journal


def test_reserved_judgment_change_is_fingerprint_mismatch(
    tmp_path, monkeypatch, evidence
):
    from orchestune.complete.service import complete_task

    request, forge = publication_with_evidence(tmp_path, monkeypatch, evidence)
    forge.inject = (
        lambda op, after: (_ for _ in ()).throw(OSError("offline"))
        if op == "post"
        else None
    )
    first = complete_task(request, forge=forge)
    assert first.completion_id
    forge.inject = lambda *_: None
    evidence[3]["findings"][0]["basis"] = "Changed judgment justification"
    rewrite(evidence)
    result = complete_task(request, forge=forge)
    assert result.failure.reason == CompleteFailureReason.REQUEST_FINGERPRINT_MISMATCH
    assert not forge.comments


def test_unrelated_pr_comment_does_not_break_pending_resume(
    tmp_path, monkeypatch, evidence
):
    from orchestune.complete.service import complete_task

    request, forge = publication_with_evidence(tmp_path, monkeypatch, evidence)
    forge.inject = (
        lambda op, after: (_ for _ in ()).throw(OSError("offline"))
        if op == "post"
        else None
    )
    first = complete_task(request, forge=forge)
    assert first.completion_id
    forge.inject = lambda *_: None
    evidence[1].comments.append(
        dict(
            id=99,
            body="Thanks! CI finished.",
            user={"login": "worker"},
            created_at="2026-10-03T00:03:00Z",
        )
    )
    resumed = complete_task(request, forge=forge)
    assert resumed.success, resumed.failure
    assert len(forge.comments) == 1


def test_changed_review_body_breaks_pending_resume(tmp_path, monkeypatch, evidence):
    from orchestune.complete.service import complete_task

    request, forge = publication_with_evidence(tmp_path, monkeypatch, evidence)
    forge.inject = (
        lambda op, after: (_ for _ in ()).throw(OSError("offline"))
        if op == "post"
        else None
    )
    first = complete_task(request, forge=forge)
    assert first.completion_id
    forge.inject = lambda *_: None
    evidence[1].comments[1]["body"] = "Updated review content on the same source"
    resumed = complete_task(request, forge=forge)
    assert resumed.failure.reason == CompleteFailureReason.REQUEST_FINGERPRINT_MISMATCH
    assert not forge.comments


def test_legacy_reserved_journal_can_resume_without_new_review(tmp_path, monkeypatch):
    from complete_lifecycle_test_support import lifecycle_environment

    from orchestune.complete.journal import completion_journal_lock
    from orchestune.complete.service import complete_task
    from orchestune.ledger.run_state import load_run_state_readonly, save_run_state

    request, forge, _ = lifecycle_environment(tmp_path, monkeypatch, "done")
    forge.inject = (
        lambda op, after: (_ for _ in ()).throw(OSError("offline"))
        if op == "post"
        else None
    )
    assert not complete_task(request, forge=forge).success
    request = replace(request, payload=replace(request.payload, reviewer=None))
    with completion_journal_lock(request.state_path):
        state = load_run_state_readonly(request.state_path)
        raw = next(iter(state.completion_journal.values()))
        raw["prepublication_policy_evidence"]["validation"].pop("review")
        raw["request_fingerprint"] = request.request_fingerprint
        save_run_state(state, request.state_path)
    forge.inject = lambda *_: None
    assert complete_task(request, forge=forge).success


def test_remote_push_during_acquisition_is_rejected(tmp_path, monkeypatch, evidence):
    from orchestune.complete.service import complete_task

    request, forge = publication_with_evidence(tmp_path, monkeypatch, evidence)
    real = forge.list_pull_request_reviews

    def push(number):
        forge.pr = replace(forge.pr, head_sha=LATER)
        return real(number)

    forge.list_pull_request_reviews = push
    result = complete_task(request, forge=forge)
    assert result.failure.reason == CompleteFailureReason.REVIEW_HEAD_MISMATCH
    assert not forge.comments


def test_legacy_journal_validation_ignores_only_absent_review():
    from orchestune.complete.service import _same_validation

    old = {"validation": {"pr": {}, "ci": {"head_sha": HEAD}}}
    new = {
        "validation": {
            "pr": {},
            "ci": {"head_sha": HEAD},
            "review": {"verdict": "pass"},
        }
    }
    assert _same_validation(old, new)
    assert not _same_validation(
        new, {"validation": {**new["validation"], "review": {"verdict": "skipped"}}}
    )


# --- #1207: same-bot review replies are not evidence ---------------------------


def _reply(id_=9, body=None, login="claude[bot]"):
    return dict(
        id=id_,
        body=body or f"{review_reply_marker()}\nRound 1 judgments",
        user={"login": login},
        created_at="2026-10-03T00:05:00Z",
    )


def test_same_bot_marked_reply_is_not_a_required_source(evidence):
    _, forge, _, _ = evidence
    forge.comments.append(_reply())
    summary = verify(evidence)
    assert summary.verdict == "pass"
    assert summary.reviewed_head_sha == HEAD


def test_same_bot_unmarked_reply_still_requires_judgment(evidence):
    _, forge, _, _ = evidence
    forge.comments.append(_reply(body="Round 1 judgments (no marker)"))
    with pytest.raises(CompletionJournalError):
        verify(evidence)


def test_marked_reply_in_review_body_does_not_hide_source(evidence):
    _, forge, _, table = evidence
    forge.reviews = [
        dict(
            id=3,
            body=f"{review_reply_marker()}\nA real finding",
            submitted_at="2026-10-03T00:02:00Z",
            commit_id=HEAD,
            user={"login": "claude[bot]"},
        )
    ]
    with pytest.raises(CompletionJournalError):
        verify(evidence)
    table["findings"].append({**table["findings"][0], "source": "review:3"})
    rewrite(evidence)
    assert verify(evidence).verdict == "pass"


def test_codex_marked_reply_is_not_a_required_source(evidence):
    request, forge, pr, table = evidence
    request = replace(request, payload=replace(request.payload, reviewer="codex"))
    forge.comments = forge.comments[:1]
    forge.comments[0]["body"] = forge.comments[0]["body"].replace("claude", "codex")
    forge.reviews = [
        dict(
            id=5,
            body="Reviewed",
            commit_id=HEAD,
            submitted_at="2026-10-03T00:01:00Z",
            user={"login": "chatgpt-codex-connector[bot]"},
        )
    ]
    forge.comments.append(_reply(login="chatgpt-codex-connector[bot]"))
    table["findings"][0]["source"] = "review:5"
    rewrite(evidence)
    summary = verify_review_evidence(request, forge, pr, HEAD)
    assert (summary.bot, summary.review_target_sha_source) == ("codex", "review_commit")


def test_marked_reply_changes_do_not_break_pending_resume(
    tmp_path, monkeypatch, evidence
):
    from orchestune.complete.service import complete_task

    request, forge = publication_with_evidence(tmp_path, monkeypatch, evidence)
    forge.inject = (
        lambda op, after: (_ for _ in ()).throw(OSError("offline"))
        if op == "post"
        else None
    )
    first = complete_task(request, forge=forge)
    assert first.completion_id
    forge.inject = lambda *_: None
    reply = _reply()
    evidence[1].comments.append(reply)
    reply["body"] += "\nedited after the first attempt"
    resumed = complete_task(request, forge=forge)
    assert resumed.success, resumed.failure
    assert len(forge.comments) == 1


# --- #1210: completion shares the strict trigger/round restoration -------------


def _trigger(id_, round_num, at, bot="claude", head=HEAD):
    return dict(
        id=id_,
        body="\n".join(
            (
                f"@{bot} review",
                review_trigger_marker(bot),
                review_round_marker(round_num),
                review_head_marker(head),
            )
        ),
        created_at=at,
        user={"login": "worker"},
    )


def _rejects(evidence, reason=CompleteFailureReason.REVIEW_EVIDENCE_INVALID):
    with pytest.raises(CompletionJournalError) as exc:
        verify(evidence)
    assert exc.value.reason == reason
    return str(exc.value)


def test_two_triggers_for_the_latest_round_are_rejected_not_chosen(evidence):
    _, forge, _, _ = evidence
    forge.comments.append(_trigger(9, 1, "2026-10-03T00:00:30Z"))
    assert "ambiguous" in _rejects(evidence)


def test_one_trigger_id_with_two_bodies_is_rejected(evidence):
    _, forge, _, _ = evidence
    forge.comments.append(_trigger(1, 2, "2026-10-03T00:05:00Z"))
    assert "different content" in _rejects(evidence)


def test_round_order_contradicting_creation_time_is_rejected(evidence):
    _, forge, _, _ = evidence
    forge.comments.append(_trigger(9, 2, "2026-10-02T00:00:00Z"))
    assert "contradicts" in _rejects(evidence)


def test_trigger_markers_inside_a_code_fence_are_not_a_trigger(evidence):
    _, forge, _, _ = evidence
    forge.comments[0]["body"] = "```\n" + forge.comments[0]["body"] + "\n```"
    assert "trigger is missing" in _rejects(evidence)


def test_a_newer_round_without_a_result_is_not_final_even_with_old_review(evidence):
    _, forge, _, _ = evidence
    forge.comments.append(_trigger(9, 2, "2026-10-03T00:10:00Z"))
    assert "not final" in _rejects(evidence)


def test_review_id_equal_to_a_trigger_id_is_still_review_evidence(evidence):
    request, forge, pr, judgments = evidence
    forge.reviews = [
        dict(
            id=1,  # same number as the trigger comment, a different id namespace
            body="Review body",
            submitted_at="2026-10-03T00:02:00Z",
            commit_id=HEAD,
            user={"login": "claude[bot]"},
        )
    ]
    assert "review:1" in _rejects(evidence)
    judgments["findings"].append({**judgments["findings"][0], "source": "review:1"})
    rewrite(evidence)
    summary = verify(evidence)
    assert summary.review_target_sha_source == "review_commit"


def test_the_pr_wide_latest_trigger_decides_the_reviewer(evidence):
    _, forge, _, _ = evidence
    forge.comments.append(_trigger(9, 2, "2026-10-03T00:10:00Z", bot="codex"))
    assert "differs from --reviewer" in _rejects(evidence)

"""Offline/MCP single-snapshot evaluation and pre-post validation (#1210), all pure."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from orchestune.review.markers import build_trigger_body
from orchestune.review.offline import (
    OfflineOutcome,
    evaluate_legacy,
    evaluate_snapshot,
    legacy_refusal,
    validate_request,
)
from orchestune.review.rounds import restore_triggers
from orchestune.review.snapshot import EvidenceContractError, RoundLimitError

HEAD = "a" * 40
OLD = "c" * 40
NOW = datetime(2026, 10, 4, 3, 30, 0, tzinfo=UTC)


def at(minute: int) -> str:
    return f"2026-10-04T03:{minute:02d}:00Z"


def table(source_ids: list[str], round_num: int = 1) -> str:
    rows = [
        {
            "source": source,
            "location": "app.py:1",
            "judgment": "adopt",
            "status": "resolved",
            "basis": "contract regression",
            "evidence": "commit abc",
        }
        for source in source_ids
    ]
    document = yaml.safe_dump({"round": round_num, "findings": rows})
    return f"```orchestune-review-judgments\n{document}```\n"


def trig(id_, round_num, minute, bot="claude", head=HEAD, reply="") -> dict[str, Any]:
    return {
        "id": id_,
        "body": build_trigger_body(reply, bot, round_num, head),
        "created_at": at(minute),
        "user": {"login": "worker"},
    }


def comment(id_, minute, body="finding", login="claude[bot]") -> dict[str, Any]:
    return {
        "id": id_,
        "body": body,
        "created_at": at(minute),
        "user": {"login": login},
    }


def review(id_, minute, commit=HEAD, body="review", login="claude[bot]"):
    return {
        "id": id_,
        "body": body,
        "submitted_at": at(minute),
        "commit_id": commit,
        "user": {"login": login},
    }


def inline(id_, parent, minute, login="claude[bot]"):
    return {
        "id": id_,
        "body": "bug here",
        "created_at": at(minute),
        "user": {"login": login},
        "pull_request_review_id": parent,
        "path": "app.py",
        "line": 3,
        "commit_id": HEAD,
    }


def snap(comments=(), reviews=(), inlines=(), head=HEAD, **extra) -> dict[str, Any]:
    value: dict[str, Any] = {
        "snapshot_version": 1,
        "repository": "owner/repo",
        "pr_number": 7,
        "acquisition": {
            "source": "github_mcp",
            "started_at": "2026-10-04T03:27:40Z",
            "observed_at": "2026-10-04T03:28:00Z",
            "head_before": {"sha": head, "fetched_at": "2026-10-04T03:27:40Z"},
            "head_after": {"sha": head, "fetched_at": "2026-10-04T03:28:00Z"},
        },
        "completeness": dict.fromkeys(
            ("issue_comments", "reviews", "inline_comments"), "complete"
        ),
        "issue_comments": list(comments),
        "reviews": list(reviews),
        "inline_comments": list(inlines),
    }
    value.update(extra)
    return value


def evaluate(value, bot="claude", **kwargs) -> OfflineOutcome:
    kwargs.setdefault("pr_number", 7)
    kwargs.setdefault("now", NOW)
    return evaluate_snapshot(value, bot_name=bot, **kwargs)


def first_round(*items, reviews=(), inlines=(), head=HEAD, **extra):
    return snap([trig(1, 1, 10), *items], reviews, inlines, head=head, **extra)


# --- identification --------------------------------------------------------------


def test_review_commit_round_identifies_every_field() -> None:
    outcome = evaluate(first_round(reviews=[review(20, 15)]))
    result = outcome.payload
    assert outcome.exit_code == 0
    assert result["acquisition_status"] == "acquired"
    assert (result["repository"], result["pr_number"]) == ("owner/repo", 7)
    assert (result["reviewer"], result["round"], result["trigger_id"]) == (
        "claude",
        1,
        1,
    )
    assert result["triggered_at"] == at(10)
    assert result["requested_head_sha"] == HEAD
    assert result["current_head_sha"] == HEAD
    assert result["reviewed_head_sha"] == HEAD
    assert (result["review_target_sha"], result["review_target_sha_source"]) == (
        HEAD,
        "review_commit",
    )
    assert result["completeness"] == dict.fromkeys(
        ("issue_comments", "reviews", "inline_comments"), "complete"
    )
    assert result["snapshot_version"] == 1
    assert result["snapshot_observed_at"] == "2026-10-04T03:28:00Z"
    assert result["is_latest_round"] is True and result["round_ended_at"] is None
    assert result["schema_version"] == 1
    assert result["evidence_warnings"] == []
    assert result["reply_validation"] is None


def test_comment_only_round_is_verified_only_by_the_fetched_head() -> None:
    matching = evaluate(first_round(comment(20, 15))).payload
    assert matching["review_target_sha_source"] == "trigger_head_verified"
    assert matching["review_target_sha"] == HEAD
    assert matching["reviewed_head_sha"] is None
    moved = evaluate(first_round(comment(20, 15), head="d" * 40)).payload
    assert moved["acquisition_status"] == "acquired"  # content is still acquired
    assert (moved["review_target_sha"], moved["review_target_sha_source"]) == (
        None,
        "unknown",
    )
    assert moved["requested_head_sha"] == HEAD and moved["current_head_sha"] == "d" * 40
    assert any("head" in warning for warning in moved["evidence_warnings"])


def test_trigger_without_head_marker_keeps_requested_head_unknown() -> None:
    item = trig(1, 1, 10)
    item["body"] = "\n".join(
        line for line in item["body"].splitlines() if "review-head" not in line
    )
    value = snap([item, comment(20, 15)])
    result = evaluate(value).payload
    assert result["requested_head_sha"] is None
    assert result["review_target_sha_source"] == "unknown"
    assert any("requested_head_sha" in w for w in result["evidence_warnings"])


def test_old_review_commit_is_kept_as_fact_not_replaced_by_current_head() -> None:
    result = evaluate(first_round(reviews=[review(20, 15, commit=OLD)])).payload
    assert result["reviewed_head_sha"] == OLD
    assert result["review_target_sha"] == OLD
    assert result["current_head_sha"] == HEAD != result["review_target_sha"]
    assert any("differs" in warning for warning in result["evidence_warnings"])


@pytest.mark.parametrize("commits", [[HEAD, OLD], ["abc"], [HEAD, "xyz"]])
def test_multiple_or_invalid_review_commits_are_unknown(commits) -> None:
    reviews = [review(20 + i, 15 + i, commit=c) for i, c in enumerate(commits)]
    result = evaluate(first_round(comment(30, 16), reviews=reviews)).payload
    assert result["reviewed_head_sha"] is None
    assert (result["review_target_sha"], result["review_target_sha_source"]) == (
        None,
        "unknown",
    )


def test_inline_only_round_has_no_target() -> None:
    value = first_round(inlines=[inline(30, None, 15)])
    result = evaluate(value).payload
    assert result["acquisition_status"] == "acquired"
    assert result["review_target_sha_source"] == "unknown"


# --- wrong-round evidence is never promoted ----------------------------------------


def test_previous_round_review_is_not_the_result_of_a_new_round() -> None:
    value = snap(
        [
            trig(1, 1, 10),
            trig(2, 2, 20, reply=table(["review:20", "issue_comment:40"])),
            comment(40, 12),
        ],
        reviews=[review(20, 14)],
    )
    outcome = evaluate(value)
    assert outcome.exit_code == 30
    assert outcome.payload["acquisition_status"] == "unavailable"
    assert outcome.payload["round"] == 2


def test_later_edit_of_a_past_review_does_not_make_it_current() -> None:
    old = review(20, 14)
    old["updated_at"] = at(25)
    value = snap([trig(1, 1, 10), trig(2, 2, 20, reply=table(["review:20"]))], [old])
    assert evaluate(value).exit_code == 30


def test_inline_on_a_past_review_is_not_the_new_rounds_content() -> None:
    value = snap(
        [trig(1, 1, 10), trig(2, 2, 20, reply=table(["review:20"]))],
        reviews=[review(20, 14)],
        inlines=[inline(30, 20, 26)],
    )
    outcome = evaluate(value)
    assert outcome.exit_code == 30
    inlines = outcome.payload["inline_comments"]
    assert all(item["provenance"] != "current" for item in inlines)


def test_bot_authored_trigger_and_telemetry_are_not_review_content() -> None:
    claude_trigger = trig(1, 1, 10)
    claude_trigger["user"] = {"login": "claude[bot]"}
    tracker = comment(30, 12, "**Claude finished** view job")
    outcome = evaluate(snap([claude_trigger, tracker]))
    assert outcome.exit_code == 30
    assert outcome.payload["review_items"] == []


def test_past_tracker_edited_later_does_not_make_this_round_in_progress() -> None:
    stale = comment(30, 5, "Claude is working…")
    stale["updated_at"] = at(25)
    outcome = evaluate(first_round(stale, reviews=[review(20, 15)]))
    assert outcome.exit_code == 0


def test_in_progress_tracker_in_this_round_is_exit_11() -> None:
    outcome = evaluate(first_round(comment(30, 15, "Claude is working…")))
    assert outcome.exit_code == 11
    assert outcome.payload["acquisition_status"] == "in_progress"


CODEX_TRACKER = (
    "<!-- codex-pull-request-review-summary -->\n"
    "| Review | Status | Commit | Review trigger |\n"
    "| --- | --- | --- | --- |\n"
    "| 📝 **Code Review** | 🔄 **{status}** since "
    '<relative-time datetime="2026-10-04T03:15:00Z">'
    "2026-10-04T03:15:00Z</relative-time> | `abc1234` | Manual request |\n"
    "<details><summary>About Codex</summary>Reviews are running.</details>"
)


def test_codex_running_tracker_in_this_round_is_exit_11() -> None:
    value = snap(
        [
            trig(1, 1, 10, bot="codex"),
            comment(
                30,
                15,
                # The Commit cell must not contradict the trigger's head (#1274).
                CODEX_TRACKER.format(status="Running").replace("abc1234", "aaaaaaa"),
                login="chatgpt-codex-connector[bot]",
            ),
        ]
    )
    outcome = evaluate(value, bot="codex")
    assert outcome.exit_code == 11
    assert outcome.payload["acquisition_status"] == "in_progress"
    assert outcome.payload["review_items"] == []


def test_codex_completed_tracker_only_is_exit_30() -> None:
    value = snap(
        [
            trig(1, 1, 10, bot="codex"),
            comment(
                30,
                15,
                CODEX_TRACKER.format(status="Completed"),
                login="chatgpt-codex-connector[bot]",
            ),
        ]
    )
    outcome = evaluate(value, bot="codex")
    assert outcome.exit_code == 30
    assert outcome.payload["review_body"] == ""
    assert outcome.payload["review_items"] == []


def test_codex_completed_tracker_with_real_review_is_acquired() -> None:
    value = snap(
        [
            trig(1, 1, 10, bot="codex"),
            comment(
                30,
                15,
                CODEX_TRACKER.format(status="Completed"),
                login="chatgpt-codex-connector[bot]",
            ),
        ],
        reviews=[
            review(
                40,
                16,
                body="Actual review result.",
                login="chatgpt-codex-connector[bot]",
            )
        ],
    )
    outcome = evaluate(value, bot="codex")
    assert outcome.exit_code == 0
    assert outcome.payload["review_body"] == "Actual review result."
    assert all(item["id"] != 30 for item in outcome.payload["review_items"])


def test_codex_completed_tracker_with_inline_review_is_acquired() -> None:
    value = snap(
        [
            trig(1, 1, 10, bot="codex"),
            comment(
                30,
                15,
                CODEX_TRACKER.format(status="Completed"),
                login="chatgpt-codex-connector[bot]",
            ),
        ],
        reviews=[
            review(
                40,
                15,
                body="",
                login="chatgpt-codex-connector[bot]",
            )
        ],
        inlines=[inline(50, 40, 16, login="chatgpt-codex-connector[bot]")],
    )
    outcome = evaluate(value, bot="codex")
    assert outcome.exit_code == 0
    assert "see 1 inline comment(s)" in outcome.payload["review_body"]
    assert [item["id"] for item in outcome.payload["inline_comments"]] == [50]


def test_trigger_id_does_not_hide_a_review_with_the_same_number() -> None:
    outcome = evaluate(first_round(reviews=[review(1, 15)]))
    assert outcome.exit_code == 0
    assert [item["id"] for item in outcome.payload["review_items"]] == [1]


# --- round selection ------------------------------------------------------------


def posted_two_rounds():
    return snap(
        [
            trig(1, 1, 10),
            comment(30, 12),
            trig(2, 2, 20, reply=table(["issue_comment:30"])),
            comment(31, 25, login="claude[bot]"),
        ]
    )


def test_latest_round_is_the_default_and_past_round_is_context_only() -> None:
    value = posted_two_rounds()
    latest = evaluate(value).payload
    assert (latest["round"], latest["is_latest_round"]) == (2, True)
    assert latest["reply_validation"]["previous_round"] == 1
    past = evaluate(value, requested_round=1).payload
    assert (past["round"], past["is_latest_round"], past["round_ended_at"]) == (
        1,
        False,
        at(20),
    )
    assert any("historical" in warning for warning in past["evidence_warnings"])
    items = {item["id"]: item for item in past["review_items"]}
    assert items[30]["provenance"] == "current"
    assert items[31]["provenance"] == "unassociated"
    assert items[31]["provenance_reason"] == "after_round_end"


def test_reviewer_must_match_the_posted_trigger_and_cannot_be_switched_here() -> None:
    with pytest.raises(EvidenceContractError, match="reviewer"):
        evaluate(first_round(comment(30, 15)), bot="codex")


def test_unposted_or_over_limit_round_is_rejected() -> None:
    value = first_round(comment(30, 15))
    with pytest.raises(EvidenceContractError, match="round 2"):
        evaluate(value, requested_round=2)
    with pytest.raises(RoundLimitError):
        evaluate(value, requested_round=6)
    six = snap(
        [
            trig(i, i, 9 + i, reply=table([], i - 1) if i > 1 else "")
            for i in range(1, 7)
        ]
    )
    with pytest.raises(RoundLimitError):
        evaluate(six)
    with pytest.raises(RoundLimitError):
        evaluate(first_round(comment(30, 15)), requested_round=2, max_rounds=1)


def test_no_posted_trigger_is_exit_30_not_an_invented_round() -> None:
    outcome = evaluate(snap([comment(30, 15)]))
    assert outcome.exit_code == 30
    assert outcome.payload["round"] is None
    assert "trigger" in outcome.payload["reason"]


def test_ambiguous_triggers_are_a_contract_error() -> None:
    with pytest.raises(EvidenceContractError, match="multiple triggers"):
        evaluate(snap([trig(1, 1, 10), trig(2, 1, 11)]))


def test_snapshot_observed_before_the_trigger_is_inconsistent() -> None:
    late = trig(1, 1, 10)
    late["created_at"] = "2026-10-04T03:28:10Z"  # within clock skew, after observed_at
    value = snap([late])
    with pytest.raises(EvidenceContractError, match="before the trigger"):
        evaluate(value)


# --- snapshot problems ----------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda v: v["completeness"].update(reviews="partial"),
        lambda v: v["acquisition"]["head_after"].update(sha="b" * 40),
        lambda v: v.pop("inline_comments"),
    ],
)
def test_incomplete_or_changed_snapshot_is_exit_30_without_review_content(
    mutate,
) -> None:
    value = first_round(comment(30, 15), reviews=[review(20, 15)])
    mutate(value)
    outcome = evaluate(value)
    assert outcome.exit_code == 30
    assert outcome.payload["acquisition_status"] == "unavailable"
    assert outcome.payload["review_items"] == []
    assert outcome.payload["review_target_sha_source"] == "unknown"


def test_stale_snapshot_is_exit_30_and_future_one_is_rejected() -> None:
    stale_now = datetime(2026, 10, 4, 4, 0, 0, tzinfo=UTC)
    assert evaluate(first_round(comment(30, 15)), now=stale_now).exit_code == 30
    early = datetime(2026, 10, 4, 3, 0, 0, tzinfo=UTC)
    with pytest.raises(EvidenceContractError, match="future"):
        evaluate(first_round(comment(30, 15)), now=early)


def test_max_snapshot_age_can_be_widened_but_never_removed() -> None:
    late = datetime(2026, 10, 4, 4, 0, 0, tzinfo=UTC)
    value = first_round(comment(30, 15))
    assert evaluate(value, now=late, max_age_seconds=7200).exit_code == 0
    with pytest.raises(EvidenceContractError, match="max_age"):
        evaluate(value, now=late, max_age_seconds=0)


def test_identity_mismatches_are_contract_errors() -> None:
    with pytest.raises(EvidenceContractError, match="PR"):
        evaluate(first_round(comment(30, 15)), pr_number=8)


def test_evaluation_never_mutates_the_input_snapshot() -> None:
    value = first_round(comment(30, 15), reviews=[review(20, 15)])
    frozen = copy.deepcopy(value)
    evaluate(value)
    assert value == frozen


# --- posted round >= 2 judgments --------------------------------------------------


def test_posted_table_is_validated_from_the_actual_trigger_body() -> None:
    result = evaluate(posted_two_rounds()).payload
    validation = result["reply_validation"]
    assert validation["validated"] is True and validation["source"] == "trigger_body"
    assert validation["previous_round"] == 1
    assert len(validation["judgment_digest"]) == len(validation["source_digest"]) == 64


@pytest.mark.parametrize(
    "reply",
    [
        table(["issue_comment:30"], 2),
        "no table",
        table([]),
        table(["issue_comment:30"]) * 2,
    ],
)
def test_invalid_posted_table_is_a_contract_error(reply) -> None:
    value = snap([trig(1, 1, 10), comment(30, 12), trig(2, 2, 20, reply=reply)])
    with pytest.raises(EvidenceContractError, match="round 2"):
        evaluate(value)


def test_body_file_must_match_the_posted_table_not_hide_it() -> None:
    value = posted_two_rounds()
    same = table(["issue_comment:30"])
    assert evaluate(value, body_text=same).exit_code == 0
    different = table(["issue_comment:30"]).replace("contract regression", "other")
    with pytest.raises(EvidenceContractError, match="differs from the posted"):
        evaluate(value, body_text=different)
    with pytest.raises(EvidenceContractError, match="round 2"):
        evaluate(value, body_text="no table")


def test_body_file_for_round_one_is_rejected_not_ignored() -> None:
    with pytest.raises(EvidenceContractError, match="round 2"):
        evaluate(first_round(comment(30, 15)), body_text=table([]))


def test_later_round_findings_do_not_enter_the_previous_round_table_check() -> None:
    value = snap(
        [
            trig(1, 1, 10),
            comment(30, 12),
            trig(2, 2, 20, reply=table(["issue_comment:30"])),
            comment(31, 25, "round two finding"),
        ]
    )
    assert evaluate(value).exit_code == 0


# --- pre-post validation -----------------------------------------------------------


def request(value, bot="claude", **kwargs) -> OfflineOutcome:
    kwargs.setdefault("pr_number", 7)
    kwargs.setdefault("now", NOW)
    return validate_request(value, bot_name=bot, **kwargs)


def test_round_one_request_needs_no_table_and_yields_a_postable_body() -> None:
    outcome = request(snap())
    receipt = outcome.payload
    assert outcome.exit_code == 0
    assert (receipt["operation"], receipt["validation_status"]) == (
        "validate_request",
        "valid",
    )
    assert (receipt["repository"], receipt["pr_number"]) == ("owner/repo", 7)
    assert (receipt["reviewer"], receipt["previous_round"], receipt["next_round"]) == (
        "claude",
        None,
        1,
    )
    assert receipt["head_sha"] == HEAD
    assert receipt["snapshot_observed_at"] == "2026-10-04T03:28:00Z"
    assert "acquisition_status" not in receipt and "verdict" not in receipt
    posted = {"id": 99, "body": receipt["trigger_body"], "created_at": at(29)}
    (parsed,) = restore_triggers([posted])
    assert (parsed.round, parsed.reviewer, parsed.requested_head_sha) == (
        1,
        "claude",
        HEAD,
    )


def test_round_two_request_validates_the_previous_table_before_posting() -> None:
    value = first_round(comment(30, 15))
    ok = request(value, body_text=table(["issue_comment:30"]))
    receipt = ok.payload
    assert (receipt["previous_round"], receipt["next_round"]) == (1, 2)
    assert len(receipt["previous_source_digest"]) == 64
    assert len(receipt["judgment_digest"]) == 64
    assert "issue_comment:30" in receipt["trigger_body"]
    assert receipt["trigger_body"].count("orchestune:review-round 2") == 1
    with pytest.raises(EvidenceContractError, match="issue_comment:30"):
        request(value, body_text=table([]))
    with pytest.raises(EvidenceContractError, match="--body-file"):
        request(value)


def test_request_head_marker_uses_the_mcp_fetched_head() -> None:
    receipt = request(snap(head="e" * 40)).payload
    assert "review-head " + "e" * 40 in receipt["trigger_body"]


def test_request_for_an_already_posted_round_issues_no_permit() -> None:
    value = posted_two_rounds()
    by_table = request(value, body_text=table(["issue_comment:30"])).payload
    assert by_table["validation_status"] == "already_posted"
    assert by_table["existing_trigger_id"] == 2
    assert by_table["next_round"] == 2 and "trigger_body" not in by_table
    by_explicit = request(value, explicit_round=2).payload
    assert by_explicit["validation_status"] == "already_posted"
    outstanding = request(first_round()).payload
    assert outstanding["validation_status"] == "already_posted"
    assert outstanding["existing_trigger_id"] == 1


def test_request_after_an_empty_round_may_use_an_explicit_empty_table() -> None:
    receipt = request(first_round(), body_text=table([])).payload
    assert receipt["validation_status"] == "valid" and receipt["next_round"] == 2


def test_request_round_and_reviewer_rules() -> None:
    value = first_round(comment(30, 15))
    reply = table(["issue_comment:30"])
    with pytest.raises(EvidenceContractError, match="next round 2"):
        request(value, body_text=reply, explicit_round=3)
    with pytest.raises(EvidenceContractError, match="switch-reviewer"):
        request(value, bot="codex", body_text=reply)
    switched = request(value, bot="codex", body_text=reply, switch_reviewer=True)
    assert switched.payload["reviewer"] == "codex"
    assert "@codex review" in switched.payload["trigger_body"]


def test_request_over_the_round_limit_cannot_be_relaxed() -> None:
    five = snap(
        [
            trig(i, i, 9 + i, reply=table([], i - 1) if i > 1 else "")
            for i in range(1, 6)
        ]
    )
    with pytest.raises(RoundLimitError):
        request(five, body_text=table([], 5))
    with pytest.raises(RoundLimitError):
        request(
            first_round(comment(30, 15)),
            body_text=table(["issue_comment:30"]),
            max_rounds=1,
        )


def test_request_with_insufficient_snapshot_yields_no_permit() -> None:
    value = first_round(comment(30, 15))
    value["completeness"]["issue_comments"] = "partial"
    outcome = request(value, body_text=table(["issue_comment:30"]))
    assert outcome.exit_code == 30
    assert outcome.payload["validation_status"] == "insufficient_evidence"
    assert "trigger_body" not in outcome.payload


def test_request_does_not_convert_incomplete_evidence_into_zero_findings() -> None:
    value = first_round(comment(30, 15))
    value["completeness"]["reviews"] = "unknown"
    assert request(value, body_text=table([])).exit_code == 30


def test_previous_round_of_another_reviewer_is_judged_after_a_switch() -> None:
    value = snap([trig(1, 1, 10, bot="codex"), comment(30, 15, login="codex")])
    receipt = request(
        value, bot="claude", body_text=table(["issue_comment:30"]), switch_reviewer=True
    ).payload
    assert receipt["validation_status"] == "valid"


# --- legacy snapshots -------------------------------------------------------------


def legacy(**extra) -> dict[str, Any]:
    value = {
        "issue_comments": [comment(1, 5, "LGTM no findings")],
        "reviews": [review(2, 6)],
        "inline_comments": [],
    }
    value.update(extra)
    return value


def test_legacy_snapshot_is_read_as_before_without_proving_anything() -> None:
    outcome = evaluate_legacy(legacy(), bot_name="claude", pr_number=7)
    result = outcome.payload
    assert outcome.exit_code == 0
    assert result["round"] is None and result["trigger_id"] is None
    assert result["requested_head_sha"] is None and result["current_head_sha"] is None
    assert result["completeness"] == dict.fromkeys(
        ("issue_comments", "reviews", "inline_comments"), "unknown"
    )
    assert result["reviewed_head_sha"] == HEAD
    assert (result["review_target_sha"], result["review_target_sha_source"]) == (
        HEAD,
        "review_commit",
    )
    joined = " ".join(result["evidence_warnings"])
    assert "legacy" in joined and "unverified" in joined


def test_legacy_incomplete_declaration_still_downgrades() -> None:
    value = legacy(completeness={"issue_comments": "complete", "reviews": "partial"})
    outcome = evaluate_legacy(value, bot_name="claude", pr_number=7)
    assert outcome.exit_code == 30
    assert "reviews" in outcome.payload["reason"]


def test_legacy_empty_and_in_progress_keep_exits_30_and_11() -> None:
    assert evaluate_legacy({}, bot_name="claude", pr_number=None).exit_code == 30
    working = legacy(issue_comments=[comment(1, 5, "Claude is working…")], reviews=[])
    assert evaluate_legacy(working, bot_name="claude", pr_number=7).exit_code == 11


def test_legacy_refusal_directs_to_the_new_format() -> None:
    outcome = legacy_refusal("validate_request", bot_name="claude", pr_number=7)
    assert outcome.exit_code == 30
    assert "snapshot_version" in outcome.payload["reason"]
    assert outcome.payload["validation_status"] == "insufficient_evidence"


# --- CLI: scripts/wait_for_review.py --review-state-file ---------------------------

ONLINE_ENTRYPOINTS = (
    "scripts.wait_for_review.post_review_trigger",
    "scripts.wait_for_review._run_gh",
    "scripts.wait_for_review._run_gh_api",
    "scripts.wait_for_review._get_pr_data",
    "scripts.wait_for_review._fetch_pr_head_sha",
    "scripts.wait_for_review._fetch_repository_slug",
    "scripts.wait_for_review.wait_for_review",
    "scripts.wait_for_review.collect_review_context",
    "scripts.wait_for_review.subprocess.run",
    "scripts.review_cli.subprocess.run",
)


class OnlineCallError(AssertionError):
    pass


def forbid(*args, **kwargs):
    raise OnlineCallError("offline path must not touch gh, polling or posting")


@pytest.fixture
def offline(tmp_path, monkeypatch):
    """Run main() offline with every online entry point replaced by a tripwire."""
    from scripts import review_cli
    from scripts.wait_for_review import main

    monkeypatch.setattr(review_cli, "_utc_now", lambda: NOW)
    monkeypatch.delenv("JEV_API_KEY", raising=False)

    def run(state, *args, state_name="state.json", **files) -> int:
        path = tmp_path / state_name
        path.write_text(json.dumps(state), encoding="utf-8")
        argv = ["wait", "--bot-name", "claude", "--review-state-file", str(path), *args]
        patches = [
            patch(target, side_effect=forbid, create=True)
            for target in ONLINE_ENTRYPOINTS
            if not target.endswith("review_cli.subprocess.run")
        ]
        with patch("sys.argv", argv):
            for stub in patches:
                stub.start()
            try:
                with pytest.raises(SystemExit) as exc:
                    main()
            finally:
                for stub in patches:
                    stub.stop()
        code = exc.value.code
        assert isinstance(code, int)
        return code

    run.tmp = tmp_path  # type: ignore[attr-defined]
    return run


def write_text(tmp_path: Path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_cli_evaluates_the_latest_round_and_writes_the_identified_result(offline):
    out = offline.tmp / "result.json"
    state = first_round(comment(30, 15), reviews=[review(20, 16)])
    assert offline(state, "--output-file", str(out)) == 0
    result = read_json(out)
    assert (result["round"], result["trigger_id"], result["repository"]) == (
        1,
        1,
        "owner/repo",
    )
    assert result["review_target_sha_source"] == "review_commit"
    assert result["jev_evaluations"] == []


def test_cli_resume_is_idempotent_and_posts_nothing(offline):
    out = offline.tmp / "result.json"
    state = first_round(comment(30, 15))
    assert offline(state, "--output-file", str(out)) == 0
    first = out.read_text(encoding="utf-8")
    assert offline(state, "--round", "1", "--output-file", str(out)) == 0
    assert out.read_text(encoding="utf-8") == first


def test_cli_exit_codes_follow_the_documented_table(offline):
    assert offline(first_round(comment(30, 15, "Claude is working…"))) == 11
    assert offline(snap([comment(30, 15)])) == 30
    incomplete = first_round(comment(30, 15))
    incomplete["completeness"]["reviews"] = "partial"
    assert offline(incomplete) == 30
    assert offline(snap([trig(1, 1, 10), trig(2, 1, 11)])) == 2
    assert offline(first_round(comment(30, 15)), "--round", "6") == 12
    assert offline(first_round(comment(30, 15)), "--round", "2") == 2
    bad_reply = snap(
        [trig(1, 1, 10), comment(30, 12), trig(2, 2, 20, reply="no table")]
    )
    assert offline(bad_reply) == 2


def test_cli_rejects_a_pr_number_that_differs_from_the_snapshot(offline):
    assert offline(first_round(comment(30, 15)), "--pr", "8") == 2
    assert offline(first_round(comment(30, 15)), "--pr", "7") == 0


def test_cli_validate_request_writes_a_receipt_and_never_posts(offline):
    out = offline.tmp / "request.json"
    reply = write_text(offline.tmp, "reply.md", table(["issue_comment:30"]))
    state = first_round(comment(30, 15))
    code = offline(
        state, "--validate-request", "--body-file", reply, "--output-file", str(out)
    )
    receipt = read_json(out)
    assert code == 0 and receipt["validation_status"] == "valid"
    assert receipt["next_round"] == 2
    assert "acquisition_status" not in receipt


def test_cli_failed_validation_overwrites_a_stale_receipt(offline):
    out = offline.tmp / "request.json"
    out.write_text(json.dumps({"validation_status": "valid", "trigger_body": "old"}))
    state = first_round(comment(30, 15))
    state["completeness"]["inline_comments"] = "partial"
    reply = write_text(offline.tmp, "reply.md", table(["issue_comment:30"]))
    code = offline(
        state, "--validate-request", "--body-file", reply, "--output-file", str(out)
    )
    receipt = read_json(out)
    assert code == 30 and receipt["validation_status"] == "insufficient_evidence"
    assert "trigger_body" not in receipt


def test_cli_invalid_reply_before_posting_is_exit_2(offline):
    reply = write_text(offline.tmp, "reply.md", table([]))
    state = first_round(comment(30, 15))
    assert offline(state, "--validate-request", "--body-file", reply) == 2
    assert offline(state, "--validate-request") == 2


def test_cli_validate_request_round_limit_is_exit_12(offline):
    reply = write_text(offline.tmp, "reply.md", table(["issue_comment:30"]))
    state = first_round(comment(30, 15))
    code = offline(
        state, "--validate-request", "--body-file", reply, "--max-rounds", "1"
    )
    assert code == 12


def test_cli_validate_request_reports_an_already_posted_round(offline):
    out = offline.tmp / "request.json"
    code = offline(
        posted_two_rounds(),
        "--validate-request",
        "--round",
        "2",
        "--output-file",
        str(out),
    )
    assert code == 0
    assert read_json(out)["validation_status"] == "already_posted"


def test_cli_evaluation_checks_the_body_file_against_the_posted_table(offline):
    same = write_text(offline.tmp, "same.md", table(["issue_comment:30"]))
    other = write_text(
        offline.tmp,
        "other.md",
        table(["issue_comment:30"]).replace("contract regression", "different"),
    )
    assert offline(posted_two_rounds(), "--body-file", same) == 0
    assert offline(posted_two_rounds(), "--body-file", other) == 2


@pytest.mark.parametrize(
    "flags",
    [
        ["--body", "text"],
        ["--timeout", "10"],
        ["--interval", "5"],
        ["--stall-grace", "60"],
        ["--max-retries", "1"],
        ["--switch-reviewer"],
        ["--max-rounds", "6"],
        ["--max-rounds", "0"],
        ["--round", "0"],
        ["--max-snapshot-age", "0"],
        ["--max-snapshot-age", "-3"],
        ["--max-snapshot-age", "inf"],
        ["--validate-request", "--no-post", "--body-file", "x"],
    ],
)
def test_cli_offline_rejects_options_that_do_not_apply(offline, flags):
    assert offline(first_round(comment(30, 15)), *flags) == 2


def test_cli_offline_accepts_explicit_defaults_that_do_apply(offline):
    state = first_round(comment(30, 15))
    assert offline(state, "--max-rounds", "5", "--no-post") == 0
    assert offline(state, "--max-snapshot-age", "900") == 0
    assert offline(state, "--jev-threshold", "0.5") == 0


def test_cli_stale_snapshot_needs_a_wider_age_or_a_fresh_fetch(offline, monkeypatch):
    from scripts import review_cli

    late = datetime(2026, 10, 4, 3, 50, 0, tzinfo=UTC)
    monkeypatch.setattr(review_cli, "_utc_now", lambda: late)
    state = first_round(comment(30, 15))
    assert offline(state) == 30
    assert offline(state, "--max-snapshot-age", "3600") == 0


def test_cli_validate_request_flags_are_offline_only(monkeypatch):
    from scripts.wait_for_review import main

    for extra in (
        ["--validate-request"],
        ["--max-snapshot-age", "60"],
    ):
        argv = ["wait", "--bot-name", "claude", "--pr", "1", *extra]
        with patch("sys.argv", argv), pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 2


def test_cli_skip_is_rejected_offline(offline):
    from scripts.wait_for_review import main

    argv = ["wait", "--bot-name", "skip", "--review-state-file", "x.json"]
    with patch("sys.argv", argv), pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


@pytest.mark.parametrize("with_key", [False, True])
def test_cli_never_calls_gh_even_with_findings_and_a_jev_key(
    offline, monkeypatch, with_key
):
    if with_key:
        monkeypatch.setenv("JEV_API_KEY", "test-key")
        monkeypatch.setenv("JEV_LOG_PATH", str(offline.tmp / "jev.jsonl"))
    state = first_round(reviews=[review(20, 15)], inlines=[inline(40, 20, 16)])
    from scripts.jev_filter import JevFindingEvaluation

    evaluation = JevFindingEvaluation(0.99, "HIGH")
    with patch("scripts.jev_filter.evaluate_finding_with_jev", return_value=evaluation):
        out = offline.tmp / "result.json"
        assert offline(state, "--output-file", str(out)) == 0
    result = read_json(out)
    assert len(result["inline_comments"]) == 1
    assert len(result["jev_evaluations"]) == 1


def test_cli_legacy_input_keeps_its_exit_codes_with_unverified_warnings(offline):
    out = offline.tmp / "result.json"
    assert offline(legacy(), "--output-file", str(out)) == 0
    result = read_json(out)
    assert result["round"] is None and result["completeness"]["reviews"] == "unknown"
    assert any("legacy" in w for w in result["evidence_warnings"])
    assert offline({}) == 30
    assert offline(legacy(completeness=None)) == 30


def test_cli_legacy_input_cannot_prove_rounds_or_validate_requests(offline):
    reply = write_text(offline.tmp, "reply.md", table([]))
    assert offline(legacy(), "--round", "1") == 30
    assert offline(legacy(), "--body-file", reply) == 30
    out = offline.tmp / "request.json"
    code = offline(legacy(), "--validate-request", "--output-file", str(out))
    assert code == 30
    receipt = read_json(out)
    assert receipt["validation_status"] == "insufficient_evidence"
    assert "snapshot_version" in receipt["reason"]


def test_cli_output_file_failure_is_nonzero_even_when_acquired(offline):
    bad = offline.tmp / "missing-dir" / "result.json"
    assert offline(first_round(comment(30, 15)), "--output-file", str(bad)) == 2


def test_cli_unknown_snapshot_version_and_bad_json_are_exit_2(offline):
    assert offline(snap(snapshot_version=2)) == 2
    from scripts.wait_for_review import main

    path = offline.tmp / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    argv = ["wait", "--bot-name", "claude", "--review-state-file", str(path)]
    with patch("sys.argv", argv), pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_cli_input_loading_failure_also_overwrites_a_stale_receipt(offline):
    out = offline.tmp / "request.json"
    stale = json.dumps({"validation_status": "valid", "trigger_body": "old"})
    reply = write_text(offline.tmp, "reply.md", table(["issue_comment:30"]))
    state = first_round(comment(30, 15))
    cases = [
        ("broken-json", ["--body-file", reply]),
        ("good-state", ["--body-file", str(offline.tmp / "missing-reply.md")]),
    ]
    for state_name, extra in cases:
        out.write_text(stale, encoding="utf-8")
        if state_name == "broken-json":
            (offline.tmp / "broken.json").write_text("{not json", encoding="utf-8")
            code = _run_raw(offline, "broken.json", extra, out)
        else:
            code = offline(
                state, "--validate-request", *extra, "--output-file", str(out)
            )
        receipt = read_json(out)
        assert code == 2, state_name
        assert receipt["validation_status"] == "rejected", state_name
        assert "trigger_body" not in receipt, state_name
    missing = offline.tmp / "no-such-state.json"
    out.write_text(stale, encoding="utf-8")
    assert _run_raw(offline, missing.name, [], out) == 2
    assert read_json(out)["validation_status"] == "rejected"


def _run_raw(offline, state_name, extra, out) -> int:
    from scripts.wait_for_review import main

    argv = [
        "wait",
        "--bot-name",
        "claude",
        "--review-state-file",
        str(offline.tmp / state_name),
        "--validate-request",
        *extra,
        "--output-file",
        str(out),
    ]
    with patch("sys.argv", argv), pytest.raises(SystemExit) as exc:
        main()
    code = exc.value.code
    assert isinstance(code, int)
    return code


def test_validate_request_rejects_unclosed_fence_before_posting(offline):
    out = offline.tmp / "request.json"
    reply = write_text(
        offline.tmp,
        "reply.md",
        table(["issue_comment:30"]) + "\n```\nunclosed",
    )
    state = first_round(comment(30, 15))
    code = offline(
        state, "--validate-request", "--body-file", reply, "--output-file", str(out)
    )
    assert code == 2
    receipt = read_json(out)
    assert receipt["validation_status"] == "rejected"
    assert "trigger_body" not in receipt

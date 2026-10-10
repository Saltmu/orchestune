"""Activity association for a reused Codex tracker comment (#1274)."""

from datetime import UTC, datetime

import pytest
import yaml

from orchestune.review.tracker_activity import (
    CompletedGrace,
    activity_time,
    parse_precise_utc,
    tracker_digest,
    tracker_is_current,
)

START = "2026-10-10T03:10:00Z"
END = "2026-10-10T03:20:00Z"


def _tracker(status="Running", commit="`abc1234`"):
    return (
        "<!-- codex-pull-request-review-summary -->\n"
        "| Review | Status | Commit | Review trigger |\n"
        "| --- | --- | --- | --- |\n"
        f"| 📝 **Code Review** | 🔄 **{status}** since "
        '<relative-time datetime="2026-10-10T01:46:23.563197Z">x</relative-time>'
        f" | {commit} | Manual request |\n"
    )


def _item(created="2026-10-09T17:09:43Z", updated="2026-10-10T03:15:00Z", **extra):
    item = {"id": 1, "body": _tracker(), "created_at": created, **extra}
    if updated is not None:
        item["updated_at"] = updated
    return item


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-10T03:10:00Z", datetime(2026, 10, 10, 3, 10, tzinfo=UTC)),
        (
            "2026-10-10T03:10:00.250Z",
            datetime(2026, 10, 10, 3, 10, 0, 250000, tzinfo=UTC),
        ),
        ("2026-10-10T12:10:00+09:00", datetime(2026, 10, 10, 3, 10, tzinfo=UTC)),
        (
            "2026-10-10T03:10:00.563197Z",
            datetime(2026, 10, 10, 3, 10, 0, 563197, tzinfo=UTC),
        ),
    ],
)
def test_precise_utc_keeps_fractions_and_offsets(value, expected):
    assert parse_precise_utc(value) == expected


@pytest.mark.parametrize(
    "value", [None, "", " ", "2026-10-10T03:10:00", "yesterday", 5, "2026-10-10"]
)
def test_precise_utc_rejects_missing_naive_and_invalid(value):
    assert parse_precise_utc(value) is None


def test_activity_time_prefers_updated_at_and_falls_back_only_when_missing():
    assert activity_time(_item()) == datetime(2026, 10, 10, 3, 15, tzinfo=UTC)
    for missing in (None, ""):
        assert activity_time(_item(updated=missing)) == datetime(
            2026, 10, 9, 17, 9, 43, tzinfo=UTC
        )
    invalid = _item(updated="garbage")
    assert activity_time(invalid) is None


def test_activity_time_uses_the_preserved_fraction_when_present():
    item = _item(
        updated="2026-10-10T03:10:00Z", updated_at_precise="2026-10-10T03:09:59.9Z"
    )
    assert activity_time(item) == datetime(2026, 10, 10, 3, 9, 59, 900000, tzinfo=UTC)


def test_updated_tracker_inside_the_interval_is_current_even_if_created_earlier():
    assert tracker_is_current(_item(), started_at=START, ended_at=END)
    assert tracker_is_current(_item(), started_at=START)


@pytest.mark.parametrize(
    ("updated", "expected"),
    [
        ("2026-10-10T03:09:59Z", False),
        ("2026-10-10T03:09:59.999999Z", False),
        ("2026-10-10T03:10:00Z", True),
        ("2026-10-10T03:19:59.999999Z", True),
        ("2026-10-10T03:20:00Z", False),
        ("2026-10-10T12:15:00+09:00", True),
        ("2026-10-10T12:20:00+09:00", False),
    ],
)
def test_interval_is_start_inclusive_and_end_exclusive(updated, expected):
    item = _item(updated=updated)
    assert tracker_is_current(item, started_at=START, ended_at=END) is expected


def test_open_round_has_no_upper_bound_and_unbounded_start_is_allowed():
    late = _item(updated="2026-12-01T00:00:00Z")
    assert tracker_is_current(late, started_at=START)
    assert tracker_is_current(_item(updated="2026-10-01T00:00:00Z"), started_at="")


def test_untouched_tracker_from_an_earlier_round_is_not_current():
    item = _item(updated=None)
    assert not tracker_is_current(item, started_at=START, ended_at=END)


def test_missing_updated_at_falls_back_to_created_at():
    item = _item(created="2026-10-10T03:12:00Z", updated=None)
    assert tracker_is_current(item, started_at=START, ended_at=END)


@pytest.mark.parametrize("updated", ["garbage", "2026-10-10T03:15:00"])
def test_present_but_invalid_updated_at_is_not_rescued_by_created_at(updated):
    item = _item(created="2026-10-10T03:12:00Z", updated=updated)
    assert not tracker_is_current(item, started_at=START, ended_at=END)


def test_updated_at_before_the_interval_ends_is_not_this_rounds_later_state():
    item = _item(created="2026-10-10T03:12:00Z", updated="2026-10-10T03:25:00Z")
    assert not tracker_is_current(item, started_at=START, ended_at=END)


@pytest.mark.parametrize(
    ("commit", "requested", "expected"),
    [
        ("`abc1234`", "abc1234" + "0" * 33, True),
        ("`abc1234`", "ABC1234" + "0" * 33, True),
        ("`abc1234`", "def5678" + "0" * 33, False),
        ("`abc1234`", None, True),
        ("`abc1234`", "", True),
        ("", "def5678" + "0" * 33, True),
        ("`zzz`", "def5678" + "0" * 33, True),
        ("`abc1234`", "abc", True),
    ],
)
def test_commit_cell_only_excludes_an_explicit_mismatch(commit, requested, expected):
    item = {**_item(), "body": _tracker(commit=commit)}
    current = tracker_is_current(
        item, started_at=START, ended_at=END, requested_head_sha=requested
    )
    assert current is expected


def test_non_tracker_is_never_a_current_tracker():
    item = {**_item(), "body": "Codex Review: Didn't find any major issues."}
    assert not tracker_is_current(item, started_at=START)


def test_digest_changes_for_same_length_edits_without_exposing_the_body():
    running = _item()
    completed = {**_item(), "body": _tracker(status="Pending")}
    assert len(running["body"]) == len(completed["body"])
    assert tracker_digest(running) != tracker_digest(completed)
    assert tracker_digest(running) == tracker_digest(dict(running))
    assert "Running" not in tracker_digest(running)


def test_grace_starts_once_and_is_capped_by_the_timeout_remainder():
    grace = CompletedGrace(seconds=30)
    assert not grace.active and grace.remaining(0) is None
    grace.observe_completed(now=100, timeout_remaining=10)
    assert grace.remaining(100) == 10
    grace.observe_completed(now=105, timeout_remaining=100)
    assert grace.remaining(105) == 5
    assert not grace.expired(109.9)
    assert grace.expired(110)


def test_grace_defaults_to_the_full_window_and_resets_on_running():
    grace = CompletedGrace(seconds=30)
    grace.observe_completed(now=0, timeout_remaining=1800)
    assert grace.remaining(10) == 20
    grace.reset()
    assert not grace.active
    grace.observe_completed(now=50, timeout_remaining=1800)
    assert grace.remaining(50) == 30


# --- v1 snapshot -> trigger restore -> offline evaluation (#1274) ----------------

from orchestune.review.markers import build_trigger_body  # noqa: E402
from orchestune.review.offline import evaluate_snapshot  # noqa: E402

HEAD = "a" * 40
OTHER = "d" * 40
OBSERVED = datetime(2026, 10, 10, 3, 28, tzinfo=UTC)
CODEX = "chatgpt-codex-connector[bot]"


def _judgments(sources, round_num=1):
    rows = [
        {
            "source": source,
            "location": "app.py:1",
            "judgment": "adopt",
            "status": "resolved",
            "basis": "contract regression",
            "evidence": "commit abc",
        }
        for source in sources
    ]
    document = yaml.safe_dump({"round": round_num, "findings": rows})
    return f"```orchestune-review-judgments\n{document}```\n"


def _trigger(id_, round_num, created, head=HEAD, judged=()):
    reply = _judgments(judged, round_num - 1) if round_num > 1 else ""
    return {
        "id": id_,
        "body": build_trigger_body(reply, "codex", round_num, head),
        "created_at": created,
        "user": {"login": "worker"},
    }


def _reused(status="Running", updated="2026-10-10T03:22:00Z", commit="`aaaaaaa`"):
    return {
        "id": 50,
        "body": _tracker(status=status, commit=commit),
        "created_at": "2026-10-10T03:11:00Z",
        "updated_at": updated,
        "user": {"login": CODEX},
    }


def _codex_review(id_, at):
    return {
        "id": id_,
        "body": "Real review.",
        "submitted_at": at,
        "commit_id": HEAD,
        "user": {"login": CODEX},
    }


def _snapshot(comments, reviews=()):
    return {
        "snapshot_version": 1,
        "repository": "owner/repo",
        "pr_number": 7,
        "acquisition": {
            "source": "github_mcp",
            "started_at": "2026-10-10T03:27:40Z",
            "observed_at": "2026-10-10T03:28:00Z",
            "head_before": {"sha": HEAD, "fetched_at": "2026-10-10T03:27:40Z"},
            "head_after": {"sha": HEAD, "fetched_at": "2026-10-10T03:28:00Z"},
        },
        "completeness": dict.fromkeys(
            ("issue_comments", "reviews", "inline_comments"), "complete"
        ),
        "issue_comments": list(comments),
        "reviews": list(reviews),
        "inline_comments": [],
    }


def _evaluate(value, **kwargs):
    return evaluate_snapshot(
        value, bot_name="codex", pr_number=7, now=OBSERVED, **kwargs
    )


def _two_rounds(*extra, reviews=(), round2="2026-10-10T03:20:00Z", judged=()):
    return _snapshot(
        [
            _trigger(1, 1, "2026-10-10T03:10:00Z"),
            _trigger(2, 2, round2, judged=judged),
            *extra,
        ],
        reviews,
    )


def test_offline_recycled_running_tracker_is_in_progress_for_the_latest_round():
    outcome = _evaluate(_two_rounds(_reused("Running")), requested_round=2)
    assert outcome.exit_code == 11
    assert outcome.payload["acquisition_status"] == "in_progress"


def test_offline_recycled_running_tracker_holds_a_partial_review_open():
    snapshot = _two_rounds(
        _reused("Running"), reviews=[_codex_review(60, "2026-10-10T03:21:00Z")]
    )
    assert _evaluate(snapshot, requested_round=2).exit_code == 11


def test_offline_completed_tracker_with_a_real_review_is_acquired():
    snapshot = _two_rounds(
        _reused("Completed"), reviews=[_codex_review(60, "2026-10-10T03:21:00Z")]
    )
    outcome = _evaluate(snapshot, requested_round=2)
    assert outcome.exit_code == 0
    assert outcome.payload["review_body"] == "Real review."


def test_offline_completed_tracker_alone_is_unavailable():
    outcome = _evaluate(_two_rounds(_reused("Completed")), requested_round=2)
    assert outcome.exit_code == 30
    assert outcome.payload["acquisition_status"] == "unavailable"
    assert outcome.payload["review_body"] == ""


def test_offline_untouched_round_one_tracker_is_not_round_two_progress():
    snapshot = _two_rounds(_reused("Running", updated="2026-10-10T03:12:00Z"))
    assert _evaluate(snapshot, requested_round=2).exit_code == 30


def test_offline_tracker_for_another_commit_does_not_hold_the_round_open():
    snapshot = _two_rounds(
        _reused("Running", commit="`ddddddd`"),
        reviews=[_codex_review(60, "2026-10-10T03:21:00Z")],
    )
    assert _evaluate(snapshot, requested_round=2).exit_code == 0


def test_offline_closed_round_ignores_a_tracker_state_written_after_its_end():
    review = _codex_review(60, "2026-10-10T03:15:00Z")
    snapshot = _two_rounds(_reused("Running"), reviews=[review], judged=["review:60"])
    outcome = _evaluate(snapshot, requested_round=1)
    assert outcome.exit_code == 0


@pytest.mark.parametrize(
    ("updated", "exit_code"),
    [
        ("2026-10-10T03:20:00.400Z", 30),  # before the trigger (…00.900) by sub-seconds
        ("2026-10-10T03:20:00.899999Z", 30),
        ("2026-10-10T03:20:00.900Z", 11),  # exactly at the trigger: start-inclusive
        ("2026-10-10T12:20:01+09:00", 11),  # offset spelling of 03:20:01Z
    ],
)
def test_offline_latest_round_start_keeps_sub_second_precision(updated, exit_code):
    snapshot = _two_rounds(
        _reused("Running", updated=updated), round2="2026-10-10T03:20:00.900Z"
    )
    assert _evaluate(snapshot, requested_round=2).exit_code == exit_code


@pytest.mark.parametrize(
    ("updated", "exit_code"),
    [
        ("2026-10-10T03:20:00.899999Z", 11),  # still inside round 1
        ("2026-10-10T03:20:00.900Z", 30),  # end-exclusive
        ("2026-10-10T03:20:00.950Z", 30),
    ],
)
def test_offline_closed_round_end_keeps_sub_second_precision(updated, exit_code):
    snapshot = _two_rounds(
        _reused("Running", updated=updated), round2="2026-10-10T03:20:00.900Z"
    )
    assert _evaluate(snapshot, requested_round=1).exit_code == exit_code


def test_offline_invalid_tracker_timestamp_is_still_a_contract_error():
    snapshot = _two_rounds(_reused("Running", updated="not-a-time"))
    with pytest.raises(Exception, match="updated_at"):
        _evaluate(snapshot, requested_round=2)

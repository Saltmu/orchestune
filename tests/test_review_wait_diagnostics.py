"""#1388: finite review waits and their Exit 20/21 diagnostics.

Reproduces the PR #1387 shape (a Codex tracker left Running after the Round 2
trigger) on a fake monotonic clock, so no test sleeps in real time.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from unittest.mock import patch

import pytest

from orchestune.review.markers import mark_review_trigger, review_head_marker
from scripts.review_poll import track_stall
from scripts.review_wait_diagnostics import (
    EXIT_STALLED,
    EXIT_TIMEOUT,
    ReviewTimeoutError,
    StalledReviewError,
    diagnostic_result,
)
from scripts.wait_for_review import main, wait_for_review

MARKER = "<!-- codex-pull-request-review-summary -->"
CODEX = "chatgpt-codex-connector[bot]"
TRIGGER_AT = "2026-10-22T17:51:37Z"
REQUESTED = "abc1234" + "0" * 33
CURRENT = "f" * 40


def _tracker_body(status: str, note: str = "") -> str:
    return (
        f"{MARKER}\n\n## Codex Review Summary\n\n"
        "| Review | Status | Commit | Review trigger |\n"
        "| --- | --- | --- | --- |\n"
        f"| 📝 **Code Review** | 🔄 **{status}** since 2026-10-22T17:51:59Z{note} "
        "| `abc1234` | Manual request |\n"
    )


def _tracker(status: str, updated_at: str, note: str = "") -> dict[str, object]:
    return {
        "id": 6347467600,
        "user": {"login": CODEX},
        "created_at": "2026-10-22T16:00:00Z",
        "updated_at": updated_at,
        "body": _tracker_body(status, note),
    }


def _state(comments=(), reviews=()) -> dict[str, list[dict[str, object]]]:
    return {
        "issue_comments": list(comments),
        "reviews": list(reviews),
        "inline_comments": [],
    }


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.now += seconds


def _drive(snapshot_at, *, timeout=1800, interval=5, stall_grace=600):
    """Run wait_for_review where each poll returns `snapshot_at(now)`."""
    clock = _Clock()
    trigger = {"id": 6347467561, "created_at": TRIGGER_AT, "body": "@codex review"}

    def fetch(pr_number, executor=None):
        return snapshot_at(clock.now)

    with (
        patch("scripts.wait_for_review._get_pr_data", side_effect=fetch),
        patch("scripts.wait_for_review.post_review_trigger", return_value=trigger),
        patch("scripts.wait_for_review._monotonic", side_effect=lambda: clock.now),
        patch("scripts.wait_for_review._sleep", side_effect=clock.sleep),
        patch("scripts.wait_for_review._fetch_pr_head_sha", return_value=REQUESTED),
        patch("scripts.wait_for_review._fetch_repository_slug", return_value="o/r"),
        # Wall-clock jumps must not matter: only the monotonic clock is a deadline.
        patch("time.time", side_effect=lambda: 1e9 - clock.now * 1000),
    ):
        try:
            return wait_for_review(
                pr_number=1387,
                timeout=timeout,
                interval=interval,
                bot_name="codex",
                stall_grace_seconds=stall_grace,
                round_num=None,
            ), clock
        except Exception as error:
            error.clock = clock  # type: ignore[attr-defined]
            raise


def test_unchanged_current_running_tracker_stalls_with_context():
    running = _tracker("Running", "2026-10-22T17:51:59Z")
    with pytest.raises(StalledReviewError) as caught:
        _drive(lambda now: _state() if now == 0 else _state([running]))
    error = caught.value
    assert error.clock.now == pytest.approx(605)  # type: ignore[attr-defined]
    context = error.context
    assert context is not None
    assert (context.pr_number, context.reviewer, context.round) == (1387, "codex", 1)
    assert context.trigger_id == 6347467561 and context.triggered_at == TRIGGER_AT
    assert context.requested_head_sha == REQUESTED
    assert context.stall_grace_seconds == 600 and context.timeout_seconds == 1800
    tracker = context.last_tracker
    assert tracker is not None and tracker.id == 6347467600
    assert tracker.status == "in_progress"
    assert tracker.updated_at == "2026-10-22T17:51:59Z"
    assert tracker.body_sha256 == hashlib.sha256(running["body"].encode()).hexdigest()
    assert [item["id"] for item in context.observed_items] == [6347467600]


def test_no_activity_times_out_on_the_monotonic_budget():
    with pytest.raises(ReviewTimeoutError) as caught:
        _drive(lambda now: _state(), timeout=60, interval=7)
    error = caught.value
    # Sleeps never exceed the remaining budget, so the wait ends exactly on it.
    assert error.clock.now == pytest.approx(60)  # type: ignore[attr-defined]
    assert error.context is not None and error.context.last_tracker is None
    assert error.context.elapsed_seconds == pytest.approx(60)


def test_progressing_running_tracker_still_ends_at_the_total_timeout():
    def snapshot(now: float):
        # A new same-length edit every poll keeps resetting the stall grace.
        stamp = f"2026-10-22T17:{52 + int(now) // 60:02d}:{int(now) % 60:02d}Z"
        return _state([_tracker("Running", stamp)])

    with pytest.raises(ReviewTimeoutError) as caught:
        _drive(snapshot, timeout=300, interval=5, stall_grace=10)
    assert caught.value.clock.now == pytest.approx(300)  # type: ignore[attr-defined]
    assert caught.value.context.last_tracker.status == "in_progress"  # type: ignore[union-attr]


def test_previous_round_running_tracker_is_a_timeout_not_a_stall():
    old = _tracker(
        "Running", "2026-10-22T17:40:00Z"
    )  # untouched since before the trigger
    with pytest.raises(ReviewTimeoutError) as caught:
        _drive(lambda now: _state([old]), timeout=900, stall_grace=60)
    assert caught.value.context.last_tracker is None  # type: ignore[union-attr]
    assert caught.value.context.observed_items == ()  # type: ignore[union-attr]


def test_track_stall_ignores_wall_clock_and_resets_on_progress():
    running = _tracker("Running", "2026-10-22T17:51:59Z")
    edited = _tracker("Running", "2026-10-22T17:51:59Z", note="!")
    kwargs = dict(
        stall_grace_seconds=600,
        bot_name="codex",
        pr_number=1387,
        latest_trigger_time=TRIGGER_AT,
        requested_head_sha=REQUESTED,
    )
    ticks = iter([0.0, 599.0, 599.0, 1198.0, 1199.0]).__next__
    # Wall-clock corrections in either direction must be irrelevant.
    with patch("time.time", side_effect=[1e9, -1e9, 0.0, 1e9, -1e9]):
        state = track_stall(running, None, None, clock=ticks, **kwargs)
        state = track_stall(running, *state, clock=ticks, **kwargs)  # 599s: not yet
        state = track_stall(edited, *state, clock=ticks, **kwargs)  # progress resets
        assert state[1] == 599.0
        state = track_stall(edited, *state, clock=ticks, **kwargs)  # 599s again
        with pytest.raises(StalledReviewError):
            track_stall(edited, *state, clock=ticks, **kwargs)  # 600s


def test_track_stall_trips_exactly_at_the_grace():
    running = _tracker("Running", "2026-10-22T17:51:59Z")
    kwargs = dict(
        stall_grace_seconds=600,
        bot_name="codex",
        pr_number=1387,
        latest_trigger_time=TRIGGER_AT,
        requested_head_sha=REQUESTED,
    )
    ticks = iter([10.0, 609.0, 610.0]).__next__
    state = track_stall(running, None, None, clock=ticks, **kwargs)
    state = track_stall(running, *state, clock=ticks, **kwargs)
    with pytest.raises(StalledReviewError):
        track_stall(running, *state, clock=ticks, **kwargs)


# --- CLI: --output-file always reflects this run ---------------------------------


def _argv(output, *extra):
    return [
        "wait_for_review.py",
        "--pr",
        "1387",
        "--bot-name",
        "codex",
        "--output-file",
        str(output),
        *extra,
    ]


def _cli(output, error, head, *extra):
    with (
        patch("sys.argv", _argv(output, *extra)),
        patch("scripts.wait_for_review.wait_for_review", side_effect=error),
        patch("scripts.wait_for_review._fetch_pr_head_sha", return_value=head),
        pytest.raises(SystemExit) as caught,
    ):
        main()
    return caught.value.code


def _context_error(cls):
    running = _tracker("Running", "2026-10-22T17:51:59Z")
    with pytest.raises(cls) as caught:
        if cls is StalledReviewError:
            _drive(lambda now: _state([running]))
        else:
            _drive(lambda now: _state(), timeout=30)
    assert caught.value.context is not None
    return caught.value


@pytest.mark.parametrize(
    ("cls", "code", "reason_code"),
    [
        (ReviewTimeoutError, EXIT_TIMEOUT, "review_timeout"),
        (StalledReviewError, EXIT_STALLED, "review_stalled"),
    ],
)
def test_exit_20_21_replace_an_old_success_with_this_runs_diagnostic(
    tmp_path, cls, code, reason_code
):
    output = tmp_path / "review.json"
    output.write_text(json.dumps({"acquisition_status": "acquired"}), encoding="utf-8")
    error = _context_error(cls)

    assert _cli(output, error, CURRENT) == code

    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["acquisition_status"] == "unavailable"
    assert (record["reason_code"], record["exit_code"]) == (reason_code, code)
    assert record["repository"] == "o/r" and record["pr_number"] == 1387
    assert record["reviewer"] == "codex" and record["round"] == 1
    assert record["trigger_id"] == 6347467561 and record["triggered_at"] == TRIGGER_AT
    assert record["requested_head_sha"] == REQUESTED
    assert record["current_head_sha"] == CURRENT
    assert record["reviewed_head_sha"] is None and record["review_target_sha"] is None
    assert record["review_target_sha_source"] == "unknown"
    assert record["review_items"] == [] and record["review_body"] == ""
    assert set(record["completeness"].values()) == {"complete"}
    assert record["wait_diagnostics"]["timeout_seconds"] in (30, 1800)
    if cls is StalledReviewError:
        assert record["wait_diagnostics"]["last_tracker"]["status"] == "in_progress"


def test_unknown_current_head_stays_null_and_is_not_the_requested_head(tmp_path):
    output = tmp_path / "review.json"
    error = _context_error(ReviewTimeoutError)
    assert _cli(output, error, None) == EXIT_TIMEOUT
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["current_head_sha"] is None
    assert record["requested_head_sha"] == REQUESTED


def test_a_timeout_without_poll_context_does_not_invent_values(tmp_path):
    output = tmp_path / "review.json"
    assert _cli(output, TimeoutError("initial fetch"), None, "--round", "2") == 20
    record = json.loads(output.read_text(encoding="utf-8"))
    assert (record["pr_number"], record["reviewer"], record["round"]) == (
        1387,
        "codex",
        2,
    )
    for key in ("repository", "trigger_id", "triggered_at", "requested_head_sha"):
        assert record[key] is None
    assert set(record["completeness"].values()) == {"unknown"}
    assert record["wait_diagnostics"]["last_tracker"] is None


def test_failed_last_poll_is_not_declared_complete():
    error = ReviewTimeoutError("t")
    good = _context_error(ReviewTimeoutError).context
    assert good is not None
    error.attach(dataclasses.replace(good, last_poll_ok=False))
    record = diagnostic_result(
        error, pr_number=1, reviewer="codex", round_num=None, current_head_sha=None
    )
    assert set(record["completeness"].values()) == {"unknown"}


def test_unwritable_output_exits_2_and_leaves_no_stale_success(tmp_path):
    output = tmp_path / "review.json"
    output.write_text(json.dumps({"acquisition_status": "acquired"}), encoding="utf-8")
    error = _context_error(ReviewTimeoutError)
    with patch("scripts.review_cli.os.replace", side_effect=OSError("disk full")):
        code = _cli(output, error, CURRENT)
    assert code == 2
    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


def test_missing_output_directory_exits_2(tmp_path, capsys):
    output = tmp_path / "missing" / "review.json"
    error = _context_error(StalledReviewError)
    assert _cli(output, error, CURRENT) == 2
    assert "Exit 21" in capsys.readouterr().err


def test_success_path_still_writes_atomically(tmp_path):
    from scripts.review_cli import write_output_file

    output = tmp_path / "review.json"
    output.write_text("old", encoding="utf-8")
    write_output_file({"acquisition_status": "acquired"}, str(output))
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "acquisition_status": "acquired"
    }
    assert [p.name for p in tmp_path.iterdir()] == ["review.json"]


def test_no_post_resume_timeout_does_not_post_or_switch_reviewer(tmp_path):
    """A finite --no-post resume creates no trigger/round and keeps the reviewer."""
    trigger = {
        "id": 6347467561,
        "user": {"login": "Saltmu"},
        "created_at": TRIGGER_AT,
        "body": mark_review_trigger("@codex review", "codex", 1)
        + "\n"
        + review_head_marker(REQUESTED),
    }
    clock = _Clock()
    with (
        patch("scripts.wait_for_review._get_pr_data", return_value=_state([trigger])),
        patch("scripts.wait_for_review.post_review_trigger") as post,
        patch("scripts.wait_for_review._monotonic", side_effect=lambda: clock.now),
        patch("scripts.wait_for_review._sleep", side_effect=clock.sleep),
        patch("scripts.wait_for_review._fetch_repository_slug", return_value="o/r"),
        pytest.raises(ReviewTimeoutError) as caught,
    ):
        wait_for_review(
            pr_number=1387, timeout=20, interval=5, bot_name="codex", post_trigger=False
        )
    post.assert_not_called()
    assert caught.value.context is not None
    assert caught.value.context.reviewer == "codex"
    assert caught.value.context.trigger_id == 6347467561

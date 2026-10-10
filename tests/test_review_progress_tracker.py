import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from orchestune.review.progress_tracker import (
    CodexTrackerStatus,
    parse_codex_tracker_commit,
    parse_codex_tracker_status,
)
from scripts.wait_for_review import (
    StalledReviewError,
    _check_immediate_review_result,
    _run_online,
    _track_stall,
    wait_for_review,
)

MARKER = "<!-- codex-pull-request-review-summary -->"


@pytest.fixture(autouse=True)
def _no_network_sha_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scripts.wait_for_review._fetch_pr_head_sha", lambda _: None)
    monkeypatch.setattr("scripts.wait_for_review._fetch_repository_slug", lambda: None)


def tracker(status: str = "Running") -> str:
    return (
        f"{MARKER}\n\n"
        "## Codex Review Summary\n"
        "This comment shows the latest Codex review activity.\n\n"
        "| Review | Status | Commit | Review trigger |\n"
        "| --- | --- | --- | --- |\n"
        f"| 📝 **Code Review** | 🔄 **{status}** since "
        '<relative-time datetime="2026-10-07T02:51:59Z">'
        "2026-10-07T02:51:59Z</relative-time> | `abc1234` | Manual request |\n"
        "\n<details><summary>About Codex</summary>Reviews are running.</details>"
    )


def test_running_tracker_status_comes_from_code_review_row() -> None:
    assert parse_codex_tracker_status(tracker()) is CodexTrackerStatus.IN_PROGRESS


def test_pending_tracker_status_is_in_progress() -> None:
    assert (
        parse_codex_tracker_status(tracker("Pending")) is CodexTrackerStatus.IN_PROGRESS
    )


def test_completed_tracker_status_ignores_running_text_elsewhere() -> None:
    assert (
        parse_codex_tracker_status(tracker("Completed")) is CodexTrackerStatus.COMPLETED
    )


def test_status_from_another_row_does_not_override_code_review() -> None:
    body = tracker("Completed").replace(
        "| --- | --- | --- | --- |",
        "| --- | --- | --- | --- |\n"
        "| Security Review | 🔄 **Running** | `abc1234` | Manual request |",
    )
    assert parse_codex_tracker_status(body) is CodexTrackerStatus.COMPLETED


def test_unknown_and_malformed_trackers_are_not_successful_states() -> None:
    assert parse_codex_tracker_status(tracker("Retrying")) is CodexTrackerStatus.UNKNOWN
    assert parse_codex_tracker_status(MARKER) is CodexTrackerStatus.UNKNOWN


def test_marker_absence_is_not_a_codex_tracker() -> None:
    assert parse_codex_tracker_status("Code Review is Running") is None


def test_unchanged_codex_running_tracker_uses_existing_stall_grace() -> None:
    item = _tracker_comment("Running", updated_at="2026-10-04T03:11:00Z")
    with patch("scripts.wait_for_review.time.time", side_effect=[100.0, 106.0]):
        signature, since = _track_stall(
            item,
            None,
            None,
            stall_grace_seconds=5,
            bot_name="codex",
            pr_number=1271,
            latest_trigger_time="2026-10-04T03:10:00Z",
        )
        with pytest.raises(StalledReviewError, match="PR #1271"):
            _track_stall(
                item,
                signature,
                since,
                stall_grace_seconds=5,
                bot_name="codex",
                pr_number=1271,
                latest_trigger_time="2026-10-04T03:10:00Z",
            )


def _tracker_comment(status: str, *, updated_at: str) -> dict[str, object]:
    return {
        "id": 201,
        "user": {"login": "chatgpt-codex-connector[bot]"},
        "created_at": "2026-10-04T03:11:00Z",
        "updated_at": updated_at,
        "body": tracker(status),
    }


@patch("scripts.wait_for_review._get_pr_data", autospec=True)
@patch("scripts.wait_for_review.post_review_trigger", autospec=True)
def test_online_waits_for_codex_tracker_and_acquires_real_review(
    mock_post, mock_get_data
):
    mock_post.return_value = {
        "id": 200,
        "created_at": "2026-10-04T03:10:00Z",
        "body": "@codex review",
    }
    running = _tracker_comment("Running", updated_at="2026-10-04T03:11:00Z")
    partial_inline = {
        "id": 301,
        "pull_request_review_id": 401,
        "user": {"login": "chatgpt-codex-connector[bot]"},
        "created_at": "2026-10-04T03:12:00Z",
        "body": "Partial finding while the review is running.",
    }
    completed = {
        **running,
        "updated_at": "2026-10-04T03:14:00Z",
        "body": tracker("Completed"),
    }
    real_review = {
        "id": 402,
        "user": {"login": "chatgpt-codex-connector[bot]"},
        "created_at": "2026-10-04T03:15:00Z",
        "updated_at": "2026-10-04T03:15:00Z",
        "body": "### Review complete\nActual review result.",
    }
    empty = {"issue_comments": [], "reviews": [], "inline_comments": []}
    mock_get_data.side_effect = [
        empty,
        {"issue_comments": [running], "reviews": [], "inline_comments": []},
        {
            "issue_comments": [running],
            "reviews": [],
            "inline_comments": [partial_inline],
        },
        {
            "issue_comments": [completed],
            "reviews": [],
            "inline_comments": [partial_inline],
        },
        {
            "issue_comments": [completed, real_review],
            "reviews": [],
            "inline_comments": [partial_inline],
        },
    ]

    result = wait_for_review(
        pr_number=1271,
        timeout=10,
        interval=0,
        bot_name="codex",
        post_trigger=True,
        stall_grace_seconds=1000,
    )

    assert result["review_body"] == "### Review complete\nActual review result."
    assert [item["id"] for item in result["inline_comments"]] == [301]


@patch("scripts.wait_for_review._get_pr_data", autospec=True)
@patch("scripts.wait_for_review.post_review_trigger", autospec=True)
def test_online_acquires_codex_inline_content_after_tracker_completes(
    mock_post, mock_get_data
):
    mock_post.return_value = {
        "id": 200,
        "created_at": "2026-10-04T03:10:00Z",
        "body": "@codex review",
    }
    running = _tracker_comment("Running", updated_at="2026-10-04T03:11:00Z")
    completed = {
        **running,
        "updated_at": "2026-10-04T03:13:00Z",
        "body": tracker("Completed"),
    }
    empty = {"issue_comments": [], "reviews": [], "inline_comments": []}
    review = {
        "id": 401,
        "user": {"login": "chatgpt-codex-connector[bot]"},
        "submitted_at": "2026-10-04T03:14:00Z",
        "body": "",
    }
    inline = {
        "id": 301,
        "pull_request_review_id": 401,
        "user": {"login": "chatgpt-codex-connector[bot]"},
        "created_at": "2026-10-04T03:14:30Z",
        "body": "A real inline finding.",
    }
    mock_get_data.side_effect = [
        empty,
        {"issue_comments": [running], "reviews": [], "inline_comments": []},
        {
            "issue_comments": [completed],
            "reviews": [review],
            "inline_comments": [inline],
        },
    ]

    result = wait_for_review(
        pr_number=1271,
        timeout=10,
        interval=0,
        bot_name="codex",
        post_trigger=True,
        stall_grace_seconds=1000,
    )

    assert "see 1 inline comment(s)" in result["review_body"]
    assert [item["id"] for item in result["inline_comments"]] == [301]


# --- #1274: Commit cell of the Code Review row ---------------------------------


def _with_commit(cell: str) -> str:
    return tracker().replace("`abc1234`", cell)


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("`abc1234`", "abc1234"),
        ("`ABC1234`", "abc1234"),
        ("abc1234", "abc1234"),
        ("`" + "a" * 40 + "`", "a" * 40),
        ("[`abc1234`](https://github.com/o/r/commit/abc1234)", "abc1234"),
    ],
)
def test_tracker_commit_is_a_hex_prefix_from_the_commit_column(cell, expected) -> None:
    assert parse_codex_tracker_commit(_with_commit(cell)) == expected


@pytest.mark.parametrize(
    "cell", ["`abc12`", "`" + "a" * 41 + "`", "`not-a-sha`", "", "`abc1234` `def5678`"]
)
def test_tracker_commit_missing_or_invalid_is_none(cell) -> None:
    assert parse_codex_tracker_commit(_with_commit(cell)) is None


def test_tracker_commit_is_none_without_marker_or_commit_column() -> None:
    assert parse_codex_tracker_commit("| Review | Commit |\n| --- | --- |") is None
    no_column = tracker().replace("| Commit ", "| Other ")
    assert parse_codex_tracker_commit(no_column) is None


def test_tracker_commit_ignores_other_rows() -> None:
    other = tracker() + "\n| 🔧 **Fix** | Done | `1111111` | x |"
    assert parse_codex_tracker_commit(other) == "abc1234"


# --- #1274: one tracker comment reused by Round 2 and later ---------------------

CODEX = "chatgpt-codex-connector[bot]"
TRIGGER_AT = "2026-10-04T03:20:00Z"


def _reused(status: str, updated_at: str, **extra: object) -> dict[str, object]:
    """Round 1's tracker (created 03:11), edited by the Round 2 review."""
    return {**_tracker_comment(status, updated_at=updated_at), **extra}


def _review(id_: int, at: str, body: str = "Real review.") -> dict[str, object]:
    return {
        "id": id_,
        "user": {"login": CODEX},
        "submitted_at": at,
        "commit_id": "a" * 40,
        "body": body,
    }


def _state(comments=(), reviews=(), inlines=()) -> dict[str, list[dict[str, object]]]:
    return {
        "issue_comments": list(comments),
        "reviews": list(reviews),
        "inline_comments": list(inlines),
    }


class _Clock:
    now = 0.0


def _run(states, *, grace=30, timeout=10, post_trigger=True, **kwargs):
    """Drive wait_for_review over (monotonic_time, state-or-Exception) steps."""
    clock = _Clock()
    steps = iter(states)
    last: list[object] = []

    def fetch(pr_number, executor=None):
        if not last:  # the initial capture
            last.append(next(steps))
        else:
            moment, value = next(steps)
            clock.now = moment
            last[:] = [(moment, value)]
        _, value = last[0]
        if isinstance(value, Exception):
            raise value
        return value

    trigger = {"id": 200, "created_at": TRIGGER_AT, "body": "@codex review"}
    with (
        patch("scripts.wait_for_review._get_pr_data", side_effect=fetch),
        patch("scripts.wait_for_review.post_review_trigger", return_value=trigger),
        patch("scripts.wait_for_review._monotonic", side_effect=lambda: clock.now),
    ):
        return wait_for_review(
            pr_number=1274,
            timeout=timeout,
            interval=0,
            bot_name="codex",
            post_trigger=post_trigger,
            stall_grace_seconds=1000,
            completed_grace_seconds=grace,
            **kwargs,
        )


EMPTY = (0, _state())


def test_round_two_waits_for_recycled_running_then_acquires_the_real_review():
    running = _reused("Running", "2026-10-04T03:21:00Z")
    completed = _reused("Completed", "2026-10-04T03:24:00Z")
    partial = _review(402, "2026-10-04T03:22:00Z", "Partial.")
    result = _run(
        [
            EMPTY,
            (1, _state([running])),
            (2, _state([running], [partial])),  # a partial review must not end it
            (3, _state([completed], [partial, _review(403, "2026-10-04T03:23:00Z")])),
        ]
    )
    assert result["acquisition_status"] == "acquired"
    assert "Real review." in result["review_body"]


def test_round_two_completed_tracker_with_review_does_not_time_out_on_created_at():
    completed = _reused("Completed", "2026-10-04T03:24:00Z")
    review = _review(403, "2026-10-04T03:23:00Z")
    result = _run([EMPTY, (1, _state([completed], [review]))])
    assert result["acquisition_status"] == "acquired"


def test_completed_tracker_only_becomes_unavailable_after_the_grace():
    completed = _reused("Completed", "2026-10-04T03:24:00Z")
    result = _run(
        [
            EMPTY,
            (100, _state([completed])),
            (110, _state([completed])),
            (131, _state([completed])),
        ]
    )
    assert result["acquisition_status"] == "unavailable"
    assert result["review_body"] == "" and result["review_items"] == []
    assert result["reviewer"] == "codex" and result["round"] == 1
    assert result["trigger_id"] == 200 and result["triggered_at"] == TRIGGER_AT
    assert "Completed" in result["reason"]
    assert result["review_target_sha"] is None


def test_review_arriving_inside_the_grace_is_acquired():
    completed = _reused("Completed", "2026-10-04T03:24:00Z")
    late = _review(403, "2026-10-04T03:25:00Z")
    result = _run(
        [EMPTY, (100, _state([completed])), (120, _state([completed], [late]))]
    )
    assert result["acquisition_status"] == "acquired"


def test_unchanged_completed_does_not_extend_the_grace():
    completed = _reused("Completed", "2026-10-04T03:24:00Z")
    states = [EMPTY] + [(100 + 10 * i, _state([completed])) for i in range(5)]
    result = _run(states)
    assert result["acquisition_status"] == "unavailable"


def test_running_again_resets_the_grace_and_a_new_completed_starts_a_new_one():
    running = _reused("Running", "2026-10-04T03:25:00Z")
    first = _reused("Completed", "2026-10-04T03:24:00Z")
    second = _reused("Completed", "2026-10-04T03:27:00Z")
    result = _run(
        [
            EMPTY,
            (100, _state([first])),  # grace until 130
            (120, _state([running])),  # reset
            (125, _state([second])),  # new grace until 155
            (140, _state([second])),  # past the first deadline, inside the second
            (156, _state([second])),
        ]
    )
    assert result["acquisition_status"] == "unavailable"


def test_a_completed_only_tracker_at_the_timeout_is_unavailable_not_a_timeout():
    completed = _reused("Completed", "2026-10-04T03:24:00Z")
    result = _run([EMPTY, (1, _state([completed]))], timeout=0)
    assert result["acquisition_status"] == "unavailable"


def test_api_errors_after_completed_are_not_read_as_no_content():
    completed = _reused("Completed", "2026-10-04T03:24:00Z")
    late = _review(403, "2026-10-04T03:25:00Z")
    result = _run(
        [
            EMPTY,
            (100, _state([completed])),
            (140, RuntimeError("gh api failed")),  # past the deadline, but an error
            (141, _state([completed], [late])),
        ],
        max_retries=1,
    )
    assert result["acquisition_status"] == "acquired"


def test_no_response_to_the_new_trigger_still_times_out():
    untouched = _reused("Completed", "2026-10-04T03:14:00Z")
    with pytest.raises(TimeoutError):
        _run([EMPTY, (1, _state([untouched]))], timeout=0)


def test_immediate_path_waits_on_recycled_running_even_with_a_partial_review():
    running = _reused("Running", "2026-10-04T03:21:00Z")
    data = _state([running], [_review(402, "2026-10-04T03:22:00Z")])
    assert _check_immediate_review_result(data, "codex", TRIGGER_AT, 2) is None


def test_immediate_path_acquires_a_completed_recycled_tracker_with_a_review():
    completed = _reused("Completed", "2026-10-04T03:24:00Z")
    data = _state([completed], [_review(402, "2026-10-04T03:22:00Z")])
    result = _check_immediate_review_result(data, "codex", TRIGGER_AT, 2)
    assert result is not None and result["acquisition_status"] == "acquired"


def test_immediate_path_does_not_treat_a_lone_completed_tracker_as_content():
    completed = _reused("Completed", "2026-10-04T03:24:00Z")
    assert (
        _check_immediate_review_result(_state([completed]), "codex", TRIGGER_AT, 2)
        is None
    )


def test_recycled_running_tracker_counts_for_stall_detection():
    item = _reused("Running", "2026-10-04T03:21:00Z")
    kwargs = dict(
        stall_grace_seconds=5,
        bot_name="codex",
        pr_number=1274,
        latest_trigger_time=TRIGGER_AT,
    )
    with patch("scripts.wait_for_review.time.time", side_effect=[100.0, 106.0]):
        signature, since = _track_stall(item, None, None, **kwargs)
        assert signature is not None
        with pytest.raises(StalledReviewError):
            _track_stall(item, signature, since, **kwargs)


def test_untouched_old_tracker_is_not_a_stall_of_the_new_trigger():
    item = _reused("Running", "2026-10-04T03:14:00Z")
    result = _track_stall(
        item,
        None,
        None,
        stall_grace_seconds=5,
        bot_name="codex",
        pr_number=1274,
        latest_trigger_time=TRIGGER_AT,
    )
    assert result == (None, None)


def test_same_length_tracker_edit_changes_the_stall_signature():
    kwargs = dict(
        stall_grace_seconds=1000,
        bot_name="codex",
        pr_number=1274,
        latest_trigger_time=TRIGGER_AT,
    )
    running = _reused("Running", "2026-10-04T03:21:00Z")
    pending = _reused("Pending", "2026-10-04T03:21:00Z")
    first, _ = _track_stall(running, None, None, **kwargs)
    second, _ = _track_stall(pending, first, 1.0, **kwargs)
    assert first != second and "Running" not in str(first)


def _args(tmp_path, **overrides):
    values = dict(
        timeout=1,
        interval=0,
        bot_name="codex",
        no_post=False,
        body=None,
        body_file=None,
        max_rounds=5,
        max_retries=1,
        round=None,
        stall_grace=600,
        switch_reviewer=False,
        jev_threshold=None,
        pr=1274,
        output_file=str(tmp_path / "out.json"),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("result", "code"),
    [
        ({"acquisition_status": "acquired", "reviewer": "codex"}, 0),
        ({"acquisition_status": "unavailable", "reviewer": "codex"}, 30),
        ({"acquisition_status": "unavailable", "reviewer": "skip"}, 0),
    ],
)
def test_run_online_exit_code_matches_the_acquisition_status(tmp_path, result, code):
    args = _args(tmp_path)
    with patch("scripts.wait_for_review.wait_for_review", return_value=result):
        with pytest.raises(SystemExit) as exit_info:
            _run_online(args)
    assert exit_info.value.code == code
    assert json.loads((tmp_path / "out.json").read_text()) == result

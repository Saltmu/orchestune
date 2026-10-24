"""One polling step of `scripts/wait_for_review.py`: stall tracking and the
Completed-tracker grace (#1274).

Kept apart from the script so the wait loop stays small. Everything the script
patches in tests (`_extract_review_result`, `_fetch_pr_head_sha`, the monotonic
clock) is injected rather than imported, so those patches keep working.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from orchestune.review.acquisition import (
    ACQUISITION_UNAVAILABLE,
    SCHEMA_VERSION,
    _activity_signature,
    _build_snapshot,
    _filter_bot_items,
    _get_item_created_timestamp,
    _get_item_timestamp,
    _in_activity_round,
    _is_explicitly_in_progress,
    _latest_bot_activity_item,
    _latest_bot_summary_item,
    _latest_current_tracker,
)
from orchestune.review.progress_tracker import (
    CodexTrackerStatus,
    parse_codex_tracker_status,
)
from orchestune.review.tracker_activity import CompletedGrace
from scripts.jev_context import JevReviewContext
from scripts.review_cli import print_review_result
from scripts.review_wait_diagnostics import (
    StalledReviewError,
    TrackerObservation,
    WaitContext,
    body_digest,
)

COMPLETENESS_SECTIONS = ("issue_comments", "reviews", "inline_comments")

__all__ = ["StalledReviewError"]


def summary_gate_open(
    data: dict[str, list[dict[str, Any]]],
    bot_name: str,
    exclude_ids: set[int | str] | None,
    trigger_time: str,
) -> bool:
    """Whether the newest summary may start acquisition.

    Non-Codex bots keep the historical rule that a summary created since the
    trigger must exist before content is acquired. Codex is judged by the
    provenance of its own review / inline content instead (#1274): its reused
    tracker is never a summary, and a Completed-with-inline round has none.
    """
    if bot_name.lower() == "codex":
        return True
    summary = _latest_bot_summary_item(data, bot_name, exclude_ids)
    return summary is not None and (
        not trigger_time or _get_item_created_timestamp(summary) >= trigger_time
    )


def track_stall(
    current_bot_activity: dict[str, Any] | None,
    last_signature: str | None,
    since: float | None,
    *,
    stall_grace_seconds: int,
    bot_name: str,
    pr_number: int,
    latest_trigger_time: str = "",
    requested_head_sha: str | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[str | None, float | None]:
    """Update in-progress tracker staleness state for one poll iteration.

    Returns the (signature, since) state to carry into the next iteration.
    Raises StalledReviewError once the same signature has persisted for at
    least stall_grace_seconds while still reporting in-progress.

    A tracker comment created before latest_trigger_time belongs to an
    earlier round (e.g. the current round's trigger hasn't drawn any bot
    response yet); it must not be attributed to the current round's stall
    tracking, or a still-unanswered new trigger would be misdiagnosed as a
    stall of a round that never actually started (should stay Exit 20 /
    the no-activity recovery path instead).

    A reused Codex tracker is placed by its last update instead of its creation
    (#1274): a Round 2+ Running update is the current round's activity, while an
    untouched earlier tracker still is not.

    `clock` is monotonic (#1388): a wall-clock correction must neither trip nor
    postpone the grace. Server timestamps only place activity in a round.
    """
    if (
        current_bot_activity is None
        or not _is_explicitly_in_progress(current_bot_activity)
        or (
            latest_trigger_time
            and not _in_activity_round(
                current_bot_activity,
                bot_name,
                latest_trigger_time,
                "",
                requested_head_sha,
            )
        )
    ):
        return None, None

    signature = _activity_signature(current_bot_activity, bot_name)
    if signature != last_signature:
        return signature, clock()
    if since is not None and clock() - since >= stall_grace_seconds:
        raise StalledReviewError(
            f"@{bot_name}'s in-progress tracker comment on PR #{pr_number} has "
            f"not changed in over {stall_grace_seconds}s while still reporting "
            "in-progress; the workflow run that owns it most likely already "
            "ended without posting a final result (see review-loop.md)."
        )
    return last_signature, since


@dataclass(frozen=True)
class WaitRound:
    """What stays fixed while polling one posted trigger."""

    pr_number: int
    bot_name: str
    round_num: int
    trigger_id: int | str | None
    trigger_time: str
    requested_head_sha: str | None
    repository: str | None
    excluded_ids: set[int | str]
    jev_threshold: float | None
    context_cache: dict[int, JevReviewContext]
    # Injected by wait_for_review so its patchable module-level helpers stay in use.
    extract_review: Callable[..., dict[str, Any] | None]
    fetch_head_sha: Callable[[int], str | None]

    def extract(self, data: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
        return self.extract_review(
            data,
            self.bot_name,
            exclude_ids=self.excluded_ids,
            latest_trigger_time=self.trigger_time,
            jev_threshold=self.jev_threshold,
            pr_number=self.pr_number,
            context_cache=self.context_cache,
            round_num=self.round_num,
            trigger_id=self.trigger_id,
            triggered_at=self.trigger_time,
            requested_head_sha=self.requested_head_sha,
            repository=self.repository,
        )


@dataclass
class PollState:
    snapshot: dict[str, str]
    grace: CompletedGrace
    stall_signature: str | None = None
    stall_since: float | None = None
    completed_timestamp: str = field(default="")
    # The last successfully fetched PR data, kept for the end-of-wait diagnostic.
    last_data: dict[str, list[dict[str, Any]]] | None = None
    last_poll_ok: bool = False


def _tracker_observation(
    data: dict[str, list[dict[str, Any]]], round_: WaitRound
) -> TrackerObservation | None:
    activity = _latest_bot_activity_item(
        data,
        round_.bot_name,
        exclude_ids=round_.excluded_ids,
        round_started_at=round_.trigger_time,
        requested_head_sha=round_.requested_head_sha,
    )
    if activity is None:
        return None
    section = next(
        (name for name, items in data.items() if any(i is activity for i in items)),
        "unknown",
    )
    status = parse_codex_tracker_status(str(activity.get("body") or ""))
    if status is None:
        status_text = "in_progress" if _is_explicitly_in_progress(activity) else "other"
    else:
        status_text = status.value
    return TrackerObservation(
        id=activity.get("id"),
        section=section,
        status=status_text,
        updated_at=_get_item_timestamp(activity),
        body_sha256=body_digest(activity),
    )


def _observed_items(
    data: dict[str, list[dict[str, Any]]], round_: WaitRound
) -> tuple[dict[str, Any], ...]:
    observed = []
    for section in COMPLETENESS_SECTIONS:
        for item in _filter_bot_items(
            data.get(section, []), round_.bot_name, round_.excluded_ids
        ):
            if round_.trigger_time and _get_item_timestamp(item) < round_.trigger_time:
                continue
            observed.append(
                {
                    "section": section,
                    "id": item.get("id"),
                    "created_at": _get_item_created_timestamp(item),
                    "updated_at": _get_item_timestamp(item),
                    "in_progress": _is_explicitly_in_progress(item),
                    "body_sha256": body_digest(item),
                }
            )
    return tuple(observed)


def wait_context(
    round_: WaitRound,
    state: PollState,
    *,
    elapsed_seconds: float,
    timeout_seconds: float,
    stall_grace_seconds: float,
    completed_grace_seconds: float,
) -> WaitContext:
    """The diagnostic context of a wait that ended without a result (#1388)."""
    data = state.last_data
    return WaitContext(
        repository=round_.repository,
        pr_number=round_.pr_number,
        reviewer=round_.bot_name,
        round=round_.round_num,
        trigger_id=round_.trigger_id,
        triggered_at=round_.trigger_time or None,
        requested_head_sha=round_.requested_head_sha,
        elapsed_seconds=round(elapsed_seconds, 3),
        timeout_seconds=timeout_seconds,
        stall_grace_seconds=stall_grace_seconds,
        completed_grace_seconds=completed_grace_seconds,
        last_poll_ok=state.last_poll_ok,
        last_tracker=_tracker_observation(data, round_) if data is not None else None,
        observed_items=_observed_items(data, round_) if data is not None else (),
    )


def unavailable_result(
    round_: WaitRound, state: PollState, grace_seconds: float
) -> dict[str, Any]:
    """A Completed tracker whose review content never arrived: unavailable, not a pass.

    The tracker table is telemetry and is not put into `review_body`; no reviewed
    SHA is claimed because no real review was acquired.
    """
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "acquisition_status": ACQUISITION_UNAVAILABLE,
        "reason": (
            f"@{round_.bot_name} reported Completed but posted no review content "
            f"within {grace_seconds:g}s"
        ),
        "repository": round_.repository,
        "pr_number": round_.pr_number,
        "reviewer": round_.bot_name,
        "round": round_.round_num,
        "trigger_id": round_.trigger_id,
        "triggered_at": round_.trigger_time,
        "requested_head_sha": round_.requested_head_sha,
        "reviewed_head_sha": None,
        "current_head_sha": round_.fetch_head_sha(round_.pr_number),
        "review_target_sha": None,
        "review_target_sha_source": "unknown",
        "review_items": [],
        "review_body": "",
        "inline_comments": [],
        "jev_evaluations": [],
        "completeness": dict.fromkeys(COMPLETENESS_SECTIONS, "complete"),
        "timestamp": state.completed_timestamp,
    }
    print_review_result(result, round_.bot_name)
    return result


def poll_step(
    round_: WaitRound,
    state: PollState,
    data: dict[str, list[dict[str, Any]]],
    *,
    stall_grace_seconds: int,
    grace_seconds: float,
    timeout_remaining: float,
    clock: Callable[[], float],
) -> dict[str, Any] | None:
    """One successful poll: a result to return, or None to keep waiting.

    A current Running tracker is judged first and holds the wait open even when
    partial content exists. Otherwise content is acquired by its own provenance;
    a Completed tracker only opens the grace window for that content to appear.
    """
    bot_name = round_.bot_name
    activity = _latest_bot_activity_item(
        data,
        bot_name,
        exclude_ids=round_.excluded_ids,
        round_started_at=round_.trigger_time,
        requested_head_sha=round_.requested_head_sha,
    )
    # A dead "in progress" tracker never trips the snapshot-diff gate below
    # (nothing about it looks new), so staleness is tracked on every poll.
    state.stall_signature, state.stall_since = track_stall(
        activity,
        state.stall_signature,
        state.stall_since,
        stall_grace_seconds=stall_grace_seconds,
        bot_name=bot_name,
        pr_number=round_.pr_number,
        latest_trigger_time=round_.trigger_time,
        requested_head_sha=round_.requested_head_sha,
        clock=clock,
    )
    current_snapshot = _build_snapshot(data, bot_name, exclude_ids=round_.excluded_ids)
    has_changes = any(
        k not in state.snapshot or state.snapshot[k] != v
        for k, v in current_snapshot.items()
    )

    if activity is not None and _is_explicitly_in_progress(activity):
        state.grace.reset()
        if has_changes:
            state.snapshot = current_snapshot
            print(f"@{bot_name} is still working; continuing to wait...")
        return None

    tracker = (
        _latest_current_tracker(
            data,
            bot_name,
            round_.excluded_ids,
            round_started_at=round_.trigger_time,
            requested_head_sha=round_.requested_head_sha,
        )
        if round_.trigger_time
        else None
    )
    completed = (
        tracker is not None
        and parse_codex_tracker_status(str(tracker.get("body") or ""))
        is CodexTrackerStatus.COMPLETED
    )
    if completed and tracker is not None:
        state.grace.observe_completed(now=clock(), timeout_remaining=timeout_remaining)
        state.completed_timestamp = _get_item_timestamp(tracker)
    else:
        state.grace.reset()

    if has_changes or state.grace.active:
        if summary_gate_open(data, bot_name, round_.excluded_ids, round_.trigger_time):
            result = round_.extract(data)
            if result is not None:
                return result
        state.snapshot = current_snapshot
        if has_changes and not completed:
            print(
                f"@{bot_name} has no current-round review content yet; "
                "continuing to wait..."
            )
    if state.grace.expired(clock()):
        return unavailable_result(round_, state, grace_seconds)
    return None

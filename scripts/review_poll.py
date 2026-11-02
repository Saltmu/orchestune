"""One polling step of `scripts/wait_for_review.py`: stall tracking and the
Completed-tracker grace (#1274).

Kept apart from the script so the wait loop stays small. Everything the script
patches in tests (`_extract_review_result`, `_fetch_pr_head_sha`, the monotonic
clock) is injected rather than imported, so those patches keep working.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from orchestune.review.acquisition import (
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
    review_result_record,
)
from orchestune.review.progress_tracker import (
    CodexTrackerStatus,
    parse_codex_tracker_status,
)
from orchestune.review.tracker_activity import CompletedGrace
from scripts.jev_context import JevReviewContext
from scripts.review_cli import print_review_result
from scripts.review_wait_diagnostics import (
    ReviewTimeoutError,
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
    result = review_result_record(
        reason=(
            f"@{round_.bot_name} reported Completed but posted no review content "
            f"within {grace_seconds:g}s"
        ),
        repository=round_.repository,
        pr_number=round_.pr_number,
        reviewer=round_.bot_name,
        round=round_.round_num,
        trigger_id=round_.trigger_id,
        triggered_at=round_.trigger_time,
        requested_head_sha=round_.requested_head_sha,
        current_head_sha=round_.fetch_head_sha(round_.pr_number),
        completeness=dict.fromkeys(COMPLETENESS_SECTIONS, "complete"),
        timestamp=state.completed_timestamp,
    )
    print_review_result(result, round_.bot_name)
    return result


def _track_activity_stall(
    round_: WaitRound,
    state: PollState,
    activity: dict[str, Any] | None,
    stall_grace_seconds: int,
    clock: Callable[[], float],
) -> None:
    # A dead "in progress" tracker never trips the snapshot-diff gate
    # (nothing about it looks new), so staleness is tracked on every poll.
    state.stall_signature, state.stall_since = track_stall(
        activity,
        state.stall_signature,
        state.stall_since,
        stall_grace_seconds=stall_grace_seconds,
        bot_name=round_.bot_name,
        pr_number=round_.pr_number,
        latest_trigger_time=round_.trigger_time,
        requested_head_sha=round_.requested_head_sha,
        clock=clock,
    )


def _snapshot_changed(state: PollState, current_snapshot: dict[str, str]) -> bool:
    return any(
        key not in state.snapshot or state.snapshot[key] != value
        for key, value in current_snapshot.items()
    )


def _observe_completed_tracker(
    round_: WaitRound,
    state: PollState,
    data: dict[str, list[dict[str, Any]]],
    timeout_remaining: float,
    clock: Callable[[], float],
) -> bool:
    tracker = (
        _latest_current_tracker(
            data,
            round_.bot_name,
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
        return True
    state.grace.reset()
    return False


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
    _track_activity_stall(round_, state, activity, stall_grace_seconds, clock)
    current_snapshot = _build_snapshot(data, bot_name, exclude_ids=round_.excluded_ids)
    has_changes = _snapshot_changed(state, current_snapshot)

    if activity is not None and _is_explicitly_in_progress(activity):
        state.grace.reset()
        if has_changes:
            state.snapshot = current_snapshot
            print(f"@{bot_name} is still working; continuing to wait...")
        return None

    completed = _observe_completed_tracker(
        round_, state, data, timeout_remaining, clock
    )
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


@dataclass(frozen=True)
class PollBudget:
    """Limits and injected clock for one blocking wait."""

    pr_number: int
    timeout: int
    interval: int
    max_retries: int
    stall_grace_seconds: int
    completed_grace_seconds: float
    fetch_data: Callable[[], dict[str, list[dict[str, Any]]]]
    clock: Callable[[], float]
    sleep: Callable[[float], None]


@dataclass(frozen=True)
class ReviewWaitRequest:
    """Scalar arguments of `wait_for_review`, kept off that function's body."""

    pr_number: int
    timeout: int
    interval: int
    bot_name: str
    post_trigger: bool
    body: str | None
    body_file: str | None
    max_rounds: int
    max_retries: int
    round_num: int | None
    stall_grace_seconds: int
    jev_threshold: float | None
    switch_reviewer: bool
    completed_grace_seconds: float
    repair_trigger_markers: bool


@dataclass(frozen=True)
class WaitBindings:
    """Callables resolved on `scripts.wait_for_review` when a wait starts.

    Tests patch those module globals. Capturing them here, at call time, keeps
    the patches in force after the loop body moves out of that module.
    """

    validate_online: Callable[..., None]
    fetch_repository: Callable[[], str | None]
    get_initial: Callable[..., dict[str, list[dict[str, Any]]]]
    validate_selection: Callable[..., None]
    record_skip: Callable[..., dict[str, Any]]
    build_snapshot: Callable[..., dict[str, str]]
    trigger_ids: Callable[[list[dict[str, Any]]], set[int | str]]
    prepare_round: Callable[..., int]
    handle_trigger: Callable[..., tuple[str, int | str | None, str | None]]
    restore_no_post: Callable[..., tuple[str, int | str | None, str | None]]
    check_immediate: Callable[..., dict[str, Any] | None]
    extract_review: Callable[..., dict[str, Any] | None]
    fetch_head: Callable[[int], str | None]
    get_pr_data: Callable[..., dict[str, list[dict[str, Any]]]]
    clock: Callable[[], float]
    sleep: Callable[[float], None]


def _ended_wait_context(
    round_: WaitRound,
    state: PollState,
    budget: PollBudget,
    start_time: float,
) -> WaitContext:
    return wait_context(
        round_,
        state,
        elapsed_seconds=budget.clock() - start_time,
        timeout_seconds=budget.timeout,
        stall_grace_seconds=budget.stall_grace_seconds,
        completed_grace_seconds=budget.completed_grace_seconds,
    )


def _poll_succeeded(
    round_: WaitRound,
    state: PollState,
    budget: PollBudget,
    start_time: float,
    current_data: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    return poll_step(
        round_,
        state,
        current_data,
        stall_grace_seconds=budget.stall_grace_seconds,
        grace_seconds=budget.completed_grace_seconds,
        timeout_remaining=budget.timeout - (budget.clock() - start_time),
        clock=budget.clock,
    )


def _note_poll_error(
    state: PollState, error: Exception, consecutive_errors: int, max_retries: int
) -> int:
    consecutive_errors += 1
    state.last_poll_ok = False
    print(f"Warning: Error checking PR review data: {error}", file=sys.stderr)
    if consecutive_errors > max_retries:
        raise RuntimeError(
            f"Exceeded maximum retries ({max_retries}) during review polling: {error}"
        ) from error
    return consecutive_errors


def _sleep_until_next(state: PollState, budget: PollBudget, start_time: float) -> bool:
    """Return True once the monotonic overall deadline has passed.

    The deadline can be overrun only by the poll in flight (each gh call is
    bounded by GH_COMMAND_TIMEOUT_SECONDS).
    """
    remaining = budget.timeout - (budget.clock() - start_time)
    if remaining <= 0:
        return True
    pause = min(budget.interval, remaining)
    grace_left = state.grace.remaining(budget.clock())
    budget.sleep(pause if grace_left is None else min(pause, grace_left))
    return False


def _timeout_result(
    round_: WaitRound, state: PollState, budget: PollBudget, start_time: float
) -> dict[str, Any]:
    if state.grace.active:
        # A Completed tracker was confirmed by a normal fetch, and no review
        # content arrived before the overall timeout.
        return unavailable_result(round_, state, budget.completed_grace_seconds)
    raise ReviewTimeoutError(
        f"Timed out waiting for @{round_.bot_name} activity on PR "
        f"#{budget.pr_number} after {budget.timeout}s.",
        _ended_wait_context(round_, state, budget, start_time),
    )


def poll_until(
    round_: WaitRound, state: PollState, budget: PollBudget
) -> dict[str, Any]:
    """Block until a review result, a completed-tracker miss, or a timeout."""
    start_time = budget.clock()
    consecutive_errors = 0
    while True:
        try:
            current_data = budget.fetch_data()
            consecutive_errors = 0
            state.last_data, state.last_poll_ok = current_data, True
            result = _poll_succeeded(round_, state, budget, start_time, current_data)
            if result is not None:
                return result
        except StalledReviewError as error:
            error.attach(_ended_wait_context(round_, state, budget, start_time))
            raise
        except Exception as error:
            consecutive_errors = _note_poll_error(
                state, error, consecutive_errors, budget.max_retries
            )
        if _sleep_until_next(state, budget, start_time):
            return _timeout_result(round_, state, budget, start_time)


def _load_round_state(
    bindings: WaitBindings,
    request: ReviewWaitRequest,
    executor: ThreadPoolExecutor,
    repository: str | None,
) -> dict[str, Any] | tuple[Any, dict[str, str], set[int | str]]:
    initial_data = bindings.get_initial(
        request.pr_number,
        executor,
        request.timeout,
        request.interval,
        max_retries=request.max_retries,
    )
    bindings.validate_selection(
        initial_data, request.bot_name, request.switch_reviewer and request.post_trigger
    )
    if request.bot_name == "skip":
        return bindings.record_skip(request.pr_number, initial_data, repository)
    return (
        initial_data,
        bindings.build_snapshot(initial_data, request.bot_name),
        bindings.trigger_ids(initial_data.get("issue_comments", [])),
    )


def _open_or_restore_round(
    bindings: WaitBindings,
    request: ReviewWaitRequest,
    initial_data: dict[str, list[dict[str, Any]]],
    initial_snapshot: dict[str, str],
    excluded_ids: set[int | str],
    current_round: int,
    context_cache: dict[int, JevReviewContext],
    repository: str | None,
) -> dict[str, Any] | tuple[str, int | str | None, str | None]:
    if request.post_trigger:
        return bindings.handle_trigger(
            request.pr_number,
            request.bot_name,
            initial_data,
            initial_snapshot,
            excluded_ids,
            current_round,
            request.max_rounds,
            request.body,
            request.body_file,
            repair_trigger_markers=request.repair_trigger_markers,
        )
    latest_trigger_time, trigger_id, requested_head_sha = bindings.restore_no_post(
        initial_data, request.bot_name, current_round, excluded_ids
    )
    immediate = bindings.check_immediate(
        initial_data,
        request.bot_name,
        latest_trigger_time,
        current_round,
        jev_threshold=request.jev_threshold,
        pr_number=request.pr_number,
        context_cache=context_cache,
        trigger_id=trigger_id,
        requested_head_sha=requested_head_sha,
        repository=repository,
        exclude_ids=excluded_ids,
    )
    if immediate is not None:
        return immediate
    return latest_trigger_time, trigger_id, requested_head_sha


def _poll_opened_round(
    bindings: WaitBindings,
    request: ReviewWaitRequest,
    executor: ThreadPoolExecutor,
    repository: str | None,
    context_cache: dict[int, JevReviewContext],
    initial_snapshot: dict[str, str],
    excluded_ids: set[int | str],
    current_round: int,
    opened: tuple[str, int | str | None, str | None],
) -> dict[str, Any]:
    latest_trigger_time, trigger_id, requested_head_sha = opened
    print(
        f"Waiting for @{request.bot_name} activity on PR #{request.pr_number} "
        f"(timeout: {request.timeout}s, interval: {request.interval}s)..."
    )
    round_ = WaitRound(
        pr_number=request.pr_number,
        bot_name=request.bot_name,
        round_num=current_round,
        trigger_id=trigger_id,
        trigger_time=latest_trigger_time,
        requested_head_sha=requested_head_sha,
        repository=repository,
        excluded_ids=excluded_ids,
        jev_threshold=request.jev_threshold,
        context_cache=context_cache,
        extract_review=bindings.extract_review,
        fetch_head_sha=bindings.fetch_head,
    )
    return poll_until(
        round_,
        PollState(initial_snapshot, CompletedGrace(request.completed_grace_seconds)),
        PollBudget(
            pr_number=request.pr_number,
            timeout=request.timeout,
            interval=request.interval,
            max_retries=request.max_retries,
            stall_grace_seconds=request.stall_grace_seconds,
            completed_grace_seconds=request.completed_grace_seconds,
            fetch_data=lambda: bindings.get_pr_data(
                request.pr_number, executor=executor
            ),
            clock=bindings.clock,
            sleep=bindings.sleep,
        ),
    )


def _finish_round_wait(
    bindings: WaitBindings,
    request: ReviewWaitRequest,
    executor: ThreadPoolExecutor,
    repository: str | None,
    context_cache: dict[int, JevReviewContext],
    loaded: tuple[Any, dict[str, str], set[int | str]],
) -> dict[str, Any]:
    initial_data, initial_snapshot, excluded_ids = loaded
    current_round = bindings.prepare_round(
        request.pr_number,
        initial_data,
        request.bot_name,
        request.round_num,
        request.post_trigger,
        request.body_file,
        request.max_rounds,
    )
    opened = _open_or_restore_round(
        bindings,
        request,
        initial_data,
        initial_snapshot,
        excluded_ids,
        current_round,
        context_cache,
        repository,
    )
    if isinstance(opened, dict):
        return opened
    return _poll_opened_round(
        bindings,
        request,
        executor,
        repository,
        context_cache,
        initial_snapshot,
        excluded_ids,
        current_round,
        opened,
    )


def _execute_review_wait(
    bindings: WaitBindings, request: ReviewWaitRequest
) -> dict[str, Any]:
    bindings.validate_online(
        request.bot_name,
        request.post_trigger,
        request.repair_trigger_markers,
        request.round_num,
    )
    context_cache: dict[int, JevReviewContext] = {}
    repository = bindings.fetch_repository()
    with ThreadPoolExecutor(max_workers=3) as executor:
        loaded = _load_round_state(bindings, request, executor, repository)
        if isinstance(loaded, dict):
            return loaded
        return _finish_round_wait(
            bindings, request, executor, repository, context_cache, loaded
        )


def execute_review_wait(
    bindings: WaitBindings,
    pr_number: int,
    *,
    timeout: int,
    interval: int,
    bot_name: str,
    post_trigger: bool,
    body: str | None,
    body_file: str | None,
    max_rounds: int,
    max_retries: int,
    round_num: int | None,
    stall_grace_seconds: int,
    jev_threshold: float | None,
    switch_reviewer: bool,
    completed_grace_seconds: float,
    repair_trigger_markers: bool,
) -> dict[str, Any]:
    return _execute_review_wait(
        bindings,
        ReviewWaitRequest(
            pr_number,
            timeout,
            interval,
            bot_name,
            post_trigger,
            body,
            body_file,
            max_rounds,
            max_retries,
            round_num,
            stall_grace_seconds,
            jev_threshold,
            switch_reviewer,
            completed_grace_seconds,
            repair_trigger_markers,
        ),
    )

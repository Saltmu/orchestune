"""Script to trigger and detect AI review activity on a GitHub Pull Request.

Posts a review trigger (optional) and polls for any new or updated activity
from the specified bot (Claude, Codex, etc.), returning the full acquired
review content directly to stdout (and optionally a JSON file) for the calling
LLM to judge. This script never computes a pass/fail verdict; see
review-loop.md for the LLM decision procedure.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

from orchestune.review.acquisition import (
    ACQUISITION_ACQUIRED as ACQUISITION_ACQUIRED,
)
from orchestune.review.acquisition import (
    ACQUISITION_IN_PROGRESS as ACQUISITION_IN_PROGRESS,
)
from orchestune.review.acquisition import (
    ACQUISITION_UNAVAILABLE as ACQUISITION_UNAVAILABLE,
)
from orchestune.review.acquisition import (
    EXIT_ACQUIRED as EXIT_ACQUIRED,
)
from orchestune.review.acquisition import (
    EXIT_IN_PROGRESS as EXIT_IN_PROGRESS,
)
from orchestune.review.acquisition import (
    EXIT_NO_RESULT as EXIT_NO_RESULT,
)
from orchestune.review.acquisition import (
    SCHEMA_VERSION as SCHEMA_VERSION,
)
from orchestune.review.acquisition import (
    _build_snapshot as _build_snapshot,
)
from orchestune.review.acquisition import (
    _filter_bot_items as _filter_bot_items,
)
from orchestune.review.acquisition import (
    _get_item_timestamp as _get_item_timestamp,
)
from orchestune.review.acquisition import (
    _is_bot_user as _is_bot_user,
)
from orchestune.review.acquisition import (
    _is_explicitly_in_progress,
    _latest_bot_activity_item,
    extract_review_result,
    normalize_review_state,
)
from orchestune.review.acquisition import (
    _latest_bot_summary_item as _latest_bot_summary_item,
)
from orchestune.review.judgment import validate_previous_round_reply
from orchestune.review.markers import (
    derive_review_target,
    ensure_review_trigger_mention,
    has_review_trigger_mention,
    mark_review_trigger,
    parse_head_marker,
    parse_trigger_reviewer,
    review_round_marker,
    review_selection_marker,
)
from orchestune.review.markers import (
    parse_round_marker as _parse_review_round_marker,
)
from orchestune.review.markers import (
    review_trigger_marker as _review_trigger_marker,
)
from orchestune.review.offline import resolve_legacy_completeness
from orchestune.review.rounds import (
    build_restorable_trigger_body,
    previous_round_window,
)
from orchestune.review.tracker_activity import (
    DEFAULT_COMPLETED_GRACE_SECONDS,
    CompletedGrace,
)
from scripts.jev_context import JevReviewContext, collect_review_context
from scripts.jev_filter import evaluate_review_findings
from scripts.review_cli import (
    EXIT_INTERNAL_ERROR as EXIT_INTERNAL_ERROR,
)
from scripts.review_cli import (
    EXIT_MAX_ROUNDS as EXIT_MAX_ROUNDS,
)
from scripts.review_cli import (
    parse_args,
    run_offline,
)
from scripts.review_cli import (
    print_review_result as _print_review_result,
)
from scripts.review_cli import (
    write_output_file as _write_output_file,
)
from scripts.review_poll import (
    COMPLETENESS_SECTIONS,
    PollState,
    StalledReviewError,
    WaitRound,
    poll_step,
    summary_gate_open,
    track_stall,
    unavailable_result,
)

# Historical private names; tests and callers patch and import them from here.
_ensure_review_trigger_mention = ensure_review_trigger_mention
_has_review_trigger_mention = has_review_trigger_mention
_mark_review_trigger = mark_review_trigger
_resolve_offline_completeness = resolve_legacy_completeness
_review_round_marker = review_round_marker
_track_stall = track_stall

EXIT_TIMEOUT = 20  # Timeout waiting for review activity
EXIT_STALLED = 21  # In-progress tracker comment stopped changing; job likely ended

GH_COMMAND_TIMEOUT_SECONDS = 30


def _monotonic() -> float:
    """Clock for the Completed grace deadline; tests replace it."""
    return time.monotonic()


_COMPLETENESS_SECTIONS = COMPLETENESS_SECTIONS

# How long a bot's own "in progress" tracker comment may report the same
# unchanged content before it is treated as stalled rather than merely slow.
# A live job keeps editing that same comment (ticking off checklist items) as
# it works, so a signature that never changes past this window most likely
# means the workflow run that owns it already ended without posting a final
# result (observed directly on PR #923 round 4, run 35411499375: the action
# posted a "Review in progress" tracker, then finished successfully 2m26s
# later without ever editing it again). See Issue #926.
DEFAULT_STALL_GRACE_SECONDS = 600


class MaxRoundsExceededError(RuntimeError):
    """Raised when the maximum number of review rounds is exceeded."""


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _run_gh(args: list[str]) -> str:
    cmd = ["gh", *args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=GH_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"gh command timed out after {GH_COMMAND_TIMEOUT_SECONDS}s"
        ) from e
    if result.returncode != 0:
        raise RuntimeError(f"gh command failed: {result.stderr.strip()}")
    return result.stdout


def _run_gh_api(endpoint: str, *extra_args: str) -> list[dict[str, Any]]:
    stdout = _run_gh(["api", "--paginate", endpoint, *extra_args])
    if not stdout.strip():
        return []
    try:
        pages = json.loads(stdout)
        if isinstance(pages, list) and pages and isinstance(pages[0], list):
            flattened: list[dict[str, Any]] = []
            for page in pages:
                flattened.extend(page)
            return flattened
        if isinstance(pages, list):
            return pages
        return [pages]
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        pos = 0
        items: list[dict[str, Any]] = []
        while pos < len(stdout):
            while pos < len(stdout) and stdout[pos].isspace():
                pos += 1
            if pos >= len(stdout):
                break
            obj, next_pos = decoder.raw_decode(stdout, pos)
            pos = next_pos
            if isinstance(obj, list):
                items.extend(obj)
            elif isinstance(obj, dict):
                items.append(obj)
    return items


def _fetch_pr_head_sha(pr_number: int) -> str | None:
    """Best-effort current PR head SHA. Failure means unknown, never a guess."""
    try:
        stdout = _run_gh(["pr", "view", str(pr_number), "--json", "headRefOid"])
        value = json.loads(stdout)["headRefOid"]
        return cast(str, value) if isinstance(value, str) and value else None
    except (RuntimeError, json.JSONDecodeError, KeyError, TypeError):
        return None


def _fetch_repository_slug() -> str | None:
    """Best-effort `owner/repo` for the current checkout. Failure means unknown."""
    try:
        stdout = _run_gh(["repo", "view", "--json", "nameWithOwner"])
        value = json.loads(stdout)["nameWithOwner"]
        return cast(str, value) if isinstance(value, str) and value else None
    except (RuntimeError, json.JSONDecodeError, KeyError, TypeError):
        return None


def _is_trigger_comment(
    item: dict[str, Any], bot_name: str, round_num: int | None = None
) -> bool:
    body = item.get("body") or ""
    body_lower = body.lower()
    marker = _review_trigger_marker(bot_name).lower()
    trigger_line = f"@{bot_name.lower()} review"

    is_trigger = marker in body_lower or any(
        line.strip().lower() == trigger_line for line in body.splitlines()
    )
    if not is_trigger:
        return False
    if round_num is not None:
        return _parse_review_round_marker(body) == round_num
    return True


def _get_latest_review_round(
    data: dict[str, list[dict[str, Any]]], bot_name: str | None = None
) -> int:
    rounds: list[int] = []
    for item in data.get("issue_comments", []):
        if bot_name and not _is_trigger_comment(item, bot_name):
            continue
        if bot_name is None and not any(
            _is_trigger_comment(item, bot) for bot in ("claude", "codex")
        ):
            continue
        round_num = _parse_review_round_marker(item.get("body") or "")
        if round_num is not None:
            rounds.append(round_num)
    return max(rounds, default=0)


def _find_existing_trigger_comment(
    data: dict[str, list[dict[str, Any]]], bot_name: str, round_num: int
) -> dict[str, Any] | None:
    matching = [
        item
        for item in data.get("issue_comments", [])
        if _is_trigger_comment(item, bot_name, round_num=round_num)
    ]
    if not matching:
        return None
    for item in reversed(matching):
        if _has_review_trigger_mention(item.get("body") or "", bot_name):
            return item
    return matching[0]


def post_review_trigger(
    pr_number: int,
    bot_name: str,
    body: str | None = None,
    body_file: str | None = None,
    round_num: int = 1,
    head_sha: str | None = None,
) -> dict[str, Any]:
    if body_file:
        with open(body_file, encoding="utf-8") as f:
            raw_body = f.read()
    elif body:
        raw_body = body
    else:
        raw_body = ""
    if bot_name not in {"claude", "codex"}:
        raise ValueError("review trigger requires claude or codex")
    head_sha = head_sha or _fetch_pr_head_sha(pr_number)
    if head_sha is None:
        raise ValueError("cannot record trigger without PR head SHA")
    comment_body = build_restorable_trigger_body(
        raw_body, bot_name, round_num, head_sha
    )

    stdout = _run_gh(
        [
            "api",
            f"repos/{{owner}}/{{repo}}/issues/{pr_number}/comments",
            "-f",
            f"body={comment_body}",
        ]
    )
    return cast(dict[str, Any], json.loads(stdout))


def _get_pr_data(
    pr_number: int, executor: ThreadPoolExecutor | None = None
) -> dict[str, list[dict[str, Any]]]:
    def fetch_endpoint(endpoint: str) -> list[dict[str, Any]]:
        return _run_gh_api(endpoint)

    endpoints = [
        f"repos/{{owner}}/{{repo}}/issues/{pr_number}/comments",
        f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/reviews",
        f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/comments",
    ]

    if executor is not None:
        futures = [executor.submit(fetch_endpoint, ep) for ep in endpoints]
        return {
            "issue_comments": futures[0].result(),
            "reviews": futures[1].result(),
            "inline_comments": futures[2].result(),
        }

    with ThreadPoolExecutor(max_workers=3) as local_executor:
        futures = [local_executor.submit(fetch_endpoint, ep) for ep in endpoints]
        return {
            "issue_comments": futures[0].result(),
            "reviews": futures[1].result(),
            "inline_comments": futures[2].result(),
        }


def _latest_review_trigger_timestamp(
    data: dict[str, list[dict[str, Any]]], bot_name: str
) -> str:
    trigger_timestamps = [
        item.get("created_at") or ""
        for item in data.get("issue_comments", [])
        if _is_trigger_comment(item, bot_name)
    ]
    return max(trigger_timestamps, default="")


def _extract_review_result(
    current_data: dict[str, list[dict[str, Any]]],
    bot_name: str,
    exclude_ids: set[int | str] | None = None,
    latest_trigger_time: str = "",
    jev_threshold: float | None = None,
    pr_number: int | None = None,
    context_cache: dict[int, JevReviewContext] | None = None,
    round_num: int | None = None,
    trigger_id: int | str | None = None,
    triggered_at: str = "",
    requested_head_sha: str | None = None,
    repository: str | None = None,
) -> dict[str, Any] | None:
    result = extract_review_result(
        normalize_review_state(current_data),
        bot_name,
        exclude_ids=exclude_ids,
        round_started_at=latest_trigger_time,
    )
    if result is None:
        return None

    current_inlines = [
        item for item in result["inline_comments"] if item["provenance"] == "current"
    ]
    jev_evaluations: list[dict[str, Any]] = []
    if current_inlines:
        review_context = None
        if os.environ.get("JEV_API_KEY") and pr_number is not None:
            cache = context_cache if context_cache is not None else {}
            if pr_number not in cache:
                cache[pr_number] = collect_review_context(pr_number)
            review_context = cache[pr_number]
        jev_report = evaluate_review_findings(
            current_inlines,
            bot_name=bot_name,
            threshold=jev_threshold,
            pr=pr_number,
            context=review_context,
        )
        jev_evaluations = jev_report["jev_evaluations"]

    reviewed_shas = {
        item.get("commit_id")
        for item in result["review_items"]
        if item.get("kind") == "review"
        and item.get("provenance") == "current"
        and item.get("commit_id")
    }
    reviewed_head_sha = next(iter(reviewed_shas)) if len(reviewed_shas) == 1 else None

    current_head_sha = _fetch_pr_head_sha(pr_number) if pr_number is not None else None

    target_sha, target_source = derive_review_target(
        result["review_items"], requested_head_sha, current_head_sha
    )
    full_result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "acquisition_status": ACQUISITION_ACQUIRED,
        "reason": "",
        "repository": repository,
        "pr_number": pr_number,
        "reviewer": bot_name,
        "round": round_num,
        "trigger_id": trigger_id,
        "triggered_at": triggered_at,
        "requested_head_sha": requested_head_sha,
        "reviewed_head_sha": reviewed_head_sha,
        "current_head_sha": current_head_sha,
        "review_target_sha": target_sha,
        "review_target_sha_source": target_source,
        "review_items": result["review_items"],
        "review_body": result["review_body"],
        "inline_comments": result["inline_comments"],
        "jev_evaluations": jev_evaluations,
        "completeness": {
            "issue_comments": "complete",
            "reviews": "complete",
            "inline_comments": "complete",
        },
        "timestamp": result["timestamp"],
    }
    _print_review_result(full_result, bot_name)
    return full_result


def _get_initial_pr_data(
    pr_number: int,
    executor: ThreadPoolExecutor,
    timeout: int,
    interval: int,
    max_retries: int = 1,
) -> dict[str, list[dict[str, Any]]]:
    initial_fetch_start = time.time()
    retries = 0
    while True:
        try:
            return _get_pr_data(pr_number, executor=executor)
        except Exception as e:
            retries += 1
            print(
                f"Warning: Error capturing initial PR review data: {e}",
                file=sys.stderr,
            )
            if retries > max_retries or (time.time() - initial_fetch_start >= timeout):
                raise TimeoutError(
                    f"Timed out capturing initial PR review data for PR #{pr_number} "
                    f"after {timeout}s ({retries} retry attempts)."
                ) from e
            time.sleep(interval)


def _handle_review_trigger(
    pr_number: int,
    bot_name: str,
    initial_data: dict[str, list[dict[str, Any]]],
    initial_snapshot: dict[str, str],
    excluded_ids: set[int | str],
    current_round: int,
    max_rounds: int,
    body: str | None,
    body_file: str | None,
) -> tuple[str, int | str | None, str | None]:
    """Post a head-bound trigger, or restore its recorded head on resume.

    Resume never guesses from the current head, which may have advanced since
    the trigger. Legacy triggers without a head marker keep requested SHA unknown.
    """
    existing_trigger = _find_existing_trigger_comment(
        initial_data, bot_name, current_round
    )
    if existing_trigger is not None:
        trigger_id = existing_trigger.get("id")
        trigger_time = str(existing_trigger.get("created_at") or "")
        existing_body = existing_trigger.get("body") or ""
        if trigger_id is not None:
            excluded_ids.add(trigger_id)
        if _has_review_trigger_mention(existing_body, bot_name):
            print(
                f"Review trigger for @{bot_name} (Round {current_round}) already exists "
                f"(Comment ID: {trigger_id}); skipping post and waiting..."
            )
            return trigger_time, trigger_id, parse_head_marker(existing_body)
        print(
            f"Review trigger comment for Round {current_round} (Comment ID: {trigger_id}) "
            f"is missing @{bot_name} review mention; reposting trigger..."
        )

    print(
        f"Posting review trigger comment (Round {current_round}/{max_rounds}) "
        f"for @{bot_name} on PR #{pr_number}..."
    )
    requested_head_sha = _fetch_pr_head_sha(pr_number)
    trigger_info = post_review_trigger(
        pr_number,
        bot_name=bot_name,
        body=body,
        body_file=body_file,
        round_num=current_round,
        head_sha=requested_head_sha,
    )
    trigger_id = trigger_info.get("id")
    trigger_time = str(trigger_info.get("created_at") or "")
    trigger_body = trigger_info.get("body") or ""
    if trigger_id is not None:
        excluded_ids.add(trigger_id)
        initial_snapshot[f"comment_{trigger_id}"] = (
            f"{trigger_time}:{len(trigger_body)}"
        )
    print(f"Trigger posted (Comment ID: {trigger_id}, time: {trigger_time})")
    return (
        trigger_time,
        trigger_id,
        parse_head_marker(trigger_body) or requested_head_sha,
    )


def _check_immediate_review_result(
    initial_data: dict[str, list[dict[str, Any]]],
    bot_name: str,
    latest_trigger_time: str,
    current_round: int,
    jev_threshold: float | None = None,
    pr_number: int | None = None,
    context_cache: dict[int, JevReviewContext] | None = None,
    trigger_id: int | str | None = None,
    requested_head_sha: str | None = None,
    repository: str | None = None,
    exclude_ids: set[int | str] | None = None,
) -> dict[str, Any] | None:
    """Acquire an already-posted round, unless its tracker is still running.

    A current Running tracker is judged first, so a partial review or inline
    comment cannot end the wait. Otherwise the content's own creation /
    submission time decides (Codex needs no summary comment).
    """
    if not latest_trigger_time or not summary_gate_open(
        initial_data, bot_name, exclude_ids, latest_trigger_time
    ):
        return None
    latest_bot_activity = _latest_bot_activity_item(
        initial_data,
        bot_name,
        exclude_ids,
        round_started_at=latest_trigger_time,
        requested_head_sha=requested_head_sha,
    )
    if latest_bot_activity is not None and _is_explicitly_in_progress(
        latest_bot_activity
    ):
        return None
    return _extract_review_result(
        initial_data,
        bot_name,
        exclude_ids=exclude_ids,
        latest_trigger_time=latest_trigger_time,
        jev_threshold=jev_threshold,
        pr_number=pr_number,
        context_cache=context_cache,
        round_num=current_round,
        trigger_id=trigger_id,
        triggered_at=latest_trigger_time,
        requested_head_sha=requested_head_sha,
        repository=repository,
    )


def _resolve_current_round(
    initial_data: dict[str, list[dict[str, Any]]],
    bot_name: str,
    round_num: int | None,
    post_trigger: bool,
) -> int:
    if round_num is not None:
        return round_num
    latest_existing_round = _get_latest_review_round(initial_data)
    if not post_trigger:
        return max(1, latest_existing_round)
    return latest_existing_round + 1


def _validate_review_selection(
    data: dict[str, Any], bot_name: str, switch: bool
) -> None:
    if bot_name not in {"claude", "codex", "skip"}:
        raise ValueError("reviewer must be claude, codex or skip")
    triggers = [
        item
        for item in data.get("issue_comments", [])
        if parse_trigger_reviewer(item.get("body") or "")
    ]
    if not triggers:
        return
    previous = max(
        triggers,
        key=lambda item: (
            _parse_review_round_marker(item.get("body") or "") or 0,
            str(item.get("created_at") or ""),
        ),
    )
    reviewer = parse_trigger_reviewer(previous.get("body") or "")
    if reviewer != bot_name and not switch:
        raise ValueError(
            f"previous reviewer is {reviewer}; explicit --switch-reviewer required"
        )


def _validate_review_reply(
    data: dict[str, Any], bot_name: str, round_num: int, body_file: str | None
) -> None:
    if round_num < 2:
        return
    if not body_file:
        raise ValueError("round 2+ requires --body-file with review judgments")
    with open(body_file, encoding="utf-8") as stream:
        reply = stream.read()
    # Legacy-tolerant reading of earlier triggers; the offline path is strict.
    window = previous_round_window(
        data.get("issue_comments", []), round_num, strict=False
    )
    validate_previous_round_reply(data, window, reply)


def _record_skip(
    pr_number: int, data: dict[str, Any], repository: str | None
) -> dict[str, Any]:
    head = _fetch_pr_head_sha(pr_number)
    if head is None:
        raise ValueError("cannot record skip without PR head SHA")
    marker = review_selection_marker("skip", head)
    if not any(
        marker in (item.get("body") or "") for item in data.get("issue_comments", [])
    ):
        body = (
            marker + "\nReview skipped by explicit selection. "
            "統合ゲート（既定required）でstatus:blocked-human-reviewに止まります。skipはreview passではありません。"
        )
        _run_gh(
            [
                "api",
                f"repos/{{owner}}/{{repo}}/issues/{pr_number}/comments",
                "-f",
                f"body={body}",
            ]
        )
    print(
        "Review skipped; not a review pass. "
        "Integration gate (required) stops at status:blocked-human-review."
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "reviewer": "skip",
        "repository": repository,
        "pr_number": pr_number,
        "acquisition_status": ACQUISITION_UNAVAILABLE,
        "reason": "review explicitly skipped; human review required",
        "current_head_sha": head,
        "review_target_sha": None,
        "review_target_sha_source": "unknown",
        "review_items": [],
        "inline_comments": [],
    }


def _check_round_limit(current_round: int, max_rounds: int) -> None:
    if current_round > max_rounds:
        raise MaxRoundsExceededError(
            f"Maximum review rounds ({max_rounds}) exceeded (attempted round {current_round})."
        )


def wait_for_review(
    pr_number: int,
    *,
    timeout: int = 1800,
    interval: int = 5,
    bot_name: str = "claude",
    post_trigger: bool = True,
    body: str | None = None,
    body_file: str | None = None,
    max_rounds: int = 5,
    max_retries: int = 1,
    round_num: int | None = None,
    stall_grace_seconds: int = DEFAULT_STALL_GRACE_SECONDS,
    jev_threshold: float | None = None,
    switch_reviewer: bool = False,
    completed_grace_seconds: float = DEFAULT_COMPLETED_GRACE_SECONDS,
) -> dict[str, Any]:
    context_cache: dict[int, JevReviewContext] = {}
    repository = _fetch_repository_slug()
    with ThreadPoolExecutor(max_workers=3) as executor:
        initial_data = _get_initial_pr_data(
            pr_number,
            executor,
            timeout,
            interval,
            max_retries=max_retries,
        )
        _validate_review_selection(initial_data, bot_name, switch_reviewer)
        if bot_name == "skip":
            return _record_skip(pr_number, initial_data, repository)
        initial_snapshot = _build_snapshot(initial_data, bot_name)
        excluded_ids: set[int | str] = set()

        current_round = _resolve_current_round(
            initial_data, bot_name, round_num, post_trigger
        )

        _check_round_limit(current_round, max_rounds)
        _validate_review_reply(initial_data, bot_name, current_round, body_file)

        trigger_id: int | str | None = None
        if post_trigger:
            latest_trigger_time, trigger_id, requested_head_sha = (
                _handle_review_trigger(
                    pr_number,
                    bot_name,
                    initial_data,
                    initial_snapshot,
                    excluded_ids,
                    current_round,
                    max_rounds,
                    body,
                    body_file,
                )
            )
        else:
            requested_head_sha = None
            latest_trigger_time = _latest_review_trigger_timestamp(
                initial_data, bot_name
            )
            existing_trigger = _find_existing_trigger_comment(
                initial_data, bot_name, current_round
            )
            if existing_trigger is not None:
                trigger_id = existing_trigger.get("id")
                requested_head_sha = parse_head_marker(
                    existing_trigger.get("body") or ""
                )
                # Bot-authored triggers must not acquire their own request as a review.
                # A trigger comment authored by the target bot itself (e.g. a
                # hosted environment re-triggering its own review under its
                # own bot identity) must not be read as the review it is
                # asking for -- without this, the trigger's own text can
                # satisfy the round-content gate before any real review
                # arrives (Codex PR #1114 round 8 finding).
                if trigger_id is not None:
                    excluded_ids.add(trigger_id)
            immediate = _check_immediate_review_result(
                initial_data,
                bot_name,
                latest_trigger_time,
                current_round,
                jev_threshold=jev_threshold,
                pr_number=pr_number,
                context_cache=context_cache,
                trigger_id=trigger_id,
                requested_head_sha=requested_head_sha,
                repository=repository,
                exclude_ids=excluded_ids,
            )
            if immediate is not None:
                return immediate

        print(
            f"Waiting for @{bot_name} activity on PR #{pr_number} (timeout: {timeout}s, interval: {interval}s)..."
        )
        round_ = WaitRound(
            pr_number=pr_number,
            bot_name=bot_name,
            round_num=current_round,
            trigger_id=trigger_id,
            trigger_time=latest_trigger_time,
            requested_head_sha=requested_head_sha,
            repository=repository,
            excluded_ids=excluded_ids,
            jev_threshold=jev_threshold,
            context_cache=context_cache,
            extract_review=_extract_review_result,
            fetch_head_sha=_fetch_pr_head_sha,
        )
        state = PollState(initial_snapshot, CompletedGrace(completed_grace_seconds))
        start_time = time.time()
        consecutive_errors = 0

        while True:
            try:
                current_data = _get_pr_data(pr_number, executor=executor)
                consecutive_errors = 0
                result = poll_step(
                    round_,
                    state,
                    current_data,
                    stall_grace_seconds=stall_grace_seconds,
                    grace_seconds=completed_grace_seconds,
                    timeout_remaining=timeout - (time.time() - start_time),
                    clock=_monotonic,
                )
                if result is not None:
                    return result
            except StalledReviewError:
                raise
            except Exception as e:
                consecutive_errors += 1
                print(f"Warning: Error checking PR review data: {e}", file=sys.stderr)
                if consecutive_errors > max_retries:
                    raise RuntimeError(
                        f"Exceeded maximum retries ({max_retries}) during review polling: {e}"
                    ) from e

            remaining = timeout - (time.time() - start_time)
            if remaining <= 0:
                break
            pause = min(interval, remaining)
            grace_left = state.grace.remaining(_monotonic())
            time.sleep(pause if grace_left is None else min(pause, grace_left))

    if state.grace.active:
        # A Completed tracker was confirmed by a normal fetch, and no review
        # content arrived before the overall timeout.
        return unavailable_result(round_, state, completed_grace_seconds)
    raise TimeoutError(
        f"Timed out waiting for @{bot_name} activity on PR #{pr_number} after {timeout}s."
    )


def _run_online(args: Any) -> None:
    wait_kwargs: dict[str, Any] = {
        "timeout": args.timeout,
        "interval": args.interval,
        "bot_name": args.bot_name,
        "post_trigger": not args.no_post,
        "body": args.body,
        "body_file": args.body_file,
        "max_rounds": args.max_rounds,
        "max_retries": args.max_retries,
        "round_num": args.round,
        "stall_grace_seconds": args.stall_grace,
        "switch_reviewer": args.switch_reviewer,
    }
    if args.jev_threshold is not None:
        wait_kwargs["jev_threshold"] = args.jev_threshold
    result = wait_for_review(args.pr, **wait_kwargs)
    if args.output_file:
        _write_output_file(result, args.output_file)
    # An explicit skip keeps its historical exit 0; a real reviewer whose
    # content could not be acquired must not look like a successful acquisition.
    if (
        result.get("acquisition_status") == ACQUISITION_UNAVAILABLE
        and result.get("reviewer") != "skip"
    ):
        sys.exit(EXIT_NO_RESULT)
    sys.exit(EXIT_ACQUIRED)


def main() -> None:
    args = parse_args(stall_grace_default=DEFAULT_STALL_GRACE_SECONDS)
    try:
        if args.review_state_file:
            sys.exit(run_offline(args, evaluate=evaluate_review_findings))
        _run_online(args)
    except MaxRoundsExceededError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(EXIT_MAX_ROUNDS)
    except StalledReviewError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(EXIT_STALLED)
    except TimeoutError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(EXIT_TIMEOUT)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(EXIT_INTERNAL_ERROR)


if __name__ == "__main__":
    main()

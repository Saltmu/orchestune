"""Script to trigger and detect AI review activity on a GitHub Pull Request.

Posts a review trigger (optional) and polls for any new or updated activity
from the specified bot (Claude, Codex, etc.), returning the full acquired
review content directly to stdout (and optionally a JSON file) for the calling
LLM to judge. This script never computes a pass/fail verdict; see
review-loop.md for the LLM decision procedure.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

from scripts.jev_context import JevReviewContext, collect_review_context
from scripts.jev_filter import evaluate_review_findings
from scripts.review_verdict import (
    ACQUISITION_ACQUIRED as ACQUISITION_ACQUIRED,
)
from scripts.review_verdict import (
    ACQUISITION_IN_PROGRESS as ACQUISITION_IN_PROGRESS,
)
from scripts.review_verdict import (
    ACQUISITION_UNAVAILABLE as ACQUISITION_UNAVAILABLE,
)
from scripts.review_verdict import (
    EXIT_ACQUIRED as EXIT_ACQUIRED,
)
from scripts.review_verdict import (
    EXIT_IN_PROGRESS as EXIT_IN_PROGRESS,
)
from scripts.review_verdict import (
    EXIT_NO_RESULT as EXIT_NO_RESULT,
)
from scripts.review_verdict import (
    SCHEMA_VERSION as SCHEMA_VERSION,
)
from scripts.review_verdict import (
    _build_snapshot as _build_snapshot,
)
from scripts.review_verdict import (
    _filter_bot_items as _filter_bot_items,
)
from scripts.review_verdict import (
    _get_item_created_timestamp,
    _is_explicitly_in_progress,
    _latest_bot_activity_item,
    _latest_bot_summary_item,
    collect_review_state,
    extract_review_result,
    normalize_review_state,
)
from scripts.review_verdict import (
    _get_item_timestamp as _get_item_timestamp,
)
from scripts.review_verdict import (
    _is_bot_user as _is_bot_user,
)

EXIT_INTERNAL_ERROR = 2  # Internal error / Unexpected exception / Arg error
EXIT_MAX_ROUNDS = 12  # Maximum review rounds exceeded
EXIT_TIMEOUT = 20  # Timeout waiting for review activity
EXIT_STALLED = 21  # In-progress tracker comment stopped changing; job likely ended

GH_COMMAND_TIMEOUT_SECONDS = 30

_COMPLETENESS_SECTIONS = ("issue_comments", "reviews", "inline_comments")

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


class StalledReviewError(RuntimeError):
    """Raised when a bot's in-progress tracker comment stops changing.

    This is distinct from a plain timeout: it means the review polling loop
    positively observed the same "in progress" tracker signature for longer
    than the stall grace window, which is strong evidence the workflow run
    that owns the comment already ended (success or failure) without ever
    posting a final result, rather than merely being slow.
    """


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


def _review_trigger_marker(bot_name: str) -> str:
    return f"<!-- orchestune:review-trigger bot={bot_name.lower()} -->"


def _review_round_marker(round_num: int) -> str:
    return f"<!-- orchestune:review-round {round_num} -->"


def _parse_review_round_marker(body: str) -> int | None:
    match = re.search(
        r"<!--\s*orchestune:review-round\s+(\d+)\s*-->", body, re.IGNORECASE
    )
    return int(match.group(1)) if match else None


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


def _mark_review_trigger(body: str, bot_name: str, round_num: int | None = None) -> str:
    trigger_marker = _review_trigger_marker(bot_name)
    result = body
    if trigger_marker not in result.lower():
        result = f"{result.rstrip()}\n\n{trigger_marker}"
    if round_num is not None:
        round_marker = _review_round_marker(round_num)
        if round_marker not in result.lower():
            result = f"{result.rstrip()}\n{round_marker}"
    return result


def _has_review_trigger_mention(body: str, bot_name: str) -> bool:
    body_lower = body.lower()
    bot = bot_name.lower()
    if bot == "claude":
        return "@claude" in body_lower and "review" in body_lower
    pattern = re.compile(rf"@(?:{re.escape(bot_name)})[,\s:]+review\b", re.IGNORECASE)
    return pattern.search(body) is not None


def _ensure_review_trigger_mention(body: str, bot_name: str) -> str:
    trimmed = body.strip()
    if not trimmed:
        return f"@{bot_name} review"
    if _has_review_trigger_mention(trimmed, bot_name):
        return trimmed
    return f"@{bot_name} review\n\n{trimmed}"


def post_review_trigger(
    pr_number: int,
    bot_name: str = "claude",
    body: str | None = None,
    body_file: str | None = None,
    round_num: int = 1,
) -> dict[str, Any]:
    if body_file:
        with open(body_file, encoding="utf-8") as f:
            raw_body = f.read()
    elif body:
        raw_body = body
    else:
        raw_body = ""
    comment_body = _ensure_review_trigger_mention(raw_body, bot_name)
    comment_body = _mark_review_trigger(comment_body, bot_name, round_num=round_num)

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


def _print_review_result(result: dict[str, Any], bot_name: str) -> None:
    """Render the acquired result. This is raw acquired content, not a verdict:
    the calling LLM still has to read it and judge whether it is adequate."""
    inline_items = result.get("inline_comments", [])
    current_inlines = [
        item for item in inline_items if item.get("provenance") == "current"
    ]
    jev_evaluations = result.get("jev_evaluations", [])
    status = result.get("acquisition_status")
    print("\n" + "=" * 72)
    if status == ACQUISITION_ACQUIRED:
        print(f"[AI Review Content Acquired - @{bot_name}] LLM judgment required")
    else:
        # A non-acquired status (unavailable/in_progress) is not ready for
        # judgment; the banner must not claim otherwise even though some
        # partial content may still be shown below for context (Codex PR
        # #1114 round 7 finding).
        reason = result.get("reason") or "no reason given"
        print(f"[AI Review NOT Acquired ({status}) - @{bot_name}] {reason}")
    print(f"Round: {result.get('round')}  Timestamp: {result.get('timestamp', '')}")
    print(
        f"Requested SHA: {result.get('requested_head_sha')}  "
        f"Reviewed SHA: {result.get('reviewed_head_sha')}  "
        f"Current SHA: {result.get('current_head_sha')}"
    )
    if inline_items:
        print(
            f"Inline comments: {len(inline_items)} total "
            f"({len(current_inlines)} current round)"
        )
        jev_by_id = {e["finding_id"]: e for e in jev_evaluations}
        # Mirror evaluate_review_findings()'s id-or-index fallback: a missing
        # id and an explicit `"id": null` are both id-less, and the fallback
        # index must be positional within `current_inlines` (the list that
        # was actually passed to evaluate_review_findings), not within the
        # full inline_items list, which may interleave historical/unassociated
        # items and shift the index basis (Codex PR #1114 round 5 finding).
        current_index_by_identity = {
            id(current_item): index
            for index, current_item in enumerate(current_inlines)
        }
        for index, item in enumerate(inline_items, start=1):
            item_id = item.get("id")
            if item_id is not None:
                finding_id: Any = item_id
            else:
                position = current_index_by_identity.get(id(item))
                # Matches jev_filter._finding_id()'s string-tagged sentinel:
                # a bare int fallback could collide with a coincidentally
                # equal supplied id (Codex PR #1114 round 6 finding).
                finding_id = f"index:{position}" if position is not None else None
            jev = jev_by_id.get(finding_id) if finding_id is not None else None
            jev_note = (
                f" [jev: {jev['decision']} ({jev['decision_reason']})]" if jev else ""
            )
            print(
                f"\n--- Inline Comment {index}: {item['path']}:{item['line']} "
                f"[{item.get('provenance')}]{jev_note} ---"
            )
            print(item["body"] or "(No comment body provided)")
        print("--- End Inline Comments ---")
    else:
        print("Inline comments: none")
    print("=" * 72 + "\n")
    for index, item in enumerate(result.get("review_items", []), start=1):
        print(
            f"--- Review Item {index} [{item.get('kind')}/{item.get('provenance')}] ---"
        )
        print(item.get("body") or "(empty body)")
        print()
    print("--- review_body (concatenated current-round bodies) ---")
    print(result.get("review_body", ""))


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
    """Post (or find) this round's trigger comment.

    Returns (trigger_time, trigger_id, requested_head_sha). The head SHA is
    only captured immediately before actually posting a *new* trigger
    comment: reusing an already-posted trigger (idempotent resume) must not
    report the PR's current head as "requested", since later commits may
    have landed after that trigger was originally sent (issue #1099 PR
    #1114 round 1 review).
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
            return trigger_time, trigger_id, None
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
    return trigger_time, trigger_id, requested_head_sha


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
    latest_bot_activity = _latest_bot_activity_item(initial_data, bot_name, exclude_ids)
    latest_bot_item = _latest_bot_summary_item(initial_data, bot_name, exclude_ids)
    if (
        latest_bot_item is not None
        and latest_trigger_time
        and _get_item_created_timestamp(latest_bot_item) >= latest_trigger_time
        and not (
            latest_bot_activity is not None
            and _is_explicitly_in_progress(latest_bot_activity)
        )
    ):
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
    return None


def _resolve_current_round(
    initial_data: dict[str, list[dict[str, Any]]],
    bot_name: str,
    round_num: int | None,
    post_trigger: bool,
) -> int:
    if round_num is not None:
        return round_num
    latest_existing_round = _get_latest_review_round(initial_data, bot_name)
    if not post_trigger:
        return max(1, latest_existing_round)
    return latest_existing_round + 1


def _track_stall(
    current_bot_activity: dict[str, Any] | None,
    last_signature: str | None,
    since: float | None,
    *,
    stall_grace_seconds: int,
    bot_name: str,
    pr_number: int,
    latest_trigger_time: str = "",
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
    """
    if (
        current_bot_activity is None
        or not _is_explicitly_in_progress(current_bot_activity)
        or (
            latest_trigger_time
            and _get_item_created_timestamp(current_bot_activity) < latest_trigger_time
        )
    ):
        return None, None

    signature = (
        f"{_get_item_timestamp(current_bot_activity)}:"
        f"{len(str(current_bot_activity.get('body') or ''))}"
    )
    if signature != last_signature:
        return signature, time.time()
    if since is not None and time.time() - since >= stall_grace_seconds:
        raise StalledReviewError(
            f"@{bot_name}'s in-progress tracker comment on PR #{pr_number} has "
            f"not changed in over {stall_grace_seconds}s while still reporting "
            "in-progress; the workflow run that owns it most likely already "
            "ended without posting a final result (see review-loop.md)."
        )
    return last_signature, since


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
        initial_snapshot = _build_snapshot(initial_data, bot_name)
        excluded_ids: set[int | str] = set()

        current_round = _resolve_current_round(
            initial_data, bot_name, round_num, post_trigger
        )

        if current_round > max_rounds:
            raise MaxRoundsExceededError(
                f"Maximum review rounds ({max_rounds}) exceeded (attempted round {current_round})."
            )

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
            # An already-posted trigger predates this invocation; the head at
            # the time it was posted was never captured, so it stays unknown
            # rather than guessed from the current head.
            requested_head_sha = None
            latest_trigger_time = _latest_review_trigger_timestamp(
                initial_data, bot_name
            )
            existing_trigger = _find_existing_trigger_comment(
                initial_data, bot_name, current_round
            )
            if existing_trigger is not None:
                trigger_id = existing_trigger.get("id")
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
        start_time = time.time()
        consecutive_errors = 0
        last_in_progress_signature: str | None = None
        in_progress_since: float | None = None

        while True:
            try:
                current_data = _get_pr_data(pr_number, executor=executor)
                consecutive_errors = 0

                # Track staleness of the current "in progress" tracker comment
                # independent of `has_changes` below: a comment that keeps
                # reporting in-progress with an *unchanged* signature across
                # polls never trips the snapshot-diff gate (nothing about it
                # looks "new"), so a genuinely dead tracker would otherwise be
                # invisible until the full timeout elapses. Raises
                # StalledReviewError once the signature has been unchanged for
                # longer than stall_grace_seconds.
                current_bot_activity = _latest_bot_activity_item(
                    current_data, bot_name, exclude_ids=excluded_ids
                )
                last_in_progress_signature, in_progress_since = _track_stall(
                    current_bot_activity,
                    last_in_progress_signature,
                    in_progress_since,
                    stall_grace_seconds=stall_grace_seconds,
                    bot_name=bot_name,
                    pr_number=pr_number,
                    latest_trigger_time=latest_trigger_time,
                )

                current_snapshot = _build_snapshot(
                    current_data, bot_name, exclude_ids=excluded_ids
                )

                has_changes = any(
                    k not in initial_snapshot or initial_snapshot[k] != v
                    for k, v in current_snapshot.items()
                )

                if has_changes:
                    if current_bot_activity is not None and _is_explicitly_in_progress(
                        current_bot_activity
                    ):
                        initial_snapshot = current_snapshot
                        print(f"@{bot_name} is still working; continuing to wait...")
                    else:
                        latest_bot_item = _latest_bot_summary_item(
                            current_data, bot_name, exclude_ids=excluded_ids
                        )
                        if latest_bot_item is not None and (
                            not latest_trigger_time
                            or _get_item_created_timestamp(latest_bot_item)
                            >= latest_trigger_time
                        ):
                            result = _extract_review_result(
                                current_data,
                                bot_name,
                                exclude_ids=excluded_ids,
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
                            if result is not None:
                                return result
                            # Activity was only execution telemetry (e.g. a lone
                            # finished-tracker comment): nothing acquired yet,
                            # keep waiting instead of re-checking every poll.
                            initial_snapshot = current_snapshot
                        else:
                            initial_snapshot = current_snapshot
                            print(
                                f"@{bot_name} activity predates the latest trigger; "
                                "continuing to wait..."
                            )

            except StalledReviewError:
                raise
            except Exception as e:
                consecutive_errors += 1
                print(f"Warning: Error checking PR review data: {e}", file=sys.stderr)
                if consecutive_errors > max_retries:
                    raise RuntimeError(
                        f"Exceeded maximum retries ({max_retries}) during review polling: {e}"
                    ) from e

            if time.time() - start_time >= timeout:
                break
            time.sleep(interval)

    raise TimeoutError(
        f"Timed out waiting for @{bot_name} activity on PR #{pr_number} after {timeout}s."
    )


def _write_output_file(result: dict[str, Any], output_file: str) -> None:
    try:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except OSError as e:
        raise RuntimeError(f"Failed to write --output-file {output_file}: {e}") from e


def _resolve_offline_completeness(state: object) -> tuple[dict[str, str], list[str]]:
    """Resolve the offline `--review-state-file` completeness declaration.

    Distinguishes three cases: no `completeness` key at all (legacy input;
    `.get()` alone can't tell this apart from an explicit `"completeness":
    null`, so presence is checked separately) keeps every section "unknown"
    with no incompleteness; a key present but not a usable object (null, a
    list, a string, ...) is a malformed-but-positive declaration and every
    section is treated as incomplete; a proper dict normalizes each of the
    three required sections (an omitted section counts as incomplete, not an
    implicit "complete") (Codex PR #1114 rounds 1-4 findings).
    """
    completeness_key_present = isinstance(state, dict) and "completeness" in state
    state_completeness = (
        state.get("completeness")
        if isinstance(state, dict) and completeness_key_present
        else None
    )
    if isinstance(state_completeness, dict):
        completeness = {
            section: state_completeness.get(section, "unknown")
            for section in _COMPLETENESS_SECTIONS
        }
        incomplete_sections = [
            section for section, status in completeness.items() if status != "complete"
        ]
    else:
        completeness = dict.fromkeys(_COMPLETENESS_SECTIONS, "unknown")
        incomplete_sections = (
            list(_COMPLETENESS_SECTIONS) if completeness_key_present else []
        )
    return completeness, incomplete_sections


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Trigger and detect AI review activity on a GitHub PR in a single blocking process."
    )
    parser.add_argument(
        "--pr",
        type=int,
        default=None,
        help="Pull Request number to watch (required unless --review-state-file is used)",
    )
    parser.add_argument(
        "--review-state-file",
        type=str,
        default=None,
        help=(
            "Path to a normalized JSON review-state snapshot from GitHub MCP or another "
            "client; evaluates immediately without invoking gh"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="Maximum time to wait in seconds (default: 1800)",
    )
    parser.add_argument(
        "--stall-grace",
        type=int,
        default=DEFAULT_STALL_GRACE_SECONDS,
        help=(
            "Seconds an in-progress tracker comment may stay unchanged before "
            f"it is treated as stalled rather than slow (default: {DEFAULT_STALL_GRACE_SECONDS})"
        ),
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=5,
        help="Polling interval in seconds (default: 5)",
    )
    parser.add_argument(
        "--bot-name",
        type=str,
        default="claude",
        help="Bot user name substring to wait for (default: 'claude')",
    )
    parser.add_argument(
        "--body",
        type=str,
        default=None,
        help="Custom comment body to post when triggering review",
    )
    parser.add_argument(
        "--body-file",
        type=str,
        default=None,
        help="Path to file containing custom comment body to post",
    )
    parser.add_argument(
        "--no-post",
        action="store_true",
        help="Skip posting a review trigger comment and only wait for activity",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=5,
        help="Maximum number of review rounds allowed (default: 5)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=1,
        help="Maximum number of retry attempts on transient failures (default: 1)",
    )
    parser.add_argument(
        "--round",
        type=int,
        default=None,
        help="Explicit review round number (default: auto-detected from PR comments)",
    )
    parser.add_argument(
        "--jev-threshold",
        type=float,
        default=None,
        help="Validity threshold for Jev finding filter (default: 0.7 or JEV_THRESHOLD env)",
    )
    parser.add_argument(
        "--output-file",
        type=str,
        default=None,
        help=(
            "Write the complete machine-readable acquisition result as JSON to this "
            "path. A write failure exits non-zero, even if acquisition itself succeeded."
        ),
    )

    args = parser.parse_args()
    try:
        if args.review_state_file:
            with open(args.review_state_file, encoding="utf-8") as state_file:
                state = json.load(state_file)
            result = collect_review_state(state, args.bot_name)
            current_inlines = [
                item
                for item in result.get("inline_comments", [])
                if item.get("provenance") == "current"
            ]
            jev_evaluations: list[dict[str, Any]] = []
            if current_inlines:
                jev_report = evaluate_review_findings(
                    current_inlines,
                    bot_name=args.bot_name,
                    threshold=args.jev_threshold,
                    pr=args.pr,
                    context=state.get("context") if isinstance(state, dict) else None,
                )
                jev_evaluations = jev_report["jev_evaluations"]
            reviewed_shas = {
                item.get("commit_id")
                for item in result.get("review_items", [])
                if item.get("kind") == "review"
                and item.get("provenance") == "current"
                and item.get("commit_id")
            }
            completeness, incomplete_sections = _resolve_offline_completeness(state)
            result.update(
                repository=None,
                pr_number=args.pr,
                reviewer=args.bot_name,
                round=None,
                trigger_id=None,
                triggered_at="",
                requested_head_sha=None,
                reviewed_head_sha=(
                    next(iter(reviewed_shas)) if len(reviewed_shas) == 1 else None
                ),
                current_head_sha=None,
                jev_evaluations=jev_evaluations,
                completeness=completeness,
            )
            if (
                incomplete_sections
                and result["acquisition_status"] == ACQUISITION_ACQUIRED
            ):
                # The adapter itself declared a section incomplete: a partial
                # fetch must not be reported as a trustworthy acquired result,
                # even though some content was found (issue #1099 PR #1114
                # round 1 review).
                result["acquisition_status"] = ACQUISITION_UNAVAILABLE
                result["reason"] = (
                    "supplied completeness declares "
                    f"{', '.join(incomplete_sections)} incomplete"
                )
            _print_review_result(result, args.bot_name)
            if args.output_file:
                _write_output_file(result, args.output_file)
            status = result["acquisition_status"]
            if status == ACQUISITION_ACQUIRED:
                sys.exit(EXIT_ACQUIRED)
            elif status == ACQUISITION_IN_PROGRESS:
                sys.exit(EXIT_IN_PROGRESS)
            else:
                sys.exit(EXIT_NO_RESULT)
        if args.pr is None:
            parser.error("--pr is required unless --review-state-file is used")
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
        }
        if args.jev_threshold is not None:
            wait_kwargs["jev_threshold"] = args.jev_threshold
        result = wait_for_review(
            args.pr,
            **wait_kwargs,
        )
        if args.output_file:
            _write_output_file(result, args.output_file)
        sys.exit(EXIT_ACQUIRED)
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

"""Script to trigger and detect AI review activity on a GitHub Pull Request.

Posts a review trigger (optional) and polls for any new or updated activity
from the specified bot (Claude, Codex, etc.), returning the latest comment
and inline remarks directly to stdout for LLM evaluation.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

from scripts.review_verdict import (
    EXIT_FINDINGS_PRESENT as EXIT_FINDINGS_PRESENT,
)
from scripts.review_verdict import (
    EXIT_IN_PROGRESS as EXIT_IN_PROGRESS,
)
from scripts.review_verdict import (
    EXIT_NO_FINDINGS as EXIT_NO_FINDINGS,
)
from scripts.review_verdict import (
    EXIT_UNDETERMINED as EXIT_UNDETERMINED,
)
from scripts.review_verdict import (
    _build_snapshot,
    _get_item_created_timestamp,
    _is_explicitly_in_progress,
    _latest_bot_activity_item,
    _latest_bot_summary_item,
    evaluate_review_state,
    evaluate_review_verdict,
    extract_review_result,
    normalize_review_state,
)
from scripts.review_verdict import (
    _filter_bot_items as _filter_bot_items,
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

GH_COMMAND_TIMEOUT_SECONDS = 30


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
    for item in data.get("issue_comments", []):
        if _is_trigger_comment(item, bot_name, round_num=round_num):
            return item
    return None


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
    mention_pattern = re.compile(rf"@{re.escape(bot_name)}\s+review\b", re.IGNORECASE)
    return bool(mention_pattern.search(body))


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
    """Render review results so inline findings cannot be overlooked in agent output."""
    inline_items = result.get("inline_comments", [])
    print("\n" + "=" * 72)
    print(f"[AI Review Update Detected - @{bot_name}]")
    print(f"Timestamp: {result.get('timestamp', '')}")
    if inline_items:
        print(f"[ACTION REQUIRED] Inline Findings: {len(inline_items)} item(s)")
        for index, item in enumerate(inline_items, start=1):
            print(f"\n--- Inline Finding {index}: {item['path']}:{item['line']} ---")
            print(item["body"] or "(No comment body provided)")
        print("--- End Inline Findings ---")
    else:
        print("Inline Findings: none")
    print("=" * 72 + "\n")
    print(result.get("review_body", ""))


def _extract_review_result(
    current_data: dict[str, list[dict[str, Any]]],
    bot_name: str,
    exclude_ids: set[int | str] | None = None,
    latest_item: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    result = extract_review_result(
        normalize_review_state(current_data),
        bot_name,
        exclude_ids=exclude_ids,
        latest_item=latest_item,
    )
    if result is not None:
        _print_review_result(result, bot_name)
    return result


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
) -> str:
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
            return trigger_time
        print(
            f"Review trigger comment for Round {current_round} (Comment ID: {trigger_id}) "
            f"is missing @{bot_name} review mention; reposting trigger..."
        )

    print(
        f"Posting review trigger comment (Round {current_round}/{max_rounds}) "
        f"for @{bot_name} on PR #{pr_number}..."
    )
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
    return trigger_time


def _check_immediate_review_result(
    initial_data: dict[str, list[dict[str, Any]]],
    bot_name: str,
    latest_trigger_time: str,
    current_round: int,
) -> dict[str, Any] | None:
    latest_bot_activity = _latest_bot_activity_item(initial_data, bot_name)
    latest_bot_item = _latest_bot_summary_item(initial_data, bot_name)
    if (
        latest_bot_item is not None
        and latest_trigger_time
        and _get_item_created_timestamp(latest_bot_item) >= latest_trigger_time
        and not (
            latest_bot_activity is not None
            and _is_explicitly_in_progress(latest_bot_activity)
        )
    ):
        result = _extract_review_result(
            initial_data, bot_name, latest_item=latest_bot_item
        )
        if result is not None:
            result["verdict"] = evaluate_review_verdict(
                result.get("review_body", ""),
                result.get("inline_comments", []),
                bot_name=bot_name,
            )
            result["round"] = current_round
            return result
    return None


def wait_for_review(
    pr_number: int,
    *,
    timeout: int = 300,
    interval: int = 5,
    bot_name: str = "claude",
    post_trigger: bool = True,
    body: str | None = None,
    body_file: str | None = None,
    max_rounds: int = 5,
    max_retries: int = 1,
    round_num: int | None = None,
) -> dict[str, Any]:
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

        latest_existing_round = _get_latest_review_round(initial_data, bot_name)
        if round_num is not None:
            current_round = round_num
        elif post_trigger:
            current_round = latest_existing_round + 1
        else:
            current_round = max(1, latest_existing_round)

        if current_round > max_rounds:
            raise MaxRoundsExceededError(
                f"Maximum review rounds ({max_rounds}) exceeded (attempted round {current_round})."
            )

        if post_trigger:
            latest_trigger_time = _handle_review_trigger(
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
        else:
            latest_trigger_time = _latest_review_trigger_timestamp(
                initial_data, bot_name
            )
            immediate = _check_immediate_review_result(
                initial_data, bot_name, latest_trigger_time, current_round
            )
            if immediate is not None:
                return immediate

        print(
            f"Waiting for @{bot_name} activity on PR #{pr_number} (timeout: {timeout}s, interval: {interval}s)..."
        )
        start_time = time.time()
        consecutive_errors = 0

        while True:
            try:
                current_data = _get_pr_data(pr_number, executor=executor)
                consecutive_errors = 0
                current_snapshot = _build_snapshot(
                    current_data, bot_name, exclude_ids=excluded_ids
                )

                has_changes = any(
                    k not in initial_snapshot or initial_snapshot[k] != v
                    for k, v in current_snapshot.items()
                )

                if has_changes:
                    latest_bot_activity = _latest_bot_activity_item(
                        current_data, bot_name, exclude_ids=excluded_ids
                    )
                    if latest_bot_activity is not None and _is_explicitly_in_progress(
                        latest_bot_activity
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
                                latest_item=latest_bot_item,
                            )
                            if result is not None:
                                result["verdict"] = evaluate_review_verdict(
                                    result.get("review_body", ""),
                                    result.get("inline_comments", []),
                                    bot_name=bot_name,
                                )
                                result["round"] = current_round
                                return result
                        else:
                            initial_snapshot = current_snapshot
                            print(
                                f"@{bot_name} activity predates the latest trigger; "
                                "continuing to wait..."
                            )

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
        default=300,
        help="Maximum time to wait in seconds (default: 300)",
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

    args = parser.parse_args()
    try:
        if args.review_state_file:
            with open(args.review_state_file, encoding="utf-8") as state_file:
                result = evaluate_review_state(json.load(state_file), args.bot_name)
            _print_review_result(result, args.bot_name)
            sys.exit(result["verdict"])
        if args.pr is None:
            parser.error("--pr is required unless --review-state-file is used")
        result = wait_for_review(
            args.pr,
            timeout=args.timeout,
            interval=args.interval,
            bot_name=args.bot_name,
            post_trigger=not args.no_post,
            body=args.body,
            body_file=args.body_file,
            max_rounds=args.max_rounds,
            max_retries=args.max_retries,
            round_num=args.round,
        )
        verdict = result.get("verdict")
        if verdict is None:
            verdict = evaluate_review_verdict(
                result.get("review_body", ""),
                result.get("inline_comments", []),
                bot_name=args.bot_name,
            )
        sys.exit(verdict)
    except MaxRoundsExceededError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(EXIT_MAX_ROUNDS)
    except TimeoutError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(EXIT_TIMEOUT)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(EXIT_INTERNAL_ERROR)


if __name__ == "__main__":
    main()

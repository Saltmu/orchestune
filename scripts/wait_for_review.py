"""Script to trigger and detect AI review activity on a GitHub Pull Request.

Posts a review trigger (optional) and polls for any new or updated activity
from the specified bot (Claude, Codex, etc.), returning the latest comment
and inline remarks directly to stdout for LLM evaluation.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

GH_COMMAND_TIMEOUT_SECONDS = 30

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


def _mark_review_trigger(body: str, bot_name: str) -> str:
    marker = _review_trigger_marker(bot_name)
    if marker in body.lower():
        return body
    return f"{body.rstrip()}\n\n{marker}"


def post_review_trigger(
    pr_number: int,
    bot_name: str = "claude",
    body: str | None = None,
    body_file: str | None = None,
) -> dict[str, Any]:
    if body_file:
        with open(body_file, encoding="utf-8") as f:
            comment_body = f.read().strip()
    elif body:
        comment_body = body.strip()
    else:
        comment_body = f"@{bot_name} review"
    comment_body = _mark_review_trigger(comment_body, bot_name)

    stdout = _run_gh(
        [
            "api",
            f"repos/{{owner}}/{{repo}}/issues/{pr_number}/comments",
            "-f",
            f"body={comment_body}",
        ]
    )
    return cast(dict[str, Any], json.loads(stdout))


def _filter_bot_items(
    items: list[dict[str, Any]],
    bot_name: str,
    exclude_ids: set[int | str] | None = None,
) -> list[dict[str, Any]]:
    """Filter list of items to those posted by the specified bot, excluding ignored IDs."""
    filtered: list[dict[str, Any]] = []
    excluded = exclude_ids or set()
    for item in items:
        item_id = item.get("id")
        if item_id in excluded or str(item_id) in excluded:
            continue
        user = (item.get("user") or {}).get("login", "")
        if _is_bot_user(user, bot_name):
            filtered.append(item)
    return filtered


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


def _is_bot_user(user_login: str, bot_name: str) -> bool:
    login = user_login.lower()
    target = bot_name.lower()
    if target == "claude":
        return "claude" in login
    if target == "codex":
        return "codex" in login or "chatgpt-codex-connector" in login
    return target in login


def _get_item_timestamp(item: dict[str, Any]) -> str:
    return (
        item.get("updated_at")
        or item.get("submitted_at")
        or item.get("created_at")
        or ""
    )


def _get_item_created_timestamp(item: dict[str, Any]) -> str:
    return item.get("submitted_at") or item.get("created_at") or ""


def _bot_candidate_items(
    data: dict[str, list[dict[str, Any]]],
    bot_name: str,
    exclude_ids: set[int | str] | None = None,
) -> list[dict[str, Any]]:
    return [
        *_filter_bot_items(data.get("issue_comments", []), bot_name, exclude_ids),
        *_filter_bot_items(data.get("reviews", []), bot_name, exclude_ids),
    ]


def _is_finished_progress_tracker(item: dict[str, Any], bot_name: str) -> bool:
    body = (item.get("body") or "").lower()
    return (
        body.startswith(f"**{bot_name.lower()} finished")
        and "view job" in body
        and "### summary" not in body
    )


def _latest_bot_activity_item(
    data: dict[str, list[dict[str, Any]]],
    bot_name: str,
    exclude_ids: set[int | str] | None = None,
) -> dict[str, Any] | None:
    candidate_items = _bot_candidate_items(data, bot_name, exclude_ids)
    if not candidate_items:
        return None
    return sorted(candidate_items, key=_get_item_timestamp)[-1]


def _latest_bot_summary_item(
    data: dict[str, list[dict[str, Any]]],
    bot_name: str,
    exclude_ids: set[int | str] | None = None,
) -> dict[str, Any] | None:
    candidate_items = _bot_candidate_items(data, bot_name, exclude_ids)
    if not candidate_items:
        return None
    summary_candidates = [
        item
        for item in candidate_items
        if not _is_finished_progress_tracker(item, bot_name)
    ]
    return sorted(summary_candidates or candidate_items, key=_get_item_timestamp)[-1]


def _latest_review_trigger_timestamp(
    data: dict[str, list[dict[str, Any]]], bot_name: str
) -> str:
    trigger_line = f"@{bot_name.lower()} review"
    marker = _review_trigger_marker(bot_name)
    trigger_timestamps = [
        item.get("created_at") or ""
        for item in data.get("issue_comments", [])
        if not _is_bot_user((item.get("user") or {}).get("login", ""), bot_name)
        and (
            marker in (body := (item.get("body") or "")).lower()
            or any(line.strip().lower() == trigger_line for line in body.splitlines())
        )
    ]
    return max(trigger_timestamps, default="")


def _is_explicitly_in_progress(item: dict[str, Any]) -> bool:
    body = item.get("body") or ""
    status_lines = [
        line.lstrip("#").strip().lower().split("<", maxsplit=1)[0].strip()
        for line in body.splitlines()
        if line.strip()
    ]
    markers = (
        "claude is working…",
        "claude is working...",
        "claude code is working…",
        "claude code is working...",
        "codex is working…",
        "codex is working...",
        "review in progress",
        "re-review in progress",
        "claude is reviewing this pr",
        "codex is reviewing this pr",
    )
    is_task_progress = "tasks" in status_lines and "- [ ]" in body
    return is_task_progress or any(line in markers for line in status_lines)


def _build_snapshot(
    data: dict[str, list[dict[str, Any]]],
    bot_name: str,
    exclude_ids: set[int | str] | None = None,
) -> dict[str, str]:
    """Record state of each bot item as id -> timestamp + body length."""
    snapshot: dict[str, str] = {}
    for c in _filter_bot_items(data.get("issue_comments", []), bot_name, exclude_ids):
        cid = str(c.get("id"))
        ts = _get_item_timestamp(c)
        body = c.get("body") or ""
        snapshot[f"comment_{cid}"] = f"{ts}:{len(body)}"

    for r in _filter_bot_items(data.get("reviews", []), bot_name, exclude_ids):
        rid = str(r.get("id"))
        ts = _get_item_timestamp(r)
        body = r.get("body") or ""
        snapshot[f"review_{rid}"] = f"{ts}:{len(body)}"

    for ic in _filter_bot_items(data.get("inline_comments", []), bot_name, exclude_ids):
        icid = str(ic.get("id"))
        ts = _get_item_timestamp(ic)
        snapshot[f"inline_{icid}"] = ts

    return snapshot


def _extract_review_result(
    current_data: dict[str, list[dict[str, Any]]],
    bot_name: str,
    exclude_ids: set[int | str] | None = None,
    latest_item: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if latest_item is None:
        latest_item = _latest_bot_summary_item(current_data, bot_name, exclude_ids)
    if latest_item is None:
        return None
    latest_body = latest_item.get("body") or ""

    # Collect bot inline comments
    inline_items: list[dict[str, Any]] = []
    for ic in _filter_bot_items(
        current_data.get("inline_comments", []), bot_name, exclude_ids
    ):
        inline_items.append(
            {
                "path": ic.get("path", "unknown"),
                "line": ic.get("line") or ic.get("original_line") or "N/A",
                "body": ic.get("body") or "",
            }
        )

    if not latest_body and inline_items:
        latest_body = (
            f"(No review summary body; see {len(inline_items)} inline comment(s) below)"
        )

    print("\n" + "=" * 60)
    print(f"[AI Review Update Detected - @{bot_name}]")
    print(f"Timestamp: {_get_item_timestamp(latest_item)}")
    if inline_items:
        print(f"Inline Comments: {len(inline_items)} item(s)")
        for item in inline_items:
            first_line = item["body"].strip().split("\n")[0] if item["body"] else ""
            print(f"  * {item['path']}:{item['line']} — {first_line}")
    print("=" * 60 + "\n")
    print(latest_body)

    return {
        "review_body": latest_body,
        "inline_comments": inline_items,
        "timestamp": _get_item_timestamp(latest_item),
    }


def wait_for_review(
    pr_number: int,
    *,
    timeout: int = 300,
    interval: int = 5,
    bot_name: str = "claude",
    post_trigger: bool = True,
    body: str | None = None,
    body_file: str | None = None,
) -> dict[str, Any]:
    with ThreadPoolExecutor(max_workers=3) as executor:
        # Capture initial state before triggering/waiting
        initial_data = _get_pr_data(pr_number, executor=executor)
        initial_snapshot = _build_snapshot(initial_data, bot_name)
        excluded_ids: set[int | str] = set()

        if post_trigger:
            print(
                f"Posting review trigger comment for @{bot_name} on PR #{pr_number}..."
            )
            trigger_info = post_review_trigger(
                pr_number, bot_name=bot_name, body=body, body_file=body_file
            )
            trigger_id = trigger_info.get("id")
            trigger_time = trigger_info.get("created_at", "")
            trigger_body = trigger_info.get("body") or ""
            if trigger_id is not None:
                excluded_ids.add(trigger_id)
                # Seed into initial snapshot to prevent self-detection if posted under bot identity
                initial_snapshot[f"comment_{trigger_id}"] = (
                    f"{trigger_time}:{len(trigger_body)}"
                )
            print(f"Trigger posted (Comment ID: {trigger_id}, time: {trigger_time})")

        if not post_trigger:
            latest_trigger_time = _latest_review_trigger_timestamp(
                initial_data, bot_name
            )
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
                    return result

        print(
            f"Waiting for @{bot_name} activity on PR #{pr_number} (timeout: {timeout}s, interval: {interval}s)..."
        )
        start_time = time.time()

        while True:
            try:
                current_data = _get_pr_data(pr_number, executor=executor)
                current_snapshot = _build_snapshot(
                    current_data, bot_name, exclude_ids=excluded_ids
                )

                # Check if any new item appeared or existing item was updated
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
                        result = _extract_review_result(
                            current_data,
                            bot_name,
                            exclude_ids=excluded_ids,
                            latest_item=latest_bot_item,
                        )
                        if result is not None:
                            return result

            except Exception as e:
                print(f"Warning: Error checking PR review data: {e}", file=sys.stderr)

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
        required=True,
        help="Pull Request number to watch",
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

    args = parser.parse_args()
    try:
        wait_for_review(
            args.pr,
            timeout=args.timeout,
            interval=args.interval,
            bot_name=args.bot_name,
            post_trigger=not args.no_post,
            body=args.body,
            body_file=args.body_file,
        )
        sys.exit(0)
    except TimeoutError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()

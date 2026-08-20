"""Script to wait for AI review completion on a GitHub Pull Request.

Encapsulates waiting logic into a single blocking process so that agents
do not need to loop or spawn multiple background tasks.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from typing import Any


def _run_gh_api(endpoint: str) -> Any:
    cmd = ["gh", "api", endpoint]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh api {endpoint} failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def _get_pr_comments(pr_number: int) -> list[dict[str, Any]]:
    return _run_gh_api(f"repos/{{owner}}/{{repo}}/issues/{pr_number}/comments")


def is_review_completed_comment(comment: dict[str, Any], bot_name: str) -> bool:
    user_login = (comment.get("user") or {}).get("login", "").lower()
    if bot_name.lower() not in user_login:
        return False
    body = comment.get("body", "")
    # If it's still a placeholder or progress indicator, not complete yet
    if (
        "is working…" in body
        or "Review in progress" in body
        or "Re-review in progress" in body
    ):
        return False
    # Typical review completion indicators
    if (
        "Re-review complete" in body
        or "Review complete" in body
        or "Claude finished" in body
        or "Summary" in body
        or "###" in body
    ):
        return True
    # If body is substantial and doesn't look like a progress spinner
    return len(body.strip()) > 100


def wait_for_review(
    pr_number: int,
    *,
    timeout: int = 600,
    interval: int = 10,
    bot_name: str = "claude",
) -> str:
    print(f"Waiting for @{bot_name} review on PR #{pr_number} (timeout: {timeout}s)...")
    start_time = time.time()

    # Get initial comment IDs to ignore pre-existing comments
    try:
        initial_comments = _get_pr_comments(pr_number)
        initial_ids = {
            c["id"]
            for c in initial_comments
            if is_review_completed_comment(c, bot_name)
        }
    except Exception as e:
        print(f"Warning: Could not fetch initial comments: {e}", file=sys.stderr)
        initial_ids = set()

    while time.time() - start_time < timeout:
        time.sleep(interval)
        try:
            current_comments = _get_pr_comments(pr_number)
            for comment in reversed(current_comments):
                if comment["id"] in initial_ids:
                    continue
                if is_review_completed_comment(comment, bot_name):
                    print(
                        f"\n[+] Review detected from @{bot_name} (comment ID: {comment['id']}):\n"
                    )
                    print(comment["body"])
                    return comment["body"]
        except Exception as e:
            print(f"Warning: Error checking comments: {e}", file=sys.stderr)

    raise TimeoutError(
        f"Timed out waiting for @{bot_name} review on PR #{pr_number} after {timeout}s."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Wait for an AI review comment on a GitHub PR in a single blocking process."
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
        default=600,
        help="Maximum time to wait in seconds (default: 600)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=10,
        help="Polling interval in seconds (default: 10)",
    )
    parser.add_argument(
        "--bot-name",
        type=str,
        default="claude",
        help="Bot user name substring to wait for (default: 'claude')",
    )

    args = parser.parse_args()
    try:
        wait_for_review(
            args.pr,
            timeout=args.timeout,
            interval=args.interval,
            bot_name=args.bot_name,
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

"""Script to wait for AI review completion on a GitHub Pull Request.

Encapsulates review trigger posting, multi-bot polling (Claude & Codex),
and findings analysis into a single blocking process.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from typing import Any, cast


def _run_gh_api(endpoint: str, *extra_args: str) -> list[dict[str, Any]]:
    cmd = ["gh", "api", "--paginate", "--slurp", endpoint, *extra_args]
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
    pages = json.loads(result.stdout)
    if isinstance(pages, list) and pages and isinstance(pages[0], list):
        flattened: list[dict[str, Any]] = []
        for page in pages:
            flattened.extend(page)
        return flattened
    if isinstance(pages, list):
        return pages
    return [pages]


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

    cmd = [
        "gh",
        "api",
        f"repos/{{owner}}/{{repo}}/issues/{pr_number}/comments",
        "-f",
        f"body={comment_body}",
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to post review comment: {result.stderr.strip()}")
    return cast(dict[str, Any], json.loads(result.stdout))


def _get_pr_data(pr_number: int) -> dict[str, list[dict[str, Any]]]:
    issue_comments = _run_gh_api(
        f"repos/{{owner}}/{{repo}}/issues/{pr_number}/comments"
    )
    try:
        reviews = _run_gh_api(f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/reviews")
    except Exception:
        reviews = []
    try:
        inline_comments = _run_gh_api(
            f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/comments"
        )
    except Exception:
        inline_comments = []
    return {
        "issue_comments": issue_comments,
        "reviews": reviews,
        "inline_comments": inline_comments,
    }


def _is_bot_user(user_login: str, bot_name: str) -> bool:
    login = user_login.lower()
    target = bot_name.lower()
    if target == "claude":
        return "claude" in login
    if target == "codex":
        return "codex" in login or "chatgpt-codex-connector" in login
    return target in login


def is_review_completed_comment(
    comment_or_review: dict[str, Any], bot_name: str
) -> bool:
    user_login = (comment_or_review.get("user") or {}).get("login", "")
    if not _is_bot_user(user_login, bot_name):
        return False
    body = comment_or_review.get("body", "")
    # If it contains an unchecked checkbox or spinner img tag, it is in-progress
    if "- [ ]" in body or "<img" in body:
        return False
    # If it contains known in-progress phrases, not complete yet
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
        or "### Summary" in body
        or "### Findings" in body
        or "Codex Review" in body
        or "💡 Codex Review" in body
        or "Here are some automated review suggestions" in body
    ):
        return True
    return False


def analyze_review_findings(
    review_body: str, inline_comments: list[dict[str, Any]]
) -> dict[str, Any]:
    inline_findings = []
    for comment in inline_comments:
        c_body = comment.get("body", "")
        path = comment.get("path", "unknown")
        line = comment.get("line") or comment.get("original_line") or "N/A"
        # Extract title or badge if present
        first_line = c_body.strip().split("\n")[0] if c_body else ""
        inline_findings.append(
            {
                "path": path,
                "line": line,
                "summary": first_line,
                "body": c_body,
            }
        )

    # Analyze review body for findings
    body_findings = []
    has_blocking_findings = False

    # Check for Claude style findings
    findings_match = re.search(
        r"###\s*Findings\s*\n(.*?)(?=\n###|\Z)", review_body, re.DOTALL
    )
    if findings_match:
        findings_text = findings_match.group(1).strip()
        if findings_text:
            body_findings.append(findings_text)
            has_blocking_findings = True

    if (
        "🔴" in review_body
        or "Still open — Bug:" in review_body
        or "Bug:" in review_body
    ):
        has_blocking_findings = True

    if inline_findings:
        has_blocking_findings = True

    # Check for explicit LGTM indicators
    is_lgtm = False
    if not has_blocking_findings:
        if (
            "LGTM" in review_body
            or "All checks passed" in review_body
            or "All checks pass" in review_body
            or "no further blocking findings" in review_body.lower()
            or "no issues found" in review_body.lower()
        ):
            is_lgtm = True

    has_findings = has_blocking_findings or (not is_lgtm and len(inline_findings) > 0)
    status = "FINDINGS_DETECTED" if has_findings else "ALL_CLEAR"

    summary_lines = [
        f"- Status: {status}",
    ]
    if inline_findings:
        summary_lines.append(f"- Inline Findings: {len(inline_findings)} item(s)")
        for item in inline_findings:
            summary_lines.append(
                f"  * {item['path']}:{item['line']} — {item['summary']}"
            )
    if body_findings:
        summary_lines.append("- Review Body Findings:")
        for fb in body_findings:
            # take first few lines
            snippet = "\n".join(fb.splitlines()[:5])
            summary_lines.append(f"  {snippet}")
    elif "🔴" in review_body:
        for line in review_body.splitlines():
            if "🔴" in line:
                summary_lines.append(f"  * {line.strip()}")

    return {
        "status": status,
        "has_findings": has_findings,
        "inline_findings": inline_findings,
        "body_findings": body_findings,
        "summary": "\n".join(summary_lines),
    }


def wait_for_review(
    pr_number: int,
    *,
    timeout: int = 600,
    interval: int = 10,
    bot_name: str = "claude",
    post_trigger: bool = True,
    body: str | None = None,
    body_file: str | None = None,
) -> dict[str, Any]:
    trigger_time = ""
    if post_trigger:
        print(f"Posting review trigger comment for @{bot_name} on PR #{pr_number}...")
        trigger_info = post_review_trigger(
            pr_number, bot_name=bot_name, body=body, body_file=body_file
        )
        trigger_time = trigger_info.get("created_at", "")
        print(
            f"Trigger posted (Comment ID: {trigger_info.get('id')}, time: {trigger_time})"
        )

    print(f"Waiting for @{bot_name} review on PR #{pr_number} (timeout: {timeout}s)...")
    start_time = time.time()

    while True:
        try:
            pr_data = _get_pr_data(pr_number)
            candidate_reviews: list[dict[str, Any]] = []

            # Check issue comments (Claude, etc.)
            for c in pr_data.get("issue_comments", []):
                c_time = c.get("created_at", "")
                if trigger_time and c_time < trigger_time:
                    continue
                if is_review_completed_comment(c, bot_name):
                    candidate_reviews.append(c)

            # Check PR reviews (Codex, etc.)
            for r in pr_data.get("reviews", []):
                r_time = r.get("submitted_at") or r.get("created_at", "")
                if trigger_time and r_time < trigger_time:
                    continue
                if is_review_completed_comment(r, bot_name):
                    candidate_reviews.append(r)

            if candidate_reviews:
                # Get the latest review
                latest_review = candidate_reviews[-1]
                review_body = latest_review.get("body", "")

                # Collect inline comments created around or after the review/trigger
                relevant_inline = []
                for ic in pr_data.get("inline_comments", []):
                    ic_user = (ic.get("user") or {}).get("login", "")
                    if not _is_bot_user(ic_user, bot_name):
                        continue
                    ic_time = ic.get("created_at", "")
                    if trigger_time and ic_time < trigger_time:
                        continue
                    relevant_inline.append(ic)

                analysis = analyze_review_findings(review_body, relevant_inline)

                print("\n" + "=" * 60)
                print(f"[AI Review Summary - @{bot_name}]")
                print(analysis["summary"])
                print("=" * 60 + "\n")
                print(review_body)

                return {
                    "review_body": review_body,
                    "status": analysis["status"],
                    "has_findings": analysis["has_findings"],
                    "inline_findings": analysis["inline_findings"],
                    "summary": analysis["summary"],
                }

        except Exception as e:
            print(f"Warning: Error checking PR review data: {e}", file=sys.stderr)

        if time.time() - start_time >= timeout:
            break
        time.sleep(interval)

    raise TimeoutError(
        f"Timed out waiting for @{bot_name} review on PR #{pr_number} after {timeout}s."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Trigger and wait for an AI review on a GitHub PR in a single blocking process."
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
        help="Skip posting a review trigger comment and only wait for review",
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

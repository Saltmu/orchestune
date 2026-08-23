"""Normalize AI review state and evaluate its verdict independently of transport."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

EXIT_NO_FINDINGS = 0
EXIT_FINDINGS_PRESENT = 10
EXIT_IN_PROGRESS = 11
EXIT_UNDETERMINED = 30

ReviewState = dict[str, list[dict[str, Any]]]
_STATE_SECTIONS = ("issue_comments", "reviews", "inline_comments")


def normalize_review_state(value: object) -> ReviewState:
    """Validate a transport-neutral snapshot supplied by GitHub CLI or an MCP client."""
    if not isinstance(value, Mapping):
        raise ValueError("review state must be a JSON object")

    normalized: ReviewState = {}
    for section in _STATE_SECTIONS:
        items = value.get(section, [])
        if not isinstance(items, list):
            raise ValueError(f"review state section {section!r} must be a list")
        if not all(isinstance(item, Mapping) for item in items):
            raise ValueError(f"review state section {section!r} must contain objects")
        normalized[section] = [dict(item) for item in items]
    return normalized


def _is_bot_user(user_login: str, bot_name: str) -> bool:
    login = user_login.lower()
    target = bot_name.lower()
    if target == "claude":
        return "claude" in login
    if target == "codex":
        return "codex" in login or "chatgpt-codex-connector" in login
    return target in login


def _filter_bot_items(
    items: list[dict[str, Any]],
    bot_name: str,
    exclude_ids: set[int | str] | None = None,
) -> list[dict[str, Any]]:
    excluded = exclude_ids or set()
    return [
        item
        for item in items
        if item.get("id") not in excluded
        and str(item.get("id")) not in excluded
        and _is_bot_user(str((item.get("user") or {}).get("login", "")), bot_name)
    ]


def _get_item_timestamp(item: dict[str, Any]) -> str:
    return str(
        item.get("updated_at")
        or item.get("submitted_at")
        or item.get("created_at")
        or ""
    )


def _get_item_created_timestamp(item: dict[str, Any]) -> str:
    return str(item.get("submitted_at") or item.get("created_at") or "")


def _bot_candidate_items(
    data: ReviewState, bot_name: str, exclude_ids: set[int | str] | None = None
) -> list[dict[str, Any]]:
    return [
        *_filter_bot_items(data["issue_comments"], bot_name, exclude_ids),
        *_filter_bot_items(data["reviews"], bot_name, exclude_ids),
    ]


def _is_finished_progress_tracker(item: dict[str, Any], bot_name: str) -> bool:
    body = str(item.get("body") or "").lower()
    return (
        body.startswith(f"**{bot_name.lower()} finished")
        and "view job" in body
        and "\n---" not in body
    )


def _latest_bot_activity_item(
    data: ReviewState, bot_name: str, exclude_ids: set[int | str] | None = None
) -> dict[str, Any] | None:
    candidates = _bot_candidate_items(data, bot_name, exclude_ids)
    return max(candidates, key=_get_item_timestamp, default=None)


def _latest_bot_summary_item(
    data: ReviewState, bot_name: str, exclude_ids: set[int | str] | None = None
) -> dict[str, Any] | None:
    candidates = _bot_candidate_items(data, bot_name, exclude_ids)
    summaries = [
        item for item in candidates if not _is_finished_progress_tracker(item, bot_name)
    ]
    return max(summaries or candidates, key=_get_item_timestamp, default=None)


def _is_explicitly_in_progress(item: dict[str, Any]) -> bool:
    body = str(item.get("body") or "")
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
        "レビュー進行中",
        "再レビュー進行中",
    )
    return _has_unfinished_task_list(body) or any(
        line in markers or bool(re.match(r"^round\s+\d+\s+レビュー進行中$", line))
        for line in status_lines
    )


def _has_unfinished_task_list(body: str) -> bool:
    if "### review complete" in body.lower():
        return False
    if "view job run" in body.lower() and any(
        line.strip().startswith("- [ ]") for line in body.splitlines()
    ):
        return True
    in_tasks_section = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip().lower()
            if in_tasks_section:
                return False
            in_tasks_section = (
                heading == "tasks" or "進行中" in heading or "in progress" in heading
            )
        elif in_tasks_section and stripped.startswith("- [ ]"):
            return True
    return False


def _build_snapshot(
    data: ReviewState, bot_name: str, exclude_ids: set[int | str] | None = None
) -> dict[str, str]:
    """Record bot activity for polling without coupling it to a data transport."""
    snapshot: dict[str, str] = {}
    for prefix, section in (("comment", "issue_comments"), ("review", "reviews")):
        for item in _filter_bot_items(data[section], bot_name, exclude_ids):
            snapshot[f"{prefix}_{item.get('id')}"] = (
                f"{_get_item_timestamp(item)}:{len(str(item.get('body') or ''))}"
            )
    for item in _filter_bot_items(data["inline_comments"], bot_name, exclude_ids):
        snapshot[f"inline_{item.get('id')}"] = _get_item_timestamp(item)
    return snapshot


def extract_review_result(
    data: ReviewState,
    bot_name: str,
    exclude_ids: set[int | str] | None = None,
    latest_item: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Extract the latest bot summary and every associated inline comment."""
    latest = latest_item or _latest_bot_summary_item(data, bot_name, exclude_ids)
    if latest is None:
        return None
    inlines = [
        {
            "path": item.get("path", "unknown"),
            "line": item.get("line") or item.get("original_line") or "N/A",
            "body": item.get("body") or "",
        }
        for item in _filter_bot_items(data["inline_comments"], bot_name, exclude_ids)
    ]
    body = latest.get("body") or ""
    if not body and inlines:
        body = f"(No review summary body; see {len(inlines)} inline comment(s) below)"
    return {
        "review_body": body,
        "inline_comments": inlines,
        "timestamp": _get_item_timestamp(latest),
    }


def _evaluate_marker_layer(body: str) -> int | None:
    match = re.search(r"<!--\s*orchestune:verdict\s+(pass|fail)\s*-->", body, re.I)
    if match:
        return (
            EXIT_NO_FINDINGS
            if match.group(1).lower() == "pass"
            else EXIT_FINDINGS_PRESENT
        )
    return None


def _evaluate_codex_signals(
    body_lower: str, inlines: list[dict[str, Any]]
) -> int | None:
    if inlines or any(
        signal in body_lower
        for signal in (
            "p1 badge",
            "p2 badge",
            "p1:",
            "p2:",
            "[p1]",
            "[p2]",
        )
    ):
        return EXIT_FINDINGS_PRESENT
    if "here are some automated review suggestions" in body_lower:
        return EXIT_NO_FINDINGS
    if any(
        signal in body_lower
        for signal in (
            "didn't find any major issues",
            "no major issues found",
            "no issues found",
            "nice work",
            "lgtm",
        )
    ):
        return EXIT_NO_FINDINGS
    return None


def _evaluate_claude_signals(
    body_lower: str, inlines: list[dict[str, Any]]
) -> int | None:
    if inlines:
        return EXIT_FINDINGS_PRESENT
    clean = any(
        re.search(pattern, body_lower)
        for pattern in (
            r"\b(?:no|without)\b(?:\s+\w+){0,3}\s+blocking\s+(?:issues?|bugs?|problems?)",
            r"\ball\s+checks\s+passed\b",
            r"\blooks\s+good\b",
            r"\blgtm\b",
        )
    )
    blocking_matches = list(
        re.finditer(r"\bblocking\s+(?:bugs?|issues?)\b", body_lower)
    )
    blocking_keyword = any(
        not re.search(
            r"\b(?:no|without)\b(?:\s+\w+){0,3}\s*$",
            body_lower[max(0, match.start() - 30) : match.start()],
        )
        for match in blocking_matches
    )
    blocking = blocking_keyword or any(
        re.search(pattern, body_lower)
        for pattern in (
            r"marking\s+this\s+\*\*fail\*\*",
            r"marking\s+this\s+fail\b",
            r"verdict:\s*fail\b",
            r"should\s+not\s+be\s+merged\b",
        )
    )
    if "### findings" in body_lower:
        findings = body_lower.split("### findings", 1)[1]
        blocking = blocking or any(
            marker in findings
            for marker in ("- [ ]", "🔴", "⚠️", "bug:", "defect:", "vulnerability")
        )
    return EXIT_FINDINGS_PRESENT if blocking else EXIT_NO_FINDINGS if clean else None


def evaluate_review_verdict(
    review_body: str,
    inline_comments: list[dict[str, Any]] | None = None,
    bot_name: str = "claude",
) -> int:
    """Apply the shared exit-code contract to an already acquired review result."""
    body = review_body or ""
    inlines = inline_comments or []
    if (marker := _evaluate_marker_layer(body)) is not None:
        return marker
    if _is_explicitly_in_progress({"body": body}):
        return EXIT_IN_PROGRESS
    if bot_name.lower() == "codex":
        verdict = _evaluate_codex_signals(body.lower(), inlines)
    elif bot_name.lower() == "claude":
        verdict = _evaluate_claude_signals(body.lower(), inlines)
    else:
        verdict = (
            EXIT_FINDINGS_PRESENT
            if inlines
            else (
                EXIT_NO_FINDINGS
                if any(
                    x in body.lower()
                    for x in ("lgtm", "all checks passed", "no blocking issues")
                )
                else None
            )
        )
    return verdict if verdict is not None else EXIT_UNDETERMINED


def evaluate_review_state(value: object, bot_name: str = "claude") -> dict[str, Any]:
    """Evaluate externally acquired review state without using the GitHub CLI."""
    data = normalize_review_state(value)
    result = extract_review_result(data, bot_name)
    if result is None:
        return {
            "review_body": "",
            "inline_comments": [],
            "timestamp": "",
            "verdict": EXIT_UNDETERMINED,
        }
    result["verdict"] = evaluate_review_verdict(
        result["review_body"], result["inline_comments"], bot_name
    )
    return result

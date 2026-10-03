"""Normalize AI review activity into a transport-neutral acquisition-state snapshot.

Acquisition (this module) is independent of semantic judgment: it identifies bot
activity, associates every review item and inline comment with the current round
via `provenance`, and reports completeness. Deciding whether the acquired content
constitutes an adequate, passing review is the calling LLM's responsibility (see
review-loop.md); this module never computes a pass/fail verdict.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

SCHEMA_VERSION = 1

ACQUISITION_ACQUIRED = "acquired"
ACQUISITION_UNAVAILABLE = "unavailable"
ACQUISITION_IN_PROGRESS = "in_progress"

# Exit codes this module's results map to. Acquisition-loop control codes
# (internal error=2, round limit=12, timeout=20, stalled=21) are raised as
# exceptions by scripts/wait_for_review.py and are not produced here.
EXIT_ACQUIRED = 0
EXIT_IN_PROGRESS = 11
EXIT_NO_RESULT = 30

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
    return sorted(candidates, key=_get_item_timestamp)[-1] if candidates else None


def _latest_bot_summary_item(
    data: ReviewState, bot_name: str, exclude_ids: set[int | str] | None = None
) -> dict[str, Any] | None:
    candidates = _bot_candidate_items(data, bot_name, exclude_ids)
    summaries = [
        item for item in candidates if not _is_finished_progress_tracker(item, bot_name)
    ]
    return (
        sorted(summaries or candidates, key=_get_item_timestamp)[-1]
        if candidates
        else None
    )


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


def _normalize_review_item(
    item: dict[str, Any], kind: str, provenance: str
) -> dict[str, Any]:
    normalized: dict[str, Any] = {
        "id": item.get("id"),
        "kind": kind,
        "body": item.get("body") or "",
        "created_at": item.get("created_at") or "",
        "updated_at": item.get("updated_at") or "",
        "submitted_at": item.get("submitted_at") or "",
        "provenance": provenance,
    }
    if kind == "review":
        normalized["state"] = item.get("state")
        normalized["commit_id"] = item.get("commit_id")
    return normalized


def _normalize_inline_item(item: dict[str, Any], provenance: str) -> dict[str, Any]:
    return {
        "path": item.get("path", "unknown"),
        "line": item.get("line") or item.get("original_line") or "N/A",
        "body": item.get("body") or "",
        **{
            key: item[key]
            for key in (
                "id",
                "diff_hunk",
                "side",
                "start_line",
                "start_side",
                "commit_id",
                "original_commit_id",
                "original_line",
                "context",
                "pull_request_review_id",
            )
            if key in item
        },
        "position_line": item.get("position_line", item.get("line")),
        "provenance": provenance,
    }


def _classify_provenance(created: str, round_started_at: str) -> str:
    """Attribute one item to the current round, an earlier round, or unknown timing.

    An empty `round_started_at` means the caller has no trigger boundary to compare
    against (e.g. an offline snapshot with no round metadata); such content is not
    discarded, it is treated as belonging to the only round the caller knows about.
    """
    if not round_started_at:
        return "current"
    if not created:
        return "unassociated"
    return "current" if created >= round_started_at else "historical"


def _classify_inline_provenance(
    item: dict[str, Any],
    round_started_at: str,
    review_provenance_by_id: dict[Any, str],
) -> str:
    """Prefer the parent review's confirmed provenance over the comment's own
    timestamp when both are known: an inline comment's `pull_request_review_id`
    proves which review it belongs to, so a comment attached to a *historical*
    review must not be promoted to `current` just because it (or a later edit)
    carries a recent timestamp; a review id that is present but not found among
    this snapshot's review items cannot be confirmed either way (Codex PR #1114
    round 3 finding).
    """
    review_id = item.get("pull_request_review_id")
    if review_id is not None:
        return review_provenance_by_id.get(review_id, "unassociated")
    return _classify_provenance(_get_item_created_timestamp(item), round_started_at)


def extract_review_result(
    data: ReviewState,
    bot_name: str,
    exclude_ids: set[int | str] | None = None,
    round_started_at: str = "",
) -> dict[str, Any] | None:
    """Collect every bot review item and inline comment, tagged with round provenance.

    Unlike the previous single-latest-item extraction, this returns *all* bot
    `issue_comments`/`reviews` and *all* inline comments the bot has posted, each
    tagged `current` (>= round_started_at), `historical` (before it), or
    `unassociated` (no usable timestamp) — nothing is discarded. Returns None only
    when the bot has posted no activity of any kind.
    """
    review_candidates = [
        *[
            (item, "issue_comment")
            for item in _filter_bot_items(data["issue_comments"], bot_name, exclude_ids)
        ],
        *[
            (item, "review")
            for item in _filter_bot_items(data["reviews"], bot_name, exclude_ids)
        ],
    ]
    inline_candidates = _filter_bot_items(
        data["inline_comments"], bot_name, exclude_ids
    )

    # Execution telemetry -- a "job finished" tracker or a body that is still
    # explicitly reporting in-progress -- is not review content, even when it
    # happens to be the most recent item in the round (Codex PR #1114 round 3
    # finding: an older in-progress body left over once a "finished" tracker
    # arrives with no summary must not be read as a completed review).
    review_items = [
        _normalize_review_item(
            item,
            kind,
            _classify_provenance(_get_item_created_timestamp(item), round_started_at),
        )
        for item, kind in sorted(
            review_candidates, key=lambda pair: _get_item_timestamp(pair[0])
        )
        if not _is_finished_progress_tracker(item, bot_name)
        and not _is_explicitly_in_progress(item)
    ]
    # Only "review" items (not issue_comments) share an id namespace with
    # inline comments' `pull_request_review_id`.
    review_provenance_by_id = {
        item["id"]: item["provenance"]
        for item in review_items
        if item["kind"] == "review" and item["id"] is not None
    }
    inline_items = [
        _normalize_inline_item(
            item,
            _classify_inline_provenance(
                item, round_started_at, review_provenance_by_id
            ),
        )
        for item in sorted(inline_candidates, key=_get_item_timestamp)
    ]

    timestamps = [_get_item_timestamp(item) for item, _ in review_candidates] + [
        _get_item_timestamp(item) for item in inline_candidates
    ]
    return _review_content_result(review_items, inline_items, timestamps)


def _review_content_result(
    review_items: list[dict[str, Any]],
    inline_items: list[dict[str, Any]],
    timestamps: list[str],
) -> dict[str, Any] | None:
    """Keep only current content as evidence of a round result."""
    # A lone "job finished" tracker comment, or a review/comment record with no
    # body and no inline comments (empty review, trigger-only, all-sections-empty),
    # is execution telemetry rather than review content: treat it the same as no
    # activity at all, distinct from a genuine zero-findings review (which always
    # carries real body text, e.g. "LGTM, no issues found") (issue #1099). This
    # must be scoped to `current`-provenance items only: a PR with a real
    # historical review body must not let that stale content satisfy a new
    # round whose own activity is empty/tracker-only (Codex PR #1114 round 2
    # finding).
    has_current_content = any(
        item["body"] for item in review_items if item["provenance"] == "current"
    ) or any(item["provenance"] == "current" for item in inline_items)
    if not has_current_content:
        return None

    current_bodies = [
        item["body"]
        for item in review_items
        if item["provenance"] == "current" and item["body"]
    ]
    review_body = "\n\n---\n\n".join(current_bodies)
    if not review_body:
        current_inlines = [
            item for item in inline_items if item["provenance"] == "current"
        ]
        if current_inlines:
            review_body = f"(No review summary body; see {len(current_inlines)} inline comment(s) below)"

    return {
        "review_items": review_items,
        "review_body": review_body,
        "inline_comments": inline_items,
        "timestamp": max(timestamps, default=""),
    }


def collect_review_state(
    value: object,
    bot_name: str = "claude",
    *,
    exclude_ids: set[int | str] | None = None,
    round_started_at: str = "",
) -> dict[str, Any]:
    """Assemble an acquisition-state result from an externally acquired snapshot.

    Used by the offline `--review-state-file` path. Online adapters
    (`scripts/wait_for_review.py`) build the equivalent result while additionally
    tracking round/trigger/SHA identifiers this transport-neutral entry point does
    not have.
    """
    data = normalize_review_state(value)
    result = extract_review_result(data, bot_name, exclude_ids, round_started_at)

    # A single snapshot (no polling loop available to this entry point) whose
    # latest bot activity explicitly reports still-in-progress must not be
    # treated as a final acquired-or-unavailable result (issue #1099 Exit 11).
    latest_activity = _latest_bot_activity_item(data, bot_name, exclude_ids)
    if latest_activity is not None and _is_explicitly_in_progress(latest_activity):
        return {
            "schema_version": SCHEMA_VERSION,
            "acquisition_status": ACQUISITION_IN_PROGRESS,
            "reason": f"@{bot_name} activity is explicitly still in progress",
            "review_items": result["review_items"] if result else [],
            "review_body": result["review_body"] if result else "",
            "inline_comments": result["inline_comments"] if result else [],
            "timestamp": (
                result["timestamp"] if result else _get_item_timestamp(latest_activity)
            ),
        }

    if result is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "acquisition_status": ACQUISITION_UNAVAILABLE,
            "reason": f"no @{bot_name} activity found in the supplied review state",
            "review_items": [],
            "review_body": "",
            "inline_comments": [],
            "timestamp": "",
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "acquisition_status": ACQUISITION_ACQUIRED,
        "reason": "",
        **result,
    }

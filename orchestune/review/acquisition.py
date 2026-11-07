"""Normalize AI review activity into a transport-neutral acquisition-state snapshot.

Acquisition (this module) is independent of semantic judgment: it identifies bot
activity, associates every review item and inline comment with the current round
via `provenance`, and reports completeness. Deciding whether the acquired content
constitutes an adequate, passing review is the calling LLM's responsibility (see
review-loop.md); this module never computes a pass/fail verdict.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, TypedDict, Unpack, cast

from orchestune.review.markers import is_review_reply
from orchestune.review.progress_tracker import (
    CodexTrackerStatus,
    parse_codex_tracker_status,
)
from orchestune.review.tracker_activity import (
    is_codex_tracker,
    tracker_digest,
    tracker_is_current,
)

# Callers outside this module still read acquisition results as open JSON.
JsonObject = dict[str, Any]

SCHEMA_VERSION = 1
_CODEX_REVIEW_STATE_RE = re.compile(
    r"<!--\s*codex-review-state:v1\s+(\{.*?\})\s*-->", re.S
)
_CODEX_REVIEWED_COMMIT_RE = re.compile(
    r"\*\*Reviewed commit:\*\*\s*`([0-9a-fA-F]{7,40})`"
)
_FULL_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")

ACQUISITION_ACQUIRED = "acquired"
ACQUISITION_UNAVAILABLE = "unavailable"
ACQUISITION_IN_PROGRESS = "in_progress"

# Exit codes this module's results map to. Acquisition-loop control codes
# (internal error=2, round limit=12, timeout=20, stalled=21) are raised as
# exceptions by scripts/wait_for_review.py and are not produced here.
EXIT_ACQUIRED = 0
EXIT_IN_PROGRESS = 11
EXIT_NO_RESULT = 30


class _CommentUser(TypedDict, total=False):
    login: str


class ReviewItem(TypedDict, total=False):
    """GitHub comment, review, or inline comment fields this module reads."""

    id: int | str | None
    user: _CommentUser
    body: str | None
    created_at: str
    updated_at: str
    submitted_at: str
    state: str | None
    commit_id: str | None
    path: str
    line: int | str | None
    original_line: int | str | None
    diff_hunk: str
    side: str
    start_line: int | str | None
    start_side: str
    original_commit_id: str | None
    context: object
    pull_request_review_id: int | str | None
    position_line: int | str | None
    kind: str
    provenance: str
    provenance_reason: str | None


class _ActivityBounds(TypedDict, total=False):
    exclude_issue_comment_ids: set[int | str] | None
    round_started_at: str
    round_ended_at: str
    requested_head_sha: str | None
    activity_started_at: str
    activity_ended_at: str


class _ReviewContent(TypedDict):
    review_items: list[ReviewItem]
    review_body: str
    inline_comments: list[ReviewItem]
    timestamp: str


class ReviewResultRecord(TypedDict, total=False):
    """Canonical acquisition result, including optional wait metadata."""

    schema_version: int
    acquisition_status: str
    reason: str
    repository: str | None
    pr_number: int | None
    reviewer: str | None
    round: int | None
    trigger_id: int | str | None
    triggered_at: str | None
    requested_head_sha: str | None
    reviewed_head_sha: str | None
    current_head_sha: str | None
    review_target_sha: str | None
    review_target_sha_source: str | None
    review_items: list[ReviewItem]
    review_body: str
    inline_comments: list[ReviewItem]
    jev_evaluations: object
    completeness: object
    timestamp: str
    reason_code: str
    exit_code: int
    wait_diagnostics: object


ReviewState = dict[str, list[ReviewItem]]
_STATE_SECTIONS = ("issue_comments", "reviews", "inline_comments")
_INLINE_COPY_KEYS = (
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


def _tracker_item(item: ReviewItem) -> JsonObject:
    """Boundary for tracker helpers that still accept an open comment dict."""
    return cast(JsonObject, item)


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
        normalized[section] = [cast(ReviewItem, dict(item)) for item in items]
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
    items: list[ReviewItem],
    bot_name: str,
    exclude_ids: set[int | str] | None = None,
) -> list[ReviewItem]:
    excluded = exclude_ids or set()
    return [
        item
        for item in items
        if item.get("id") not in excluded
        and str(item.get("id")) not in excluded
        and _is_bot_user(str((item.get("user") or {}).get("login", "")), bot_name)
    ]


def _filter_bot_issue_comments(
    items: list[ReviewItem],
    bot_name: str,
    exclude_ids: set[int | str] | None = None,
    exclude_issue_comment_ids: set[int | str] | None = None,
) -> list[ReviewItem]:
    """Bot PR comments minus review replies, which are judgments, not review evidence.

    Only normal PR comments can declare a reply (first non-blank line marker);
    reviews and inline comments keep using `_filter_bot_items` (#1207).
    `exclude_issue_comment_ids` hides trigger comments without colliding with
    review or inline ids, which live in a different id namespace (#1210).
    """
    excluded = (exclude_ids or set()) | (exclude_issue_comment_ids or set())
    return [
        item
        for item in _filter_bot_items(items, bot_name, excluded)
        if not is_review_reply(item.get("body"))
    ]


def _get_item_timestamp(item: ReviewItem) -> str:
    return str(
        item.get("updated_at")
        or item.get("submitted_at")
        or item.get("created_at")
        or ""
    )


def _get_item_created_timestamp(item: ReviewItem) -> str:
    return str(item.get("submitted_at") or item.get("created_at") or "")


def _codex_review_commit_sha(item: ReviewItem) -> str | None:
    """Full commit SHA from Codex's completed review-state receipt, if present."""
    body = str(item.get("body") or "")
    match = _CODEX_REVIEW_STATE_RE.search(body)
    if not match:
        return None
    try:
        state = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    code_review = state.get("code_review") if isinstance(state, dict) else None
    commit = code_review.get("commit_sha") if isinstance(code_review, dict) else None
    status = code_review.get("status") if isinstance(code_review, dict) else None
    return (
        commit.lower()
        if status == "completed"
        and isinstance(commit, str)
        and _FULL_SHA_RE.fullmatch(commit)
        else None
    )


def _bind_codex_receipt_commits(
    result: _ReviewContent,
    comments: list[ReviewItem],
    round_started_at: str,
    round_ended_at: str,
) -> None:
    """Bind Codex receipt comments to matching completed full-SHA tracker data."""
    completed = [
        (item, sha)
        for item in comments
        if is_codex_tracker(_tracker_item(item))
        and _classify_round(
            _get_item_timestamp(item), round_started_at, round_ended_at
        )[0]
        == "current"
        and (sha := _codex_review_commit_sha(item)) is not None
    ]
    for receipt in result["review_items"]:
        if receipt["kind"] != "issue_comment" or receipt["provenance"] != "current":
            continue
        body = str(receipt.get("body") or "")
        if "<!-- codex-review-receipt:v1 -->" not in body:
            continue
        reviewed = _CODEX_REVIEWED_COMMIT_RE.search(body)
        if not reviewed:
            continue
        prefix = reviewed.group(1).lower()
        matching = {sha for _, sha in completed if sha.startswith(prefix)}
        if len(matching) == 1:
            receipt["commit_id"] = matching.pop()


def _activity_signature(item: ReviewItem, bot_name: str) -> str:
    """Change fingerprint of one comment / review: time, length, and for a Codex
    tracker a body digest, so a same-length Running -> Pending edit still shows.
    The body itself is never part of the signature."""
    signature = f"{_get_item_timestamp(item)}:{len(str(item.get('body') or ''))}"
    if bot_name.lower() == "codex" and is_codex_tracker(_tracker_item(item)):
        signature += f":{tracker_digest(_tracker_item(item))}"
    return signature


def _bot_candidate_items(
    data: ReviewState,
    bot_name: str,
    exclude_ids: set[int | str] | None = None,
    exclude_issue_comment_ids: set[int | str] | None = None,
) -> list[ReviewItem]:
    return [
        *_filter_bot_issue_comments(
            data["issue_comments"], bot_name, exclude_ids, exclude_issue_comment_ids
        ),
        *_filter_bot_items(data["reviews"], bot_name, exclude_ids),
    ]


def _is_finished_progress_tracker(item: ReviewItem, bot_name: str) -> bool:
    """Keep progress telemetry out of review content.

    The legacy name is retained because `scripts.review_verdict` re-exports it;
    Codex tracker comments are telemetry regardless of their current status.
    """
    body = str(item.get("body") or "").lower()
    return (
        body.startswith(f"**{bot_name.lower()} finished")
        and "view job" in body
        and "\n---" not in body
    ) or (
        bot_name.lower() == "codex"
        and parse_codex_tracker_status(str(item.get("body") or "")) is not None
    )


def _in_round(item: ReviewItem, round_started_at: str, round_ended_at: str) -> bool:
    """Created inside [start, end); an item without a creation time is never inside."""
    created = _get_item_created_timestamp(item)
    return (
        bool(created)
        and created >= round_started_at
        and (not round_ended_at or created < round_ended_at)
    )


def _in_activity_round(
    item: ReviewItem,
    bot_name: str,
    round_started_at: str,
    round_ended_at: str,
    requested_head_sha: str | None,
) -> bool:
    """Whether bot activity belongs to the round.

    A Codex tracker comment is reused and edited by every round, so only it is
    placed by its last activity time (`updated_at`) and its commit; every other
    item keeps the creation-time rule (#1210). `round_started_at` /
    `round_ended_at` are the activity bounds here (#1274).
    """
    if bot_name.lower() == "codex" and is_codex_tracker(_tracker_item(item)):
        return tracker_is_current(
            _tracker_item(item),
            started_at=round_started_at,
            ended_at=round_ended_at,
            requested_head_sha=requested_head_sha,
        )
    return _in_round(item, round_started_at, round_ended_at)


def _latest_bot_activity_item(
    data: ReviewState,
    bot_name: str,
    exclude_ids: set[int | str] | None = None,
    *,
    exclude_issue_comment_ids: set[int | str] | None = None,
    round_started_at: str = "",
    round_ended_at: str = "",
    requested_head_sha: str | None = None,
    activity_started_at: str = "",
    activity_ended_at: str = "",
) -> ReviewItem | None:
    """Latest bot activity; with round bounds only that round's own activity.

    A past tracker edited later (newer `updated_at`) belongs to its own round and
    must not make the evaluated round look still in progress (#1210). A reused
    Codex tracker updated inside the round is the round's own activity even
    though it was created earlier (#1274); `activity_*` carry the sub-second
    bounds for that comparison and default to the round bounds.
    """
    candidates = _bot_candidate_items(
        data, bot_name, exclude_ids, exclude_issue_comment_ids
    )
    if round_started_at or round_ended_at:
        candidates = [
            item
            for item in candidates
            if _in_activity_round(
                item,
                bot_name,
                activity_started_at or round_started_at,
                activity_ended_at or round_ended_at,
                requested_head_sha,
            )
        ]
    if bot_name.lower() == "codex":
        tracker_items = [
            item
            for item in candidates
            if parse_codex_tracker_status(str(item.get("body") or "")) is not None
        ]
        if tracker_items:
            latest_tracker = sorted(tracker_items, key=_get_item_timestamp)[-1]
            if (
                parse_codex_tracker_status(str(latest_tracker.get("body") or ""))
                is CodexTrackerStatus.IN_PROGRESS
            ):
                return latest_tracker
    return sorted(candidates, key=_get_item_timestamp)[-1] if candidates else None


def _latest_current_tracker(
    data: ReviewState,
    bot_name: str,
    exclude_ids: set[int | str] | None = None,
    *,
    exclude_issue_comment_ids: set[int | str] | None = None,
    round_started_at: str = "",
    round_ended_at: str = "",
    requested_head_sha: str | None = None,
) -> ReviewItem | None:
    """The Codex tracker that is current activity of the round, or None.

    Its status (Running / Completed / unknown) is telemetry about the round, not
    review content; callers must not treat it as an acquired review.
    """
    if bot_name.lower() != "codex":
        return None
    trackers = [
        item
        for item in _bot_candidate_items(
            data, bot_name, exclude_ids, exclude_issue_comment_ids
        )
        if is_codex_tracker(_tracker_item(item))
        and tracker_is_current(
            _tracker_item(item),
            started_at=round_started_at,
            ended_at=round_ended_at,
            requested_head_sha=requested_head_sha,
        )
    ]
    return sorted(trackers, key=_get_item_timestamp)[-1] if trackers else None


def _latest_bot_summary_item(
    data: ReviewState, bot_name: str, exclude_ids: set[int | str] | None = None
) -> ReviewItem | None:
    candidates = _bot_candidate_items(data, bot_name, exclude_ids)
    summaries = [
        item for item in candidates if not _is_finished_progress_tracker(item, bot_name)
    ]
    if (
        not summaries
        and bot_name.lower() == "codex"
        and any(
            parse_codex_tracker_status(str(item.get("body") or "")) is not None
            for item in candidates
        )
    ):
        return None
    return (
        sorted(summaries or candidates, key=_get_item_timestamp)[-1]
        if candidates
        else None
    )


def _is_explicitly_in_progress(item: ReviewItem) -> bool:
    body = str(item.get("body") or "")
    if parse_codex_tracker_status(body) is CodexTrackerStatus.IN_PROGRESS:
        return True
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
    for prefix, items in (
        (
            "comment",
            _filter_bot_issue_comments(data["issue_comments"], bot_name, exclude_ids),
        ),
        ("review", _filter_bot_items(data["reviews"], bot_name, exclude_ids)),
    ):
        for item in items:
            signature = (
                f"{_get_item_timestamp(item)}:{len(str(item.get('body') or ''))}"
            )
            if bot_name.lower() == "codex" and is_codex_tracker(_tracker_item(item)):
                # Same-length edits (Running -> Pending) must still register.
                signature += f":{tracker_digest(_tracker_item(item))}"
            snapshot[f"{prefix}_{item.get('id')}"] = signature
    for item in _filter_bot_items(data["inline_comments"], bot_name, exclude_ids):
        snapshot[f"inline_{item.get('id')}"] = _get_item_timestamp(item)
    return snapshot


def _normalize_review_item(
    item: ReviewItem, kind: str, provenance: str, reason: str | None = None
) -> ReviewItem:
    normalized: ReviewItem = {
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
    if reason:
        normalized["provenance_reason"] = reason
    return normalized


def _normalize_inline_item(
    item: ReviewItem, provenance: str, reason: str | None = None
) -> ReviewItem:
    raw = cast(Mapping[str, object], item)
    return cast(
        ReviewItem,
        {
            "path": item.get("path", "unknown"),
            "line": item.get("line") or item.get("original_line") or "N/A",
            "body": item.get("body") or "",
            **{key: raw[key] for key in _INLINE_COPY_KEYS if key in raw},
            "position_line": raw.get("position_line", item.get("line")),
            "provenance": provenance,
            **({"provenance_reason": reason} if reason else {}),
        },
    )


AFTER_ROUND_END = "after_round_end"


def _classify_round(
    created: str, round_started_at: str, round_ended_at: str = ""
) -> tuple[str, str | None]:
    """Attribute one item to the evaluated round, another round, or unknown timing.

    Without any boundary the caller knows only one round, so nothing is discarded
    and the item is `current`. With a closed interval `[start, end)`, an item that
    appeared at or after the end is `unassociated` (reason `after_round_end`)
    rather than promoted, and the boundary uses creation time, never `updated_at`.
    """
    if not round_started_at and not round_ended_at:
        return "current", None
    if not created:
        return "unassociated", None
    if round_started_at and created < round_started_at:
        return "historical", None
    if round_ended_at and created >= round_ended_at:
        return "unassociated", AFTER_ROUND_END
    return "current", None


def _classify_provenance(
    created: str, round_started_at: str, round_ended_at: str = ""
) -> str:
    return _classify_round(created, round_started_at, round_ended_at)[0]


def _classify_inline_round(
    item: ReviewItem,
    round_started_at: str,
    review_provenance_by_id: dict[int | str, tuple[str, str | None]],
    round_ended_at: str = "",
) -> tuple[str, str | None]:
    """Prefer the parent review's confirmed provenance over the comment's own
    timestamp when both are known: an inline comment's `pull_request_review_id`
    proves which review it belongs to, so a comment attached to a *historical*
    review must not be promoted to `current` just because it (or a later edit)
    carries a recent timestamp; a review id that is present but not found among
    this snapshot's review items cannot be confirmed either way (Codex PR #1114
    round 3 finding). A confirmed current parent does not rescue an inline
    created after the round ended (#1210).
    """
    created = _get_item_created_timestamp(item)
    review_id = item.get("pull_request_review_id")
    if review_id is None:
        return _classify_round(created, round_started_at, round_ended_at)
    provenance, reason = review_provenance_by_id.get(review_id, ("unassociated", None))
    if (
        provenance == "current"
        and round_ended_at
        and created
        and created >= round_ended_at
    ):
        return "unassociated", AFTER_ROUND_END
    return provenance, reason


def _classify_inline_provenance(
    item: ReviewItem,
    round_started_at: str,
    review_provenance_by_id: dict[int | str, str],
) -> str:
    return _classify_inline_round(
        item,
        round_started_at,
        {key: (value, None) for key, value in review_provenance_by_id.items()},
    )[0]


def _review_candidates(
    data: ReviewState,
    bot_name: str,
    exclude_ids: set[int | str] | None,
    exclude_issue_comment_ids: set[int | str] | None = None,
) -> list[tuple[ReviewItem, str]]:
    """Bot PR comments (replies excluded) and formal reviews, tagged with their kind."""
    return [
        *(
            (item, "issue_comment")
            for item in _filter_bot_issue_comments(
                data["issue_comments"], bot_name, exclude_ids, exclude_issue_comment_ids
            )
        ),
        *(
            (item, "review")
            for item in _filter_bot_items(data["reviews"], bot_name, exclude_ids)
        ),
    ]


def _normalize_review_items(
    candidates: list[tuple[ReviewItem, str]],
    bot_name: str,
    round_started_at: str,
    round_ended_at: str,
) -> list[ReviewItem]:
    """Review-content items only: execution telemetry -- a "job finished" tracker
    or a body still explicitly reporting in-progress -- is not review content,
    even when it is the most recent item in the round (Codex PR #1114 round 3
    finding: an older in-progress body left over once a "finished" tracker
    arrives with no summary must not be read as a completed review)."""
    return [
        _normalize_review_item(
            item,
            kind,
            *_classify_round(
                _get_item_created_timestamp(item), round_started_at, round_ended_at
            ),
        )
        for item, kind in sorted(
            candidates, key=lambda pair: _get_item_timestamp(pair[0])
        )
        if not _is_finished_progress_tracker(item, bot_name)
        and not _is_explicitly_in_progress(item)
    ]


def extract_review_result(
    data: ReviewState,
    bot_name: str,
    exclude_ids: set[int | str] | None = None,
    round_started_at: str = "",
    *,
    round_ended_at: str = "",
    exclude_issue_comment_ids: set[int | str] | None = None,
) -> JsonObject | None:
    """Collect every bot review item and inline comment, tagged with round provenance.

    Unlike the previous single-latest-item extraction, this returns *all* bot
    `issue_comments`/`reviews` and *all* inline comments the bot has posted, each
    tagged `current` (inside `[round_started_at, round_ended_at)`), `historical`
    (before it), or `unassociated` (no usable timestamp, or after the round end
    with `provenance_reason=after_round_end`) — nothing is discarded. Returns None
    only when the bot has posted no activity of any kind.
    """
    review_candidates = _review_candidates(
        data, bot_name, exclude_ids, exclude_issue_comment_ids
    )
    inline_candidates = _filter_bot_items(
        data["inline_comments"], bot_name, exclude_ids
    )
    review_items = _normalize_review_items(
        review_candidates, bot_name, round_started_at, round_ended_at
    )
    # Only "review" items (not issue_comments) share an id namespace with
    # inline comments' `pull_request_review_id`.
    review_provenance_by_id = {
        item["id"]: (item["provenance"], item.get("provenance_reason"))
        for item in review_items
        if item["kind"] == "review" and item["id"] is not None
    }
    inline_items = [
        _normalize_inline_item(
            item,
            *_classify_inline_round(
                item, round_started_at, review_provenance_by_id, round_ended_at
            ),
        )
        for item in sorted(inline_candidates, key=_get_item_timestamp)
    ]

    timestamps = [_get_item_timestamp(item) for item, _ in review_candidates] + [
        _get_item_timestamp(item) for item in inline_candidates
    ]
    result = _review_content_result(review_items, inline_items, timestamps)
    if result is not None and bot_name.lower() == "codex":
        _bind_codex_receipt_commits(
            result, data["issue_comments"], round_started_at, round_ended_at
        )
    return cast(JsonObject | None, result)


def _review_content_result(
    review_items: list[ReviewItem],
    inline_items: list[ReviewItem],
    timestamps: list[str],
) -> _ReviewContent | None:
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


def _status_result(
    status: str, reason: str, result: _ReviewContent | None, timestamp: str
) -> JsonObject:
    """A non-acquired result; any content already seen is kept for context."""
    return cast(
        JsonObject,
        {
            "schema_version": SCHEMA_VERSION,
            "acquisition_status": status,
            "reason": reason,
            "review_items": result["review_items"] if result else [],
            "review_body": result["review_body"] if result else "",
            "inline_comments": result["inline_comments"] if result else [],
            "timestamp": timestamp,
        },
    )


def _acquired_result(result: _ReviewContent) -> JsonObject:
    return cast(
        JsonObject,
        {
            "schema_version": SCHEMA_VERSION,
            "acquisition_status": ACQUISITION_ACQUIRED,
            "reason": "",
            **result,
        },
    )


def _in_progress_result(
    data: ReviewState,
    bot_name: str,
    result: _ReviewContent | None,
    exclude_ids: set[int | str] | None,
    **bounds: Unpack[_ActivityBounds],
) -> JsonObject | None:
    """An in-progress status when the round's latest bot activity says so.

    A single snapshot (no polling loop available to its callers) whose latest
    activity explicitly reports still-in-progress must not be treated as a final
    acquired-or-unavailable result (issue #1099 Exit 11).
    """
    latest = _latest_bot_activity_item(data, bot_name, exclude_ids, **bounds)
    if latest is None or not _is_explicitly_in_progress(latest):
        return None
    return _status_result(
        ACQUISITION_IN_PROGRESS,
        f"@{bot_name} activity is explicitly still in progress",
        result,
        result["timestamp"] if result else _get_item_timestamp(latest),
    )


def has_reviewer_activity(
    value: object,
    bot_name: str,
    *,
    exclude_issue_comment_ids: set[int | str] | None = None,
    round_started_at: str = "",
    round_ended_at: str = "",
    requested_head_sha: str | None = None,
    activity_started_at: str = "",
    activity_ended_at: str = "",
) -> bool:
    """Whether the bot did anything in the round, review content or not.

    A lone "job finished" or tracker comment is no review content (the state is
    `unavailable`) but proves the reviewer saw the request (#1436).
    """
    return (
        _latest_bot_activity_item(
            normalize_review_state(value),
            bot_name,
            exclude_issue_comment_ids=exclude_issue_comment_ids,
            round_started_at=round_started_at,
            round_ended_at=round_ended_at,
            requested_head_sha=requested_head_sha,
            activity_started_at=activity_started_at,
            activity_ended_at=activity_ended_at,
        )
        is not None
    )


def collect_review_state(
    value: object,
    bot_name: str = "claude",
    *,
    exclude_ids: set[int | str] | None = None,
    round_started_at: str = "",
    round_ended_at: str = "",
    exclude_issue_comment_ids: set[int | str] | None = None,
    requested_head_sha: str | None = None,
    activity_started_at: str = "",
    activity_ended_at: str = "",
) -> JsonObject:
    """Assemble an acquisition-state result from an externally acquired snapshot.

    Used by the offline `--review-state-file` path and by completion. Online
    adapters (`scripts/wait_for_review.py`) build the equivalent result while
    additionally tracking round/trigger/SHA identifiers this transport-neutral
    entry point does not have. With round bounds, both the content and the
    in-progress check are limited to that round's interval; a reused Codex
    tracker counts when updated inside it (#1274, see `_in_activity_round`).
    """
    data = normalize_review_state(value)
    content = cast(
        _ReviewContent | None,
        extract_review_result(
            data,
            bot_name,
            exclude_ids,
            round_started_at,
            round_ended_at=round_ended_at,
            exclude_issue_comment_ids=exclude_issue_comment_ids,
        ),
    )
    in_progress = _in_progress_result(
        data,
        bot_name,
        content,
        exclude_ids,
        exclude_issue_comment_ids=exclude_issue_comment_ids,
        round_started_at=round_started_at,
        round_ended_at=round_ended_at,
        requested_head_sha=requested_head_sha,
        activity_started_at=activity_started_at,
        activity_ended_at=activity_ended_at,
    )
    if in_progress is not None:
        return in_progress
    if content is None:
        return _status_result(
            ACQUISITION_UNAVAILABLE,
            f"no @{bot_name} activity found in the supplied review state",
            None,
            "",
        )
    return _acquired_result(content)


_RESULT_KEYS = (
    "schema_version",
    "acquisition_status",
    "reason",
    "repository",
    "pr_number",
    "reviewer",
    "round",
    "trigger_id",
    "triggered_at",
    "requested_head_sha",
    "reviewed_head_sha",
    "current_head_sha",
    "review_target_sha",
    "review_target_sha_source",
    "review_items",
    "review_body",
    "inline_comments",
    "jev_evaluations",
    "completeness",
    "timestamp",
)
_AFTER_REASON = ("reason_code", "exit_code")
_TRAILING_RESULT_KEYS = ("wait_diagnostics",)
_LIST_RESULT_KEYS = ("review_items", "inline_comments", "jev_evaluations")


def reviewed_head_sha(review_items: list[ReviewItem]) -> str | None:
    """The current review commit when every current review names the same one."""
    shas = {
        item.get("commit_id")
        for item in review_items
        if item.get("kind") == "review"
        and item.get("provenance") == "current"
        and item.get("commit_id")
    }
    if len(shas) == 1:
        return next(iter(shas))
    return None


def _result_default(key: str) -> object:
    if key == "schema_version":
        return SCHEMA_VERSION
    if key == "acquisition_status":
        return ACQUISITION_UNAVAILABLE
    if key == "reason" or key == "review_body":
        return ""
    if key == "review_target_sha_source":
        return "unknown"
    if key in _LIST_RESULT_KEYS:
        return []
    return None


def review_result_record(**fields: Unpack[ReviewResultRecord]) -> JsonObject:
    """Canonical wait result: every key in schema order, unset ones at defaults.

    `completeness` is required. `reason_code` and `exit_code` are inserted after
    `reason` only when supplied. `wait_diagnostics` is the only trailing key.
    """
    supplied = cast(dict[str, object], fields)
    allowed = set(_RESULT_KEYS) | set(_AFTER_REASON) | set(_TRAILING_RESULT_KEYS)
    unknown = sorted(set(supplied) - allowed)
    if unknown:
        raise ValueError(f"unknown review result field: {', '.join(unknown)}")
    if "completeness" not in supplied:
        raise ValueError("completeness is required")
    record: dict[str, object] = {
        key: supplied[key] if key in supplied else _result_default(key)
        for key in _RESULT_KEYS
    }
    extras = [key for key in _AFTER_REASON if key in supplied]
    if extras:
        ordered: dict[str, object] = {}
        for key, value in record.items():
            ordered[key] = value
            if key == "reason":
                for extra in extras:
                    ordered[extra] = supplied[extra]
        record = ordered
    if "wait_diagnostics" in supplied:
        record["wait_diagnostics"] = supplied["wait_diagnostics"]
    return cast(JsonObject, record)

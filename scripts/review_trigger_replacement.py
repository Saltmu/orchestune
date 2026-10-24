"""Publish explicit replacement edges once, verify them, then retire old markers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orchestune.infra.json_state import write_json_atomic
from orchestune.infra.process_utils import FileLock
from orchestune.review.markers import superseded_trigger_marker
from orchestune.review.rounds import (
    ReviewTrigger,
    build_restorable_trigger_body,
    effective_lines,
    parse_trigger,
    restore_triggers,
    trigger_history,
    validate_replacement_target,
)
from orchestune.review.snapshot import EvidenceContractError

Comments = Callable[[], list[dict[str, Any]]]
RunGh = Callable[[list[str]], str]


def _read_comment(run_gh: RunGh, comment_id: object) -> dict[str, Any]:
    value = json.loads(
        run_gh(["api", f"repos/{{owner}}/{{repo}}/issues/comments/{comment_id}"])
    )
    if not isinstance(value, dict) or str(value.get("id")) != str(comment_id):
        raise EvidenceContractError("comment re-fetch did not confirm its id")
    return value


def retire_replaced_triggers(
    comments: list[dict[str, Any]], current_id: object, run_gh: RunGh
) -> None:
    """Retry cleanup without another POST. Fetch each old body before editing it."""
    restore_triggers(comments)
    records = {str(t.id): t for t in trigger_history(comments)}
    current = records[str(current_id)]
    while current.replaces is not None:
        old = records[current.replaces]
        item = _read_comment(run_gh, old.id)
        parsed = parse_trigger(item)
        if parsed is None or (
            parsed.reviewer,
            parsed.round,
            parsed.requested_head_sha,
            parsed.author,
        ) != (old.reviewer, old.round, old.requested_head_sha, old.author):
            raise EvidenceContractError(
                "replacement source metadata changed before cleanup"
            )
        if parsed.active:
            body = _supersede_body(parsed.body, old.reviewer)
            try:
                run_gh(
                    [
                        "api",
                        f"repos/{{owner}}/{{repo}}/issues/comments/{old.id}",
                        "-X",
                        "PATCH",
                        "-f",
                        f"body={body}",
                    ]
                )
            except Exception as error:
                raise EvidenceContractError(
                    f"replacement {current_id} verified; retry marker cleanup, never re-post"
                ) from error
        current = old


def _supersede_body(body: str, bot: str) -> str:
    lines = body.splitlines(keepends=True)
    prefix = ""
    count = 0
    for index, line in enumerate(lines):
        prefix += line
        effective = effective_lines(prefix)
        if (
            len(effective) > count
            and effective[-1].startswith("<!--")
            and "orchestune:review-trigger" in effective[-1].lower()
        ):
            ending = line[len(line.rstrip("\r\n")) :]
            lines[index] = superseded_trigger_marker(bot) + ending
        count = len(effective)
    return "".join(lines)


def _confirmed_candidate(
    comments: list[dict[str, Any]], body: str, author: str
) -> dict[str, Any] | None:
    matches = [
        i
        for i in comments
        if i.get("body") == body
        and str((i.get("user") or {}).get("login") or "").lower() == author
    ]
    if len(matches) > 1:
        raise EvidenceContractError("multiple matching replacement publications")
    restore_triggers(comments)
    return matches[0] if matches else None


def _post_once(
    pr_number: int,
    body: str,
    author: str,
    run_gh: RunGh,
    fetch_comments: Comments,
    journal: Path,
) -> dict[str, Any]:
    if journal.exists():
        existing = _confirmed_candidate(fetch_comments(), body, author)
        if existing is None:
            raise EvidenceContractError(
                "unconfirmed replacement POST; re-fetch or repair manually, never resend"
            )
        return existing
    write_json_atomic(
        journal,
        {"state": "posting", "body_digest": hashlib.sha256(body.encode()).hexdigest()},
    )
    try:
        response = json.loads(
            run_gh(
                [
                    "api",
                    f"repos/{{owner}}/{{repo}}/issues/{pr_number}/comments",
                    "-f",
                    f"body={body}",
                ]
            )
        )
    except Exception:
        response = None
    comments = fetch_comments()
    candidate = _confirmed_candidate(comments, body, author)
    if candidate is None:
        raise EvidenceContractError(
            "unconfirmed replacement POST; re-fetch before any further action"
        )
    if response is not None and str(response.get("id")) != str(candidate["id"]):
        raise EvidenceContractError("POST response differs from re-fetched replacement")
    return candidate


def publish_replacement(
    *,
    pr_number: int,
    previous: dict[str, Any],
    bot_name: str,
    head_sha: str,
    reply_body: str,
    fetch_comments: Comments,
    fetch_head: Callable[[], str | None],
    run_gh: RunGh,
    journal_dir: Path | None = None,
) -> dict[str, Any]:
    """The caller supplies fully paginated comments and fresh PR HEAD observations."""
    old = parse_trigger(previous)
    if old is None or old.requested_head_sha is None:
        raise EvidenceContractError(
            "replacement source needs a trigger and HEAD marker"
        )
    body = build_restorable_trigger_body(
        reply_body, bot_name, old.round, head_sha, replaces=old.id
    )
    directory = journal_dir or Path(".orchestune/ci/review-replacements")
    directory.mkdir(parents=True, exist_ok=True)
    journal = directory / f"{pr_number}-{old.id}-{bot_name}-{head_sha}.json"
    with FileLock(journal.with_suffix(".lock"), timeout=0):
        return _publish_locked(
            pr_number, old, body, head_sha, fetch_comments, fetch_head, run_gh, journal
        )


def _publish_locked(
    pr_number: int,
    old: ReviewTrigger,
    body: str,
    head_sha: str,
    fetch_comments: Comments,
    fetch_head: Callable[[], str | None],
    run_gh: RunGh,
    journal: Path,
) -> dict[str, Any]:
    comments = fetch_comments()
    triggers = restore_triggers(comments)
    same_round = [t for t in triggers if t.round == old.round]
    if not same_round:
        raise EvidenceContractError("replacement source round is no longer active")
    candidate = _confirmed_candidate(comments, body, old.author or "")
    if candidate is None:
        if str(same_round[0].id) != str(old.id) or same_round[0].body != old.body:
            raise EvidenceContractError(
                "replacement source changed; reload the request"
            )
        proposed = parse_trigger(
            {"id": "candidate", "body": body, "created_at": old.created_at}
        )
        assert proposed is not None
        validate_replacement_target(
            trigger_history(comments), old.id, proposed.reviewer, head_sha
        )
        if old.round != max(t.round for t in trigger_history(comments)):
            raise EvidenceContractError("historical rounds cannot be replaced")
        if fetch_head() != head_sha:
            raise EvidenceContractError(
                "PR HEAD changed during replacement preparation"
            )
        identity = json.loads(run_gh(["api", "user"]))
        if not old.author or str(identity.get("login") or "").lower() != old.author:
            raise EvidenceContractError(
                "replacement must be posted by the source author"
            )
        candidate = _post_once(
            pr_number, body, old.author, run_gh, fetch_comments, journal
        )
    verified = _read_comment(run_gh, candidate["id"])
    if verified.get("body") != body or parse_trigger(verified) != parse_trigger(
        candidate
    ):
        raise EvidenceContractError("replacement body/id was not verified by re-fetch")
    comments = fetch_comments()
    if not any(str(t.id) == str(verified["id"]) for t in restore_triggers(comments)):
        raise EvidenceContractError("replacement is not the unique active tail")
    write_json_atomic(journal, {"state": "verified", "trigger_id": verified["id"]})
    retire_replaced_triggers(comments, verified["id"], run_gh)
    return verified

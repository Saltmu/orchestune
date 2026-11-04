"""Publish explicit replacement edges once, verify them, then retire old markers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestune.infra.json_state import write_json_atomic
from orchestune.infra.process_utils import FileLock
from orchestune.review.markers import parse_head_marker, superseded_trigger_marker
from orchestune.review.rounds import (
    ReviewTrigger,
    build_restorable_trigger_body,
    effective_lines,
    parse_trigger,
    restore_triggers,
    trigger_comment_ids,
    trigger_history,
    validate_replacement_target,
)
from orchestune.review.snapshot import EvidenceContractError

Comments = Callable[[], list[dict[str, Any]]]
RunGh = Callable[[list[str]], str]


@dataclass(frozen=True)
class TriggerOps:
    """GitHub operations supplied by `scripts.wait_for_review` at call time."""

    fetch_head: Callable[[], str | None]
    fetch_comments: Comments
    run_gh: RunGh
    publish: Callable[..., dict[str, Any]]
    post_trigger: Callable[..., dict[str, Any]]
    retire: Callable[..., None]


def _read_trigger_body(body: str | None, body_file: str | None) -> str:
    raw = body or ""
    if body_file:
        with open(body_file, encoding="utf-8") as stream:
            raw = stream.read()
    return raw


def _replace_trigger(
    pr_number: int,
    existing_trigger: dict[str, Any],
    bot_name: str,
    requested_head_sha: str,
    body: str | None,
    body_file: str | None,
    excluded_ids: set[int | str],
    ops: TriggerOps,
) -> tuple[str, int | str | None, str]:
    trigger_info = ops.publish(
        pr_number=pr_number,
        previous=existing_trigger,
        bot_name=bot_name,
        head_sha=requested_head_sha,
        reply_body=_read_trigger_body(body, body_file),
        fetch_comments=ops.fetch_comments,
        fetch_head=ops.fetch_head,
        run_gh=ops.run_gh,
    )
    excluded_ids.add(trigger_info["id"])
    return str(trigger_info["created_at"]), trigger_info["id"], requested_head_sha


def _reuse_or_replace_trigger(  # noqa: PLR0913
    pr_number: int,
    bot_name: str,
    comments: list[dict[str, Any]],
    existing_trigger: dict[str, Any],
    active: list[ReviewTrigger],
    excluded_ids: set[int | str],
    current_round: int,
    requested_head_sha: str | None,
    body: str | None,
    body_file: str | None,
    repair_trigger_markers: bool,
    ops: TriggerOps,
) -> tuple[str, int | str | None, str | None]:
    trigger_id = existing_trigger.get("id")
    trigger_time = str(existing_trigger.get("created_at") or "")
    existing_head = parse_head_marker(existing_trigger.get("body") or "")
    if trigger_id is not None:
        excluded_ids.add(trigger_id)
    replacing = active[0].reviewer != bot_name or (
        existing_head and requested_head_sha and existing_head != requested_head_sha
    )
    if repair_trigger_markers and replacing:
        raise ValueError(
            "marker repair cannot replace a reviewer or HEAD; resume the verified trigger"
        )
    if not replacing:
        if repair_trigger_markers and active[0].replaces is not None:
            ops.retire(comments, trigger_id, ops.run_gh)
        print(
            f"Review trigger for @{bot_name} (Round {current_round}) already exists "
            f"(Comment ID: {trigger_id}); skipping post and waiting..."
        )
        return trigger_time, trigger_id, existing_head
    if requested_head_sha is None:
        raise ValueError("cannot replace trigger without current PR HEAD")
    return _replace_trigger(
        pr_number,
        existing_trigger,
        bot_name,
        requested_head_sha,
        body,
        body_file,
        excluded_ids,
        ops,
    )


def _post_new_trigger(  # noqa: PLR0913
    pr_number: int,
    bot_name: str,
    initial_snapshot: dict[str, str],
    excluded_ids: set[int | str],
    current_round: int,
    max_rounds: int,
    body: str | None,
    body_file: str | None,
    requested_head_sha: str | None,
    ops: TriggerOps,
) -> tuple[str, int | str | None, str | None]:
    print(
        f"Posting review trigger comment (Round {current_round}/{max_rounds}) "
        f"for @{bot_name} on PR #{pr_number}..."
    )
    trigger_info = ops.post_trigger(
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


def _existing_round_trigger(
    comments: list[dict[str, Any]],
    excluded_ids: set[int | str],
    current_round: int,
    repair_trigger_markers: bool,
) -> tuple[list[ReviewTrigger], dict[str, Any] | None]:
    excluded_ids.update(trigger_comment_ids(comments))
    active = [
        item for item in restore_triggers(comments) if item.round == current_round
    ]
    existing_trigger = next(
        (
            item
            for item in comments
            if active and str(item.get("id")) == str(active[0].id)
        ),
        None,
    )
    if repair_trigger_markers and existing_trigger is None:
        raise ValueError("marker repair requires an existing replacement trigger")
    return active, existing_trigger


def resolve_round_trigger(  # noqa: PLR0913
    pr_number: int,
    bot_name: str,
    initial_data: dict[str, list[dict[str, Any]]],
    initial_snapshot: dict[str, str],
    excluded_ids: set[int | str],
    current_round: int,
    max_rounds: int,
    body: str | None,
    body_file: str | None,
    repair_trigger_markers: bool,
    ops: TriggerOps,
) -> tuple[str, int | str | None, str | None]:
    """Post a head-bound trigger, or restore the recorded head when one exists."""
    requested_head_sha = ops.fetch_head()
    comments = initial_data.get("issue_comments", [])
    active, existing_trigger = _existing_round_trigger(
        comments, excluded_ids, current_round, repair_trigger_markers
    )
    if existing_trigger is not None:
        return _reuse_or_replace_trigger(
            pr_number,
            bot_name,
            comments,
            existing_trigger,
            active,
            excluded_ids,
            current_round,
            requested_head_sha,
            body,
            body_file,
            repair_trigger_markers,
            ops,
        )
    return _post_new_trigger(
        pr_number,
        bot_name,
        initial_snapshot,
        excluded_ids,
        current_round,
        max_rounds,
        body,
        body_file,
        requested_head_sha,
        ops,
    )


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


def _check_replacement_allowed(
    comments: list[dict[str, Any]],
    same_round: list[ReviewTrigger],
    old: ReviewTrigger,
    body: str,
    head_sha: str,
    fetch_head: Callable[[], str | None],
    run_gh: RunGh,
) -> None:
    if str(same_round[0].id) != str(old.id) or same_round[0].body != old.body:
        raise EvidenceContractError("replacement source changed; reload the request")
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
        raise EvidenceContractError("PR HEAD changed during replacement preparation")
    identity = json.loads(run_gh(["api", "user"]))
    if not old.author or str(identity.get("login") or "").lower() != old.author:
        raise EvidenceContractError("replacement must be posted by the source author")


def _verify_published(
    candidate: dict[str, Any],
    body: str,
    run_gh: RunGh,
    fetch_comments: Comments,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    verified = _read_comment(run_gh, candidate["id"])
    if verified.get("body") != body or parse_trigger(verified) != parse_trigger(
        candidate
    ):
        raise EvidenceContractError("replacement body/id was not verified by re-fetch")
    comments = fetch_comments()
    if not any(str(t.id) == str(verified["id"]) for t in restore_triggers(comments)):
        raise EvidenceContractError("replacement is not the unique active tail")
    return verified, comments


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
        _check_replacement_allowed(
            comments, same_round, old, body, head_sha, fetch_head, run_gh
        )
        candidate = _post_once(
            pr_number, body, old.author or "", run_gh, fetch_comments, journal
        )
    verified, comments = _verify_published(candidate, body, run_gh, fetch_comments)
    write_json_atomic(journal, {"state": "verified", "trigger_id": verified["id"]})
    retire_replaced_triggers(comments, verified["id"], run_gh)
    return verified

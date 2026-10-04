"""Single-snapshot MCP/offline review evaluation and pre-post validation.

Pure orchestration: no network, subprocess or filesystem access. The CLI adapter
reads files, injects the clock and adds advisory Jev output. A result states what
was acquired and which evidence proves the round and head; it never decides pass or
fail, and the offline JSON never replaces completion's own fresh verification.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from orchestune.review.acquisition import (
    ACQUISITION_ACQUIRED,
    ACQUISITION_IN_PROGRESS,
    ACQUISITION_UNAVAILABLE,
    EXIT_ACQUIRED,
    EXIT_IN_PROGRESS,
    EXIT_NO_RESULT,
    SCHEMA_VERSION,
    collect_review_state,
)
from orchestune.review.judgment import (
    PreviousRoundReply,
    parse_judgments,
    validate_previous_round_reply,
)
from orchestune.review.markers import build_trigger_body, derive_review_target
from orchestune.review.rounds import (
    ReviewRoundContext,
    ReviewTrigger,
    plan_next_round,
    previous_round_window,
    restore_triggers,
    select_round,
    trigger_comment_ids,
)
from orchestune.review.snapshot import (
    DEFAULT_MAX_SNAPSHOT_AGE_SECONDS,
    SECTIONS,
    EvidenceContractError,
    InsufficientEvidenceError,
    SnapshotEvidence,
    validate_snapshot,
)

RECEIPT_VERSION = 1
LEGACY_WARNING = (
    "legacy snapshot (no snapshot_version): round, trigger and head are unverified; "
    "'current' is a compatibility label, not proof of the target round"
)
_EXIT_BY_STATUS = {
    ACQUISITION_ACQUIRED: EXIT_ACQUIRED,
    ACQUISITION_IN_PROGRESS: EXIT_IN_PROGRESS,
    ACQUISITION_UNAVAILABLE: EXIT_NO_RESULT,
}


@dataclass(frozen=True)
class OfflineOutcome:
    exit_code: int
    payload: dict[str, Any]


def _unavailable(
    bot_name: str, pr_number: int | None, reason: str, repository: object = None
) -> dict[str, Any]:
    """An explicit no-result payload: nothing is filled in from partial evidence."""
    return {
        "schema_version": SCHEMA_VERSION,
        "acquisition_status": ACQUISITION_UNAVAILABLE,
        "reason": reason,
        "repository": repository if isinstance(repository, str) else None,
        "pr_number": pr_number,
        "reviewer": bot_name,
        "round": None,
        "trigger_id": None,
        "triggered_at": "",
        "requested_head_sha": None,
        "reviewed_head_sha": None,
        "current_head_sha": None,
        "review_target_sha": None,
        "review_target_sha_source": "unknown",
        "review_items": [],
        "review_body": "",
        "inline_comments": [],
        "completeness": dict.fromkeys(SECTIONS, "unknown"),
        "timestamp": "",
        "evidence_warnings": [],
    }


def _rejected_receipt(
    reason: str, bot_name: str, pr_number: int | None
) -> dict[str, Any]:
    """Overwrites any stale receipt so a failed check can never be posted."""
    return {
        "receipt_version": RECEIPT_VERSION,
        "operation": "validate_request",
        "validation_status": "insufficient_evidence",
        "reason": reason,
        "reviewer": bot_name,
        "pr_number": pr_number,
    }


def _head_warnings(
    context: ReviewRoundContext, current: str, target: str | None
) -> list[str]:
    warnings: list[str] = []
    if context.requested_head_sha is None:
        warnings.append(
            "requested_head_sha unknown: the trigger carries no head marker"
        )
    elif context.requested_head_sha != current:
        warnings.append(
            "head changed since the trigger: requested_head_sha differs from "
            "current_head_sha"
        )
    if target is not None and target != current:
        warnings.append(
            "review target differs from current_head_sha: re-review before done"
        )
    return warnings


def _identify(
    result: dict[str, Any],
    evidence: SnapshotEvidence,
    context: ReviewRoundContext,
    reply: dict[str, Any] | None,
) -> dict[str, Any]:
    """Attach the proven identity and SHAs; facts are kept even when they disagree."""
    target, source = derive_review_target(
        result["review_items"], context.requested_head_sha, evidence.head_sha
    )
    warnings = _head_warnings(context, evidence.head_sha, target)
    if not context.is_latest:
        warnings.append(
            "historical round: context only, never the latest round for completion"
        )
    result.update(
        repository=evidence.repository,
        pr_number=evidence.pr_number,
        reviewer=context.reviewer,
        round=context.round,
        trigger_id=context.trigger_id,
        triggered_at=context.started_at,
        requested_head_sha=context.requested_head_sha,
        reviewed_head_sha=target if source == "review_commit" else None,
        current_head_sha=evidence.head_sha,
        review_target_sha=target,
        review_target_sha_source=source,
        completeness=evidence.completeness,
        snapshot_version=1,
        snapshot_observed_at=evidence.observed_at,
        round_ended_at=context.ended_at,
        is_latest_round=context.is_latest,
        evidence_warnings=warnings,
        reply_validation=reply,
    )
    return result


def _check_reply(
    state: Mapping[str, Any], window: Any, body: str, label: str
) -> PreviousRoundReply:
    try:
        return validate_previous_round_reply(state, window, body)
    except ValueError as error:
        raise EvidenceContractError(f"{label} invalid: {error}") from error


def _verify_posted_reply(
    state: Mapping[str, Any],
    triggers: list[ReviewTrigger],
    context: ReviewRoundContext,
    body_text: str | None,
) -> dict[str, Any] | None:
    """Round 2+ is validated from the trigger body that was actually posted."""
    if context.round < 2:
        if body_text is not None:
            raise EvidenceContractError(
                "--body-file only applies to round 2+ evaluation; round 1 has no table"
            )
        return None
    trigger = next(t for t in triggers if t.round == context.round)
    window = previous_round_window(state["issue_comments"], context.round, strict=True)
    label = f"posted round {context.round} trigger judgments"
    posted = _check_reply(state, window, trigger.body, label)
    if body_text is not None:
        given = _check_reply(
            state, window, body_text, f"--body-file for round {context.round}"
        )
        if (given.previous_round, given.findings) != (
            posted.previous_round,
            posted.findings,
        ):
            raise EvidenceContractError(
                "the --body-file table differs from the posted trigger table"
            )
    return {
        "validated": True,
        "source": "trigger_body",
        "previous_round": posted.previous_round,
        "previous_reviewer": posted.reviewer,
        "source_digest": posted.source_digest,
        "judgment_digest": posted.judgment_digest,
    }


def _evaluate(
    value: object,
    *,
    bot_name: str,
    pr_number: int | None,
    now: datetime,
    requested_round: int | None,
    max_rounds: int,
    max_age_seconds: float,
    body_text: str | None,
) -> OfflineOutcome:
    evidence = validate_snapshot(
        value, pr_number=pr_number, now=now, max_age_seconds=max_age_seconds
    )
    comments = evidence.state["issue_comments"]
    triggers = restore_triggers(comments)
    context = select_round(
        triggers,
        repository=evidence.repository,
        pr_number=evidence.pr_number,
        requested_round=requested_round,
        max_rounds=max_rounds,
    )
    if bot_name != context.reviewer:
        raise EvidenceContractError(
            f"round {context.round} was requested from {context.reviewer}; the "
            f"reviewer cannot change when evaluating (--bot-name {bot_name})"
        )
    if evidence.observed_at < context.started_at:
        raise EvidenceContractError(
            "snapshot was observed before the trigger was posted"
        )
    reply = _verify_posted_reply(evidence.state, triggers, context, body_text)
    result = collect_review_state(
        evidence.state,
        bot_name,
        exclude_issue_comment_ids=trigger_comment_ids(comments),
        round_started_at=context.started_at,
        round_ended_at=context.ended_at or "",
    )
    result = _identify(result, evidence, context, reply)
    return OfflineOutcome(_EXIT_BY_STATUS[result["acquisition_status"]], result)


def evaluate_snapshot(
    value: object,
    *,
    bot_name: str,
    pr_number: int | None,
    now: datetime,
    requested_round: int | None = None,
    max_rounds: int = 5,
    max_age_seconds: float = DEFAULT_MAX_SNAPSHOT_AGE_SECONDS,
    body_text: str | None = None,
) -> OfflineOutcome:
    """Evaluate one posted round of a v1 snapshot; posting and polling never occur.

    Contract violations raise `EvidenceContractError` (Exit 2) and an exceeded
    round limit raises `RoundLimitError` (Exit 12); missing, partial or stale
    evidence is an Exit 30 outcome that carries no review content.
    """
    try:
        return _evaluate(
            value,
            bot_name=bot_name,
            pr_number=pr_number,
            now=now,
            requested_round=requested_round,
            max_rounds=max_rounds,
            max_age_seconds=max_age_seconds,
            body_text=body_text,
        )
    except InsufficientEvidenceError as error:
        repository = value.get("repository") if isinstance(value, Mapping) else None
        payload = _unavailable(bot_name, pr_number, str(error), repository)
        return OfflineOutcome(EXIT_NO_RESULT, payload)


def _judged_round(body_text: str) -> int | None:
    try:
        return int(parse_judgments(body_text)["round"])
    except ValueError:
        return None


def _outstanding(evidence: SnapshotEvidence, latest: ReviewTrigger) -> bool:
    """True when the latest posted round has no acquired result yet."""
    result = collect_review_state(
        evidence.state,
        latest.reviewer,
        exclude_issue_comment_ids=trigger_comment_ids(evidence.state["issue_comments"]),
        round_started_at=latest.created_at,
    )
    return bool(result["acquisition_status"] != ACQUISITION_ACQUIRED)


def _already_posted(
    evidence: SnapshotEvidence,
    triggers: list[ReviewTrigger],
    explicit_round: int | None,
    body_text: str | None,
) -> ReviewTrigger | None:
    """The posted trigger this request would duplicate, if there is one."""
    by_round = {trigger.round: trigger for trigger in triggers}
    if explicit_round is not None:
        return by_round.get(explicit_round)
    if body_text is not None:
        judged = _judged_round(body_text)
        return by_round.get(judged + 1) if judged is not None else None
    if triggers and _outstanding(evidence, triggers[-1]):
        return triggers[-1]
    return None


def _receipt(
    evidence: SnapshotEvidence,
    status: str,
    *,
    reviewer: str,
    next_round: int,
    reply: PreviousRoundReply | None = None,
    trigger_body: str | None = None,
    existing: ReviewTrigger | None = None,
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "receipt_version": RECEIPT_VERSION,
        "operation": "validate_request",
        "validation_status": status,
        "repository": evidence.repository,
        "pr_number": evidence.pr_number,
        "reviewer": reviewer,
        "previous_round": next_round - 1 if next_round > 1 else None,
        "next_round": next_round,
        "head_sha": evidence.head_sha,
        "snapshot_observed_at": evidence.observed_at,
        "previous_source_digest": reply.source_digest if reply else None,
        "judgment_digest": reply.judgment_digest if reply else None,
    }
    if trigger_body is not None:
        receipt["trigger_body"] = trigger_body
    if existing is not None:
        receipt["existing_trigger_id"] = existing.id
        receipt["existing_trigger_created_at"] = existing.created_at
        receipt["evaluate_round"] = existing.round
    return receipt


def _reply_for_next_round(
    evidence: SnapshotEvidence, next_round: int, body_text: str | None
) -> PreviousRoundReply | None:
    """Round 1 has no previous round; round 2+ must judge the one before it."""
    if next_round < 2:
        return None
    if body_text is None:
        raise EvidenceContractError(
            "round 2+ requires --body-file with the previous round's judgments"
        )
    window = previous_round_window(
        evidence.state["issue_comments"], next_round, strict=True
    )
    return _check_reply(
        evidence.state, window, body_text, f"--body-file for round {next_round}"
    )


def _validate_request(
    value: object,
    *,
    bot_name: str,
    pr_number: int | None,
    now: datetime,
    switch_reviewer: bool,
    max_rounds: int,
    explicit_round: int | None,
    body_text: str | None,
    max_age_seconds: float,
) -> OfflineOutcome:
    evidence = validate_snapshot(
        value, pr_number=pr_number, now=now, max_age_seconds=max_age_seconds
    )
    comments = evidence.state["issue_comments"]
    triggers = restore_triggers(comments)
    posted = _already_posted(evidence, triggers, explicit_round, body_text)
    if posted is not None:
        receipt = _receipt(
            evidence,
            "already_posted",
            reviewer=posted.reviewer,
            next_round=posted.round,
            existing=posted,
        )
        return OfflineOutcome(EXIT_ACQUIRED, receipt)
    next_round = plan_next_round(
        triggers,
        bot=bot_name,
        switch_reviewer=switch_reviewer,
        max_rounds=max_rounds,
        explicit_round=explicit_round,
    )
    reply = _reply_for_next_round(evidence, next_round, body_text)
    candidate = build_trigger_body(
        body_text or "", bot_name, next_round, evidence.head_sha
    )
    receipt = _receipt(
        evidence,
        "valid",
        reviewer=bot_name,
        next_round=next_round,
        reply=reply,
        trigger_body=candidate,
    )
    return OfflineOutcome(EXIT_ACQUIRED, receipt)


def validate_request(
    value: object,
    *,
    bot_name: str,
    pr_number: int | None,
    now: datetime,
    switch_reviewer: bool = False,
    max_rounds: int = 5,
    explicit_round: int | None = None,
    body_text: str | None = None,
    max_age_seconds: float = DEFAULT_MAX_SNAPSHOT_AGE_SECONDS,
) -> OfflineOutcome:
    """Check the previous round's judgments before the client posts one comment.

    A `valid` receipt carries the combined body candidate for the MCP client to
    post once; it is not a posting permit forever, and success is not a review
    pass. An already-posted next round issues no permit and names the trigger.
    """
    try:
        return _validate_request(
            value,
            bot_name=bot_name,
            pr_number=pr_number,
            now=now,
            switch_reviewer=switch_reviewer,
            max_rounds=max_rounds,
            explicit_round=explicit_round,
            body_text=body_text,
            max_age_seconds=max_age_seconds,
        )
    except InsufficientEvidenceError as error:
        receipt = _rejected_receipt(str(error), bot_name, pr_number)
        return OfflineOutcome(EXIT_NO_RESULT, receipt)


def resolve_legacy_completeness(state: object) -> tuple[dict[str, str], list[str]]:
    """Resolve the legacy `completeness` declaration of an unversioned snapshot.

    Distinguishes three cases: no `completeness` key at all (legacy input;
    `.get()` alone can't tell this apart from an explicit `"completeness":
    null`, so presence is checked separately) keeps every section "unknown"
    with no incompleteness; a key present but not a usable object (null, a
    list, a string, ...) is a malformed-but-positive declaration and every
    section is treated as incomplete; a proper dict normalizes each of the
    three required sections (an omitted section counts as incomplete, not an
    implicit "complete") (Codex PR #1114 rounds 1-4 findings).
    """
    key_present = isinstance(state, dict) and "completeness" in state
    declared = state.get("completeness") if isinstance(state, dict) else None
    if isinstance(declared, dict):
        completeness = {
            section: declared.get(section, "unknown") for section in SECTIONS
        }
        incomplete = [s for s, status in completeness.items() if status != "complete"]
    else:
        completeness = dict.fromkeys(SECTIONS, "unknown")
        incomplete = list(SECTIONS) if key_present else []
    return completeness, incomplete


def evaluate_legacy(
    value: object, *, bot_name: str, pr_number: int | None
) -> OfflineOutcome:
    """Read an unversioned snapshot as before, proving nothing about round or head."""
    result = collect_review_state(value, bot_name)
    completeness, incomplete = resolve_legacy_completeness(value)
    target, source = derive_review_target(result.get("review_items", []), None, None)
    result.update(
        repository=None,
        pr_number=pr_number,
        reviewer=bot_name,
        round=None,
        trigger_id=None,
        triggered_at="",
        requested_head_sha=None,
        reviewed_head_sha=target if source == "review_commit" else None,
        current_head_sha=None,
        review_target_sha=target,
        review_target_sha_source=source,
        completeness=completeness,
        evidence_warnings=[LEGACY_WARNING],
    )
    if incomplete and result["acquisition_status"] == ACQUISITION_ACQUIRED:
        # A partial fetch must not be reported as a trustworthy acquired result,
        # even though some content was found (issue #1099 PR #1114 round 1).
        result["acquisition_status"] = ACQUISITION_UNAVAILABLE
        result["reason"] = (
            f"supplied completeness declares {', '.join(incomplete)} incomplete"
        )
    return OfflineOutcome(_EXIT_BY_STATUS[result["acquisition_status"]], result)


def legacy_refusal(
    operation: str, *, bot_name: str, pr_number: int | None
) -> OfflineOutcome:
    """Operations that must prove a round or head cannot run on a legacy snapshot."""
    reason = (
        f"{operation} requires a snapshot_version 1 review snapshot; a legacy "
        "snapshot cannot prove the round, trigger or head (see review-loop.md)"
    )
    if operation == "validate_request":
        payload = _rejected_receipt(reason, bot_name, pr_number)
    else:
        payload = _unavailable(bot_name, pr_number, reason)
    return OfflineOutcome(EXIT_NO_RESULT, payload)

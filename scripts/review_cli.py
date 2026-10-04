"""Argument contract, offline adapter and output for `scripts/wait_for_review.py`.

The online waiting loop stays in `wait_for_review.py`. This module owns the parts
that touch the command line and files: the parser (with explicit-vs-default
detection), the offline `--review-state-file` adapter, and result/receipt output.
`--review-state-file` is always one non-posting, non-polling evaluation; it never
falls back to gh, GitHub authentication or posting a trigger. Runtime
dependencies (clock, Jev evaluation) are injected so callers can substitute them.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestune.review.acquisition import ACQUISITION_ACQUIRED
from orchestune.review.offline import (
    OfflineOutcome,
    evaluate_legacy,
    evaluate_snapshot,
    legacy_refusal,
    validate_request,
)
from orchestune.review.snapshot import (
    DEFAULT_MAX_SNAPSHOT_AGE_SECONDS,
    EvidenceContractError,
    RoundLimitError,
    is_snapshot_v1,
)

EXIT_INTERNAL_ERROR = 2  # Internal error / Unexpected exception / Arg error
EXIT_MAX_ROUNDS = 12  # Maximum review rounds exceeded

DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_INTERVAL_SECONDS = 5
DEFAULT_MAX_RETRIES = 1
DEFAULT_MAX_ROUNDS = 5
OFFLINE_MAX_ROUNDS = 5

EvaluateFindings = Callable[..., dict[str, Any]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def build_parser(stall_grace_default: int) -> argparse.ArgumentParser:
    """The CLI parser. Online-only numeric options default to None so that an
    explicit value is distinguishable from the default (and rejected offline)."""
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
            "Path to a snapshot_version 1 review snapshot fetched through GitHub MCP "
            "(or a legacy raw snapshot); one non-posting, non-polling evaluation "
            "that never invokes gh"
        ),
    )
    parser.add_argument(
        "--validate-request",
        action="store_true",
        help=(
            "With --review-state-file: validate the previous round's judgments in "
            "--body-file before the MCP client posts one combined comment; writes a "
            "receipt and never posts"
        ),
    )
    parser.add_argument(
        "--max-snapshot-age",
        type=float,
        default=None,
        help=(
            "Offline only: maximum snapshot age in seconds "
            f"(positive; default {DEFAULT_MAX_SNAPSHOT_AGE_SECONDS})"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help=f"Online only: maximum time to wait in seconds (default: {DEFAULT_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--stall-grace",
        type=int,
        default=None,
        help=(
            "Online only: seconds an in-progress tracker comment may stay unchanged "
            f"before it is treated as stalled rather than slow (default: {stall_grace_default})"
        ),
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help=f"Online only: polling interval in seconds (default: {DEFAULT_INTERVAL_SECONDS})",
    )
    parser.add_argument(
        "--bot-name",
        type=str,
        required=True,
        choices=("claude", "codex", "skip"),
        help="Explicit reviewer selection (skip records a blocked human-review choice)",
    )
    parser.add_argument(
        "--body",
        type=str,
        default=None,
        help="Online only: custom comment body to post when triggering review",
    )
    parser.add_argument(
        "--body-file",
        type=str,
        default=None,
        help=(
            "Path to the review-reply file with the previous round's judgments "
            "(online: also the trigger body to post)"
        ),
    )
    parser.add_argument(
        "--no-post",
        action="store_true",
        help="Skip posting a review trigger comment and only wait for activity",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=None,
        help=(
            f"Maximum number of review rounds allowed (default: {DEFAULT_MAX_ROUNDS}; "
            f"offline accepts 1-{OFFLINE_MAX_ROUNDS}, counted PR-wide)"
        ),
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=None,
        help=f"Online only: maximum retry attempts on transient failures (default: {DEFAULT_MAX_RETRIES})",
    )
    parser.add_argument(
        "--round",
        type=int,
        default=None,
        help=(
            "Explicit review round number (default: auto-detected from PR comments; "
            "offline: a posted round to evaluate, or the next round for --validate-request)"
        ),
    )
    parser.add_argument(
        "--jev-threshold",
        type=float,
        default=None,
        help="Validity threshold for Jev finding filter (default: 0.7 or JEV_THRESHOLD env)",
    )
    parser.add_argument(
        "--output-file",
        type=str,
        default=None,
        help=(
            "Write the complete machine-readable acquisition result (or the "
            "--validate-request receipt) as JSON to this path. A write failure "
            "exits non-zero, even if acquisition itself succeeded."
        ),
    )
    parser.add_argument(
        "--switch-reviewer",
        action="store_true",
        help="With --validate-request only: allow a reviewer change on explicit user instruction",
    )
    return parser


def _validate_offline(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    online_only = [
        flag
        for flag, value in (
            ("--timeout", args.timeout),
            ("--interval", args.interval),
            ("--stall-grace", args.stall_grace),
            ("--max-retries", args.max_retries),
        )
        if value is not None
    ]
    if online_only:
        parser.error(
            f"{', '.join(online_only)} apply only to online waiting; "
            "--review-state-file is a single non-polling snapshot evaluation"
        )
    if args.body is not None:
        parser.error(
            "--body is not supported with --review-state-file: the MCP client posts "
            "the comment; use --body-file to supply the judgments"
        )
    if args.validate_request and args.no_post:
        parser.error("--no-post cannot be combined with --validate-request")
    if args.switch_reviewer and not args.validate_request:
        parser.error(
            "--switch-reviewer applies only to --validate-request; an already "
            "posted switch is evaluated without it"
        )
    if args.max_rounds is None:
        args.max_rounds = DEFAULT_MAX_ROUNDS
    elif not 1 <= args.max_rounds <= OFFLINE_MAX_ROUNDS:
        parser.error(f"--max-rounds must be between 1 and {OFFLINE_MAX_ROUNDS}")
    if args.round is not None and args.round < 1:
        parser.error("--round must be a positive integer")
    age = args.max_snapshot_age
    if age is not None and not (math.isfinite(age) and age > 0):
        parser.error("--max-snapshot-age must be a positive number of seconds")


def _validate_online(
    parser: argparse.ArgumentParser, args: argparse.Namespace, stall_grace_default: int
) -> None:
    if args.pr is None:
        parser.error("--pr is required unless --review-state-file is used")
    if args.max_snapshot_age is not None:
        parser.error("--max-snapshot-age applies only with --review-state-file")
    for name, default in (
        ("timeout", DEFAULT_TIMEOUT_SECONDS),
        ("interval", DEFAULT_INTERVAL_SECONDS),
        ("max_retries", DEFAULT_MAX_RETRIES),
        ("max_rounds", DEFAULT_MAX_ROUNDS),
        ("stall_grace", stall_grace_default),
    ):
        if getattr(args, name) is None:
            setattr(args, name, default)


def parse_args(
    argv: Sequence[str] | None = None, *, stall_grace_default: int
) -> argparse.Namespace:
    """Parse and validate the CLI contract; any violation exits with status 2."""
    parser = build_parser(stall_grace_default)
    args = parser.parse_args(argv)
    if args.bot_name == "skip" and (args.review_state_file or args.no_post):
        parser.error("skip requires an online PR selection comment")
    if args.validate_request and not args.review_state_file:
        parser.error("--validate-request requires --review-state-file")
    if args.review_state_file:
        _validate_offline(parser, args)
    else:
        _validate_online(parser, args, stall_grace_default)
    return args


def write_output_file(result: dict[str, Any], output_file: str) -> None:
    try:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except OSError as e:
        raise RuntimeError(f"Failed to write --output-file {output_file}: {e}") from e


def _print_header(result: dict[str, Any], bot_name: str) -> None:
    status = result.get("acquisition_status")
    print("\n" + "=" * 72)
    if status == ACQUISITION_ACQUIRED:
        print(f"[AI Review Content Acquired - @{bot_name}] LLM judgment required")
    else:
        # A non-acquired status (unavailable/in_progress) is not ready for
        # judgment; the banner must not claim otherwise even though some
        # partial content may still be shown below for context (Codex PR
        # #1114 round 7 finding).
        reason = result.get("reason") or "no reason given"
        print(f"[AI Review NOT Acquired ({status}) - @{bot_name}] {reason}")
    print(f"Round: {result.get('round')}  Timestamp: {result.get('timestamp', '')}")
    print(
        f"Requested SHA: {result.get('requested_head_sha')}  "
        f"Reviewed SHA: {result.get('reviewed_head_sha')}  "
        f"Current SHA: {result.get('current_head_sha')}"
    )
    print(
        f"Target SHA: {result.get('review_target_sha')} "
        f"({result.get('review_target_sha_source', 'unknown')})"
    )
    for warning in result.get("evidence_warnings", []):
        print(f"Evidence warning: {warning}")


def _jev_finding_id(item: dict[str, Any], current_position: int | None) -> Any:
    """Mirror evaluate_review_findings()'s id-or-index fallback: a missing id and
    an explicit `"id": null` are both id-less, and the fallback index must be
    positional within the current inlines (the list that was actually evaluated),
    not within the full inline list, which may interleave historical or
    unassociated items and shift the index basis (Codex PR #1114 round 5
    finding). It matches jev_filter._finding_id()'s string-tagged sentinel: a
    bare int fallback could collide with a coincidentally equal supplied id
    (Codex PR #1114 round 6 finding)."""
    if item.get("id") is not None:
        return item["id"]
    return f"index:{current_position}" if current_position is not None else None


def _print_inline_comments(result: dict[str, Any]) -> None:
    inline_items = result.get("inline_comments", [])
    if not inline_items:
        print("Inline comments: none")
        return
    current_inlines = [i for i in inline_items if i.get("provenance") == "current"]
    print(
        f"Inline comments: {len(inline_items)} total "
        f"({len(current_inlines)} current round)"
    )
    jev_by_id = {e["finding_id"]: e for e in result.get("jev_evaluations", [])}
    position_by_identity = {
        id(item): index for index, item in enumerate(current_inlines)
    }
    for index, item in enumerate(inline_items, start=1):
        finding_id = _jev_finding_id(item, position_by_identity.get(id(item)))
        jev = jev_by_id.get(finding_id) if finding_id is not None else None
        jev_note = (
            f" [jev: {jev['decision']} ({jev['decision_reason']})]" if jev else ""
        )
        print(
            f"\n--- Inline Comment {index}: {item['path']}:{item['line']} "
            f"[{item.get('provenance')}]{jev_note} ---"
        )
        print(item["body"] or "(No comment body provided)")
    print("--- End Inline Comments ---")


def print_review_result(result: dict[str, Any], bot_name: str) -> None:
    """Render the acquired result. This is raw acquired content, not a verdict:
    the calling LLM still has to read it and judge whether it is adequate."""
    _print_header(result, bot_name)
    _print_inline_comments(result)
    print("=" * 72 + "\n")
    for index, item in enumerate(result.get("review_items", []), start=1):
        print(
            f"--- Review Item {index} [{item.get('kind')}/{item.get('provenance')}] ---"
        )
        print(item.get("body") or "(empty body)")
        print()
    print("--- review_body (concatenated current-round bodies) ---")
    print(result.get("review_body", ""))


def _add_jev(
    payload: dict[str, Any],
    args: argparse.Namespace,
    state: object,
    evaluate: EvaluateFindings,
) -> None:
    current = [
        item
        for item in payload.get("inline_comments", [])
        if item.get("provenance") == "current"
    ]
    evaluations: list[dict[str, Any]] = []
    if current:
        report = evaluate(
            current,
            bot_name=args.bot_name,
            threshold=args.jev_threshold,
            pr=payload.get("pr_number") or args.pr,
            context=state.get("context") if isinstance(state, dict) else None,
        )
        evaluations = report["jev_evaluations"]
    payload["jev_evaluations"] = evaluations


def _offline_outcome(
    args: argparse.Namespace, state: object, now: datetime, body_text: str | None
) -> OfflineOutcome:
    age = args.max_snapshot_age or DEFAULT_MAX_SNAPSHOT_AGE_SECONDS
    if args.validate_request:
        if not is_snapshot_v1(state):
            return legacy_refusal(
                "validate_request", bot_name=args.bot_name, pr_number=args.pr
            )
        return validate_request(
            state,
            bot_name=args.bot_name,
            pr_number=args.pr,
            now=now,
            switch_reviewer=args.switch_reviewer,
            max_rounds=args.max_rounds,
            explicit_round=args.round,
            body_text=body_text,
            max_age_seconds=age,
        )
    if is_snapshot_v1(state):
        return evaluate_snapshot(
            state,
            bot_name=args.bot_name,
            pr_number=args.pr,
            now=now,
            requested_round=args.round,
            max_rounds=args.max_rounds,
            max_age_seconds=age,
            body_text=body_text,
        )
    if args.round is not None or body_text is not None:
        return legacy_refusal(
            "evaluation with --round or --body-file",
            bot_name=args.bot_name,
            pr_number=args.pr,
        )
    return evaluate_legacy(state, bot_name=args.bot_name, pr_number=args.pr)


def _print_receipt(receipt: dict[str, Any], to_stdout: bool) -> None:
    status = receipt.get("validation_status")
    print(f"[validate-request] {status}")
    if status == "valid":
        print(
            f"Next round {receipt['next_round']} for @{receipt['reviewer']} at head "
            f"{receipt['head_sha']}: post trigger_body once as one normal PR comment "
            f"(the MCP client posts; this command never does), re-fetch, then "
            f"evaluate round {receipt['next_round']}."
        )
    elif status == "already_posted":
        print(
            f"Round {receipt['next_round']} is already posted (trigger "
            f"{receipt['existing_trigger_id']}); do not post again. Evaluate it with "
            f"--round {receipt['next_round']}."
        )
    else:
        print(f"Reason: {receipt.get('reason')}")
    if to_stdout:
        print(json.dumps(receipt, ensure_ascii=False, indent=2))


def _fail(args: argparse.Namespace, error: Exception, exit_code: int) -> int:
    """Report a rejected offline request; never leave a stale receipt postable."""
    print(f"Error: {error}", file=sys.stderr)
    if args.validate_request and args.output_file:
        write_output_file(
            {
                "receipt_version": 1,
                "operation": "validate_request",
                "validation_status": "rejected",
                "exit_code": exit_code,
                "reason": str(error),
            },
            args.output_file,
        )
    return exit_code


def run_offline(
    args: argparse.Namespace,
    *,
    evaluate: EvaluateFindings,
    now: datetime | None = None,
) -> int:
    """One offline evaluation or pre-post validation; returns the process exit code.

    Never posts, polls, or calls gh. Contract violations return Exit 2 and a
    round-limit violation Exit 12; unreadable input or output raises, which the
    caller reports as Exit 2 as well.
    """
    with open(args.review_state_file, encoding="utf-8") as state_file:
        state = json.load(state_file)
    body_text = (
        Path(args.body_file).read_text(encoding="utf-8") if args.body_file else None
    )
    try:
        outcome = _offline_outcome(args, state, now or _utc_now(), body_text)
    except RoundLimitError as error:
        return _fail(args, error, EXIT_MAX_ROUNDS)
    except (EvidenceContractError, ValueError) as error:
        return _fail(args, error, EXIT_INTERNAL_ERROR)
    payload = outcome.payload
    if args.validate_request:
        _print_receipt(payload, to_stdout=not args.output_file)
    else:
        _add_jev(payload, args, state, evaluate)
        print_review_result(payload, args.bot_name)
    if args.output_file:
        write_output_file(payload, args.output_file)
    return outcome.exit_code

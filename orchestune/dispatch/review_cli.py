"""`orchestune review status|retry|override` for Layer-2 semantic review (#1436).

``status`` is read-only. ``retry`` and ``override`` only preview the marker a
collaborator would post; ``--apply`` posts it once, after validating the
attempt, its head and the actor again. An override is always a human's explicit
decision: nothing here falls back to it automatically.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.config_loader import load_and_resolve_config
from orchestune.dispatch.postcycle_semantic_review import SemanticReviewCycleRunner
from orchestune.forge import Forge, GitHubForge
from orchestune.infra.cli_output import CliOutput, make_output
from orchestune.integrator.attempt import OpenAttempt, find_open_attempts
from orchestune.review.semantic_gate import (
    TERMINAL_PASSED_PHASES,
    SemanticReviewHumanOverrideV1,
    SemanticReviewPhase,
    SemanticReviewRetryV1,
    SemanticReviewStateV1,
    format_semantic_review_override_marker,
    format_semantic_review_retry_marker,
)

_TEMP_BRANCH = re.compile(r"^integration/temp-parent-issue-(\d+)-")
_WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})
_HEAD_MOVED = "semantic state head does not match the PR head"


class _Refusal(Exception):
    """A request that must not be carried out (exit status 1)."""


@dataclass(frozen=True)
class _Plan:
    marker: str
    request_id: str
    state: SemanticReviewStateV1
    already_posted: bool


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchestune review",
        description="Inspect and resume a Layer-2 semantic review attempt.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("status", "show why an integration PR waits or stopped (read-only)"),
        ("retry", "resume an escalated attempt (preview unless --apply)"),
        ("override", "skip the gate for this head (preview unless --apply)"),
    ):
        command = commands.add_parser(name, help=text, description=text)
        command.add_argument("--pr", type=int, required=True, help="integration PR")
        if name == "status":
            command.add_argument("--json", action="store_true")
            continue
        command.add_argument("--reason", required=True, help="why (recorded)")
        command.add_argument(
            "--apply", action="store_true", help="post the marker (default: preview)"
        )
    return parser


def _parent_of(forge: Forge, pr_number: int) -> int:
    getter: Callable[[int], object] = getattr(forge, "get_pull_request")  # noqa: B009
    branch = str(getattr(getter(pr_number), "head_ref", ""))
    found = _TEMP_BRANCH.match(branch)
    if found is None:
        raise _Refusal(f"PR #{pr_number} is not a Layer-2 integration attempt PR")
    return int(found.group(1))


def _attempt(forge: Forge, parent: int, pr_number: int) -> OpenAttempt:
    attempts = find_open_attempts(forge, parent)
    mine = [a for a in attempts if a.pr_number == pr_number]
    if not mine:
        raise _Refusal(f"PR #{pr_number} is not an open attempt of parent #{parent}")
    open_ones = [
        a
        for a in attempts
        if a.state is None
        or a.state.phase
        not in {SemanticReviewPhase.INVALIDATED, *TERMINAL_PASSED_PHASES}
    ]
    if len(open_ones) > 1:
        raise _Refusal(
            f"multiple open attempts for parent #{parent}; resolve them first"
        )
    return mine[0]


def _readable_state(attempt: OpenAttempt) -> SemanticReviewStateV1:
    if attempt.state is None:
        if attempt.state_error == _HEAD_MOVED:
            raise _Refusal(
                "the PR head moved since the attempt state was saved; "
                "let the dispatcher cycle record it first"
            )
        raise _Refusal(f"attempt state is unreadable: {attempt.state_error}")
    return attempt.state


def _require_human_writer(forge: Forge) -> str:
    actor = forge.get_authenticated_user()
    permission = (
        ""
        if actor.casefold().endswith("[bot]")
        else forge.get_actor_permission(actor).casefold()
    )
    if permission not in _WRITE_PERMISSIONS:
        raise _Refusal(
            f"{actor} is not a person with write access; "
            "only such a collaborator may resume or skip the gate"
        )
    return actor


def _request_id(state: SemanticReviewStateV1, kind: str, reason: str) -> str:
    digest = hashlib.sha256(
        f"{state.attempt_id}:{state.head_sha}:{kind}:{reason}".encode()
    ).hexdigest()
    return f"cli-{digest[:16]}"


def _posted(forge: Forge, pr_number: int, marker: str) -> bool:
    return any(
        str(c.get("body") or "").strip() == marker
        for c in forge.list_all_issue_comments(pr_number)
    )


def _plan(forge: Forge, parent: int, pr_number: int, kind: str, reason: str) -> _Plan:
    if not reason.strip():
        raise _Refusal("a non-empty --reason is required")
    reason = reason.strip()
    actor = _require_human_writer(forge)
    state = _readable_state(_attempt(forge, parent, pr_number))
    if state.phase is SemanticReviewPhase.INVALIDATED:
        raise _Refusal("the attempt is invalidated; a new attempt replaces it")
    if state.phase in TERMINAL_PASSED_PHASES:
        raise _Refusal(f"the attempt already ended ({state.phase.value})")
    if kind == "retry" and state.phase is not SemanticReviewPhase.ESCALATED:
        raise _Refusal(
            f"retry needs an escalated attempt (phase is {state.phase.value})"
        )
    request_id = _request_id(state, kind, reason)
    if kind == "retry":
        if state.last_retry_request_id == request_id:
            raise _Refusal("this retry request was already consumed")
        marker = format_semantic_review_retry_marker(
            SemanticReviewRetryV1(
                state.attempt_id,
                state.head_sha,
                request_id,
                "retry",
                reason,
                False,
                actor,
            )
        )
    else:
        marker = format_semantic_review_override_marker(
            SemanticReviewHumanOverrideV1(
                state.attempt_id, state.head_sha, reason, actor
            )
        )
    return _Plan(marker, request_id, state, _posted(forge, pr_number, marker))


def _show_status(
    runner: SemanticReviewCycleRunner,
    forge: Forge,
    parent: int,
    pr_number: int,
    out: CliOutput,
    as_json: bool,
) -> None:
    _, diagnostics = runner.inspect(_attempt(forge, parent, pr_number))
    if as_json:
        out.payload(json.dumps(diagnostics.to_dict(), ensure_ascii=False, indent=2))
        return
    lines = [
        *diagnostics.lines(),
        *(f"resume: {c}" for c in diagnostics.resume_commands),
    ]
    out.emit(out.text_block("\n".join(lines)))


def _act(forge: Forge, parent: int, args: argparse.Namespace, out: CliOutput) -> None:
    plan = _plan(forge, parent, args.pr, args.command, args.reason)
    if plan.already_posted:
        out.emit(out.text_block(f"already posted for PR #{args.pr}; nothing to do"))
        return
    if not args.apply:
        out.emit(
            out.text_block(
                f"Preview ({args.command}) for PR #{args.pr}, attempt "
                f"{plan.state.attempt_id[:12]} at {plan.state.head_sha}:\n"
                f"{plan.marker}\nRe-run with --apply to post it."
            )
        )
        return
    plan = _plan(forge, parent, args.pr, args.command, args.reason)  # just before
    if plan.already_posted:
        out.emit(out.text_block(f"already posted for PR #{args.pr}; nothing to do"))
        return
    forge.create_issue_comment(args.pr, plan.marker)
    out.emit(out.text_block(f"Posted the {args.command} marker on PR #{args.pr}."))


def main(
    argv: Sequence[str] | None = None,
    *,
    forge: Forge | None = None,
    config: DispatcherConfig | None = None,
    now: datetime | None = None,
    repository_root: Path | None = None,
    output: CliOutput | None = None,
) -> int:
    args = _parser().parse_args(argv)
    out = output or make_output(machine=getattr(args, "json", False))
    try:
        forge = forge or GitHubForge()
        parent = _parent_of(forge, args.pr)
        if args.command != "status":
            _act(forge, parent, args, out)
            return 0
        config = config or load_and_resolve_config(
            ["--parent-issue", str(parent)], forge=forge
        )
        runner = SemanticReviewCycleRunner(
            config,
            forge=forge,
            repository_root=(repository_root or Path(".")).resolve(),
            now=now or datetime.now(UTC),
            worker_id="review-cli",
            verify_ci_fn=lambda _branch, _sha: None,
        )
        _show_status(runner, forge, parent, args.pr, out, args.json)
    except _Refusal as refusal:
        print(f"orchestune review: {refusal}", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 - reported, never a traceback
        print(f"orchestune review: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    return 0

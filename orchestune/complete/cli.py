"""Command-line entry point for idempotent task completion."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from orchestune.claim.local_identity import caller_claim_id
from orchestune.claim.workspace import resolve_claim_workspace
from orchestune.complete.contracts import CompleteRequest, CompleteStage
from orchestune.complete.service import complete_task


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Complete an Orchestune task")
    parser.add_argument("--issue", required=True, type=int)
    parser.add_argument("--pr", type=int)
    parser.add_argument(
        "--result", required=True, choices=("done", "not-needed", "blocked")
    )
    parser.add_argument("--reason")
    parser.add_argument("--reviewer", choices=("claude", "codex", "skip"))
    parser.add_argument("--review-reply", type=Path)
    parser.add_argument("--state", type=Path, help="shared run_state.json path")
    parser.add_argument(
        "--completion-id", help="resume or read an immutable completion result"
    )
    parser.add_argument(
        "--no-apply",
        action="store_true",
        help="validate without CI, state, or GitHub writes",
    )
    return parser


def _credentials(
    issue_number: int, state_path: Path | None = None
) -> tuple[str | None, str | None, Path]:
    workspace = (
        resolve_claim_workspace(explicit_state_path=state_path)
        if state_path is not None
        else resolve_claim_workspace()
    )
    return None, caller_claim_id(), workspace.run_state_path


def _request_from_args(args: argparse.Namespace) -> CompleteRequest:
    token, claim_id, state_path = _credentials(args.issue, args.state)
    common = {
        "completion_id": args.completion_id,
        "owner_token": token,
        "claim_id": claim_id,
        "dry_run": args.no_apply,
        "state_path": state_path,
        "worktree_root": Path.cwd(),
    }
    if args.result == "done":
        if args.pr is None:
            raise ValueError("--pr is required when --result=done")
        if args.reason is not None:
            raise ValueError("--reason is only valid when --result=blocked")
        if args.reviewer is None:
            raise ValueError("--reviewer is required when --result=done")
        if args.reviewer != "skip" and args.review_reply is None:
            raise ValueError("--review-reply is required for claude/codex")
        return CompleteRequest.done(
            args.issue,
            args.pr,
            reviewer=args.reviewer,
            review_reply=args.review_reply,
            **common,
        )
    if args.reviewer is not None or args.review_reply is not None:
        raise ValueError(
            "--reviewer and --review-reply are only valid when --result=done"
        )
    if args.result == "blocked":
        if args.pr is not None:
            raise ValueError("--pr is only valid when --result=done")
        if not args.reason:
            raise ValueError("--reason is required when --result=blocked")
        return CompleteRequest.blocked(args.issue, args.reason, **common)
    if args.pr is not None or args.reason is not None:
        raise ValueError("--pr and --reason are not valid when --result=not-needed")
    return CompleteRequest.not_needed(args.issue, **common)


def _print_progress(completion_id: str, stage: CompleteStage) -> None:
    print(f"Completion ID: {completion_id}", flush=True)
    print(f"Reached stage: {stage.value}", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        request = _request_from_args(args)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 40

    result = complete_task(request, on_progress=_print_progress)
    if result.completion_id is not None:
        print(f"Completion ID: {result.completion_id}")
    print(f"Reached stage: {result.stage.value}")
    if result.success:
        if result.preview:
            print(f"Completion preview validated for Issue #{result.issue_number}.")
            return 0
        print(f"Completion handed off to GC for Issue #{result.issue_number}.")
        return 0
    assert result.failure is not None
    print(f"Complete failed: {result.failure.reason.value}: {result.failure.message}")
    return int(result.failure.exit_code)

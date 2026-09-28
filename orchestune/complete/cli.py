"""Command-line entry point for idempotent task completion."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

from orchestune.claim.workspace import resolve_claim_workspace
from orchestune.complete.contracts import CompleteRequest, CompleteStage
from orchestune.complete.service import complete_task
from orchestune.complete.unclaimed import token_directory
from orchestune.infra.private_tokens import (
    _read_owner_token as read_private_owner_token,
)
from orchestune.ledger.completion_reservations import completion_record
from orchestune.ledger.run_state import load_run_state_readonly


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Complete an Orchestune task")
    parser.add_argument("--issue", required=True, type=int)
    parser.add_argument("--pr", type=int)
    parser.add_argument(
        "--result", required=True, choices=("done", "not-needed", "blocked")
    )
    parser.add_argument("--reason")
    parser.add_argument(
        "--completion-id", help="resume or read an immutable completion result"
    )
    parser.add_argument(
        "--no-apply",
        action="store_true",
        help="validate without CI, state, or GitHub writes",
    )
    return parser


def _credentials(issue_number: int) -> tuple[str | None, str | None, Path]:
    workspace = resolve_claim_workspace()
    state = load_run_state_readonly(workspace.run_state_path)
    active = state.active_worktrees.get(str(issue_number))
    if active is None:
        record = completion_record(state, issue_number)
        if record is not None and record.get("stage") != "handed_off":
            return (
                read_private_owner_token(
                    token_directory(workspace.run_state_path), record["completion_id"]
                ),
                None,
                workspace.run_state_path,
            )
        return None, None, workspace.run_state_path
    if not active.claim_id:
        return None, None, workspace.run_state_path
    token_path = (
        workspace.run_state_path.parent
        / ".orchestune"
        / "claim-tokens"
        / f"{active.claim_id}.token"
    )
    return (
        _read_owner_token(token_path),
        active.claim_id,
        workspace.run_state_path,
    )


def _read_owner_token(path: Path) -> str | None:
    try:
        if os.name != "nt" and path.stat().st_mode & 0o077:
            return None
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def _request_from_args(args: argparse.Namespace) -> CompleteRequest:
    token, claim_id, state_path = _credentials(args.issue)
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
        return CompleteRequest.done(args.issue, args.pr, **common)
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

"""Command-line interface entry point for the orchestune claim command."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from orchestune.claim.amend import FootprintAmendOutcome, amend_claim_footprint
from orchestune.claim.contracts import (
    ClaimFailure,
    ClaimFailureReason,
    ClaimOutcome,
    ClaimRequest,
)
from orchestune.claim.service import claim_task, resume_claim
from orchestune.claim.workspace import resolve_claim_workspace


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Claim an Orchestune task issue.")
    parser.add_argument("issue_number", type=int, help="GitHub Issue number to claim")
    parser.add_argument("--no-apply", action="store_true", help="validate only")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--resume", metavar="CLAIM_ID", help="resume an interrupted claim"
    )
    mode.add_argument(
        "--amend-footprint",
        action="store_true",
        help="widen the held claim to the Issue footprint and files already changed",
    )
    parser.add_argument("--state", type=Path, help="path to the local run_state.json")
    parser.add_argument(
        "--timeout", type=float, help="run-state lock timeout in seconds"
    )
    return parser


def _token_directory(state_path: Path | None) -> Path:
    workspace = resolve_claim_workspace(explicit_state_path=state_path)
    return workspace.run_state_path.parent / ".orchestune" / "claim-tokens"


def _print_success(outcome: ClaimOutcome, *, preview: bool) -> None:
    if preview:
        print(
            "Dry run: validation succeeded; no Git, Forge, or state changes were made."
        )
        print(f"Issue: #{outcome.issue_number}")
        print(f"Claim ID: {outcome.claim_id or 'unavailable'}")
        print(f"Planned branch: {outcome.branch or 'unavailable'}")
        print(f"Planned worktree: {outcome.worktree_path or 'unavailable'}")
        print(f"Planned base: {outcome.base_ref or 'unavailable'}")
        print(
            f"Owner kind: {(outcome.owner_kind.value if outcome.owner_kind else 'unavailable')}"
        )
        print(
            "Unverified: Git fetch, worktree creation, state persistence, Forge metadata and label updates."
        )
        return
    print(f"Issue: #{outcome.issue_number}")
    print(f"Claim ID: {outcome.claim_id or 'unavailable'}")
    print(f"Branch: {outcome.branch or 'unavailable'}")
    print(f"Worktree: {outcome.worktree_path or 'unavailable'}")
    print(f"Base: {outcome.base_ref or 'unavailable'}")
    print(
        f"Owner kind: {(outcome.owner_kind.value if outcome.owner_kind else 'unavailable')}"
    )


def _print_failure(failure: ClaimFailure) -> None:
    print(f"Claim failed: reason={failure.reason.value}", file=sys.stderr)
    print(f"Message: {failure.message}", file=sys.stderr)
    if failure.conflicting_issue_number is not None:
        print(f"conflicting_issue=#{failure.conflicting_issue_number}", file=sys.stderr)
    if failure.conflicting_branch:
        print(f"conflicting_branch={failure.conflicting_branch}", file=sys.stderr)
    if failure.conflicting_path:
        print(f"conflicting_path={failure.conflicting_path}", file=sys.stderr)
    actions = failure.next_actions or (
        "Inspect the claim state and retry when the conflict is resolved.",
    )
    for action in actions:
        print(f"Next action: {action}", file=sys.stderr)


def _resume_or_preview(args: argparse.Namespace, token: str) -> ClaimOutcome:
    if args.no_apply:
        request = ClaimRequest(
            issue_number=args.issue_number,
            resume_claim_id=args.resume,
            owner_token=token,
            dry_run=True,
            timeout_seconds=args.timeout,
            state_path=args.state,
        )
        return claim_task(request, apply=False)
    return resume_claim(
        args.resume, token, state_path=args.state, timeout_seconds=args.timeout
    )


def _run_claim(args: argparse.Namespace, token_dir: Path) -> ClaimOutcome:
    if args.resume:
        return _resume_or_preview(args, "")
    request = ClaimRequest(
        issue_number=args.issue_number,
        dry_run=args.no_apply,
        timeout_seconds=args.timeout,
        state_path=args.state,
    )
    return claim_task(request, apply=not args.no_apply)


def _run_amend(args: argparse.Namespace, token_dir: Path) -> FootprintAmendOutcome:
    return amend_claim_footprint(
        args.issue_number,
        apply=not args.no_apply,
        state_path=args.state,
        timeout_seconds=args.timeout,
    )


def _render_amend(outcome: FootprintAmendOutcome, *, preview: bool) -> int:
    if not outcome.success:
        assert outcome.failure is not None
        _print_failure(outcome.failure)
        return int(outcome.failure.exit_code)
    if preview:
        print("Dry run: no Forge or state changes were made.")
    print(f"Issue: #{outcome.issue_number}")
    print(f"Claim ID: {outcome.claim_id}")
    print(f"Worktree: {outcome.worktree_path}")
    print(f"Added: {', '.join(outcome.added) or 'none'}")
    print(f"Footprint: {', '.join(outcome.amended_footprint)}")
    if not preview:
        print(
            f"Issue footprint updated: {'yes' if outcome.issue_body_updated else 'no'}"
        )
    return 0


def _render_outcome(outcome: ClaimOutcome, token_dir: Path, *, preview: bool) -> int:
    if not outcome.success:
        assert outcome.failure is not None
        _print_failure(outcome.failure)
        return int(outcome.failure.exit_code)
    _print_success(outcome, preview=preview)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse a claim request, invoke the service, and render safe diagnostics."""
    args = _build_parser().parse_args(argv)
    if args.timeout is not None and args.timeout < 0:
        _print_failure(
            ClaimFailure(
                reason=ClaimFailureReason.STATE_LOCK_FAILED,
                message="--timeout must be greater than or equal to zero.",
            )
        )
        return 22
    try:
        token_dir = _token_directory(args.state)
        if args.amend_footprint:
            amended = _run_amend(args, token_dir)
        else:
            outcome = _run_claim(args, token_dir)
    except Exception:
        print(
            "Claim failed: reason=generic_error\n"
            "Message: claim service invocation failed; inspect protected local records and retry.",
            file=sys.stderr,
        )
        return 1
    if args.amend_footprint:
        return _render_amend(amended, preview=args.no_apply)
    return _render_outcome(outcome, token_dir, preview=args.no_apply)


if __name__ == "__main__":
    raise SystemExit(main())

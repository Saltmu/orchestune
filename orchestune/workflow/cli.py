"""L4 presentation for read-only advisory workflow gates."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from orchestune.workflow.check import check_gate
from orchestune.workflow.contracts import GATES
from orchestune.workflow.next import next_step
from orchestune.workflow.readers import read_context
from orchestune.workflow.stages import load_stage_definitions


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Issue/PR number must be positive")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchestune workflow",
        description="Read-only advisory workflow evidence; never runs actions.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    next_parser = commands.add_parser(
        "next", help="Show the next workflow step without writes"
    )
    check = commands.add_parser("check", help="Check a workflow gate without writes")
    for command in (next_parser, check):
        command.add_argument("--issue", type=_positive, required=True)
        command.add_argument("--session-dir", type=Path, required=True)
        command.add_argument("--pr", type=_positive)
        command.add_argument("--pr-body", type=Path)
        command.add_argument("--review-reply", type=Path)
        command.add_argument(
            "--snapshot",
            type=Path,
            help="Explicit MCP workflow snapshot v1 (state backend=mcp)",
        )
        command.add_argument(
            "--json",
            action="store_true",
            help="Emit the versioned workflow-result object",
        )
    check.add_argument("gate", choices=GATES)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Usage errors have no result JSON; invalid evidence returns an unmet result."""
    args = _parser().parse_args(argv)
    try:
        stages = load_stage_definitions()
        context = read_context(
            issue_number=args.issue,
            session_dir=args.session_dir,
            pr_number=args.pr,
            pr_body=args.pr_body,
            review_reply=args.review_reply,
            snapshot=args.snapshot,
        )
        result = (
            next_step(context, stages=stages)
            if args.command == "next"
            else check_gate(context, args.gate, stages=stages)
        )
    except Exception as error:
        print(f"workflow {args.command} failed: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    else:
        label = "next" if args.command == "next" else result.gate_id
        print(f"{label}: {result.status} (stage: {result.stage_id})")
        for check in result.checks:
            print(f"  {check.id}: {check.status}")
            for missing in check.missing:
                print(f"    {missing}")
        for reference in result.references:
            print(f"  See: {reference}")
    return int(result.exit_code)

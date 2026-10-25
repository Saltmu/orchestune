"""Command-line interface for safe decomposition-generation replacement."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import cast

from orchestune.forge import GitHubForge
from orchestune.infra.cli_output import CliOutput, make_output
from orchestune.replan.apply import ReplanApplyForge, _parent_number, apply_replan
from orchestune.replan.plan import load_replan_plan
from orchestune.replan.preview import ReplanPreview, build_replan_preview
from orchestune.replan.snapshot import collect_replan_snapshot

EXIT_SUCCESS = 0
EXIT_CONFIG = 2
EXIT_CONFIRMATION = 3
EXIT_PARTIAL = 4
EXIT_NOOP = 5
EXIT_CONFLICT = 6


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview or apply a safe replacement of unstarted Issue generations."
    )
    parser.add_argument("--plan", default="decomposition_plan.md")
    parser.add_argument("--parent-issue", type=int)
    parser.add_argument("--template", default=".github/issue_template.md")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply the preview after explicit confirmation",
    )
    parser.add_argument(
        "--confirm-preview", metavar="TOKEN", help="token printed by a fresh preview"
    )
    return parser


def _stdout() -> CliOutput:
    return make_output(sys.stdout)


def _stderr() -> CliOutput:
    return make_output(sys.stderr)


def _write_lines(output: CliOutput, lines: Sequence[str]) -> None:
    """Write literal lines through the shared output layer (plain text is unchanged)."""
    for line in lines:
        output.emit(output.text_block(line))


def _print_preview(preview: ReplanPreview, parent_issue: int) -> None:
    lines = [
        f"Replan preview for parent Issue #{parent_issue}",
        f"Plan revision: {preview.plan_revision}",
    ]
    for decision in preview.decisions:
        target = (
            f"{decision.subject} (#{decision.issue_number})"
            if decision.issue_number is not None
            else decision.subject
        )
        lines.append(f"{decision.action}: {target} — {decision.reason}")
    lines.append(f"Preview token: {preview.preview_token}")
    _write_lines(_stdout(), lines)


def _is_unsafe(preview: ReplanPreview) -> bool:
    return any(
        item.action in {"manual-review", "conflict"} for item in preview.decisions
    )


def _refusal(args: argparse.Namespace, preview: ReplanPreview) -> int | None:
    """Return the exit code that refuses ``--apply``, or ``None`` to proceed."""
    if args.confirm_preview is None:
        _write_lines(_stderr(), ["Error: --apply requires --confirm-preview TOKEN"])
        return EXIT_CONFIRMATION
    if args.confirm_preview != preview.preview_token:
        _write_lines(
            _stderr(),
            ["Error: preview requires a fresh, conflict-free confirmation"],
        )
        return EXIT_CONFIRMATION
    if _is_unsafe(preview):
        _write_lines(
            _stderr(),
            ["Error: preview contains conflicts requiring manual review"],
        )
        return EXIT_CONFLICT
    return None


def _apply(
    args: argparse.Namespace, preview: ReplanPreview, forge: ReplanApplyForge
) -> int:
    refusal = _refusal(args, preview)
    if refusal is not None:
        return refusal
    try:
        result = apply_replan(
            args.plan,
            args.confirm_preview,
            forge=forge,
            template_path=args.template,
            parent_issue_number=args.parent_issue,
        )
    except ValueError as error:
        _write_lines(_stderr(), [f"Error: {error}"])
        return EXIT_CONFIRMATION
    except Exception as error:
        _write_lines(
            _stderr(),
            [
                "Error: replan did not complete; run a new preview before retrying: "
                f"{error}"
            ],
        )
        return EXIT_PARTIAL
    if result.degraded:
        _write_lines(
            _stderr(),
            ["Warning: replan partially applied; run a new preview before retrying."],
        )
        return EXIT_PARTIAL
    if not result.created_issue_numbers and not result.retired_issue_numbers:
        _write_lines(_stdout(), ["No-op: this generation is already active."])
        return EXIT_NOOP
    _write_lines(
        _stdout(),
        [
            f"Applied replan: created={len(result.created_issue_numbers)}, "
            f"reused={len(result.reused_issue_numbers)}, "
            f"retired={len(result.retired_issue_numbers)}"
        ],
    )
    return EXIT_SUCCESS


def main(argv: Sequence[str] | None = None) -> int:
    """Run a read-only preview by default and require its exact token for apply."""

    args = _build_arg_parser().parse_args(argv)
    try:
        plan = load_replan_plan(args.plan)
        parent = _parent_number(plan.parent_issue_number, args.parent_issue)
        forge = cast(ReplanApplyForge, GitHubForge())
        preview = build_replan_preview(plan, collect_replan_snapshot(forge, parent))
    except (OSError, ValueError) as error:
        _write_lines(_stderr(), [f"Error: {error}"])
        return EXIT_CONFIG

    _print_preview(preview, parent)
    if not args.apply:
        if _is_unsafe(preview):
            _write_lines(
                _stderr(),
                ["Error: preview contains conflicts requiring manual review"],
            )
            return EXIT_CONFLICT
        return EXIT_SUCCESS
    return _apply(args, preview, forge)


if __name__ == "__main__":
    raise SystemExit(main())

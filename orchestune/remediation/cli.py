"""Preview and explicitly confirm remediation registrations."""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from orchestune.forge import GitHubForge
from orchestune.infra.cli_output import CliOutput, make_output
from orchestune.remediation.apply import apply_remediation
from orchestune.remediation.models import RemediationPreview
from orchestune.remediation.preview import build_remediation_preview
from orchestune.remediation.snapshot import collect_remediation_snapshot
from orchestune.remediation.spec import load_remediation_spec, positive_number


def _stdout() -> CliOutput:
    return make_output(sys.stdout)


def _stderr() -> CliOutput:
    return make_output(sys.stderr)


def _write_lines(output: CliOutput, lines: Sequence[str]) -> None:
    """Write literal lines through the shared output layer (plain text is unchanged)."""
    for line in lines:
        output.emit(output.text_block(line))


def _preview_lines(preview: RemediationPreview, parent: int) -> list[str]:
    lines = [
        f"Remediation preview for parent Issue #{parent}",
        f"Spec revision: {preview.spec_revision}",
    ]
    for item in preview.tasks:
        task = item.task
        lines.append(
            f"{item.action}: {task.subtask.id} (Issue {item.issue_number or 'new'})"
        )
        lines.append(f"  kind: {task.kind}")
        lines.append(f"  parent_acceptance: {task.parent_acceptance}")
        lines.append(f"  matched sources: {item.acceptance_sources}")
        lines.append(f"  origins: {task.origins}")
        lines.append(f"  reason: {task.reason}")
        lines.append(f"  footprint: {task.subtask.footprint}")
        lines.append(f"  depends_on: {task.subtask.depends_on}")
        lines.extend(f"  diff: {difference}" for difference in item.footprint_diff)
        if item.completed_steps or item.remaining_steps:
            lines.append(
                f"  completed: {item.completed_steps}; remaining: {item.remaining_steps}"
            )
    lines.extend(
        f"Error [{problem.category}]: {problem.message}" for problem in preview.problems
    )
    lines.append(f"Preview token: {preview.preview_token}")
    return lines


def main(argv: Sequence[str] | None = None, *, forge: Any = None) -> int:
    """Read the spec and remote state, and print a preview with its exit status."""
    parser = argparse.ArgumentParser(
        description="Preview parent-scope remediation tasks without writing anything."
    )
    parser.add_argument("--spec", required=True)
    parser.add_argument("--parent-issue", type=int)
    parser.add_argument("--template", default=".github/issue_template.md")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-preview")
    args = parser.parse_args(argv)
    if args.apply and not args.confirm_preview:
        parser.error("--apply requires --confirm-preview")
    if args.confirm_preview and not args.apply:
        parser.error("--confirm-preview requires --apply")
    if args.apply:
        result = apply_remediation(
            args.spec,
            args.confirm_preview,
            forge=forge,
            template_path=args.template,
            parent_issue_number=args.parent_issue,
        )
        _write_lines(_stdout(), [result.message])
        return result.exit_code
    try:
        spec = load_remediation_spec(args.spec)
        if args.parent_issue is not None and spec.parent_issue_number not in (
            None,
            args.parent_issue,
        ):
            raise ValueError("--parent-issue conflicts with spec parent_issue_number")
        parent = positive_number(
            args.parent_issue
            if args.parent_issue is not None
            else spec.parent_issue_number,
            "parent_issue_number",
        )
        snapshot = collect_remediation_snapshot(
            forge if forge is not None else GitHubForge(), parent, spec
        )
        template = None
        if args.template and Path(args.template).is_file():
            template = Path(args.template).read_text(encoding="utf-8")
        preview = build_remediation_preview(spec, snapshot, template=template)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        _write_lines(_stderr(), [f"Error: {exc}"])
        return 2
    _write_lines(_stdout(), _preview_lines(preview, parent))
    return preview.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

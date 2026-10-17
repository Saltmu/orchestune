"""Preview and explicitly confirm remediation registrations."""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from typing import Any

from orchestune.forge import GitHubForge
from orchestune.remediation.apply import apply_remediation
from orchestune.remediation.models import RemediationPreview
from orchestune.remediation.preview import build_remediation_preview
from orchestune.remediation.snapshot import collect_remediation_snapshot
from orchestune.remediation.spec import load_remediation_spec, positive_number


def _print_preview(preview: RemediationPreview, parent: int) -> None:
    print(f"Remediation preview for parent Issue #{parent}")
    print(f"Spec revision: {preview.spec_revision}")
    for item in preview.tasks:
        task = item.task
        print(f"{item.action}: {task.subtask.id} (Issue {item.issue_number or 'new'})")
        print(f"  kind: {task.kind}")
        print(f"  parent_acceptance: {task.parent_acceptance}")
        print(f"  matched sources: {item.acceptance_sources}")
        print(f"  origins: {task.origins}")
        print(f"  reason: {task.reason}")
        print(f"  footprint: {task.subtask.footprint}")
        print(f"  depends_on: {task.subtask.depends_on}")
        for difference in item.footprint_diff:
            print(f"  diff: {difference}")
        if item.completed_steps or item.remaining_steps:
            print(
                f"  completed: {item.completed_steps}; remaining: {item.remaining_steps}"
            )
    for problem in preview.problems:
        print(f"Error [{problem.category}]: {problem.message}")
    print(f"Preview token: {preview.preview_token}")


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
        print(result.message)
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
        preview = build_remediation_preview(spec, snapshot)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    _print_preview(preview, parent)
    return preview.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

"""CLI facade for decomposition-plan Issue provisioning."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from orchestune.dag.models import ConfigError, resolve_repo_root
from orchestune.forge import GitHubForge
from orchestune.provisioning.flow import (
    IssuePreview,
    ProvisionResult,
    _build_provisioning_dag,
)
from orchestune.provisioning.flow import provision_issues as _provision_issues
from orchestune.provisioning.parent import _resolve_parent_issue
from orchestune.provisioning.plan import (
    PlanMetadata,
    _load_plan,
    _parent_body,
    restore_plan_file_from_parent,
)
from orchestune.provisioning.rendering import (
    _build_subtask_issue_body,
    _derive_labels,
    _render_issue_body,
    _subtask_id_from_body,
    _validate_template_identity_marker,
)
from orchestune.provisioning.subtasks import (
    _link_subtask_relationships,
    _provision_subtask,
)

__all__ = (
    "IssuePreview",
    "PlanMetadata",
    "ProvisionResult",
    "_build_provisioning_dag",
    "_build_subtask_issue_body",
    "_derive_labels",
    "_link_subtask_relationships",
    "_load_plan",
    "_parent_body",
    "_print_result",
    "_provision_subtask",
    "_render_issue_body",
    "_resolve_parent_issue",
    "_subtask_id_from_body",
    "_validate_template_identity_marker",
    "main",
    "provision_issues",
)


def provision_issues(*args: object, **kwargs: object) -> ProvisionResult:
    """Compatibility entry point for callers of ``orchestune.provisioning``."""
    return _provision_issues(*args, **kwargs)  # type: ignore[arg-type]


def _print_result(result: ProvisionResult) -> None:
    if not result.applied:
        print("Dry run (--no-apply): no Issues were created.")
        if result.parent_issue_number is not None:
            print(f"Target parent issue: #{result.parent_issue_number}")
        for preview in result.previews:
            status = "reuse expected" if preview.already_has_issue else "would create"
            print(f"\n=== {preview.subtask_id} ({status}) ===")
            print(f"Title: {preview.title}")
            print(f"Labels: {', '.join(preview.labels)}")
            print(preview.body)
        return
    print(f"Parent issue: #{result.parent_issue_number}")
    print(f"Created: {len(result.created)}")
    for subtask_id, number in result.created.items():
        print(f"  + {subtask_id} -> #{number}")
    print(f"Reused: {len(result.reused)}")
    for subtask_id, number in result.reused.items():
        print(f"  = {subtask_id} -> #{number}")
    if result.degraded_subtask_ids:
        print(
            f"\nOperating mode: degraded/parent-metadata for {len(result.degraded_subtask_ids)} subtask(s)"
        )
        for subtask_id in result.degraded_subtask_ids:
            print(f"  ! {subtask_id}")
    else:
        print(
            "\nOperating mode: full (native sub-issue/blocked_by relationships linked)"
        )
    if not result.plan_synced:
        print(
            f"\nWarning: could not sync decomposition plan into #{result.parent_issue_number}'s body."
        )
        print(
            "The local plan file was updated, but the parent Issue body does not reflect the latest state."
        )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="orchestune provision: decomposition_plan.mdからIssueを起票する"
    )
    parser.add_argument("--plan", default="decomposition_plan.md")
    parser.add_argument("--template", default=".github/issue_template.md")
    parser.add_argument("--apply", dest="apply", action="store_true", default=True)
    parser.add_argument("--no-apply", dest="apply", action="store_false")
    parser.add_argument("--parent-issue", type=int, default=None)
    parser.add_argument(
        "--restore-plan", type=int, metavar="PARENT_ISSUE", default=None
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _build_arg_parser().parse_args(argv)
    try:
        if args.restore_plan is not None:
            out = restore_plan_file_from_parent(
                GitHubForge(), args.restore_plan, args.plan
            )
            print(
                f"Successfully restored decomposition plan from #{args.restore_plan} to {out}"
            )
            raise SystemExit(0)
        result = _provision_issues(
            args.plan,
            forge=GitHubForge(),
            apply=args.apply,
            template_path=args.template,
            repo_root=resolve_repo_root(args.plan),
            parent_issue=args.parent_issue,
        )
    except ConfigError as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    _print_result(result)
    raise SystemExit(0)

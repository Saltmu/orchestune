"""Parent completion and advisory final acceptance requests.

Only a verified final merge closes the parent; acceptance review never gates it.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from orchestune.forge import Forge, GitHubForge
from orchestune.integrator.final_acceptance_review import (
    request_final_acceptance_review,
)
from orchestune.integrator.final_pr_body import collect_child_summaries
from orchestune.integrator.parent_branch_state import is_current_parent_branch_merged
from orchestune.integrator.pr import (
    ParentFinalPrMigrationError,
    ensure_parent_final_pr,
    mark_parent_final_pr_pending,
    migrate_open_parent_final_pr,
)
from orchestune.issue_parsing import find_children_by_parent
from orchestune.models import IssueRecord


def _unsafe_parent_final_pr_result(
    parent_issue_number: int, error: ParentFinalPrMigrationError
) -> dict:
    print(
        f"Error: Parent issue #{parent_issue_number} remains unsafe until final "
        f"PR #{error.pr_number} is migrated: {error.error}",
        file=sys.stderr,
    )
    return {
        "status": "unsafe_final_pr",
        "pr_number": error.pr_number,
        "reason": str(error.error),
    }


def _migrate_legacy_final_pr_before_completion(
    parent_issue_number: int, forge: Forge
) -> dict | None:
    # Even pending PRs must lose legacy closing references before a human merge.
    try:
        migrate_open_parent_final_pr(parent_issue_number, forge=forge)
    except ParentFinalPrMigrationError as error:
        return _unsafe_parent_final_pr_result(parent_issue_number, error)
    return None


def process_parent_completion(
    parent_issue_number: int,
    apply: bool,
    forge: Forge | None = None,
    *,
    final_acceptance_review: str = "advisory",
    final_acceptance_reviewer: str | None = None,
) -> dict:
    if not apply:
        return {
            "status": "skipped",
            "final_acceptance_review": {
                "status": "skipped_dry_run",
                "reason": "dry_run",
            },
        }
    result = _process_parent_completion(
        parent_issue_number,
        forge or GitHubForge(),
        final_acceptance_review,
        final_acceptance_reviewer,
    )
    result.setdefault(
        "final_acceptance_review",
        {"status": "not_applicable", "reason": result["status"]},
    )
    return result


def _process_parent_completion(
    parent: int, forge: Forge, mode: str, reviewer: str | None
) -> dict:
    if unsafe_result := _migrate_legacy_final_pr_before_completion(parent, forge):
        return unsafe_result
    discovered = find_children_by_parent(forge, parent).issues
    children = sorted(
        {
            child.number: child for child in discovered if child.number != parent
        }.values(),
        key=lambda child: child.number,
    )
    # Reopened/added children take precedence over historical merged-PR records.
    open_children = [child.number for child in children if child.state != "CLOSED"]
    if open_children:
        mark_parent_final_pr_pending(parent, children, forge)
        return {"status": "waiting_on_children", "open_children": open_children}
    if is_current_parent_branch_merged(f"parent/issue-{parent}", parent, forge):
        return _close_merged_parent(parent, forge)
    if children:
        return _prepare_final_pr(parent, children, forge, mode, reviewer)
    return {
        "status": "waiting_on_children",
        "open_children": [],
        "final_acceptance_review": {
            "status": "not_applicable",
            "reason": "no_children",
        },
    }


def _close_merged_parent(parent: int, forge: Forge) -> dict:
    if forge.get_issue_state(parent) != "OPEN":
        return {"status": "already_closed"}
    forge.close_issue(
        parent,
        "completed",
        comment=(
            f"親ブランチ `parent/issue-{parent}` からmainへの最終PRのマージを"
            "検知したため、このIssueを自動的にクローズしました。"
        ),
    )
    return {"status": "parent_closed", "parent_issue_number": parent}


def _prepare_final_pr(
    parent: int,
    children: Sequence[IssueRecord],
    forge: Forge,
    mode: str,
    reviewer: str | None,
) -> dict:
    summaries = collect_child_summaries(forge, parent, children)
    try:
        pr = ensure_parent_final_pr(
            parent, forge=forge, children=children, summaries=summaries
        )
    except ParentFinalPrMigrationError as error:
        return _unsafe_parent_final_pr_result(parent, error)
    result = {"status": "final_pr_ready", "pr_number": pr}
    review: dict
    if pr is None:
        review = {"status": "not_applicable", "reason": "pr_unavailable"}
    elif mode == "off":
        review = {"status": "skipped_off", "reason": "off"}
    elif reviewer not in {"claude", "codex"}:
        review = {"status": "reviewer_unresolved", "reason": "reviewer_unresolved"}
    else:
        try:
            review = request_final_acceptance_review(
                forge=forge,
                parent_issue_number=parent,
                pr_number=pr,
                children=children,
                summaries=summaries,
                reviewer=reviewer,
            )
        except Exception as error:
            # An advisory failure cannot discard the already prepared final PR.
            print(f"Warning: final acceptance review #{pr}: {error}", file=sys.stderr)
            review = {"status": "failed", "reason": "request_failed"}
    return {**result, "final_acceptance_review": review}

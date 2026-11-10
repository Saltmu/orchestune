"""Parent completion and advisory final acceptance requests.

Only a verified final merge closes the parent; acceptance review never gates it.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from orchestune.forge import Forge, GitHubForge
from orchestune.infra.git_cli import (
    MergeProbeResult,
    MergeProbeStatus,
    probe_merge_conflicts,
)
from orchestune.integrator.final_acceptance_review import (
    request_final_acceptance_review,
)
from orchestune.integrator.final_pr_body import collect_child_summaries
from orchestune.integrator.parent_branch_state import is_current_parent_branch_merged
from orchestune.integrator.pr import (
    ParentFinalPrMigrationError,
    ensure_parent_final_pr,
    load_and_migrate_parent_final_pr,
    mark_parent_final_pr_pending,
)
from orchestune.issue_notice import post_notice_if_changed
from orchestune.issue_parsing import find_children_by_parent
from orchestune.models import IssueRecord, PrRecord

NOTICE_KIND_FINAL_PR_BASE_CONFLICT = "final-pr-base-conflict"


def render_final_pr_base_conflict_notice(
    parent: int, conflicting_files: Sequence[str]
) -> str:
    files_list = "\n".join(f"- `{f}`" for f in conflicting_files)
    return (
        f"親ブランチ `parent/issue-{parent}` と `main` の間にマージ競合が検出されたため、"
        "最終統合PRの作成を保留しました。\n\n"
        f"**競合ファイル:**\n{files_list}\n\n"
        "`main` の最新の変更を親ブランチへマージして競合を解消してください。"
    )


def render_final_pr_base_conflict_resolved_notice(parent: int) -> str:
    return (
        f"親ブランチ `parent/issue-{parent}` と `main` の間のマージ競合が解消されたことを確認しました。"
        "最終統合PRの作成を再開します。"
    )


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
) -> tuple[dict | None, tuple[PrRecord, ...] | None]:
    """Migrate before child discovery.

    The tuple is the selected PR to reuse when nothing was written. ``None``
    tells the pending marker to read the list again after a successful write.
    """
    try:
        selected, wrote = load_and_migrate_parent_final_pr(
            parent_issue_number, forge=forge
        )
    except ParentFinalPrMigrationError as error:
        return _unsafe_parent_final_pr_result(parent_issue_number, error), None
    if wrote or selected is None:
        return None, None if wrote else ()
    return None, (selected,)


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
    unsafe_result, open_prs = _migrate_legacy_final_pr_before_completion(parent, forge)
    if unsafe_result is not None:
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
        mark_parent_final_pr_pending(parent, children, forge, open_prs=open_prs)
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


def _probe_and_notify_final_pr_conflicts(
    parent: int,
    base_branch: str,
    forge: Forge,
) -> tuple[bool, MergeProbeResult]:
    parent_branch = f"parent/issue-{parent}"
    probe = probe_merge_conflicts(base_branch, parent_branch)
    if probe.status == MergeProbeStatus.conflict:
        post_notice_if_changed(
            forge,
            parent,
            NOTICE_KIND_FINAL_PR_BASE_CONFLICT,
            render_final_pr_base_conflict_notice(parent, probe.conflicting_files),
        )
        return False, probe
    if probe.status == MergeProbeStatus.clean:
        post_notice_if_changed(
            forge,
            parent,
            NOTICE_KIND_FINAL_PR_BASE_CONFLICT,
            render_final_pr_base_conflict_resolved_notice(parent),
            update_only=True,
        )
    else:  # unknown
        print(
            f"Warning: probe_merge_conflicts for parent issue #{parent} against "
            f"{base_branch!r} returned unknown: {probe.detail}",
            file=sys.stderr,
        )
    return True, probe


def _prepare_final_pr(
    parent: int,
    children: Sequence[IssueRecord],
    forge: Forge,
    mode: str,
    reviewer: str | None,
) -> dict:
    base_branch = "main"
    can_proceed, probe = _probe_and_notify_final_pr_conflicts(
        parent, base_branch, forge
    )
    if not can_proceed:
        return {
            "status": "final_pr_conflict",
            "conflicting_files": list(probe.conflicting_files),
        }

    summaries = collect_child_summaries(forge, parent, children)
    try:
        pr = ensure_parent_final_pr(
            parent, forge=forge, children=children, summaries=summaries
        )
    except ParentFinalPrMigrationError as error:
        return _unsafe_parent_final_pr_result(parent, error)
    result = {"status": "final_pr_ready", "pr_number": pr}
    if probe.status == MergeProbeStatus.unknown:
        result["base_merge_probe"] = "unknown"
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
        except Exception as error:  # noqa: BLE001 - boundary: report advisory review failure without discarding the prepared PR
            # An advisory failure cannot discard the already prepared final PR.
            print(f"Warning: final acceptance review #{pr}: {error}", file=sys.stderr)
            review = {"status": "failed", "reason": "request_failed"}
    return {**result, "final_acceptance_review": review}

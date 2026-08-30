"""run_state.json消失時・不整合時の自己修復（self-healing）処理。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from orchestune.consistency.invariants.execution import (
    EXECUTION_OBSERVATION_UNKNOWN,
    RUN_STATE_MISSING,
    WORKTREE_MISSING,
)
from orchestune.consistency.repairs.execution import COMMAND_BOOKKEEPING
from orchestune.dispatch.execution_profiles import resolve_execution_profile
from orchestune.dispatch.execution_repair import (
    command_finding_codes,
    evaluate_execution_repair_plan,
)
from orchestune.dispatch.state import ActiveWorktree, RunState
from orchestune.issue_parsing import (
    FOOTPRINT_BLOCK_PATTERN,
    parse_task_from_issue,
    recovery_counters_from_body,
)
from orchestune.models import IssueRecord, PrRecord
from orchestune.pr_link_notice import pr_matches_issue

if TYPE_CHECKING:
    from orchestune.dispatch.config import DispatcherConfig

_FORCE_SERIAL_LABEL = "status:force-serial"


def _extract_raw_subtask_id(issue: IssueRecord) -> str | None:
    """Issue本文のFootprint YAMLブロックから、素のsubtask_id（未検出ならNone）を取り出す。

    呼び出し側ごとにNone時のフォールバック方針が異なる（自己修復ブランチ名生成では
    合成IDへフォールバックするが、依存解決用マップでは未検出issueを含めない）ため、
    フォールバックを持たない共通の抽出処理として切り出している。
    """
    match = FOOTPRINT_BLOCK_PATTERN.search(issue.body)
    if not match:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    subtask_id = data.get("subtask_id")
    return str(subtask_id) if subtask_id else None


def _parse_subtask_info_from_issue(
    issue: IssueRecord,
) -> tuple[str, tuple[str, ...]]:
    """Issueの本文から subtask_id と declared_footprint を抽出する。"""
    match = FOOTPRINT_BLOCK_PATTERN.search(issue.body)
    subtask_id = _extract_raw_subtask_id(issue)
    declared_footprint: tuple[str, ...] = ()
    if match:
        try:
            data = yaml.safe_load(match.group(1))
            if isinstance(data, dict):
                footprint = data.get("footprint", [])
                if isinstance(footprint, list):
                    declared_footprint = tuple(footprint)
        except Exception:
            pass

    if not subtask_id:
        subtask_id = f"issue-{issue.number}"

    return subtask_id, declared_footprint


def _dependency_issue_numbers(
    issue: IssueRecord,
    issue_to_subtask_id: dict[int, str],
    subtask_id_to_issue_number: dict[str, int],
) -> tuple[int, ...]:
    """自己修復に使う依存Issue番号をnative関係またはYAMLから解決する。"""
    if issue.blocked_by:
        return issue.blocked_by

    task = parse_task_from_issue(issue, issue_to_subtask_id)
    return tuple(
        subtask_id_to_issue_number[subtask_id]
        for subtask_id in task.depends_on
        if subtask_id in subtask_id_to_issue_number
    )


def _restored_base_branch(
    issue: IssueRecord,
    open_prs: list[PrRecord],
    issue_to_subtask_id: dict[int, str],
    subtask_id_to_issue_number: dict[str, int],
) -> str:
    """Issueの親・依存関係から自己修復時のbase branchを決定する。"""
    base_branch = "origin/main"
    if issue.parent and issue.parent.get("number") is not None:
        base_branch = f"parent/issue-{issue.parent['number']}"

    dependency_issue_numbers = _dependency_issue_numbers(
        issue,
        issue_to_subtask_id,
        subtask_id_to_issue_number,
    )
    for pr in open_prs:
        if any(
            pr_matches_issue(pr, dep_num, issue_to_subtask_id.get(dep_num))
            for dep_num in dependency_issue_numbers
        ):
            return pr.head_ref

    return base_branch


def _recovery_counters_for_issue(issue: IssueRecord) -> tuple[int, bool]:
    """#516レビュー指摘: Issue本文（Footprintフェンス）を第一のソースとしつつ、
    `status:force-serial`ラベルが付いているのに本文フィールドが無い/false
    のケースをラベル側の権威で補う。想定されるケースは2つ:
    (1) 本フィールド導入前からforced_serialだった移行時のIssue（本文に
        フィールドが無い）、(2) `_persist_recovery_counters`の本文書き込みは
        成功したがその後の`add_label`が失敗し、次のイベントまで本文が
        更新されないまま残ったケースの逆——ここでは扱わない（本文が
        `true`で確定していればそちらが優先される）。ラベルは`recompute_count`
        を持たないためforced_serial側のみのフォールバックとする。
    """
    recompute_count, forced_serial = recovery_counters_from_body(issue.body)
    if _FORCE_SERIAL_LABEL in issue.labels:
        forced_serial = True
    return (recompute_count, forced_serial)


def _resolve_recovery_pr_and_branch(
    issue: IssueRecord,
    subtask_id: str,
    open_prs: list[PrRecord],
) -> tuple[str, str | None, str | None]:
    for pr in open_prs:
        if pr_matches_issue(pr, issue.number, subtask_id):
            return pr.head_ref, str(pr.number), f"PR#{pr.number}"
    return f"claude/issue-{issue.number}-{subtask_id}", None, None


def _build_restored_active_worktree(
    issue: IssueRecord,
    subtask_id: str,
    declared_footprint: tuple[str, ...],
    open_prs: list[PrRecord],
    issue_to_subtask_id: dict[int, str],
    subtask_id_to_issue_number: dict[str, int],
    config: DispatcherConfig,
) -> ActiveWorktree:
    recompute_count, forced_serial = _recovery_counters_for_issue(issue)
    branch_name, external_id, external_url = _resolve_recovery_pr_and_branch(
        issue, subtask_id, open_prs
    )
    slug = branch_name.replace("/", "-")
    worktree_path = Path(config.worktree_root) / slug
    restored_base = _restored_base_branch(
        issue, open_prs, issue_to_subtask_id, subtask_id_to_issue_number
    )

    task = parse_task_from_issue(issue, issue_to_subtask_id)
    execution_selection = resolve_execution_profile(
        task.execution_profile,
        config.dispatch_target,
        config.execution_profile_config,
    )

    return ActiveWorktree(
        issue_number=issue.number,
        branch=branch_name,
        worktree_path=str(worktree_path),
        pid=None,
        started_at=None,
        declared_footprint=declared_footprint,
        recompute_count=recompute_count,
        forced_serial=forced_serial,
        external_id=external_id,
        external_url=external_url,
        base_branch=restored_base,
        profile=execution_selection.profile,
        model=execution_selection.model,
        reasoning_effort=execution_selection.reasoning_effort,
        selection_reason=execution_selection.reason,
    )


def _decide_missing_active_worktrees(
    run_state: RunState,
    in_progress_issues: list[IssueRecord],
    config: DispatcherConfig,
) -> list[tuple[str, str, ActiveWorktree]]:
    issue_to_subtask_id: dict[int, str] = {}
    for issue in in_progress_issues:
        raw_subtask_id = _extract_raw_subtask_id(issue)
        if raw_subtask_id is not None:
            issue_to_subtask_id[issue.number] = raw_subtask_id
    subtask_id_to_issue_number = {
        subtask_id: issue_number
        for issue_number, subtask_id in issue_to_subtask_id.items()
    }

    tasks_by_issue = {
        issue.number: parse_task_from_issue(issue, issue_to_subtask_id)
        for issue in in_progress_issues
    }
    evaluation = evaluate_execution_repair_plan(run_state, tasks_by_issue, config)
    missing_subjects = {
        command.subject_id
        for command in evaluation.commands
        if command.code == COMMAND_BOOKKEEPING
        and RUN_STATE_MISSING in command_finding_codes(command)
    }
    if not missing_subjects:
        return []

    try:
        open_prs = config.resolved_forge.list_open_prs()
    except Exception as e:
        print(f"Self-healing warning: Failed to list open PRs: {e}", file=sys.stderr)
        open_prs = []

    restorations: list[tuple[str, str, ActiveWorktree]] = []
    for issue in in_progress_issues:
        if str(issue.number) not in missing_subjects:
            continue
        subtask_id, declared_footprint = _parse_subtask_info_from_issue(issue)
        active_worktree = _build_restored_active_worktree(
            issue,
            subtask_id,
            declared_footprint,
            open_prs,
            issue_to_subtask_id,
            subtask_id_to_issue_number,
            config,
        )
        restorations.append((str(issue.number), subtask_id, active_worktree))

    return restorations


def _apply_restore_missing_active_worktrees(
    run_state: RunState,
    restorations: list[tuple[str, str, ActiveWorktree]],
) -> bool:
    """decide層が算出した復元内容のみをrun_state.active_worktreesへ書き込む。"""
    if not restorations:
        return False

    for key, subtask_id, active_worktree in restorations:
        run_state.active_worktrees[key] = active_worktree
        print(
            f"Self-healing: Restored active worktree state for subtask '{subtask_id}' "
            f"(Issue #{active_worktree.issue_number})",
            file=sys.stderr,
        )

    return True


def _restore_missing_active_worktrees(
    run_state: RunState,
    in_progress_issues: list[IssueRecord],
    config: DispatcherConfig,
) -> bool:
    """in-progressなIssueからActiveWorktreeを復元する（decide+applyの薄いラッパー）。"""
    restorations = _decide_missing_active_worktrees(
        run_state, in_progress_issues, config
    )
    return _apply_restore_missing_active_worktrees(run_state, restorations)


def _decide_stale_recovery_counters(
    run_state: RunState,
    in_progress_issues: list[IssueRecord],
) -> list[tuple[str, int, bool]]:
    """#516再2巡目レビュー指摘: `_persist_recovery_counters`がIssue本文への
    書き込みに成功した直後、サイクル終端の`save_run_state`前にプロセスが
    停止すると、run_state.json上のActiveWorktreeは古い値のまま残る。この
    エントリは`active_worktrees`に既に存在する（`_decide_missing_active_worktrees`
    の対象外）ため、次回起動時も永久にstaleなまま——本文の方が進んでいる
    のに古い値で強制直列化が解除されたままになりうる。

    本文/ラベル側の値が現在のrun_state側の値より「安全な方向」へ進んでいる
    場合のみ反映する（recompute_countは大きい方、forced_serialはtrueが勝つ）。
    逆方向（本文側の書き込みがまだ追いついていないだけの一時的なラグ）で
    run_state側の進捗を巻き戻すことは絶対にしない——それ自体がforced_serial
    フォールバックを不安定にしうるため。
    """
    issues_by_number = {issue.number: issue for issue in in_progress_issues}
    reconciliations: list[tuple[str, int, bool]] = []
    for key, active in run_state.active_worktrees.items():
        issue = issues_by_number.get(active.issue_number)
        if issue is None:
            continue
        recompute_count, forced_serial = _recovery_counters_for_issue(issue)
        new_recompute_count = max(active.recompute_count, recompute_count)
        new_forced_serial = active.forced_serial or forced_serial
        if (
            new_recompute_count != active.recompute_count
            or new_forced_serial != active.forced_serial
        ):
            reconciliations.append((key, new_recompute_count, new_forced_serial))
    return reconciliations


def _apply_stale_recovery_counters(
    run_state: RunState,
    reconciliations: list[tuple[str, int, bool]],
) -> bool:
    """decide層が算出した反映内容のみをrun_state.active_worktreesへ書き込む。"""
    if not reconciliations:
        return False

    for key, recompute_count, forced_serial in reconciliations:
        active = run_state.active_worktrees[key]
        active.recompute_count = recompute_count
        active.forced_serial = forced_serial
        print(
            f"Self-healing: Reconciled stale recovery counters for Issue "
            f"#{active.issue_number} (recompute_count={recompute_count}, "
            f"forced_serial={forced_serial})",
            file=sys.stderr,
        )

    return True


def _reconcile_stale_recovery_counters(
    run_state: RunState,
    in_progress_issues: list[IssueRecord],
) -> bool:
    """既存active_worktreesエントリの復旧カウンタをIssue本文/ラベルと
    突き合わせて更新する（decide+applyの薄いラッパー）。"""
    reconciliations = _decide_stale_recovery_counters(run_state, in_progress_issues)
    return _apply_stale_recovery_counters(run_state, reconciliations)


def _report_reobserved_execution_findings(
    run_state: RunState,
    in_progress_issues: list[IssueRecord],
    config: DispatcherConfig,
) -> None:
    """Re-observe repaired tasks and retain unresolved/unknown facts for next cycle."""
    issue_to_subtask_id = {
        issue.number: raw
        for issue in in_progress_issues
        if (raw := _extract_raw_subtask_id(issue)) is not None
    }
    tasks_by_issue = {
        issue.number: parse_task_from_issue(issue, issue_to_subtask_id)
        for issue in in_progress_issues
    }
    evaluation = evaluate_execution_repair_plan(run_state, tasks_by_issue, config)
    for finding in evaluation.report.findings:
        if finding.subject_id is None:
            continue
        active = next(
            (
                item
                for item in run_state.active_worktrees.values()
                if str(item.issue_number) == finding.subject_id
            ),
            None,
        )
        if active is None:
            continue
        if finding.code == WORKTREE_MISSING:
            print(
                "Self-healing warning: Physical worktree for subtask "
                f"'{finding.subject_id}' not found at '{active.worktree_path}'.",
                file=sys.stderr,
            )
        elif finding.code == EXECUTION_OBSERVATION_UNKNOWN:
            print(
                "Self-healing warning: Execution repair remains deferred for Issue "
                f"#{active.issue_number}: {finding.observed.summary}",
                file=sys.stderr,
            )


def recover_run_state(
    run_state: RunState,
    in_progress_issues: list[IssueRecord],
    config: DispatcherConfig,
) -> bool:
    """run_state.jsonが失われたり不整合が起きている場合に、GitHub API (in_progress_issues / open_prs)
    およびローカルの物理的な git worktree から RunState を自動復元する。
    """
    modified = _restore_missing_active_worktrees(run_state, in_progress_issues, config)
    modified = (
        _reconcile_stale_recovery_counters(run_state, in_progress_issues) or modified
    )
    _report_reobserved_execution_findings(run_state, in_progress_issues, config)
    return modified

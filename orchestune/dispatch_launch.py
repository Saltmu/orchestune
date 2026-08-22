"""起動候補の選定（スタック可否判定・重複起動防止）と、選出タスクの実起動。"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from orchestune.dispatch_escalation import apply_human_review_escalation
from orchestune.dispatch_labels import transition_status_label
from orchestune.dispatch_scoring import Task, parse_task_from_issue
from orchestune.dispatch_state import ActiveWorktree, RunState, save_run_state
from orchestune.dispatch_worktree import create_worktree_and_launch
from orchestune.git_cli import run_git
from orchestune.issue_parsing import (
    backfill_launch_history,
    launch_history_from_body,
    launch_history_in_window,
)
from orchestune.models import IssueRecord, PrRecord

if TYPE_CHECKING:
    from orchestune.dispatch_config import DispatcherConfig
    from orchestune.dispatch_rules import CycleContext


def _get_stack_eligible_tasks(
    blocked_issues: list[IssueRecord],
    tasks_by_issue: dict[int, Task],
    done_subtask_ids: set[str],
    ci_passed_pr_subtask_ids: set[str],
    subtask_branch_map: dict[str, str],
    completed_subtask_ids: set[str] | None = None,
) -> tuple[list[Task], dict[int, str]]:
    stack_eligible_tasks = []
    task_to_base_branch = {}
    resolved_grand_deps = done_subtask_ids | (completed_subtask_ids or set())

    for issue in blocked_issues:
        task = tasks_by_issue.get(issue.number) or parse_task_from_issue(issue)
        if not task.subtask_id or not task.depends_on:
            continue

        # #381レビュー対応(Codex P2): stacked launch成功時のtransition_status_labelが
        # add(status:in-progress)後のremove(status:blocked)に失敗すると、Issueが
        # status:blocked/status:in-progressを同時に持つ中断状態のまま残りうる。
        # status:in-progressは「実際にactive_worktreesへ実起動済み」の確定
        # シグナルであり、この状態のIssueを新たなstack候補として扱うと、稼働中
        # セッションが既に開いたPRを重複起動と誤認しstatus:blocked-human-review
        # へ誤ってエスカレーションしうる。status:in-progressの除去が完了する
        # まで候補から除外する。
        if "status:in-progress" in task.status_labels:
            continue

        # GitHub native blocked_by のうちの一部がマップ未存在で task.depends_on から欠落している場合は
        # 未解決の blocker が存在するため fail closed (スタッキング対象外) とする
        if issue.blocked_by and len(task.depends_on) < len(issue.blocked_by):
            continue

        all_resolved_or_stackable = True
        stackable_deps = []
        for dep in task.depends_on:
            if dep in done_subtask_ids:
                continue
            elif dep in ci_passed_pr_subtask_ids:
                dep_task = None
                for t in tasks_by_issue.values():
                    if t.subtask_id == dep:
                        dep_task = t
                        break
                if dep_task:
                    if not all(
                        grand_dep in resolved_grand_deps
                        for grand_dep in dep_task.depends_on
                    ):
                        all_resolved_or_stackable = False
                        break
                stackable_deps.append(dep)
            else:
                all_resolved_or_stackable = False
                break

        # スタッキング可能な未マージ依存先が「ちょうど1つ」の場合のみスタッキング起動を許可する
        # （複数ある場合は、両方の変更をベースブランチとして同時に取り込めないためマージされるまでブロックする）
        if all_resolved_or_stackable and len(stackable_deps) == 1:
            stack_eligible_tasks.append(task)
            dep = stackable_deps[0]
            task_to_base_branch[task.issue_number] = subtask_branch_map[dep]

    return stack_eligible_tasks, task_to_base_branch


@dataclass
class DuplicateCandidateDecision:
    task: Task
    is_duplicate: bool
    existing_pr: PrRecord | None = None


def _is_orchestune_issue_branch(head_ref: str, issue_number: int) -> bool:
    """PR本文のCloses一致フォールバックをOrchestune由来らしいブランチに限定する。"""
    return head_ref.startswith(f"claude/issue-{issue_number}-")


def _decide_duplicate_candidates(
    candidate_tasks: list[Task],
    ctx: CycleContext,
) -> list[DuplicateCandidateDecision]:
    """既にオープンなPRが存在するcandidate_tasksを検知し、重複起動かどうかを判断する
    （githubラベル等の書き換えは行わない。`git ls-remote`は読み取り専用）。

    `ctx`（`orchestune.dispatch_cycle.CycleContext`）に1サイクル分の読み取り専用
    データ（`pr_by_branch`/`prs`/`run_state`）をまとめて渡すことで、新しい判断
    パターンが追加のデータを必要とする場合の引数伝播を`ctx`への1フィールド追加
    に閉じ込める（#86）。
    """
    decisions = []
    for task in candidate_tasks:
        expected_branch = f"claude/issue-{task.issue_number}-{task.subtask_id}"

        existing_pr = ctx.pr_by_branch.get(expected_branch)
        if not existing_pr:
            for pr in ctx.prs:
                if (
                    task.issue_number in pr.closes_issue_numbers
                    and _is_orchestune_issue_branch(pr.head_ref, task.issue_number)
                ):
                    existing_pr = pr
                    break

        is_duplicate = False
        if existing_pr:
            # 重複起動をスキップする条件の判定
            last_completed = None
            for cw in reversed(ctx.run_state.completed_worktrees):
                if cw.issue_number == task.issue_number:
                    last_completed = cw
                    break

            if not last_completed:
                # 過去の完了履歴がないのにPRが存在する場合は、人間が作成したとみなしてスキップ
                is_duplicate = True
            else:
                # リモートブランチの最新コミットSHAを取得
                remote_sha = None
                ls_remote_failed = False
                try:
                    ref_name = f"refs/heads/{existing_pr.head_ref}"
                    res = run_git(
                        ["ls-remote", "origin", ref_name], cwd=None, check=True
                    )
                    output = res.stdout.strip()
                    if output:
                        remote_sha = output.split()[0]
                except Exception:
                    ls_remote_failed = True

                # 履歴のSHAとリモートのSHAが両方取得でき、かつそれらが異なる場合のみ重複（人間介入）と判定。
                # ただし、ls-remoteが例外等で失敗した場合は、安全のため重複とみなして起動をスキップする。
                if ls_remote_failed:
                    is_duplicate = True
                elif last_completed.commit_sha and remote_sha:
                    if last_completed.commit_sha != remote_sha:
                        is_duplicate = True

        decisions.append(
            DuplicateCandidateDecision(
                task=task, is_duplicate=is_duplicate, existing_pr=existing_pr
            )
        )
    return decisions


def _apply_duplicate_skip(
    decisions: list[DuplicateCandidateDecision],
    ctx: CycleContext,
) -> list[Task]:
    """decide層が判定した重複候補をstatus:blocked-human-reviewへ遷移させ、
    重複でないタスクのみを起動候補として返す。"""
    valid_candidate_tasks = []
    for decision in decisions:
        task = decision.task
        if decision.is_duplicate and decision.existing_pr:
            existing_pr = decision.existing_pr
            print(
                f"Skipping task {task.subtask_id} (Issue #{task.issue_number}) because an open PR #{existing_pr.number} already exists on branch '{existing_pr.head_ref}' and has been updated.",
                file=sys.stderr,
            )
            if ctx.config.apply:
                apply_human_review_escalation(
                    task.issue_number,
                    task.status_labels,
                    f"重複起動防止: このサブタスクに対応するオープンなPR #{existing_pr.number} (ブランチ: `{existing_pr.head_ref}`) が既に検出され、更新されています。\n"
                    f"重複したエージェントセッションの起動を防ぐため、自動起動をスキップし、ステータスを `status:blocked-human-review` に変更しました。\n"
                    f"必要に応じて手動でPRをマージするか、再起動したい場合は既存のPRをクローズした上で再度 `status:queued` に設定してください。",
                    forge=ctx.config.resolved_forge,
                )
        else:
            valid_candidate_tasks.append(task)
    return valid_candidate_tasks


@dataclass
class TaskLaunchPlan:
    task: Task
    branch_name: str
    base_branch_for_launch: str | None
    base_branch_for_state: str


def _decide_yaml_error_tasks(candidate_tasks: list[Task]) -> list[Task]:
    """YAMLパースに失敗しているタスクを判定する（副作用なし）。"""
    return [task for task in candidate_tasks if task.yaml_error]


def _apply_yaml_error_blocking(
    yaml_error_tasks: list[Task], config: DispatcherConfig
) -> None:
    for task in yaml_error_tasks:
        transition_status_label(
            config.resolved_forge,
            task.issue_number,
            "status:blocked",
            ("status:queued",),
        )
        config.resolved_forge.add_comment(
            task.issue_number,
            "YAMLのパースに失敗したため、タスクをブロックしました。フォーマットを確認してください。",
        )


def _decide_task_launch_plan(
    selected: list[Task],
    task_to_base_branch: dict[int, str],
    config: DispatcherConfig,
) -> list[TaskLaunchPlan]:
    """選出されたタスクごとに、起動時のブランチ名・ベースブランチを決定する（副作用なし）。"""
    plans = []
    for task in selected:
        branch_name = f"claude/issue-{task.issue_number}-{task.subtask_id or 'task'}"
        base_branch = task_to_base_branch.get(task.issue_number)
        if base_branch is None:
            if config.parent_issue_number is not None:
                base_branch_for_launch = f"parent/issue-{config.parent_issue_number}"
                base_branch_for_state = base_branch_for_launch
            else:
                base_branch_for_launch = None
                base_branch_for_state = "origin/main"
        else:
            base_branch_for_launch = base_branch
            base_branch_for_state = base_branch

        plans.append(
            TaskLaunchPlan(
                task=task,
                branch_name=branch_name,
                base_branch_for_launch=base_branch_for_launch,
                base_branch_for_state=base_branch_for_state,
            )
        )
    return plans


def _persist_launch_history(now: float, config: DispatcherConfig) -> None:
    """#514: 今回の起動タイムスタンプを親Issue本文へ**起動前に**追記する
    （スロットの予約）。呼び出し元は例外を捕捉して起動を見送る（fail-closed）。

    `run_state.json`が消えるステートレスCIランナーでは`launch_history`が
    毎回空になり、`max_launches_per_window`が実質無効になる。復元側は
    `dispatch_reconciliation._restore_launch_history`。

    スコープ（Issue #514の決定）: `--parent-issue`未指定（フラットモード）は
    永続化先の親Issueが無いため対象外。

    #519レビュー指摘(P2)の明確化: **永続化ストアだけが親ごと**（親Issue本文が
    唯一の置き場所のため）であって、実行時のクオータ判定（`quota_available`）は
    `run_state.launch_history`をグローバルに数える既存挙動のまま——本PRはそこを
    変更しない。真の意味での「親ごとのクオータ」にするには`quota_available`側の
    変更が要り、それは複数親を永続ディスクで運用している既存利用者の束縛を
    緩める挙動変更になるため、#514のスコープ外とする。

    #519レビュー指摘(P2): 基準にするのは`run_state.launch_history`ではなく
    **その親Issue自身の既存履歴**。`run_state.json`は複数の親（big rock）を
    またいで共有されるため、グローバル履歴をそのまま書くと親Aの起動が親Bの
    永続履歴へ混入し、Bのランナーがステートレスになった後もBを絞り続ける。
    ここで確実に「この親のもの」と分かるのは、今まさに起動した`now`だけ。

    重複タイムスタンプは畳まない: 同一サイクルで複数タスクが起動すると
    いずれも同じ`now`を持つが、それらは別々の起動を表す正当なデータのため
    （#519レビュー指摘(P1)）。

    ウィンドウ外は書かない（本文の単調肥大化を防ぐ）。`launch_history_in_window`
    は`now`の前後1ウィンドウの帯だけを残すため、本文が手で編集されて遥か未来の
    値が入っても書き戻しの時点で落ちる（#519レビュー7巡目 P2）。帯の内側の値は
    変更しない——復元側のマージはタイムスタンプ値を同一性のキーにしているため
    （#519レビュー8巡目 P2）。値が既に一致していれば
    `backfill_launch_history`が`None`を返すため、無駄な`update_issue_body`
    呼び出しは発生しない。

    **単一ランナー前提（#377と同じ制約）**: この読み取り→更新は`gh issue edit`
    による本文全体の上書きで、条件付き更新（CAS）の手段が無い。同一の親Issueへ
    複数のランナーが同時にディスパッチすると、後勝ちで先のランナーの予約が
    消え、双方が起動しうる。`run_dispatch_cycle`の`file_lock`は同一ファイル
    システム内のプロセス間ロックであり、CIランナーをまたいだ直列化には効かない。

    これは本機能が持ち込んだ制約ではなく、`architecture.md`の設計前提（#377）
    が既に述べている通り、ディスパッチャ全体が単一ランナー上でのシリアル実行を
    前提としていることによる（そもそも本機能以前は`launch_history`が
    `run_state.json`にしか無く、クロスランナーでは上限が全く効かなかった）。
    複数ランナー構成では、同前提が推奨する`concurrency`グループの設定で
    直列化すること——コード変更を伴わずに根本解決できる唯一の手段である。
    """
    if config.parent_issue_number is None:
        return
    issue = config.resolved_forge.get_issue(config.parent_issue_number)
    if issue is None:
        return
    merged = launch_history_in_window(
        launch_history_from_body(issue.body), now, config.window_seconds
    )
    merged.append(now)
    patched_body = backfill_launch_history(issue.body, sorted(merged))
    if patched_body is not None:
        config.resolved_forge.update_issue_body(
            config.parent_issue_number, patched_body
        )


def _release_launch_reservation(now: float, config: DispatcherConfig) -> None:
    """#519レビュー4巡目(P2): `_persist_launch_history`で確保した予約を1件分
    取り消す。

    予約が守っているのは**エージェントのクオータ**なので、エージェントが
    1つも起動しなかった決定論的な失敗（不正なブランチ名・worktree作成失敗等）
    では解放しなければならない。既定(`max_launches_per_window=1`)では、
    こうした失敗1件が同じ親配下の全タスクを1ウィンドウぶんブロックしてしまう。

    多重集合の意味論を保つため、取り消すのは`now`の出現1回分のみ（同一サイクルで
    複数タスクが起動すると同じ`now`が複数入るため、全部消してはいけない）。

    解放自体の失敗は非致命的に握る: 残るのは過大計上＝上限が厳しくなる方向で、
    窓から抜ければ自然回復するため（予約の失敗とは非対称で、あちらは
    過少計上＝危険側なのでfail-closedにしている）。
    """
    if config.parent_issue_number is None:
        return
    try:
        issue = config.resolved_forge.get_issue(config.parent_issue_number)
        if issue is None:
            return
        remaining = launch_history_from_body(issue.body)
        if now not in remaining:
            return
        remaining.remove(now)
        patched_body = backfill_launch_history(issue.body, sorted(remaining))
        if patched_body is not None:
            config.resolved_forge.update_issue_body(
                config.parent_issue_number, patched_body
            )
    except Exception as e:
        print(
            f"Warning: failed to release the launch reservation in parent issue "
            f"#{config.parent_issue_number}: {e}",
            file=sys.stderr,
        )


@contextmanager
def _launch_reservation(
    now: float, config: DispatcherConfig
) -> Iterator[Callable[[], None]]:
    """#568: 親Issueの起動履歴予約を構造的に管理するコンテキストマネージャ。

    起動試行前に予約を書き込み、起動が成功して`commit()`が明示的に呼ばれなかった場合は、
    例外送出や`continue`による脱出時にも`finally`節で確実に予約を解放する。
    これにより、報告処理（`transition_status_label`等）のforge例外やコード配置順序の
    変更による予約漏れを構造的に防止する。
    """
    _persist_launch_history(now, config)
    committed = False

    def commit() -> None:
        nonlocal committed
        committed = True

    try:
        yield commit
    finally:
        if not committed:
            _release_launch_reservation(now, config)


def _apply_task_launches(
    plans: list[TaskLaunchPlan],
    run_state: RunState,
    now: float,
    config: DispatcherConfig,
    open_prs: Sequence[PrRecord] | None = None,
) -> list[Task]:
    """decide層が立てた起動計画に基づき、worktree作成・エージェント（LLM）の実起動
    ・run_state/githubラベルの更新を行う。

    #225レビュー対応: ループ内・末尾のsave_run_state呼び出しには必ず
    config.window_seconds/open_prsを渡す。ここで渡さないと、起動ループ途中の
    プロセス異常終了時にディスクへ残る中間状態がデフォルトの24時間窓・
    無保護のまま刈り込まれ、レート制限誤判定や重複起動誤判定を再発させる
    （最終的な_finalize_launch側の再保存で通常は上書きされて隠れてしまう）。
    """
    actually_selected = []
    for plan in plans:
        task = plan.task
        assert config.dispatch_target is not None

        # #514/#519レビュー2巡目(P1): レート制限の永続化は「使う前に予約する」
        # 順序で行う。起動後に書いて失敗を握り潰すと、ステートレスランナーでは
        # 「エージェントは起動済みだが、親Issueにも（消えた）run_stateにも記録が
        # 無い」状態が残り、次サイクルが同じ窓の中でもう1件起動できてしまう——
        # この永続化がdurableにしようとしている当の上限が破れる。
        #
        # 予約後・起動前にクラッシュした場合は「記録はあるが起動していない」に
        # なるが、これは上限が厳しくなる方向であり、窓から抜ければ自然に回復する
        # （安全側の非対称）。予約に失敗したタスクは起動せずqueuedのまま残し、
        # 次サイクルで再試行する（fail-closed）。
        #
        # #568: 予約成功後の起動試行・解放ライフサイクルを try/finally で保護し、
        # 起動未完了時の解放（_release_launch_reservation）を構造的に保証する。
        try:
            _persist_launch_history(now, config)
        except Exception as e:
            print(
                f"Warning: skipping launch of issue #{task.issue_number}: failed to "
                f"reserve a launch slot in parent issue "
                f"#{config.parent_issue_number}: {e}",
                file=sys.stderr,
            )
            continue

        reservation_committed = False
        try:
            launch = create_worktree_and_launch(
                task,
                plan.branch_name,
                config.worktree_root,
                config.dispatch_target,
                apply=True,
                base_branch=plan.base_branch_for_launch,
            )
            if not launch.launched:
                # エージェントは1つも起動していないためクオータを消費していない。
                # 予約は finally 節で自動的に解放される（#568）。
                # GitHubへの報告処理がforgeエラーで例外を送出しても、finally により
                # 解放が確実に実行される。
                old_labels = tuple(
                    label
                    for label in ("status:queued", "status:blocked")
                    if label in task.status_labels
                )
                if launch.validation_error:
                    transition_status_label(
                        config.resolved_forge,
                        task.issue_number,
                        "status:blocked-human-review",
                        old_labels,
                    )
                    config.resolved_forge.add_comment(
                        task.issue_number,
                        f"ブランチ名またはsubtask_idが不正なため、タスクをブロックしました (`status:blocked-human-review`)。\n"
                        f"エラー内容:\n```\n{launch.error_message}\n```",
                    )
                else:
                    transition_status_label(
                        config.resolved_forge,
                        task.issue_number,
                        "status:blocked",
                        old_labels,
                    )
                    config.resolved_forge.add_comment(
                        task.issue_number,
                        f"Git worktreeの作成またはエージェントの起動に失敗しました。\n"
                        f"エラー内容:\n```\n{launch.error_message}\n```",
                    )
                continue

            reservation_committed = True

            # run_stateへの登録・永続化を先に行い、GitHubラベルの更新は後で行う。
            # 起動(create_worktree_and_launch)は既に成功しているため、この順序なら
            # この後でクラッシュしても「run_stateには記録済みだがGitHubラベルは
            # まだstatus:queuedのまま」という、次回サイクルの冒頭でラベルを見て
            # 機械的に検出・破棄できる非対称にしかならない（逆順だと「GitHub側は
            # 確定・run_state側は空」という検出不能な非対称になってしまう）。
            # #262レビュー対応: サイクル共通の`now`やworktree準備（prune/backup/add）
            # 開始前の時刻を`started_at`に使うと、実際の起動（cloud routine fire等）
            # より前の時刻になり、その間に作成された無関係な既存PRを新sessionの
            # 成果物と誤認しうる。`create_worktree_and_launch`が
            # dispatch_target.launch()呼び出し直前に取得した時刻を使う。
            run_state.active_worktrees[str(task.issue_number)] = ActiveWorktree(
                issue_number=task.issue_number,
                branch=plan.branch_name,
                worktree_path=launch.worktree_path,
                pid=launch.pid,
                started_at=launch.dispatch_started_at or now,
                declared_footprint=task.footprint,
                external_id=launch.external_id,
                external_url=launch.external_url,
                base_branch=plan.base_branch_for_state,
            )
            run_state.launch_history.append(now)
            save_run_state(
                run_state,
                config.run_state_path,
                now=now,
                launch_window_seconds=config.window_seconds,
                open_prs=open_prs,
            )

            # #381: status:in-progressを先にadd、旧ラベルは後でremoveする。この順序
            # なら、ラベル更新の途中でクラッシュしてもIssueは必ずいずれかの
            # status:*ラベルを持ち続ける。
            transition_status_label(
                config.resolved_forge,
                task.issue_number,
                "status:in-progress",
                (
                    label
                    for label in ("status:queued", "status:blocked")
                    if label in task.status_labels
                ),
            )

            actually_selected.append(task)
        finally:
            if not reservation_committed:
                _release_launch_reservation(now, config)

    save_run_state(
        run_state,
        config.run_state_path,
        now=now,
        launch_window_seconds=config.window_seconds,
        open_prs=open_prs,
    )
    return actually_selected


@dataclass
class LaunchContext:
    """#476: `_launch_selected_tasks`の7引数を集約するDTO。"""

    selected: list[Task]
    task_to_base_branch: dict[int, str]
    candidate_tasks: list[Task]
    run_state: RunState
    now: float
    config: DispatcherConfig
    open_prs: Sequence[PrRecord] | None = None


def _launch_selected_tasks(ctx: LaunchContext) -> list[Task]:
    """decide+applyの薄いラッパー（呼び出し互換のため維持）。"""
    yaml_error_tasks = _decide_yaml_error_tasks(ctx.candidate_tasks)
    _apply_yaml_error_blocking(yaml_error_tasks, ctx.config)

    plans = _decide_task_launch_plan(ctx.selected, ctx.task_to_base_branch, ctx.config)
    return _apply_task_launches(
        plans, ctx.run_state, ctx.now, ctx.config, open_prs=ctx.open_prs
    )

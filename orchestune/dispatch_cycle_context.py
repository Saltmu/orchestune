"""Issue取得(GitHub)から、後続の各フェーズが読み取り専用で参照する
`CycleContext`の構築までを担うコーディネーター。"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_filters import _filter_by_parent
from orchestune.dispatch_phase_reconciliation import _dispatch_not_needed_review
from orchestune.dispatch_recovery import _extract_raw_subtask_id
from orchestune.dispatch_rules import CycleContext
from orchestune.dispatch_scoring import parse_task_from_issue
from orchestune.dispatch_state import RunState
from orchestune.issue_parsing import find_children_by_parent
from orchestune.models import IssueRecord


@dataclass
class IssuesByStatus:
    """ステータスラベル別に取得したIssueの束。

    Issue取得直後は`list[IssueRecord]`が6個ばらばらのローカル変数になりがちで、
    後段で似た名前の変数を取り違えるミスを誘発しやすいため、1つの型にまとめる。
    """

    queued: list[IssueRecord]
    locked: list[IssueRecord]
    in_progress: list[IssueRecord]
    blocked: list[IssueRecord]
    done: list[IssueRecord]
    not_needed: list[IssueRecord]

    def all(self) -> list[IssueRecord]:
        return [
            *self.queued,
            *self.locked,
            *self.in_progress,
            *self.blocked,
            *self.done,
            *self.not_needed,
        ]

    def filtered_by_parent(self, parent_issue_number: int | None) -> IssuesByStatus:
        """`parent_issue_number`が指定されている場合、親Issueが一致する子Issueのみに絞る。"""
        return IssuesByStatus(
            queued=_filter_by_parent(self.queued, parent_issue_number),
            locked=_filter_by_parent(self.locked, parent_issue_number),
            in_progress=_filter_by_parent(self.in_progress, parent_issue_number),
            blocked=_filter_by_parent(self.blocked, parent_issue_number),
            done=_filter_by_parent(self.done, parent_issue_number),
            not_needed=_filter_by_parent(self.not_needed, parent_issue_number),
        )


# #512/PR#520レビュー8巡目対応(Codex P2): 台帳に残るIssueの状態を一括で解決する
# ためのラベル。`_fetch_issues`が取得しない終端ラベル——とりわけ本機能自身が付与する
# `status:blocked-human-review`——を1リクエストずつ問い合わせると、エスカレーション
# 済みのタスクが増えるほど毎サイクルのAPI呼び出しが線形に増えてしまう。
_LEDGER_BULK_LOOKUP_LABELS = (
    "status:blocked-human-review",
    "status:manual-merge-required",
)

# #512/PR#520レビュー14巡目対応(Codex P2): 一括取得の取得件数と、1サイクルあたりの
# 個別問い合わせ件数の上限。台帳には件数上限を設けていない（未完了タスクの回数を
# 失わないため）ので、解決処理側でAPI呼び出し回数を有界に保つ。
_LEDGER_BULK_LOOKUP_MIN_LIMIT = 1000
_LEDGER_DIRECT_LOOKUPS_PER_CYCLE = 50


def _rotated_lookup_batch(run_state: RunState, unresolved: set[int]) -> list[int]:
    """個別問い合わせに回す記録を、サイクルをまたいで走査位置を進めながら選ぶ。

    PR#520レビュー15巡目対応(Codex P2): 常にIssue番号の若い順で先頭N件を見ると、
    それより後ろの記録は永久に確認されない（若い番号のIssueが開いたままなら、
    その後ろでクローズされた記録の回数がいつまでも台帳に残る）。

    同16巡目対応(Codex P2): 走査位置は壁時計ではなく`run_state`のカーソルで進める。
    `int(time.time()) % 件数`だと、一定周期で起動されるディスパッチャー
    （例: 300秒cron・未解決600件）では常に同じ2箇所しか見ないという組み合わせが
    生じ、間の記録が永久に取り残される。カーソルなら起動間隔に関わらず、
    高々`ceil(件数 / 1サイクルの上限)`サイクルで全件を一巡できる。

    ここで決めるのは「クローズ確認をどの順で行うか」だけで、ディスパッチの判断
    （起動対象・エスカレーションの有無）はこの順序に依存しない——確認できなかった
    記録は保持され、次サイクル以降で確認されるだけである。
    """
    ordered = sorted(unresolved)
    if len(ordered) <= _LEDGER_DIRECT_LOOKUPS_PER_CYCLE:
        run_state.task_reclaim_lookup_cursor = 0
        return ordered
    offset = run_state.task_reclaim_lookup_cursor % len(ordered)
    rotated = ordered[offset:] + ordered[:offset]
    batch = rotated[:_LEDGER_DIRECT_LOOKUPS_PER_CYCLE]
    run_state.task_reclaim_lookup_cursor = (offset + len(batch)) % len(ordered)
    return batch


def _resolve_ledger_issue_states(
    run_state: RunState, issues: IssuesByStatus, config: DispatcherConfig
) -> dict[int, str]:
    """台帳に記録があるIssueの状態（OPEN/CLOSED）を、API呼び出しを抑えつつ解決する。

    1. サイクルが既に取得したIssue一覧（追加コストなし）
    2. 1で解決できない分は、終端ラベルのIssueをラベル単位で一括取得
       （エスカレーション済みタスクが何件あっても定数回のリクエストで済む）
    3. それでも残る分（status:*ラベルを持たない等）のみ、Issue番号を直接問い合わせる

    取得に失敗したIssueは結果に含めない（呼び出し側は記録を保持する＝安全側）。

    PR#520レビュー14巡目対応(Codex P2): 2の一括取得は台帳の記録数を賄える件数を
    明示的に要求し（既定の1000件で頭打ちにしない）、3の個別問い合わせは1サイクル
    あたり`_LEDGER_DIRECT_LOOKUPS_PER_CYCLE`件までに制限する。台帳自体は有界化して
    いない（未完了タスクの回数を失わないため）ので、ここを無制限にすると
    大規模リポジトリで毎サイクル数千回の逐次API呼び出しになり、ディスパッチ全体が
    詰まりかねない。上限に掛かって未解決のまま残った記録は保持され、次サイクル以降で
    順次確認される（記録が残る側＝安全側の遅延にすぎない）。問い合わせ対象は
    `_rotated_lookup_batch`が`run_state`のカーソルで順送りに選ぶため、特定の記録だけが
    永久に確認されないということはない。
    """
    recorded = set(run_state.task_reclaim_counts)
    states = {
        issue.number: issue.state.upper()
        for issue in issues.all()
        if issue.number in recorded
    }
    unresolved = recorded - set(states)
    for label in _LEDGER_BULK_LOOKUP_LABELS:
        if not unresolved:
            return states
        try:
            fetched = config.resolved_forge.list_issues_by_label(
                label,
                state="all",
                limit=max(_LEDGER_BULK_LOOKUP_MIN_LIMIT, len(unresolved) * 2),
            )
        except Exception as e:  # noqa: BLE001 - 解決できない分は次の手段へ
            print(
                f"Warning: could not list {label!r} issues while checking reclaim "
                f"counts: {e}",
                file=sys.stderr,
            )
            continue
        for issue in fetched:
            if issue.number in unresolved:
                states[issue.number] = issue.state.upper()
        unresolved -= set(states)
    for issue_number in _rotated_lookup_batch(run_state, unresolved):
        try:
            states[issue_number] = config.resolved_forge.get_issue_state(
                issue_number
            ).upper()
        except Exception as e:  # noqa: BLE001 - 確認できない記録は保持する
            print(
                f"Warning: could not check whether issue #{issue_number} is "
                f"closed; keeping its reclaim count: {e}",
                file=sys.stderr,
            )
    return states


def discard_reclaim_counts_for_closed_issues(
    run_state: RunState, issues: IssuesByStatus, config: DispatcherConfig
) -> list[int]:
    """#512: GitHub上でクローズ済みと確認できたIssueの回収回数を台帳から破棄する。

    PR#520レビュー6巡目対応(Codex P2): 破棄の根拠は「Issueが閉じたこと」ただ一つに
    統一する。`_finalize_completed_worktree`が完了を検知した時点（`status:done`）で
    破棄していたが、その時点ではIssueはまだ開いており、Integratorの仮マージCIが
    失敗すれば`handle_merge_failure`が同じIssueを`status:queued`へ差し戻す。
    そこで回数が0に戻っていると、「GC回収 → ワーカー完了 → 統合失敗」の繰り返しで
    `max_task_reclaims`を素通りできてしまう。クローズ済みのIssueが自動で再起動
    されることはないため、これが安全に破棄できる唯一のタイミングである。

    PR#520レビュー7巡目対応(Codex P2): 判定は取得済みのIssue一覧だけに頼らない。
    `_fetch_issues`はステータスラベル別のopen検索（`status:done`/`status:not-needed`
    のみstate="all"）であり、`status:blocked-human-review`のまま閉じられたIssueや
    `--parent-issue`の対象外のIssueは一覧に現れないため、一覧だけを根拠にすると
    それらのクローズを永久に観測できない（解決手順は`_resolve_ledger_issue_states`）。

    状態を確認できなかったエントリは保持する（安全側: 回数を失うと上限判定が
    0からやり直しになる）。この判定は毎サイクルGitHubから導出し直すため、破棄を
    ディスクへ即時永続化する必要はない（保存前に落ちても次サイクルで同じ結論に
    到達する）。

    なお、クローズ後にディスパッチサイクルが1度もその状態を観測しないまま人手で
    再オープンされた場合は、直前の回数がそのまま引き継がれる（そのIssueは常に
    OPENに見えるため）。回数が多い側へ倒れる＝上限に早く到達して人間の確認へ回る
    方向の誤差であり、終端の無い経路は生まれない。
    """
    if not run_state.task_reclaim_counts:
        return []
    states = _resolve_ledger_issue_states(run_state, issues, config)
    removed: list[int] = []
    for issue_number in sorted(run_state.task_reclaim_counts):
        if states.get(issue_number) == "CLOSED":
            del run_state.task_reclaim_counts[issue_number]
            removed.append(issue_number)
    return removed


def _group_by_status(issues: list[IssueRecord]) -> IssuesByStatus:
    """#156: `forge.list_sub_issues`が返す親Issue配下の全Issueを、
    `list_issues_by_label`のstate引数（open/all）と同じ意味論でステータス
    ラベル別に分類する（`status:done`/`status:not-needed`はclosedも含める）。"""
    queued: list[IssueRecord] = []
    locked: list[IssueRecord] = []
    in_progress: list[IssueRecord] = []
    blocked: list[IssueRecord] = []
    done: list[IssueRecord] = []
    not_needed: list[IssueRecord] = []

    for issue in issues:
        is_open = issue.state == "OPEN"
        if is_open and "status:queued" in issue.labels:
            queued.append(issue)
        if is_open and "status:external-lock" in issue.labels:
            locked.append(issue)
        if is_open and "status:in-progress" in issue.labels:
            in_progress.append(issue)
        if is_open and "status:blocked" in issue.labels:
            blocked.append(issue)
        if "status:done" in issue.labels:
            done.append(issue)
        if "status:not-needed" in issue.labels:
            not_needed.append(issue)

    return IssuesByStatus(
        queued=queued,
        locked=locked,
        in_progress=in_progress,
        blocked=blocked,
        done=done,
        not_needed=not_needed,
    )


def _fetch_issues(config: DispatcherConfig) -> IssuesByStatus:
    """ステータスラベルごとにIssueをGitHubから取得する。

    #156: `config.parent_issue_number`が指定されている場合、無関係な親配下の
    Issueまでリポジトリ全体から取得して後段で破棄する無駄を避けるため、
    `forge.list_sub_issues`による親Issue起点のfast pathを使う。
    """
    if config.parent_issue_number is not None:
        result = find_children_by_parent(
            config.resolved_forge, config.parent_issue_number
        )
        return _group_by_status(result.issues)

    return IssuesByStatus(
        queued=config.resolved_forge.list_issues_by_label("status:queued"),
        locked=config.resolved_forge.list_issues_by_label("status:external-lock"),
        in_progress=config.resolved_forge.list_issues_by_label("status:in-progress"),
        blocked=config.resolved_forge.list_issues_by_label("status:blocked"),
        # #236: 完了Issueは人間が通常のGitHub運用でCloseすることが多いため、
        # 依存解決判定はclosedなIssueも含めて検索する。
        done=config.resolved_forge.list_issues_by_label("status:done", state="all"),
        # #280: セッションがstatus:not-neededを付与すると同時にstatus:in-progressを
        # 外すため、in_progress側の一覧には現れなくなる。tasks_by_issueに含めて
        # おかないと_process_active_worktrees側で完了検知できず、依存解決からも
        # 漏れてしまう（closedなIssueもクローズ後の依存解決に必要なためstate="all"）。
        not_needed=config.resolved_forge.list_issues_by_label(
            "status:not-needed", state="all"
        ),
    )


def _build_cycle_context(
    issues: IssuesByStatus,
    run_state: RunState,
    config: DispatcherConfig,
) -> CycleContext:
    """取得済みIssue群から、後続の各ステージが読み取り専用で参照する
    `CycleContext`を組み立てる。"""
    all_issues = issues.all()

    issue_to_subtask_id: dict[int, str] = {}
    for issue in all_issues:
        sub_id = _extract_raw_subtask_id(issue)
        if sub_id:
            issue_to_subtask_id[issue.number] = sub_id

    tasks_by_issue = {
        issue.number: parse_task_from_issue(issue, issue_to_subtask_id)
        for issue in all_issues
    }
    issue_number_by_subtask_id = {
        task.subtask_id: task.issue_number
        for task in tasks_by_issue.values()
        if task.subtask_id
    }

    prs = config.resolved_forge.list_open_prs(paginate_files=True)

    done_subtask_ids = {
        task.subtask_id
        for task in tasks_by_issue.values()
        if "status:done" in task.status_labels and task.subtask_id
    }

    pr_by_branch = {pr.head_ref: pr for pr in prs}
    ci_passed_pr_subtask_ids = set()
    changes_requested_subtask_ids = set()
    subtask_branch_map = {}

    for task in tasks_by_issue.values():
        if not task.subtask_id:
            continue
        branch_name = f"claude/issue-{task.issue_number}-{task.subtask_id}"
        subtask_branch_map[task.subtask_id] = branch_name

        pr = pr_by_branch.get(branch_name)
        if pr:
            if pr.review_decision == "CHANGES_REQUESTED":
                changes_requested_subtask_ids.add(task.subtask_id)
            elif pr.is_ci_passing:
                ci_passed_pr_subtask_ids.add(task.subtask_id)

    return CycleContext(
        run_state=run_state,
        tasks_by_issue=tasks_by_issue,
        issue_number_by_subtask_id=issue_number_by_subtask_id,
        done_subtask_ids=done_subtask_ids,
        ci_passed_pr_subtask_ids=ci_passed_pr_subtask_ids,
        changes_requested_subtask_ids=changes_requested_subtask_ids,
        subtask_branch_map=subtask_branch_map,
        prs=prs,
        pr_by_branch=pr_by_branch,
        config=config,
        not_needed_review_dispatcher=_dispatch_not_needed_review,
    )

"""Integratorの仮マージCI通過後、統合コーディネーターが担う2つの役割:

1. 意味的レビュー（LLMによる統合diffのバグ検知）:
   Integratorが作成した統合PR（`temp_branch` → `base_branch`）を対象に、
   dispatcherと**同一のClaude Code汎用ルーチン**（`ORCHESTUNE_ROUTINE_ID`/
   `ORCHESTUNE_ROUTINE_TOKEN`）を起動し、DAGでは検知できない意味的バグ
   （同一のグローバル設定に対する競合する利用など）を探させる。
   レビューセッションは統合PRへ所見をコメントするのみで完結し、ラベル付与・
   マージ・Issueのクローズ等は一切行わない（fire-and-forgetで、Python側が
   その後の結果を追跡・自動実行することもない）。**最終マージは常に人間が行う。**

2. `status:not-needed`判定の独立検証（#282）:
   別のセッションが「既に要件を満たしており対応不要」と判定したIssueを、
   新規セッションが独立に再検証する。判定結果はIssueへのラベル付与
   （`not-needed-review:passed`/`failed`）でPython側へ伝え、`process_pending_not_needed_reviews()`が
   ポーリングしてIssueを決定論的にクローズする（こちらはmainへの書き込みを
   伴わないため、Python側の自動実行を維持している）。

差し戻し後の再レビューは、fireのたびに前回の指摘を記憶しない新規のClaude Code
セッションが起動されるため、判断のバイアスが自然に避けられる
（metaswarmプロジェクトの知見と整合）。
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from orchestune.bounded_limit import exceeds_limit
from orchestune.dispatch.escalation import apply_human_review_escalation
from orchestune.dispatch.targets import (
    ROUTINE_ID_ENV_VAR,
    ROUTINE_TOKEN_ENV_VAR,
    ClaudeCodeCloudRoutineDispatchTarget,
    DispatchHandle,
)
from orchestune.dispatch.worktree import file_lock
from orchestune.forge import Forge, GitHubForge
from orchestune.infra.not_needed_review_state import (
    NotNeededReviewState,
    PendingNotNeededReview,
    load_not_needed_review_state,
    save_not_needed_review_state,
)
from orchestune.labels import StatusLabel

# #282: status:not-needed判定の独立検証結果ラベル。
NOT_NEEDED_VERIFIED_LABEL = "not-needed-review:passed"
NOT_NEEDED_REJECTED_LABEL = "not-needed-review:failed"

# #282: 対応不要判定によるクローズ時、事後の可視性確保のためメンションする、
# 本リポジトリの唯一のメンテナー。
NOT_NEEDED_ATTENTION_MENTION = "@Saltmu"

# #511: どちらの結果ラベルも付かないまま保留され続けるstatus:not-needed検証
# レビューのタイムアウト秒数の既定値。`DispatcherConfig.not_needed_review_timeout_seconds`
# と同じ値（呼び出し元がconfigを経由しない直接呼び出し・テストでも有限の終端を
# 既定で持つように、独立した定数として重複させている）。
DEFAULT_NOT_NEEDED_REVIEW_TIMEOUT_SECONDS = 86400.0

# #511: タイムアウトしたstatus:not-needed検証レビューをエスカレーションする際に
# 除去すべき、対象Issueが本来まだ保持しているはずの旧ラベル。
_NOT_NEEDED_STATUS_LABEL = StatusLabel.NOT_NEEDED


class RoutineFirer(Protocol):
    """任意テキスト指示でルーチンをfireできるオブジェクト（テスト差し替え用）。"""

    def fire_text(self, text: str) -> DispatchHandle: ...


def build_review_routine_prompt(
    temp_branch: str,
    base_branch: str,
    pr_number: int,
    parent_issue_number: int | None,
    merged_subtask_ids: Sequence[str],
) -> str:
    """意味的レビューを実行させるためのルーチン指示テキストを構築する。

    再レビュー時のバイアス回避のため、過去の指摘内容は一切含めない
    （新規セッションが毎回まっさらな状態でレビューする）。
    """
    subtask_list = ", ".join(merged_subtask_ids) if merged_subtask_ids else "(不明)"
    parent_ref = f"#{parent_issue_number}" if parent_issue_number else "(親Issue不明)"
    merge_statement = (
        "本PRは統合システムのパイプラインによって自動マージ・管理されます。"
        if parent_issue_number is not None
        else "最終的なマージ判断は人間が行います。"
    )
    return (
        "あなたは複数の並列実装タスクを統合した統合PRの最終レビュアーです。\n"
        "各サブタスクの単体CIおよび仮マージCI（Ruff/Mypy/Pytest）は既に通過しています。\n\n"
        f"対象PR: #{pr_number}（ブランチ `{temp_branch}` → `{base_branch}`）\n"
        f"統合対象サブタスク: {subtask_list}\n"
        f"親Issue（参考）: {parent_ref}\n\n"
        "手順:\n"
        f"1. `git fetch origin {temp_branch}` の上で "
        f"`git diff {base_branch}...origin/{temp_branch}` の結合diffを取得する。\n"
        "2. 静的解析やテストでは検知できない『意味的バグ』のみを探す。特に:\n"
        "   - 同一のグローバル設定・共有状態・定数に対する、複数タスク間の競合する変更\n"
        "   - 一方のタスクが変更した関数シグネチャ・契約に、他方が追随できていない不整合\n"
        "   - 個々には正しいが結合すると破綻するロジック（重複した副作用・二重処理等）\n"
        f"3. 所見（問題なし、または検出した問題の具体的な説明）を "
        f"`gh pr comment {pr_number} --body-file -` でPR #{pr_number} 自身にコメントする。\n"
        "**重要な制約**: あなたはPRへのコメント投稿のみを行ってください。PRのマージ"
        "（`gh pr merge`等）、ラベル付与、Issueのクローズ、mainブランチへの直接の書き込みは"
        f"絶対に実行しないでください。{merge_statement}\n"
        "前回のレビュー内容は与えられていません。今回のdiffだけを根拠に判断してください。"
    )


def build_not_needed_review_prompt(issue_number: int, subtask_id: str) -> str:
    """#282: `status:not-needed`（対応不要）判定を独立に検証させるための
    ルーチン指示テキストを構築する。

    再レビュー時のバイアス回避のため、判定を行った側の主張以外の事前情報は
    与えず、新規セッションが自らIssue・コメント・`main`を確認して判断する。
    """
    return (
        "あなたは、別のセッションが「対応不要（既に要件を満たしている）」と"
        f"判定したGitHub Issue #{issue_number}（サブタスク: {subtask_id}）を"
        "独立に検証するレビュアーです。\n\n"
        "手順:\n"
        f"1. `gh issue view {issue_number} --comments` でIssue本文と、"
        "「対応不要」と判定した根拠のコメントを確認する。\n"
        "2. その根拠が正しいか、`main`ブランチの実際のコード・テストを確認して"
        "独立に検証する（該当コミット・ファイルが本当に存在し、要件を満たしているか）。\n"
        "3. 判定に応じて次のいずれかをGitHub上で実施する:\n"
        f"   - 根拠が妥当（本当に対応不要） → Issue #{issue_number} に "
        f"`{NOT_NEEDED_VERIFIED_LABEL}` ラベルのみを付与する"
        f'（`gh issue edit {issue_number} --add-label "{NOT_NEEDED_VERIFIED_LABEL}"`）。'
        "Issueのクローズは行わない（クローズは別のシステムが責任を持って行う）。\n"
        f"   - 根拠が不当（実際にはまだ対応が必要） → Issue #{issue_number} の"
        "ラベルを`status:not-needed`から`status:queued`へ付け替え、なぜ対応不要と"
        f"言えないのかを具体的にコメントする。あわせて`{NOT_NEEDED_REJECTED_LABEL}`"
        "ラベルを付与する。\n"
        "**重要な制約**: あなたはラベル付与・コメント・（不当時の）ラベル付け替えのみを"
        "行ってください。Issueのクローズ（`gh issue close`等）は絶対に実行しないで"
        "ください。実際のクローズは、あなたが付与したラベルを検知した別のシステムが"
        "責任を持って行います。\n"
        "前回のレビュー内容は与えられていません。今回自分で確認した内容だけを"
        "根拠に判断してください。"
    )


class IntegrationCoordinator:
    """dispatcherと同一のルーチンを起動して意味的レビューを委譲する。

    `dispatch_review()` は毎回ルーチンをfireするだけで、判定（合否ラベル付与）は
    起動されたClaude Codeセッションが担う。fireのたびに新規セッションが立つため、
    再レビュー時も前回の指摘を引き継がない。
    """

    def __init__(self, routine_firer: RoutineFirer):
        self._routine_firer = routine_firer

    def dispatch_review(
        self,
        temp_branch: str,
        base_branch: str,
        pr_number: int,
        parent_issue_number: int | None,
        merged_subtask_ids: Sequence[str],
    ) -> DispatchHandle:
        prompt = build_review_routine_prompt(
            temp_branch=temp_branch,
            base_branch=base_branch,
            pr_number=pr_number,
            parent_issue_number=parent_issue_number,
            merged_subtask_ids=merged_subtask_ids,
        )
        return self._routine_firer.fire_text(prompt)

    def dispatch_not_needed_review(
        self, issue_number: int, subtask_id: str
    ) -> DispatchHandle:
        """#282: `status:not-needed`判定を独立に検証するレビューをfireする。"""
        prompt = build_not_needed_review_prompt(issue_number, subtask_id)
        return self._routine_firer.fire_text(prompt)


def build_integration_coordinator() -> IntegrationCoordinator | None:
    """環境変数のルーチン認証情報から統合コーディネーターを構築する。

    `ORCHESTUNE_ROUTINE_ID`/`ORCHESTUNE_ROUTINE_TOKEN` が揃っていなければ `None` を
    返し、呼び出し側で意味的レビューを安全にスキップさせる。dispatcher本体では、
    既に構築済みの `ClaudeCodeCloudRoutineDispatchTarget` を直接再利用する経路も使う。
    """
    routine_id = os.environ.get(ROUTINE_ID_ENV_VAR)
    routine_token = os.environ.get(ROUTINE_TOKEN_ENV_VAR)
    if not (routine_id and routine_token):
        return None
    return IntegrationCoordinator(
        ClaudeCodeCloudRoutineDispatchTarget(routine_id, routine_token)
    )


def record_pending_not_needed_review(
    state_path: str | Path,
    issue_number: int,
    subtask_id: str,
    session_handle: DispatchHandle,
) -> None:
    """#282: `dispatch_not_needed_review`直後に呼び、後続サイクルでの
    ポーリング対象として記録する。"""
    lock_path = Path(state_path).with_suffix(".lock")
    with file_lock(lock_path):
        state = load_not_needed_review_state(state_path)
        state.pending.append(
            PendingNotNeededReview(
                issue_number=issue_number,
                subtask_id=subtask_id,
                dispatched_at=time.time(),
                session_external_id=session_handle.external_id,
                session_external_url=session_handle.external_url,
            )
        )
        save_not_needed_review_state(state, state_path)


def process_pending_not_needed_reviews(
    state_path: str | Path,
    forge: Forge | None = None,
    now: float | None = None,
    timeout_seconds: float = DEFAULT_NOT_NEEDED_REVIEW_TIMEOUT_SECONDS,
) -> dict:
    """Finalize completed or timed-out not-needed review entries safely."""
    forge = forge or GitHubForge()
    now = time.time() if now is None else now
    lock_path = Path(state_path).with_suffix(".lock")
    with file_lock(lock_path):
        state = load_not_needed_review_state(state_path)
        if not state.pending:
            return {"closed": [], "reopened": [], "timed_out": [], "still_pending": 0}

        closed_summary: list[int] = []
        reopened_summary: list[int] = []
        timed_out_summary: list[int] = []
        # #226/PR#227: クローズ＋ラベル削除（またはreopenのラベル削除）まで成功し、
        # 完了シグナルを消費し終えたエントリのissue_number。台帳から確実に除外する対象。
        consumed: set[int] = set()

        try:
            for entry in state.pending:
                labels = _get_pending_review_labels(entry.issue_number, forge)
                if labels is None:
                    continue

                _finalize_pending_review(
                    entry,
                    labels,
                    forge,
                    now,
                    timeout_seconds,
                    consumed,
                    closed_summary,
                    reopened_summary,
                    timed_out_summary,
                )
        finally:
            # #226/PR#227: 保存する台帳は「元のpending − 消費済み」で構成する。
            # 消費済み（closed/reopened）エントリのみを除外し、要再試行・処理中・未処理の
            # エントリはすべて温存する。still_pendingを積み上げて保存する方式だと、
            # BaseException（割り込み・強制終了）でループが中断した際に未処理の後続エントリを
            # 取りこぼす。逆に「一切保存しない」方式だと、割り込み前に完了シグナルを消費済みの
            # 先行エントリまで台帳へ復帰させてしまい、次サイクルで完了ラベルが無いため永久に
            # pending化する（いずれも#205と同種のリーク）。消費済みだけを除外することで、
            # 正常完了時も割り込み時も一貫して安全な台帳を残す。
            remaining = [e for e in state.pending if e.issue_number not in consumed]
            save_not_needed_review_state(
                NotNeededReviewState(pending=remaining), state_path
            )

    return {
        "closed": closed_summary,
        "reopened": reopened_summary,
        "timed_out": timed_out_summary,
        "still_pending": len(remaining),
    }


def _get_pending_review_labels(
    issue_number: int, forge: Forge
) -> tuple[str, ...] | None:
    try:
        return forge.get_issue_labels(issue_number)
    except Exception as error:  # noqa: BLE001 - transient forge failure
        print(
            f"Warning: failed to poll labels for issue {issue_number}: {error}",
            file=sys.stderr,
        )
        return None


def _finalize_pending_review(
    entry: PendingNotNeededReview,
    labels: tuple[str, ...],
    forge: Forge,
    now: float,
    timeout_seconds: float,
    consumed: set[int],
    closed: list[int],
    reopened: list[int],
    timed_out: list[int],
) -> None:
    try:
        if NOT_NEEDED_VERIFIED_LABEL in labels:
            _close_verified_issue(entry.issue_number, forge)
            forge.remove_label(entry.issue_number, NOT_NEEDED_VERIFIED_LABEL)
            consumed.add(entry.issue_number)
            closed.append(entry.issue_number)
        elif NOT_NEEDED_REJECTED_LABEL in labels:
            forge.remove_label(entry.issue_number, NOT_NEEDED_REJECTED_LABEL)
            consumed.add(entry.issue_number)
            reopened.append(entry.issue_number)
        elif exceeds_limit(now - entry.dispatched_at, timeout_seconds):
            _escalate_timed_out_review(
                entry.issue_number,
                labels,
                timeout_seconds,
                forge,
                on_label_applied=lambda: consumed.add(entry.issue_number),
            )
            timed_out.append(entry.issue_number)
    except Exception as error:  # noqa: BLE001 - leave entry pending for retry
        print(
            f"Warning: failed to finalize not-needed review for issue "
            f"{entry.issue_number}: {error}",
            file=sys.stderr,
        )


def _close_verified_issue(issue_number: int, forge: Forge) -> None:
    """#205: クローズ成功確定前にpassedラベルを消費しないよう、クローズだけを
    独立させたヘルパー。前サイクルで一度クローズに成功していれば（ラベル除去だけが
    失敗して再試行された場合）、二重クローズによる誤動作（コメント重複等）を
    避けるためクローズ自体は再実行しない。
    """
    if forge.get_issue_state(issue_number) == "OPEN":
        forge.close_issue(
            issue_number,
            "not planned",
            comment=(
                f"{NOT_NEEDED_ATTENTION_MENTION} "
                "独立したレビューセッションでも対応不要と確認できたため、"
                "自動的にクローズしました。誤りであれば再オープンしてください。"
            ),
        )


def _escalate_timed_out_review(
    issue_number: int,
    labels: tuple[str, ...],
    timeout_seconds: float,
    forge: Forge,
    on_label_applied: Callable[[], None],
) -> None:
    """#511: `status:not-needed`検証レビューが結果ラベルを一切返さないまま
    タイムアウトしたエントリを、`status:blocked-human-review`へ終端させる。

    除去対象は現在フェッチ済みの`labels`に実際に含まれる場合のみ渡す
    （`apply_human_review_escalation`は`current_status_labels`に含まれる
    ラベルしか除去しないため、無条件に渡しても副作用は無いが、実際に
    保持しているラベルだけを「除去対象」として明示する方が意図が正確）。
    """
    stale_status_labels = tuple(
        label for label in labels if label == _NOT_NEEDED_STATUS_LABEL
    )
    apply_human_review_escalation(
        issue_number,
        stale_status_labels,
        (
            "対応不要（`status:not-needed`）判定の独立検証レビューが、"
            f"{timeout_seconds:.0f}秒以内に結果（`{NOT_NEEDED_VERIFIED_LABEL}`/"
            f"`{NOT_NEEDED_REJECTED_LABEL}`）を返しませんでした"
            "（レビューセッションのクラッシュ等が疑われます）。\n"
            "自動判定を打ち切り、人間の確認が必要な状態へ変更しました。"
        ),
        forge=forge,
        on_label_applied=on_label_applied,
    )

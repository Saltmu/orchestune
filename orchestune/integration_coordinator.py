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
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from orchestune import github
from orchestune.dispatch_targets import (
    ROUTINE_ID_ENV_VAR,
    ROUTINE_TOKEN_ENV_VAR,
    ClaudeCodeCloudRoutineDispatchTarget,
    DispatchHandle,
)
from orchestune.not_needed_review_state import (
    NotNeededReviewState,
    PendingNotNeededReview,
    load_not_needed_review_state,
    save_not_needed_review_state,
)

# #282: status:not-needed判定の独立検証結果ラベル。
NOT_NEEDED_VERIFIED_LABEL = "not-needed-review:passed"
NOT_NEEDED_REJECTED_LABEL = "not-needed-review:failed"

# #282: 対応不要判定によるクローズ時、事後の可視性確保のためメンションする、
# 本リポジトリの唯一のメンテナー。
NOT_NEEDED_ATTENTION_MENTION = "@Saltmu"


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
        "絶対に実行しないでください。最終的なマージ判断は人間が行います。\n"
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


def process_pending_not_needed_reviews(state_path: str | Path) -> dict:
    """#282: 保留中の`status:not-needed`検証レビューをポーリングし、検証に通った
    ものはIssueを決定論的にクローズする。レビューセッション自身はクローズを
    実行しないため、この関数が「クローズしても問題ないものを実際にクローズする」
    実行主体を担う。

    - `not-needed-review:passed` を検知 → Issueをクローズし、人間へメンションした
      コメントを残す（事後の可視性確保）。ラベルを外して記録を消費する。
    - `not-needed-review:failed` を検知 → `status:queued`への差し戻しは既に
      レビューセッション自身が行っているため、Python側はラベルを外して記録を
      消費するのみ。
    - どちらのラベルもまだ無ければ、記録はそのまま保持し次サイクルで再確認する。
    """
    state = load_not_needed_review_state(state_path)
    if not state.pending:
        return {"closed": [], "reopened": [], "still_pending": 0}

    still_pending: list[PendingNotNeededReview] = []
    closed_summary: list[int] = []
    reopened_summary: list[int] = []

    for entry in state.pending:
        try:
            labels = github.get_issue_labels(entry.issue_number)
        except Exception as exc:  # noqa: BLE001 - GitHub障害でクラッシュさせない
            print(
                f"Warning: failed to poll labels for issue "
                f"{entry.issue_number}: {exc}",
                file=sys.stderr,
            )
            still_pending.append(entry)
            continue

        if NOT_NEEDED_VERIFIED_LABEL in labels:
            github.remove_label(entry.issue_number, NOT_NEEDED_VERIFIED_LABEL)
            github.close_issue(
                entry.issue_number,
                "not planned",
                comment=(
                    f"{NOT_NEEDED_ATTENTION_MENTION} "
                    "独立したレビューセッションでも対応不要と確認できたため、"
                    "自動的にクローズしました。誤りであれば再オープンしてください。"
                ),
            )
            closed_summary.append(entry.issue_number)
        elif NOT_NEEDED_REJECTED_LABEL in labels:
            github.remove_label(entry.issue_number, NOT_NEEDED_REJECTED_LABEL)
            reopened_summary.append(entry.issue_number)
        else:
            still_pending.append(entry)

    save_not_needed_review_state(
        NotNeededReviewState(pending=still_pending), state_path
    )
    return {
        "closed": closed_summary,
        "reopened": reopened_summary,
        "still_pending": len(still_pending),
    }

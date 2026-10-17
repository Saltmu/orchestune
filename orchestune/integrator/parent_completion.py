"""#170: 親Issue配下の全子Issue完了検知と、最終マージ検知後の親Issueクローズ。

子Issue→親ブランチのマージ・クローズは`integrator.AutoMergeChildIntegrationStep`が
毎サイクル自動で行う。本モジュールはその一つ上の階層、すなわち「親ブランチ→main」
という人間が行う最終マージのライフサイクルを扱う:

1. 親Issue配下の全子Issueがクローズされたら、`parent/issue-{N}` → `main`の
   最終統合PRを用意する（`ensure_parent_final_pr`）。マージするかどうかの判断は
   常に人間が行う。
2. その最終PRが実際にマージされたことを検知したら、親Issueを決定論的にクローズする。

dispatch cycleのSupervisor-owned blocked promotionと同様、永続stateは持たず、
毎サイクルGitHub APIへ冪等に問い合わせるだけで十分なため、状態ファイルは使わない。
"""

from __future__ import annotations

import sys

from orchestune.forge import Forge, GitHubForge
from orchestune.integrator.parent_branch_state import is_current_parent_branch_merged
from orchestune.integrator.pr import (
    ParentFinalPrMigrationError,
    ensure_parent_final_pr,
    mark_parent_final_pr_pending,
    migrate_open_parent_final_pr,
)
from orchestune.issue_parsing import find_children_by_parent


def _unsafe_parent_final_pr_result(
    parent_issue_number: int, error: ParentFinalPrMigrationError
) -> dict:
    """移行不能なlegacy最終PRを、待機状態へ隠さず結果として可視化する。"""
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
    """子Issue状態の判定より前に既存最終PRだけを安全化する。"""
    try:
        migrate_open_parent_final_pr(parent_issue_number, forge=forge)
    except ParentFinalPrMigrationError as error:
        return _unsafe_parent_final_pr_result(parent_issue_number, error)
    return None


def process_parent_completion(
    parent_issue_number: int, apply: bool, forge: Forge | None = None
) -> dict:
    if not apply:
        return {"status": "skipped"}

    forge = forge or GitHubForge()
    parent_branch = f"parent/issue-{parent_issue_number}"

    # #699: legacy最終PRのclosing referenceは、子Issueが追加されてopenな
    # 状態へ戻った後でも、次cycleより先に人間がマージできる。子Issue状態を
    # 待つ前に既存PRだけを安全化し、移行不能ならunsafeなPR番号を可視化する。
    if unsafe_result := _migrate_legacy_final_pr_before_completion(
        parent_issue_number, forge
    ):
        return unsafe_result

    # #255: 過去のmerged PR記録より先に現在の子Issue状態を確認する。
    # openな子Issueが1件でもあれば、親Issueを再open後に新しい作業が
    # 進行中であることを意味するため、historical記録の有無に関わらず
    # closeしてはならない。
    # #485 review (P1): a metadata-only child (native sub-issue linking was
    # unavailable when it was created) would otherwise be invisible here,
    # letting this treat the parent as already complete — creating the final
    # PR too early, or even closing the parent while that child is still open.
    children = find_children_by_parent(forge, parent_issue_number).issues
    # #802: 万一親Issue自身が子Issue一覧に混入した場合でも、完了判定を
    # 誤ってブロックしないよう自己参照（child.number == parent_issue_number）を除外する。
    open_children = [
        child.number
        for child in children
        if child.state != "CLOSED" and child.number != parent_issue_number
    ]
    if open_children:
        # #814: 最終PR作成後に子Issueが追加された場合、openな最終PRの本文が
        # 「全子Issueが完了したため」のままだと人間が誤ってマージできる。
        # legacy参照の安全化（上の#699）を済ませた後にだけ保留表示へ更新する。
        mark_parent_final_pr_pending(parent_issue_number, children, forge)
        return {"status": "waiting_on_children", "open_children": open_children}

    if is_current_parent_branch_merged(parent_branch, parent_issue_number, forge):
        if forge.get_issue_state(parent_issue_number) == "OPEN":
            forge.close_issue(
                parent_issue_number,
                "completed",
                comment=(
                    f"親ブランチ `{parent_branch}` からmainへの最終PRのマージを"
                    "検知したため、このIssueを自動的にクローズしました。"
                ),
            )
            return {
                "status": "parent_closed",
                "parent_issue_number": parent_issue_number,
            }
        return {"status": "already_closed"}

    if children:
        # #681: 一覧テーブルの生成に必要な子Issueは既に取得済みなので、
        # `find_children_by_parent`を再実行させずそのまま渡す。
        try:
            pr_number = ensure_parent_final_pr(
                parent_issue_number, forge=forge, children=children
            )
        except ParentFinalPrMigrationError as error:
            return _unsafe_parent_final_pr_result(parent_issue_number, error)
        return {"status": "final_pr_ready", "pr_number": pr_number}

    return {"status": "waiting_on_children", "open_children": []}

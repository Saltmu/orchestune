"""#170: 親Issue配下の全子Issue完了検知と、最終マージ検知後の親Issueクローズ。

子Issue→親ブランチのマージ・クローズは`integrator.AutoMergeChildIntegrationStep`が
毎サイクル自動で行う。本モジュールはその一つ上の階層、すなわち「親ブランチ→main」
という人間が行う最終マージのライフサイクルを扱う:

1. 親Issue配下の全子Issueがクローズされたら、`parent/issue-{N}` → `main`の
   最終統合PRを用意する（`ensure_parent_final_pr`）。マージするかどうかの判断は
   常に人間が行う。
2. その最終PRが実際にマージされたことを検知したら、親Issueを決定論的にクローズする。

`_promote_blocked_tasks`（`dispatch_cycle.py`）と同様、永続stateは持たず、
毎サイクルGitHub APIへ冪等に問い合わせるだけで十分なため、状態ファイルは使わない。
"""

from __future__ import annotations

import subprocess
import sys

from orchestune import github
from orchestune.integrator_pr import ensure_parent_final_pr


def _is_current_parent_branch_merged(parent_branch: str) -> bool:
    """#255: historical merged PR記録（`is_branch_merged_into`）だけでは、
    親Issueを再openしてparent branchへ新commitを積む・branchを同名で
    再作成するケースを区別できない（branch名一致だけの過去のPR記録は
    そのまま残り続けるため）。まず安価な事前フィルタとしてhistorical記録の
    有無を見て、記録があれば現在のbranch tip SHAを`is_current_branch_tip_merged_into`
    で再検証する。

    - 現在のtipがmainへ含まれない（再open後の新commit・branch再作成）:
      Falseを返す。
    - branch自体が削除済み（404）: 最終マージ後の正規のクリーンアップと
      みなし、historical記録の通りTrueを返す（`is_branch_merged_into`が
      branch削除後も有効であることを保証する設計を維持する）。
    - それ以外の理由でtip検証自体が失敗: fail closedとしてFalseを返す
      （次cycleで再試行される）。
    """
    if not github.is_branch_merged_into(parent_branch, "main"):
        return False

    try:
        return github.is_current_branch_tip_merged_into(parent_branch, "main")
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").lower()
        if "404" in stderr or "not found" in stderr:
            return True
        print(
            f"Warning: Failed to verify current tip of parent branch "
            f"'{parent_branch}': {e}",
            file=sys.stderr,
        )
        return False
    except ValueError as e:
        print(
            f"Warning: Failed to verify current tip of parent branch "
            f"'{parent_branch}': {e}",
            file=sys.stderr,
        )
        return False


def process_parent_completion(parent_issue_number: int | None, apply: bool) -> dict:
    if parent_issue_number is None or not apply:
        return {"status": "skipped"}

    parent_branch = f"parent/issue-{parent_issue_number}"

    # #255: 過去のmerged PR記録より先に現在の子Issue状態を確認する。
    # openな子Issueが1件でもあれば、親Issueを再open後に新しい作業が
    # 進行中であることを意味するため、historical記録の有無に関わらず
    # closeしてはならない。
    children = github.list_sub_issues(parent_issue_number)
    open_children = [child.number for child in children if child.state != "CLOSED"]
    if open_children:
        return {"status": "waiting_on_children", "open_children": open_children}

    if _is_current_parent_branch_merged(parent_branch):
        if github.get_issue_state(parent_issue_number) == "OPEN":
            github.close_issue(
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
        pr_number = ensure_parent_final_pr(parent_issue_number)
        return {"status": "final_pr_ready", "pr_number": pr_number}

    return {"status": "waiting_on_children", "open_children": []}

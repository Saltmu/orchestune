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

from orchestune import github
from orchestune.integrator_pr import ensure_parent_final_pr


def process_parent_completion(parent_issue_number: int | None, apply: bool) -> dict:
    if parent_issue_number is None or not apply:
        return {"status": "skipped"}

    parent_branch = f"parent/issue-{parent_issue_number}"

    if github.is_branch_merged_into(parent_branch, "main"):
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

    children = github.list_sub_issues(parent_issue_number)
    if children and all(child.state == "CLOSED" for child in children):
        pr_number = ensure_parent_final_pr(parent_issue_number)
        return {"status": "final_pr_ready", "pr_number": pr_number}

    open_children = [child.number for child in children if child.state != "CLOSED"]
    return {"status": "waiting_on_children", "open_children": open_children}

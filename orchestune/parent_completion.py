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


def _is_current_parent_branch_merged(
    parent_branch: str, parent_issue_number: int
) -> bool:
    """#255: historical merged PR記録だけでは、親Issueを再openしてparent
    branchへ新commitを積む・branchを同名で再作成するケースを区別できない
    （branch名一致だけの過去のPR記録はそのまま残り続けるため）。

    - まず`get_merged_pr_timestamp`でhistorical記録の有無と`mergedAt`を得る。
      記録が無ければ未マージ。
    - #276レビュー対応(P1): 記録があっても、親Issueの直近の再open時刻以前に
      マージされたものであれば、再open後にまだ何もマージされていない
      ことを意味するため信頼しない（Falseを返す）。「現在の子Issue状態」
      「現在のbranch tip内容」だけを見る判定では、再open直後（新しい
      子Issue/commitがまだ無い状態）で即座に再closeしてしまう窓を防げない。
      GitHubの`mergedAt`/イベント`created_at`は秒精度のため、同一秒に
      記録されるケース（境界判別不能）も「reopen後にマージされた証拠が
      ない」としてfail closedに扱う（`<=`で比較する）。
    - 上記を通過した記録があれば、現在のbranch tip SHAを
      `is_current_branch_tip_merged_into`で再検証する。
      - 現在のtipがmainへ含まれない（再open後の新commit・branch再作成）:
        Falseを返す。
      - #276レビュー対応(P2): 検証自体が例外で失敗した場合、
        `is_current_branch_tip_merged_into`内部はbranch存在確認とcompare
        呼び出しの2段階のAPIコールを行うため、どちらの404かをこの例外
        だけからは区別できない。`branch_exists`で別途branch存在を確認し、
        真に存在しないと確認できた場合のみ、最終マージ後の正規の
        クリーンアップとみなしてhistorical記録を信頼する。branchが
        存在するのに検証が失敗した場合、またはbranch存在確認自体が
        失敗した場合はfail closedとしてFalseを返す。
    """
    merged_at = github.get_merged_pr_timestamp(parent_branch, "main")
    if merged_at is None:
        return False

    reopened_at = github.get_issue_last_reopened_at(parent_issue_number)
    # #276レビュー対応: GitHubのmergedAt/イベントcreated_atは秒精度のため、
    # 最終マージによるcloseとその直後のreopenが同一秒に記録されうる。
    # 要件は「mergeが直近のreopenより後に発生したこと」の確認であり、
    # 同値（境界が判別できない）はreopen後にマージされた証拠にならない
    # ため、`<=`でfail closedにする。
    if reopened_at is not None and merged_at <= reopened_at:
        return False

    try:
        return github.is_current_branch_tip_merged_into(parent_branch, "main")
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or "").lower()
        if "404" in detail or "not found" in detail:
            try:
                branch_still_exists = github.branch_exists(parent_branch)
            except Exception as branch_check_error:
                print(
                    f"Warning: Failed to verify existence of parent branch "
                    f"'{parent_branch}': {branch_check_error}",
                    file=sys.stderr,
                )
                return False
            if not branch_still_exists:
                return True
            print(
                f"Warning: Tip verification for parent branch '{parent_branch}' "
                f"failed with a not-found response, but the branch still "
                f"exists; treating as unmerged: {e}",
                file=sys.stderr,
            )
            return False
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

    if _is_current_parent_branch_merged(parent_branch, parent_issue_number):
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

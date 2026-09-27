"""Common launch instructions, branch verification, and completion evidence."""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Literal

from orchestune.forge import Forge, GitHubForge
from orchestune.infra.git_cli import run_git
from orchestune.models import PrRecord
from orchestune.outcome_record import (
    OutcomeLookupResult,
    OutcomeLookupState,
    parse_from_comments,
)
from orchestune.targets.contracts import DispatchHandle, ReviewerBot

NONINTERACTIVE_DISPATCH_INSTRUCTION = (
    "これは非対話型のバックグラウンド自動実行であり、標準入力からの応答は得られません。"
    "planning_modeによるユーザー承認待ちで停止せず、"
    "実装プラン作成後は直ちに実装・検証・コミットまで完了させてください。"
)


def _noninteractive_instruction(reviewer_bot: ReviewerBot | None) -> str:
    if reviewer_bot is None:
        return NONINTERACTIVE_DISPATCH_INSTRUCTION
    return (
        NONINTERACTIVE_DISPATCH_INSTRUCTION
        + f"PR作成後のレビュー担当には必ず `{reviewer_bot}` を指定し、"
        "レビュー完了と指摘解消まで自律的に進めてください。"
    )


def _resolve_base_branch_val(base_branch: str | None) -> str:
    """PR作成時のベースブランチ名を正規化する（未指定時は'main'）。"""
    return (base_branch.removeprefix("origin/") if base_branch else "") or "main"


def _parse_github_timestamp(value: str) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _is_stale_pr_for_handle(pr: PrRecord, handle: DispatchHandle) -> bool:
    """#246: session開始（`handle.started_at`）より前に作成されたPRは、状態
    （OPEN/MERGED/CLOSED）に関係なく現在のsessionの成果物ではない。同名branchの
    古いMERGED PR等で再キュー後の新sessionが即completed扱いされないよう除外する。

    `created_at`を取得・解釈できないPRも現世代の証拠とはみなさない（fail
    closed）。`started_at`を持たないhandle（復元経路等）は従来通り除外しない。
    CLOSED PRの`closed_at < started_at`によるstale判定（#210）は、
    `closed_at >= created_at`であるため`created_at`判定に包含される。

    #262レビュー対応: GitHubの`created_at`は秒精度で切り捨てられる一方、
    `handle.started_at`は小数秒を含む（各task起動直前に`time.time()`で
    取得）。直接比較すると、実際にはsession開始「後」に作成された正規PRでも
    `created_at < started_at`が真になり誤ってstale扱いされうる
    （例: created_at=X.000, started_at=X.900）。比較はGitHub側の精度に
    合わせて`started_at`を秒単位に切り捨ててから行い、同じ秒に作成された
    PRはstaleとしない。"""
    if handle.started_at is None:
        return False
    created_at = _parse_github_timestamp(pr.created_at)
    return created_at is None or created_at < math.floor(handle.started_at)


def _resolve_issue_number_for_outcome(
    handle: DispatchHandle, open_prs: list[PrRecord]
) -> int | None:
    """`handle.issue_number`が未設定な場合、マッチした（ブランチ名一致の）
    open PRのGitHubクロージング参照（`closes_issue_numbers`）から対象Issueを
    解決する。これはPRの**メタデータ**であり、#998で読取対象外とした
    PR**コメント**とは別物なので、Issueコメント正本の契約に反しない。

    Codexレビュー(#1015 round3 P2) 対応: 1つのPRが複数Issueをcloseする場合、
    どれが実際のディスパッチ対象タスクかは`closes_issue_numbers`だけからは
    判別できない（先頭を採ると別Issueを誤って対象にしうる）。曖昧な場合は
    解決を諦め、他のマッチPRで一意に解決できないか続けて試す。
    """
    if handle.issue_number is not None:
        return handle.issue_number
    for pr in open_prs:
        if len(pr.closes_issue_numbers) == 1:
            return pr.closes_issue_numbers[0]
    return None


def _lookup_issue_outcome(
    issue_number: int | None, forge: Forge, *, since: float | None
) -> OutcomeLookupResult:
    """#998: Issueコメントのみを宣言の正本として`OutcomeRecord`を解決する。

    旧来はPRコメントへもフォールバックしていたが、Issueコメント正本の契約に
    伴い廃止した（通信再送や複数PRでの重複読み取りによる誤ったattempt増加を
    防ぐため）。取得に失敗した場合はABSENT（未投稿）と区別してUNKNOWNを返す。
    """
    if issue_number is None:
        return OutcomeLookupResult(state=OutcomeLookupState.UNKNOWN)
    try:
        comments = forge.list_comments(issue_number)
    except Exception:
        return OutcomeLookupResult(state=OutcomeLookupState.UNKNOWN)
    record = parse_from_comments(comments, since=since)
    if record is None:
        return OutcomeLookupResult(state=OutcomeLookupState.ABSENT)
    return OutcomeLookupResult(state=OutcomeLookupState.FOUND, record=record)


def _check_open_prs_outcome(
    open_prs: list[PrRecord],
    handle: DispatchHandle,
    forge: Forge,
) -> Literal["completed", "unknown", "pending"]:
    issue_number = _resolve_issue_number_for_outcome(handle, open_prs)
    lookup = _lookup_issue_outcome(issue_number, forge, since=handle.started_at)
    if lookup.state is OutcomeLookupState.FOUND:
        return "completed"
    if lookup.state is OutcomeLookupState.UNKNOWN:
        return "unknown"
    return "pending"


def _task_pr_completion_status(
    handle: DispatchHandle,
    forge: Forge | None = None,
) -> Literal["pending", "completed", "abandoned", "unknown"]:
    if handle.branch_name is None and handle.issue_number is None:
        return "pending"
    forge = forge or GitHubForge()
    try:
        prs = forge.list_prs(state="all")
    except Exception:
        return "unknown"
    matching_prs = [
        pr
        for pr in prs
        if (
            (handle.branch_name is not None and pr.head_ref == handle.branch_name)
            or (
                handle.issue_number is not None
                and handle.issue_number in pr.closes_issue_numbers
            )
        )
        and not _is_stale_pr_for_handle(pr, handle)
    ]
    if any(pr.state == "MERGED" for pr in matching_prs):
        return "completed"

    open_prs = [pr for pr in matching_prs if pr.state == "OPEN"]
    if open_prs:
        return _check_open_prs_outcome(open_prs, handle, forge)

    if any(pr.state == "CLOSED" for pr in matching_prs):
        return "abandoned"
    return "pending"


class BranchReachabilityError(RuntimeError):
    """#244レビュー対応: `_push_branch_and_verify`の到達性検証失敗専用の例外。

    `create_worktree_and_launch`側で汎用`RuntimeError`を捕捉すると、
    このチェック以外の実装バグまで通常の起動失敗として握り潰してしまうため、
    この専用型だけを捕捉させる。
    """


def _push_branch_and_verify(
    branch_name: str, worktree_path: Path, *, force: bool = False
) -> None:
    """#244: stacked/parent base付きで作成されたローカルtask branchを、リモート
    セッションがその内容ごとcheckoutできるようoriginへpushし、到達性を検証する。

    push後に`git ls-remote`でリモートbranchのSHAをローカルHEADと照合し、
    確認できない場合は`BranchReachabilityError`を送出する
    （呼び出し側はfireせずfail closed）。

    #384: `force=True`の場合は`--force-with-lease`を付与する。自動リベースは
    ローカルで既存の履歴を書き換えるため、force無しの通常pushは常に
    non-fast-forwardで拒否される（初回起動時の新規ブランチpushには影響しない）。
    """
    push_args = ["push", "--set-upstream", "origin", branch_name]
    if force:
        push_args.insert(1, "--force-with-lease")
    run_git(push_args, cwd=worktree_path, check=True)
    local_sha = run_git(
        ["rev-parse", "HEAD"], cwd=worktree_path, check=True
    ).stdout.strip()
    ls_remote_output = run_git(
        ["ls-remote", "origin", f"refs/heads/{branch_name}"],
        cwd=worktree_path,
        check=True,
    ).stdout.strip()
    remote_sha = ls_remote_output.split()[0] if ls_remote_output else ""
    if not remote_sha or remote_sha != local_sha:
        raise BranchReachabilityError(
            f"リモートブランチ '{branch_name}' の到達性を検証できませんでした "
            f"(local={local_sha or '不明'}, remote={remote_sha or '不在'})。"
            "baseの変更を含まないセッション起動を防ぐため、fireを中止します。"
        )

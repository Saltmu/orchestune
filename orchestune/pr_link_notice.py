"""#676: 親ブランチ宛てPRと対象Issueを相互リンクする通知コメント。

GitHubの`Closes #N`による自動リンクとIssueサイドバーの「Development」欄は、
PRのbaseが既定ブランチ(`main`)の場合にしか働かない。Orchestuneの
Epic-Subtask運用では子タスクのPRが`parent/issue-*`ブランチを対象にするため、
子Issueだけを見ても「どのPRで作業され、いつマージされたのか」を辿れない。

このモジュールは、その欠落を埋める通知コメントの本文生成と、重複投稿を
防ぐ冪等性判定を担う。冪等性の根拠をコメント本文中のHTMLマーカーに置くのは、
GitHub Actionsのランナーがサイクルごとに使い捨てられ、ローカルの状態ファイルが
サイクルをまたいで残らないため（`dispatch.state`ではなくGitHub上に痕跡を
残す必要がある）。
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable, Mapping
from typing import Any

from orchestune.forge import Forge
from orchestune.models import PrRecord, Task, normalize_newlines

KIND_CREATED = "created"
KIND_MERGED = "merged"

#: 通知の対象とするbaseブランチの接頭辞。既定ブランチ宛てのPRはGitHubが
#: 自動リンクするため、補完コメントは不要（かつノイズになる）。
PARENT_BRANCH_PREFIX = "parent/"

#: `claude/issue-12-task-a`のような、Issue番号を含むheadブランチ名。
#: `closes_issue_numbers`が空のPR（エージェントが`Closes`記法を書かずに
#: 起票した場合など）でも対象Issueを特定できるようにする。
_ISSUE_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9._-]+/issue-(\d+)-")


def notice_marker(kind: str, pr_number: int) -> str:
    """通知1件を一意に識別する、コメント本文へ埋め込む機械可読マーカー。"""
    return f"<!-- orchestune:pr-link:{kind}:{pr_number} -->"


def render_created_notice(pr_number: int, base_branch: str) -> str:
    return (
        f"{notice_marker(KIND_CREATED, pr_number)}\n"
        f"🔗 このIssueに対応するPR #{pr_number} が作成されました"
        f"（ターゲットブランチ: `{base_branch}`）。\n\n"
        "GitHubの仕様上、既定ブランチ以外を対象とするPRはIssueの「Development」欄へ"
        "自動リンクされないため、Orchestuneがこのコメントで相互リンクを補完しています。"
    )


def render_merged_notice(pr_number: int, base_branch: str) -> str:
    return (
        f"{notice_marker(KIND_MERGED, pr_number)}\n"
        f"✅ PR #{pr_number} が親ブランチ `{base_branch}` にマージされました。\n\n"
        "Integratorが親ブランチへの自動マージを完了したため、"
        "このIssueを自動的にクローズしました。"
    )


def has_notice(
    comments: Iterable[Mapping[str, Any]], kind: str, pr_number: int
) -> bool:
    marker = notice_marker(kind, pr_number)
    return any(
        marker in normalize_newlines(str(comment.get("body") or ""))
        for comment in comments
    )


def _already_notified(
    forge: Forge, issue_number: int, kind: str, pr_number: int
) -> bool | None:
    """通知済みかどうかを返す。判定できなかった場合は`None`。

    コメント一覧を読めないまま投稿すると多重投稿になり得るため、呼び出し側は
    `None`をfail closed（投稿しない）として扱い、次サイクルの再試行に委ねる。
    """
    try:
        return has_notice(forge.list_comments(issue_number), kind, pr_number)
    except Exception as error:  # noqa: BLE001 - 通知はベストエフォート
        print(
            f"Warning: Failed to read comments on issue #{issue_number} while "
            f"checking the {kind} PR link notice for PR #{pr_number}: {error}",
            file=sys.stderr,
        )
        return None


def notify_pr_created(
    forge: Forge, issue_number: int, pr_number: int, base_branch: str
) -> bool:
    """未通知であればPR作成の通知コメントを投稿し、投稿したかどうかを返す。"""
    if _already_notified(forge, issue_number, KIND_CREATED, pr_number) is not False:
        return False
    try:
        forge.add_comment(issue_number, render_created_notice(pr_number, base_branch))
    except Exception as error:  # noqa: BLE001 - 通知はベストエフォート
        print(
            f"Warning: Failed to post the created PR link notice for PR "
            f"#{pr_number} on issue #{issue_number}: {error}",
            file=sys.stderr,
        )
        return False
    return True


def merged_notice_if_new(
    forge: Forge, issue_number: int, pr_number: int, base_branch: str
) -> str | None:
    """通知済みでなければマージ完了通知の本文を返す（通知済みなら`None`）。

    Integratorは`close_issue`の`comment`として渡すため、投稿自体はここでは
    行わない。クローズとコメントを1回のAPI呼び出しに束ねることで、
    「コメントは残ったがクローズに失敗した」中途半端な状態を作らない。

    PR#684レビュー対応(Codex P2): 通知済みかどうかを判定できなかった場合は、
    作成通知(`notify_pr_created`)とは逆に**fail open**で本文を返す。マージ通知は
    Issueをクローズする最後の書き込みであり、クローズ済みIssueは以降のサイクルで
    統合対象から外れる（`PrepareTasksStep`）ため再試行の機会がない。ここで
    見送るとPRリンクが恒久的に失われるので、重複コメントの可能性を受け入れる。
    """
    if _already_notified(forge, issue_number, KIND_MERGED, pr_number) is True:
        return None
    return render_merged_notice(pr_number, base_branch)


def requires_link_notice(pr: PrRecord) -> bool:
    return (pr.base_ref or "").startswith(PARENT_BRANCH_PREFIX)


def target_issue_numbers(pr: PrRecord, known_issue_numbers: set[int]) -> list[int]:
    """PRが対応するタスクIssueの番号を、`Closes`参照とheadブランチ名から解決する。"""
    numbers = set(pr.closes_issue_numbers)
    match = _ISSUE_BRANCH_PATTERN.match(pr.head_ref or "")
    if match:
        numbers.add(int(match.group(1)))
    return sorted(numbers & known_issue_numbers)


def notice_candidate_issue_numbers(tasks: Iterable[Task]) -> set[int]:
    """作成通知の走査対象とするタスクIssueの番号を返す。

    PR#684レビュー対応(Codex P2): 親ブランチ運用では、統合済みタスクの子PRが
    （ブランチ削除に失敗した場合などに）開いたまま残ることがある。全タスクを
    毎サイクル走査すると、マーカーを読み直すためだけの`list_comments`呼び出しが
    完了タスク数に比例して増え続けるため、既にクローズされたIssueと
    `integration:included`で統合を終えたタスクを候補から外す。作成通知が
    間に合わなかった分は、統合時のマージ通知が引き継ぐ。
    """
    return {
        task.issue_number
        for task in tasks
        if task.issue_state != "CLOSED"
        and "integration:included" not in task.status_labels
    }


def notify_open_pr_links(
    forge: Forge, prs: Iterable[PrRecord], known_issue_numbers: set[int]
) -> list[dict[str, Any]]:
    """親ブランチ宛てのオープンPRについて、未通知の対象Issueへ通知を投稿する。

    ディスパッチャーが検知したPRを起点にするため、エージェントが自分で起票した
    PRも同じ経路で通知される。1件の失敗は他のPRの通知を止めない。
    """
    events: list[dict[str, Any]] = []
    for pr in prs:
        if not requires_link_notice(pr):
            continue
        for issue_number in target_issue_numbers(pr, known_issue_numbers):
            if notify_pr_created(forge, issue_number, pr.number, pr.base_ref):
                events.append(
                    {
                        "issue_number": issue_number,
                        "pr_number": pr.number,
                        "kind": KIND_CREATED,
                    }
                )
    return events


__all__ = [
    "KIND_CREATED",
    "KIND_MERGED",
    "has_notice",
    "merged_notice_if_new",
    "notice_candidate_issue_numbers",
    "notice_marker",
    "notify_open_pr_links",
    "notify_pr_created",
    "render_created_notice",
    "render_merged_notice",
    "requires_link_notice",
    "target_issue_numbers",
]

"""#676: 親ブランチ宛てPRと対象Issueを相互リンクする通知コメント。

GitHubの`Closes #N`による自動リンクとIssueサイドバーの「Development」欄は、
PRのbaseが既定ブランチ(`main`)の場合にしか働かない。Orchestuneの
Epic-Subtask運用では子タスクのPRが`parent/issue-*`ブランチを対象にするため、
子Issueだけを見ても「どのPRで作業され、いつマージされたのか」を辿れない。

このモジュールは、その欠落を埋める通知コメントの本文生成と、重複投稿を
防ぐ冪等性判定を担う。冪等性の根拠をコメント本文中のHTMLマーカーに置くのは、
GitHub Actionsのランナーがサイクルごとに使い捨てられ、ローカルの状態ファイルが
サイクルをまたいで残らないため（`ledger.run_state`ではなくGitHub上に痕跡を
残す必要がある）。
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable, Mapping
from enum import Enum
from typing import TYPE_CHECKING, Any

from orchestune.models import PrRecord, normalize_newlines
from orchestune.pr_link_notice_trust import NoticePresence, TrustContext


class NoticeDelivery(Enum):
    """Result of attempting to ensure a PR link notice."""

    ALREADY_PRESENT = "ALREADY_PRESENT"
    POSTED = "POSTED"
    DEFERRED_READ = "DEFERRED_READ"
    DEFERRED_TRUST = "DEFERRED_TRUST"
    DEFERRED_WRITE = "DEFERRED_WRITE"

    @property
    def can_close(self) -> bool:
        """Return True if the child issue may be closed safely."""
        return self in {NoticeDelivery.ALREADY_PRESENT, NoticeDelivery.POSTED}


if TYPE_CHECKING:
    from orchestune.task_metadata import TaskMetadata

KIND_CREATED = "created"
KIND_MERGED = "merged"

#: 通知の対象とするbaseブランチの接頭辞。既定ブランチ宛てのPRはGitHubが
#: 自動リンクするため、補完コメントは不要（かつノイズになる）。
PARENT_BRANCH_PREFIX = "parent/"

#: Issue番号を含むheadブランチ名・タイトル・本文の抽出パターン。
#: closes_issue_numbers が空のPR（非デフォルトブランチ宛てPRやCloses記法なしPR）
#: でも対象Issueを安全に特定できるようにする。
_ISSUE_BRANCH_PATTERNS = (
    re.compile(r"^[A-Za-z0-9._-]+/issue-(\d+)(?:[-_./]|$)"),
    re.compile(r"(?:^|/)(?:issue-|issue/|parent/issue-)(\d+)(?:[-_./]|$)"),
    re.compile(
        r"(?:^|/)(?:fix|feat|chore|refactor|bugfix|hotfix)/issue-(\d+)(?:[-_./]|$)"
    ),
)
_PR_TITLE_PATTERNS = (
    re.compile(r"(?:^|[^\w])#(\d+)(?:[^\w]|$)"),
    re.compile(r"(?:^|[^\w])issue[- :](\d+)(?:[^\w]|$)", re.IGNORECASE),
)
_PR_BODY_PATTERNS = (
    re.compile(r"(?:closes|fixes|resolves|issue:?)\s*#(\d+)", re.IGNORECASE),
)


def extract_issue_numbers_from_pr(pr: PrRecord) -> set[int]:
    """PRから関連するIssue番号の集合を抽出する。

    GraphQLのclosesIssuesReferences（closes_issue_numbers）に加え、
    非デフォルトブランチ宛てPRで空になるGitHub APIの仕様を補うため、
    head_ref、title、bodyから対象Issue番号を安全に逆引きする。
    """
    numbers = set(pr.closes_issue_numbers)

    head_ref = pr.head_ref or ""
    for pattern in _ISSUE_BRANCH_PATTERNS:
        match = pattern.search(head_ref)
        if match:
            numbers.add(int(match.group(1)))

    title = getattr(pr, "title", "") or ""
    for pattern in _PR_TITLE_PATTERNS:
        for match in pattern.finditer(title):
            numbers.add(int(match.group(1)))

    body = getattr(pr, "body", "") or ""
    for pattern in _PR_BODY_PATTERNS:
        for match in pattern.finditer(body):
            numbers.add(int(match.group(1)))

    return numbers


def pr_matches_issue(
    pr: PrRecord,
    issue_number: int,
    subtask_id: str | None = None,
) -> bool:
    """PRが指定のIssue（およびサブタスク）に対応しているかを判定する。"""
    if issue_number in extract_issue_numbers_from_pr(pr):
        return True
    if subtask_id and pr.head_ref:
        normalized_head = pr.head_ref.replace("/", "-")
        if (
            f"issue-{issue_number}" in normalized_head
            or str(issue_number) in normalized_head
        ):
            if subtask_id in normalized_head:
                return True
    return False


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
    comments: Iterable[Mapping[str, Any]],
    kind: str,
    pr_number: int,
    *,
    trust: TrustContext,
) -> NoticePresence:
    """通知マーカーの一致と投稿者の信頼性を検証し、存在状態を返す。"""
    marker = notice_marker(kind, pr_number)
    candidates: list[dict[str, Any]] = [
        dict(comment)
        for comment in comments
        if marker in normalize_newlines(str(comment.get("body") or ""))
    ]
    presence, _ = trust.evaluate_candidate_markers(candidates)
    return presence


def _already_notified(
    forge: Any,
    issue_number: int,
    kind: str,
    pr_number: int,
    trust: TrustContext,
) -> NoticePresence | None:
    """通知済みかどうかを返す。読み取りに失敗した場合は`None`。

    コメント一覧を読めないまま投稿すると多重投稿になり得るため、呼び出し側は
    `None`をfail closed（投稿しない）として扱い、次サイクルの再試行に委ねる。
    """
    try:
        comments = forge.list_all_issue_comments(issue_number)
        return has_notice(comments, kind, pr_number, trust=trust)
    except Exception as error:  # noqa: BLE001 - 通知はベストエフォート
        print(
            f"Warning: Failed to read comments on issue #{issue_number} while "
            f"checking the {kind} PR link notice for PR #{pr_number}: {error}",
            file=sys.stderr,
        )
        return None


def notify_pr_created(
    forge: Any,
    issue_number: int,
    pr_number: int,
    base_branch: str,
    *,
    trust: TrustContext | None = None,
) -> bool:
    """未通知であればPR作成の通知コメントを投稿し、投稿したかどうかを返す。"""
    ctx = trust or TrustContext(forge)
    presence = _already_notified(forge, issue_number, KIND_CREATED, pr_number, ctx)
    if presence is not NoticePresence.ABSENT:
        return False
    try:
        forge.create_issue_comment(
            issue_number, render_created_notice(pr_number, base_branch)
        )
    except Exception as error:  # noqa: BLE001 - 通知はベストエフォート
        print(
            f"Warning: Failed to post the created PR link notice for PR "
            f"#{pr_number} on issue #{issue_number}: {error}",
            file=sys.stderr,
        )
        return False
    return True


def ensure_pr_merged_notice(
    forge: Any,
    issue_number: int,
    pr_number: int,
    base_branch: str,
    *,
    trust: TrustContext | None = None,
) -> NoticeDelivery:
    """マージ完了通知がIssueに存在する状態を保証し、配送結果を返す。

    #686: マーカー判定と投稿者検証を分離し、NoticeDeliveryを返す。
    ALREADY_PRESENT または POSTED の場合のみクローズ可能（can_close=True）。
    保留時はクローズを抑止し、次サイクルでの再試行に委ねる。
    """
    ctx = trust or TrustContext(forge)
    try:
        comments = forge.list_all_issue_comments(issue_number)
    except Exception as error:
        print(
            f"Warning: Failed to read comments on issue #{issue_number} while "
            f"checking the merged PR link notice for PR #{pr_number}: {error}",
            file=sys.stderr,
        )
        return NoticeDelivery.DEFERRED_READ

    presence = has_notice(comments, KIND_MERGED, pr_number, trust=ctx)
    if presence is NoticePresence.PRESENT:
        return NoticeDelivery.ALREADY_PRESENT
    if presence is NoticePresence.UNKNOWN:
        reason = ctx.last_deferred_reason or "unknown"
        print(
            f"Warning: Deferred merged PR link notice for PR #{pr_number} on "
            f"issue #{issue_number} due to untrusted/unresolved marker: {reason}",
            file=sys.stderr,
        )
        return NoticeDelivery.DEFERRED_TRUST

    try:
        forge.create_issue_comment(
            issue_number, render_merged_notice(pr_number, base_branch)
        )
    except Exception as error:
        print(
            f"Warning: Failed to post the merged PR link notice for PR "
            f"#{pr_number} on issue #{issue_number}: {error}",
            file=sys.stderr,
        )
        return NoticeDelivery.DEFERRED_WRITE

    return NoticeDelivery.POSTED


def requires_link_notice(pr: PrRecord) -> bool:
    """通知対象のPRかどうかを判定する。

    PR#684レビュー対応(Codex P2): upstream repository上のheadを持つPRだけを
    対象とする。forkからのPRも`list_open_prs()`には含まれ、`parent/*`をbaseに
    `<任意のprefix>/issue-{N}-...`というheadを名乗ることも、既知のIssueを`Closes`で
    参照することもできる。identityを確認できないPR（`is_cross_repository`が
    不明）も含めてfail closedで除外し、第三者が「このIssueに対応するPR」という
    権威ある体裁の通知をIssueへ書き込めないようにする
    （`integrator.pr._is_reusable_integration_pr`と同じ方針）。
    """
    return pr.is_cross_repository is False and (pr.base_ref or "").startswith(
        PARENT_BRANCH_PREFIX
    )


def target_issue_numbers(pr: PrRecord, expected_bases: Mapping[int, str]) -> list[int]:
    """PRが対応するタスクIssueの番号を、`Closes`参照とheadブランチ名・タイトル等から解決する。

    解決した候補のうち、PRのbaseがそのIssueの親ブランチと完全に一致するものだけを
    返す（`notice_expected_bases`参照）。
    """
    numbers = extract_issue_numbers_from_pr(pr)
    return sorted(
        number for number in numbers if expected_bases.get(number) == pr.base_ref
    )


def notice_expected_bases(tasks: Iterable[TaskMetadata]) -> dict[int, str]:
    """作成通知の走査対象タスクと、そのIssueが本来ぶら下がる親ブランチの対応表。

    PR#684レビュー対応(Codex P2): 親ブランチ運用では、統合済みタスクの子PRが
    （ブランチ削除に失敗した場合などに）開いたまま残ることがある。全タスクを
    毎サイクル走査すると、マーカーを読み直すためだけの`list_comments`呼び出しが
    完了タスク数に比例して増え続けるため、既にクローズされたIssueと
    `integration:included`で統合を終えたタスクを候補から外す。作成通知が
    間に合わなかった分は、統合時のマージ通知が引き継ぐ。

    同(Codex P2): 値として期待される親ブランチ名を持たせ、`parent/`接頭辞だけの
    照合にしない。別の親（例: `parent/issue-200`）宛てのPRが`Closes`やhead名で
    このサイクルの子Issueを参照しているだけで通知してしまうと、無関係なPRを
    「このIssueに対応するPR」として書き込むことになる。親が特定できないタスクは
    照合できないため、fail closedで候補から外す。
    """
    return {
        task.issue_number: f"{PARENT_BRANCH_PREFIX}issue-{task.parent_number}"
        for task in tasks
        if task.parent_number is not None
        and task.issue_state != "CLOSED"
        and "integration:included" not in task.status_labels
    }


def notify_open_pr_links(
    forge: Any, prs: Iterable[PrRecord], expected_bases: Mapping[int, str]
) -> list[dict[str, Any]]:
    """親ブランチ宛てのオープンPRについて、未通知の対象Issueへ通知を投稿する。

    ディスパッチャーが検知したPRを起点にするため、エージェントが自分で起票した
    PRも同じ経路で通知される。1件の失敗は他のPRの通知を止めない。
    TrustContextをバッチ内で共有し、identityとcollaborator一覧を再利用する。
    """
    events: list[dict[str, Any]] = []
    trust = TrustContext(forge)
    for pr in prs:
        if not requires_link_notice(pr):
            continue
        for issue_number in target_issue_numbers(pr, expected_bases):
            if notify_pr_created(
                forge, issue_number, pr.number, pr.base_ref, trust=trust
            ):
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
    "NoticeDelivery",
    "ensure_pr_merged_notice",
    "extract_issue_numbers_from_pr",
    "has_notice",
    "notice_expected_bases",
    "notice_marker",
    "notify_open_pr_links",
    "notify_pr_created",
    "pr_matches_issue",
    "render_created_notice",
    "render_merged_notice",
    "requires_link_notice",
    "target_issue_numbers",
]

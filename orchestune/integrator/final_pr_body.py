"""#681: 最終統合PR（`parent/issue-N` → `main`）の本文生成。

`ensure_parent_final_pr`が用意するこのPRのマージが「最終マージ」であり、常に
人間が行う。その本文には2つの役割がある:

1. **親Issueの自動クローズとDevelopment連携**: `Closes #N`を書いておくと、
   既定ブランチ(`main`)向けPRであるためGitHubがマージ時に親Issueを閉じ、
   Issueサイドバーの「Development」欄にも親Issueが連携される。
   `parent_completion`側のマージ検知クローズは残したまま二重化する
   （どちらか一方が働けば親Issueは確実に閉じる）。
2. **レビュー履歴のトレーサビリティ**: 最終レビュアー（人間）が各サブタスクの
   変更内容とAIコードレビューの結果を辿れるよう、子Issue・マージ済み
   サブタスクPR・レビュー結果の一覧をテーブルで示す。

一覧の収集はすべてベストエフォートで、失敗しても最終PRの確保自体は妨げない
（一覧が欠けることより、最終PRが存在しないことのほうが重い）。
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from orchestune.forge import Forge
from orchestune.models import IssueRecord, PrRecord
from orchestune.outcome_record import OutcomeRecord, parse_from_comments
from orchestune.pr_link_notice import requires_link_notice, target_issue_numbers

#: 空セルのプレースホルダ。空文字のままでは列ずれと見分けが付かない。
EMPTY_CELL = "—"

_TABLE_HEADER = (
    "| 子Issue | タイトル | サブタスクPR | レビュー結果 |",
    "| --- | --- | --- | --- |",
)


@dataclass(frozen=True)
class ChildSummary:
    """一覧テーブル1行分の表示データ。"""

    issue_number: int
    title: str
    pr_numbers: tuple[int, ...] = ()
    review: str = ""


#: `GH-45`形式のIssue参照。`#`のエスケープでは潰せない別形式。
_GH_REFERENCE = re.compile(r"GH-(?=\d)", re.IGNORECASE)

#: `https://github.com/owner/repo/issues/45`形式のIssue参照。
_GITHUB_REFERENCE_URL = re.compile(
    r"(https?)://(?=(?:www\.)?github\.com/[^/\s]+/[^/\s]+/(?:issues|pull)/\d)",
    re.IGNORECASE,
)


def _escape_cell(text: str) -> str:
    r"""Markdownテーブルのセルへ埋めても安全な文字列にする。

    これは体裁ではなく安全性の要件: 子Issueのタイトルやレビュー結果に
    `fixes #45`のようなクローズキーワードが含まれていると、この本文を持つ
    最終統合PRのマージで**無関係な#45まで自動クローズ**されてしまう。

    PR#690レビュー対応(Codex P2): 以前は`#`だけを潰していたが、GitHubが
    受け付けるクローズ参照は`#45`・`owner/repo#45`・`GH-45`・
    `https://github.com/owner/repo/issues/45`の4形式で、`#`のエスケープは
    前2つしか止められていなかった。残る2形式もここで無害化する。
    どれも`\`によるASCII記号のエスケープで参照として解釈されなくなり、
    描画結果は元の文字列のまま変わらない。

    `\`を`|`より先に処理するのは、`a\|b`のようなタイトルでそのまま`|`を
    エスケープすると`a\\|b`（エスケープされた円記号＋生のセル区切り）となり、
    表が壊れるため。`GH-`とURLのエスケープは、後から入れた`\`が二重化されない
    よう`\`の処理より後に置く。改行の潰し込みは行の分断を防ぐためのもの。
    """
    collapsed = " ".join(text.split())
    escaped = collapsed.replace("\\", "\\\\").replace("|", "\\|").replace("#", "\\#")
    escaped = _GH_REFERENCE.sub(lambda match: match.group(0)[:-1] + r"\-", escaped)
    return _GITHUB_REFERENCE_URL.sub(lambda match: match.group(1) + r":\/\/", escaped)


def _render_row(summary: ChildSummary) -> str:
    """Issue番号・PR番号は素の`#N`で書き、GitHubの相互参照リンクを働かせる。

    どちらも整数であり、クローズキーワードを伴わないためエスケープは不要
    （かつエスケープするとリンクにならない）。"""
    prs = ", ".join(f"#{number}" for number in summary.pr_numbers) or EMPTY_CELL
    title = _escape_cell(summary.title) or EMPTY_CELL
    review = _escape_cell(summary.review) or EMPTY_CELL
    return f"| #{summary.issue_number} | {title} | {prs} | {review} |"


def render_final_pr_body(
    parent_issue_number: int, summaries: Sequence[ChildSummary]
) -> str:
    """最終統合PRの本文を組み立てる（純粋関数）。

    `summaries`が空ならテーブルごと省略する。これは子Issueを1件も解決できな
    かった縮退状態であり、見出しだけの空テーブルは「サブタスクが無い」という
    誤った情報になる。
    """
    lines = [
        f"Closes #{parent_issue_number}",
        "",
        f"親Issue #{parent_issue_number} 配下の全子Issueが完了したため、"
        "Orchestune Integratorが作成した最終統合PRです。",
        "",
        "このPRのマージが最終マージです。人間がレビューの上マージしてください。"
        "マージ時にGitHubが親Issueを自動的にクローズし、Orchestuneも"
        "マージを検知して同じクローズを冪等に試みます。",
    ]
    if summaries:
        lines += ["", "## 子Issue・サブタスクPR一覧", "", *_TABLE_HEADER]
        lines += [_render_row(summary) for summary in summaries]
    return "\n".join(lines) + "\n"


def _merged_subtask_prs(
    prs: Iterable[PrRecord], parent_branch: str, child_numbers: Iterable[int]
) -> dict[int, list[PrRecord]]:
    """マージ済みサブタスクPRを子Issue番号ごとに集める。

    照合規則は`pr_link_notice`(#684)に揃える: upstream repository上のheadを
    持つPRだけを対象とし(`requires_link_notice`)、PRのbaseがその子Issueの
    親ブランチと完全に一致するものだけを採る(`target_issue_numbers`)。
    forkのPRや別の親ブランチ宛てのPRを「このサブタスクのPR」という権威ある
    体裁で最終統合PR本文へ書き込ませないため。
    """
    expected_bases = {number: parent_branch for number in child_numbers}
    matched: dict[int, list[PrRecord]] = {}
    for pr in sorted(prs, key=lambda record: record.number):
        if pr.state != "MERGED" or not requires_link_notice(pr):
            continue
        for issue_number in target_issue_numbers(pr, expected_bases):
            matched.setdefault(issue_number, []).append(pr)
    return matched


class _CollectionDegraded(Exception):
    """forge読み取りに失敗し、完全な一覧を作れなくなったことを表す内部シグナル。

    PR#690レビュー対応(Codex P2): 読み取り失敗を「レコードが無い」と同じ
    `None`で表すと、`_review_text`がフォールバックや空欄の行を作ってしまい、
    その行は`ensure_parent_final_pr`の`if summaries`ガードを通過して投稿済みの
    正しい表を上書きする。失敗は別物として伝え、`list_prs`失敗時と同じ
    「一覧ごと省略」の縮退経路へ倒す。
    """


def _outcome_from(
    forge: Forge, number: int, issue_number: int, pr_number: int | None
) -> OutcomeRecord | None:
    """`number`のコメント欄から、この子タスクのOutcome Recordを1件選ぶ。

    PR#690レビュー対応(Codex P2): 識別チェックは`parse_from_comments`の
    **前**に掛ける。`parse_from_comments`はコメント全体から最新の1件を選ぶ
    ため、後段で弾く形だと「最新は別タスクのレコード、その1つ前がこの子の
    正しいレコード」というコメント欄で、正しい方が検討されずに捨てられる。
    先に絞り込めば「この子を指すレコードのうち最新」を選べる。
    """
    try:
        comments: Sequence[Mapping[str, Any]] = forge.list_comments(number)
    except Exception as error:  # noqa: BLE001 - 一覧生成はベストエフォート
        print(
            f"Warning: Failed to read comments on #{number} while building the "
            f"final integration PR body; omitting the child table rather than "
            f"rendering rows with degraded review results: {error}",
            file=sys.stderr,
        )
        raise _CollectionDegraded from error
    owned = [
        comment
        for comment in comments
        if (record := parse_from_comments([comment])) is not None
        and _identifies_child(record, issue_number, pr_number)
    ]
    return parse_from_comments(owned)


def _identifies_child(
    record: OutcomeRecord, issue_number: int, pr_number: int | None
) -> bool:
    """PR#690レビュー対応(Codex P2): そのレコードの識別フィールドが、この子
    タスクのものだと述べているかを確認する。

    1つのPRが複数Issueを閉じる場合や、古いレコードが貼り直された場合、
    走査対象のコメント欄には別タスクのOutcome Recordが載りうる。`issue`/`pr`は
    レコード契約上の識別子なので、ここで照合しなければ他タスクの
    レビュー結果をこの子Issueの行として掲載してしまう。

    `pr`は任意フィールドであり、未設定は「PRを主張していない」を意味する。
    不一致として弾くと`pr`を省略した正当なレコードが全て失われるため、
    値が入っているときだけ照合する。
    """
    if record.issue != issue_number:
        return False
    return pr_number is None or record.pr is None or record.pr == pr_number


def _child_outcome(
    forge: Forge, issue_number: int, prs: Sequence[PrRecord]
) -> OutcomeRecord | None:
    """Outcome Recordをマージ済みサブタスクPR→子Issueの順に探す。

    local-ci-developerスキルは完了時のoutcomeを**PRコメント**へ投稿する契約
    のため、この順なら子Issue1件あたり原則1回のAPI呼び出しで解決でき、
    `AGENTS.md`のAPIコスト制限に沿う。

    PR#690レビュー対応(Codex P2): 複数のサブタスクPRがある場合は**PR番号の
    大きい方から**見る。`_merged_subtask_prs`は表示順を安定させるため昇順で
    返すが、その順で先頭を採ると、後から作られたPRに新しい結果があっても
    古いPRの結果を載せてしまう（行にはPRが両方並ぶので齟齬になる）。PR番号は
    GitHub上で単調増加するので、最大のものがこのサブタスクについての最新の
    表明にあたる。全PRのコメントを読んで`created_at`を比較する案は採らない:
    複数PRがある子Issueで必ず全件読むことになり、上記のAPIコスト制限に反する
    一方、PR番号による近似で取り違えが起きるのは「古いPRへ後から新しい
    outcomeが貼られた」場合に限られる。

    識別フィールドがこの子タスクを指さないレコードは採用しない
    （`_outcome_from`が`_identifies_child`で絞り込む）。1件も採れなかった
    場合は`_review_text`がPRの`reviewDecision`へフォールバックする。
    """
    for pr in reversed(prs):
        outcome = _outcome_from(forge, pr.number, issue_number, pr.number)
        if outcome is not None:
            return outcome
    return _outcome_from(forge, issue_number, issue_number, None)


def _review_text(outcome: OutcomeRecord | None, prs: Sequence[PrRecord]) -> str:
    """レビュー結果セルの文字列。

    Outcome Record（bot・verdict・ラウンド数まで分かる）を優先し、無い場合は
    PRの`reviewDecision`へフォールバックする。
    """
    if outcome is None:
        decisions = {pr.review_decision for pr in prs if pr.review_decision}
        return ", ".join(sorted(decisions))
    rounds = outcome.review.rounds
    details = [
        detail
        for detail in (
            outcome.reason,
            outcome.review.bot,
            outcome.review.verdict,
            f"{rounds}ラウンド" if rounds is not None else None,
        )
        if detail
    ]
    return f"{outcome.result} ({' / '.join(details)})" if details else outcome.result


def _summarize_child(
    forge: Forge, child: IssueRecord, prs: Sequence[PrRecord]
) -> ChildSummary:
    return ChildSummary(
        issue_number=child.number,
        title=child.title,
        pr_numbers=tuple(pr.number for pr in prs),
        review=_review_text(_child_outcome(forge, child.number, prs), prs),
    )


def collect_child_summaries(
    forge: Forge, parent_issue_number: int, children: Iterable[IssueRecord]
) -> list[ChildSummary]:
    """子Issueごとの一覧行を組み立てる（forge読み取りあり・ベストエフォート）。

    `find_children_by_parent`はネイティブSub-issueと本文metadata由来の候補を
    連結した順で返すため、本文が毎サイクル並び替わらないよう子Issue番号で
    整列してから組み立てる。

    PR#690レビュー対応(Codex P2): forge読み取りに失敗した場合は、空リストを
    返して「縮退」を呼び出し元へ伝える。`prs=[]`やレビュー欄の空文字のまま行を
    作ると、実際にはサブタスクPRやレビュー結果を持つ子Issueの欄が`—`になり、
    「PRもレビューも無かった」と読める偽の行ができる。この行は再利用PRでは
    `ensure_parent_final_pr`の`if summaries`ガードを通過して投稿済みの正しい表を
    上書きし、新規PRでは誤情報をそのまま掲載する。空リストならテーブル省略・
    本文非更新という既存の縮退経路へ倒せる。マージ済みPRの取得
    (`list_prs`)とコメントの取得(`list_comments`)の双方が同じ扱いになる。
    """
    ordered = sorted(children, key=lambda child: child.number)
    try:
        prs: list[PrRecord] = forge.list_prs(state="merged")
    except Exception as error:  # noqa: BLE001 - 一覧生成はベストエフォート
        print(
            f"Warning: Failed to list merged PRs while building the final "
            f"integration PR body for #{parent_issue_number}; omitting the child "
            f"table rather than rendering rows that wrongly show no subtask "
            f"PRs: {error}",
            file=sys.stderr,
        )
        return []
    matched = _merged_subtask_prs(
        prs,
        f"parent/issue-{parent_issue_number}",
        [child.number for child in ordered],
    )
    try:
        return [
            _summarize_child(forge, child, matched.get(child.number, []))
            for child in ordered
        ]
    except _CollectionDegraded:
        return []


__all__ = [
    "EMPTY_CELL",
    "ChildSummary",
    "collect_child_summaries",
    "render_final_pr_body",
]

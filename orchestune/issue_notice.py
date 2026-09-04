"""#787: エンジンの判断理由をIssueへ書き残す、供給元非依存の通知レイヤ。

Orchestuneがタスクを起動しない・ラベルを付け替える理由は、これまでCLIの
巨大なJSON出力か`events.jsonl`にしか現れなかった。`events.jsonl`はgitignore
対象でCI環境では実行のたびに揮発するため、運用者が後から「なぜ止まっていた
のか」を辿る場所が実質的に存在しない。判断の理由を対象Issue自身のコメントへ
残すことで、Issueを開けば経緯が読める状態にする。

重複投稿の抑止は、`pr_link_notice`が確立した「コメント本文へ埋め込んだHTML
マーカー」に依拠する。GitHub Actionsのランナーはサイクルごとに使い捨てられ、
ローカルの状態ファイルはサイクルをまたいで残らないため、冪等性の根拠は
GitHub上に置く必要がある。

`pr_link_notice`と異なり、判定は「投稿済みかどうか」ではなく**「本文（＝理由）が
変わったかどうか」**で行う。外部ロックのように毎サイクル再評価される状態は、
「新たにロックされた瞬間」だけを捉えると、ロックが継続している間に衝突相手が
変わった場合にIssue上の理由が古いまま取り残される。

供給元を`kind`で名前空間化しているのは、将来consistency supervisorのfindingを
`kind=f"finding:{code}"`として同じ仕組みに載せられるようにするため。
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping
from typing import Any

from orchestune.forge import Forge
from orchestune.models import normalize_newlines

#: stderrへ出す警告の接頭辞。`local-ci.ps1`を使うWindows(cp932)でも壊れない
#: よう、この経路には非ASCII文字を出さない。
WARN_PREFIX = "[orchestune:warn]"


def notice_marker(kind: str) -> str:
    """通知の供給元1件を一意に識別する、コメント本文へ埋め込む機械可読マーカー。"""
    return f"<!-- orchestune:notice:{kind} -->"


def render_notice(kind: str, body: str) -> str:
    return f"{notice_marker(kind)}\n{body}"


def latest_notice_body(comments: Iterable[Mapping[str, Any]], kind: str) -> str | None:
    """同じ`kind`の通知のうち、最後に投稿された本文を返す（無ければ`None`）。"""
    marker = notice_marker(kind)
    latest: str | None = None
    for comment in comments:
        body = normalize_newlines(str(comment.get("body") or ""))
        if body.startswith(marker):
            latest = body[len(marker) :].lstrip("\n")
    return latest


def _warn(message: str) -> None:
    print(f"{WARN_PREFIX} {message}", file=sys.stderr)


def post_notice_if_changed(
    forge: Forge,
    issue_number: int,
    kind: str,
    body: str,
    *,
    update_only: bool = False,
) -> bool:
    """理由が前回と変わっている場合だけ通知を投稿し、投稿したかどうかを返す。

    既存コメントを読めなかった場合はfail closed（投稿しない）で次サイクルの
    再試行に委ねる。読めないまま投稿すると、サイクルのたびに同じ理由を
    書き込み続けることになるため。

    `update_only`は「既に同じ`kind`の通知があるIssueにだけ書く」指定。状態の
    解消を伝える通知（例: 外部ロックの解除）に使う。理由を書いていないIssueへ
    解消だけを通知しても文脈が無く、ノイズにしかならない。
    """
    try:
        previous = latest_notice_body(forge.list_comments(issue_number), kind)
    except Exception as error:  # noqa: BLE001 - 通知はベストエフォート
        _warn(
            f"skipped the '{kind}' notice on issue #{issue_number}: "
            f"could not read existing comments ({type(error).__name__})"
        )
        return False

    if update_only and previous is None:
        return False
    if previous == normalize_newlines(body):
        return False

    try:
        forge.add_comment(issue_number, render_notice(kind, body))
    except Exception as error:  # noqa: BLE001 - 通知はベストエフォート
        _warn(
            f"failed to post the '{kind}' notice on issue #{issue_number} "
            f"({type(error).__name__})"
        )
        return False
    return True

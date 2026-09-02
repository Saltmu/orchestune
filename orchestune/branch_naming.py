"""Single source of truth for Orchestune task branch names (#777).

Task branches are assigned and pushed by Orchestune itself before an agent
starts working — see `orchestune.dispatch.targets._build_text` (the launch
instruction that tells the agent to `fetch`/`checkout` the assigned name
rather than create a new branch) and `.launch()` (which pushes it to origin
first). The name is therefore not a value callers should reconstruct with
their own string formatting: every writer and reader of a task branch name
must go through this module, so the naming convention only has to change in
one place and stays consistent across Dispatcher, Integrator, and status
reporting.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, TypeVar


class _HasHeadRef(Protocol):
    """PRのhead_ref・is_cross_repositoryだけを要求する構造的部分型。

    `orchestune.models.PrRecord`に依存せず、この規約モジュールを
    dependency-free（L0）に保つための最小限の型。読み取り専用の
    `@property`として宣言する必要がある — 通常の属性宣言だと
    setterまで要求され、frozen dataclassである`PrRecord`（読み取り専用
    属性）が構造的に一致しなくなる。
    """

    @property
    def head_ref(self) -> str: ...

    @property
    def is_cross_repository(self) -> bool | None: ...


_PrT = TypeVar("_PrT", bound=_HasHeadRef)


#: Default branch prefix, preserved for compatibility with every task branch
#: Orchestune has already assigned and pushed. Callers may pass a different
#: `prefix` to `build_task_branch_name`, but no caller does so today — there
#: is no current consumer that needs a non-default prefix (YAGNI); the
#: parameter exists so one can be wired in later without another rewrite.
DEFAULT_TASK_BRANCH_PREFIX = "claude"

_TASK_BRANCH_PATTERN = re.compile(
    r"^(?P<prefix>[^/]+)/issue-(?P<issue_number>\d+)-(?P<subtask_id>.+)$"
)


def build_task_branch_name(
    issue_number: int,
    subtask_id: str | None,
    *,
    prefix: str = DEFAULT_TASK_BRANCH_PREFIX,
) -> str:
    """タスクの正規ブランチ名を組み立てる。

    `subtask_id`が未指定/空の場合は`"task"`にフォールバックする（Orchestune起動時
    の命名規約と一致させる）。
    """
    return f"{prefix}/issue-{issue_number}-{subtask_id or 'task'}"


@dataclass(frozen=True)
class ParsedTaskBranch:
    prefix: str
    issue_number: int
    subtask_id: str


def parse_task_branch_name(branch: str) -> ParsedTaskBranch | None:
    """`<prefix>/issue-{N}-{subtask_id}`形状のブランチ名を分解する。

    prefixは特定ツール名に固定しない（`codex/`・`agy/`・人間が作成した
    `feat/`・`fix/`等のブランチも認識対象とする）。この形状に一致しない場合は
    `None`を返す。`parent/issue-{N}`（サブタスク接尾辞を持たないスタック用の
    ベースブランチ）はサブタスクブランチではないため一致しない。
    """
    match = _TASK_BRANCH_PATTERN.match(branch)
    if not match:
        return None
    return ParsedTaskBranch(
        prefix=match.group("prefix"),
        issue_number=int(match.group("issue_number")),
        subtask_id=match.group("subtask_id"),
    )


def branch_matches_task(
    branch: str, issue_number: int, subtask_id: str | None = None
) -> bool:
    """ブランチ名が指定Issue（・指定サブタスク）の正規形状に一致するかを判定する。

    `subtask_id`が`None`の場合はサブタスクを問わずIssue番号のみで判定する。
    """
    parsed = parse_task_branch_name(branch)
    if parsed is None or parsed.issue_number != issue_number:
        return False
    if subtask_id is not None and parsed.subtask_id != (subtask_id or "task"):
        return False
    return True


def find_unique_matching_pr_branch(
    prs: Sequence[_HasHeadRef], issue_number: int, subtask_id: str | None
) -> str | None:
    """PRのhead_refから、指定Issue・サブタスクに厳密一致するブランチ名を解決する。

    段階的解決の②（fail-closed設計、#777）: `issue_number`と`subtask_id`の
    両方を要求し、異なる複数のブランチ名がマッチした場合は`None`を返して
    呼び出し側にfail-closedさせる（曖昧な場合にtie-breakで1件へ絞らない）。
    同一ブランチ名を指す複数のPRレコード（closed→reopen等）は曖昧とみなさない。

    forkからのPR（`is_cross_repository`が`True`または不明な`None`）は除外する
    （Codexレビュー指摘、PR#780）: この関数が解決した名前は呼び出し側が
    upstream（`origin`）からfetchするため、forkのhead_refをそのまま信頼すると
    無関係なupstreamブランチを誤ってfetch/mergeする、またはforkの貢献を
    誤って却下する経路になる。`is_cross_repository is False`（既知のupstream
    PRであることが確認できた場合）のみ候補に含める。
    """
    matches = {
        pr.head_ref
        for pr in prs
        if pr.head_ref
        and pr.is_cross_repository is False
        and branch_matches_task(pr.head_ref, issue_number, subtask_id)
    }
    if len(matches) == 1:
        return next(iter(matches))
    return None


def find_verified_pr(prs: Sequence[_PrT], head_ref: str) -> _PrT | None:
    """`head_ref`に一致し、かつupstream（同一リポジトリ）由来と確認できた
    PRのみを返す。

    Codexレビュー指摘（PR#780 Round2/Round5）: `find_unique_matching_pr_branch`
    でブランチ名自体はfork安全に解決できても、その名前を鍵に素朴な
    `{pr.head_ref: pr}`辞書を引くと、forkが同じhead_ref文字列を名乗っている
    場合に辞書の重複キー上書き順序次第でforkのPRレコードを拾ってしまう
    （review_decision/is_ci_passing等がforkのものになる）。head_refが一致する
    PRを`is_cross_repository is False`まで確認しながら都度探索することで、
    この種の「名前は正しいがPRレコードが違う」誤りを防ぐ。
    """
    for pr in prs:
        if pr.head_ref == head_ref and pr.is_cross_repository is False:
            return pr
    return None

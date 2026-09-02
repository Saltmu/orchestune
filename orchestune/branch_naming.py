"""Single source of truth for Orchestune task branch names (#777).

Task branches are assigned and pushed by Orchestune itself before an agent
starts working — see `orchestune.dispatch.targets._build_text` (the launch
instruction that tells the agent to `fetch`/`checkout` the assigned name
rather than create a new branch) and `.launch()` (which pushes it to origin
first). The name is therefore not a value callers should reconstruct with
their own string formatting: every writer and reader of a task branch name
goes through this module, so the naming convention only has to change in one
place and stays consistent across Dispatcher, Integrator, and status
reporting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Default branch prefix. Kept at `claude` for compatibility with every task
#: branch Orchestune has already assigned and pushed, and because the Claude
#: Code cloud routine's default branch-push restriction only permits
#: `claude/`-prefixed branches (see `docs/*/setup.md`). Callers may pass a
#: different `prefix` to `build_task_branch_name`; no caller does so today,
#: so no configuration is wired up for it yet (YAGNI).
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

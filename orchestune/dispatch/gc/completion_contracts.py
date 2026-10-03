"""Completion values, application context and Forge diagnostics."""

import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import NamedTuple

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.rules import NotNeededReviewDispatcher
from orchestune.dispatch.summary import WARN_PREFIX, ascii_safe
from orchestune.ledger.run_state import ActiveWorktree, RunState
from orchestune.models import PrRecord
from orchestune.outcome_record import OutcomeRecord
from orchestune.task_metadata import TaskMetadata


@dataclass(frozen=True, slots=True)
class CompletedWorktreeDecision:
    action: str
    subtask_id: str = ""
    commit_sha: str | None = None
    outcome: OutcomeRecord | None = None
    operation: str = ""
    error: str = ""


class _CompletionContext(NamedTuple):
    active: ActiveWorktree
    active_task: TaskMetadata | None
    config: DispatcherConfig
    dispatch_not_needed_review: NotNeededReviewDispatcher | None
    run_state: RunState | None
    now: float
    open_prs: Sequence[PrRecord] | None
    on_early_death_requeue: Callable[[], None] | None
    on_review_timeout_requeue: Callable[[], None] | None


COMPLETION_HOLD_ACTIONS = frozenset(
    {
        "completion_skipped_dirty_worktree",
        "completion_skipped_forge_error",
        "completion_skipped_prior_merge_indeterminate",
    }
)


def is_completion_hold_event(event: Mapping[str, object]) -> bool:
    """Return whether a completion event must be excluded from same-cycle GC."""
    return event.get("action") in COMPLETION_HOLD_ACTIONS


class ForgeFailure(NamedTuple):
    """握り潰したForge呼び出し1件。どの操作がなぜ失敗したのかを対で保つ。

    PR#789レビュー対応(Codex P2): 説明文字列だけを集めると、呼び出し側が
    「どの操作が失敗したのか」を推測で補うことになる（オープンPRのコメント取得が
    失敗しても`list_prs`と報告されていた）。
    """

    operation: str
    description: str


def failed_operations(failures: Sequence[ForgeFailure]) -> str:
    """失敗した呼び出し名を重複なく並べる。空なら空文字。"""
    return ", ".join(dict.fromkeys(failure.operation for failure in failures))


def failure_descriptions(failures: Sequence[ForgeFailure]) -> str:
    """同じ説明は畳む。同一の障害で複数の呼び出しが落ちるのが普通のため。"""
    return "; ".join(dict.fromkeys(failure.description for failure in failures))


def describe_forge_error(error: Exception) -> str:
    """例外を1行へ縮める。原文はUTF-8のレポートに残すためここでは変換しない。"""
    detail = str(error).strip().splitlines()
    return f"{type(error).__name__}: {detail[0]}" if detail else type(error).__name__


def warn_forge_failure(
    operation: str,
    issue_number: int | None,
    error: Exception,
    error_sink: list[ForgeFailure] | None = None,
) -> str:
    """#787: Forge呼び出しの失敗を握り潰す直前に、その事実を必ず表に出す。

    これらの失敗はいずれも`"unknown"`という保守的な判定へ丸められる。無言で
    丸めると、API障害による保留とタスク側の問題が運用者から区別できない。

    stderrへ出す1行はWindows(cp932)のコンソールにも出るため`ascii_safe`を通す。
    例外メッセージは外部由来で非ASCII文字を含みうるが、ここで送出される
    `UnicodeEncodeError`は保守的な保留をサイクルの失敗に化けさせてしまう。
    原文はUTF-8で書かれるレポート側に`error_sink`経由で残す。
    """
    description = describe_forge_error(error)
    subject = f"issue #{issue_number}" if issue_number is not None else "the repository"
    print(
        ascii_safe(
            f"{WARN_PREFIX} forge API call '{operation}' failed for {subject}: "
            f"{description}"
        ),
        file=sys.stderr,
    )
    if error_sink is not None:
        error_sink.append(ForgeFailure(operation, description))
    return description

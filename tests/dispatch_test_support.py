"""dispatch系テストが共有する準備処理ファクトリ（#916）。

`tests/conftest.py`ではなく独立モジュールに置いている理由: conftest.pyは
全テストへ暗黙に作用するため、dispatch固有の既定値をそこへ足すと無関係な
テストの前提まで動かしてしまう。`test_`で始まらないためpytestには収集されない。

各ファクトリは「1つの型」と「1つの既定値セット」だけに責務を持つ。領域固有の
既定値（GC系のIssue 280、Context API系の`StatusLabel`既定など）は利用側
モジュールの薄いラッパーで上書きする。ここでは上書き可能な素の既定値のみを
持ち、テストごとの観点は利用側に残す。
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_actions import CycleActionAdapter
from orchestune.dispatch.dependency_resolution import resolve_all_dependencies
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.state import ActiveWorktree, RunState
from orchestune.models import IssueRecord, Task
from tests.conftest import make_issue

#: dispatch系テストが既定で使う作成時刻（タイムゾーン付きISO8601）。
DEFAULT_CREATED_AT = "2026-01-01T00:00:00+00:00"

#: `is_process_alive`をimportしている全モジュール。#479で`dispatch_gc`を
#: 分割した際に参照元が4箇所へ増えたため、どれか1つでもpatchし漏れると
#: GC判定が実プロセスを見に行ってしまう。
GC_PROCESS_ALIVE_TARGETS = (
    "orchestune.dispatch.execution_repair.is_process_alive",
    "orchestune.dispatch.gc.is_process_alive",
    "orchestune.dispatch.gc.completion.is_process_alive",
    "orchestune.dispatch.gc.zombies.is_process_alive",
)


def make_state_root(prefix: str = "orchestune-test-state-") -> Path:
    """`DispatcherConfig`の各パスを載せる一時ディレクトリを作る。

    モジュールごとに呼び出して別ディレクトリを持たせることで、
    `events.jsonl` / `run_state.json` の書き込みが他モジュールと衝突しない。
    """
    return Path(tempfile.mkdtemp(prefix=prefix))


_DEFAULT_STATE_ROOT = make_state_root()


def make_test_dispatcher_config(
    state_root: Path | None = None, **overrides: Any
) -> DispatcherConfig:
    """イベントログ・run_state・worktreeを`state_root`配下へ向けた設定。"""
    root = _DEFAULT_STATE_ROOT if state_root is None else state_root
    values: dict[str, Any] = {
        "events_log_path": root / "events.jsonl",
        "run_state_path": root / "run_state.json",
        "worktree_root": root / "worktrees",
    }
    values.update(overrides)
    return DispatcherConfig(**values)


def make_test_task(issue_number: int = 1, **overrides: Any) -> Task:
    """dispatch系テストの既定`Task`。

    既定値は「cycle系テストが最も多く使う形」に合わせてある
    （`subtask_id="task-a"`、`footprint=()`、`status:in-progress`）。
    GC系・Context API系の既定値差は利用側ラッパーで上書きする。
    """
    values: dict[str, Any] = {
        "issue_number": issue_number,
        "subtask_id": "task-a",
        "footprint": (),
        "symbols": (),
        "risk": False,
        "priority": "medium",
        "progress_partial": False,
        "status_labels": ("status:in-progress",),
        "created_at": DEFAULT_CREATED_AT,
        "depends_on": (),
    }
    values.update(overrides)
    return Task(**values)


def make_test_active_worktree(
    issue_number: int = 1, **overrides: Any
) -> ActiveWorktree:
    """dispatch系テストの既定`ActiveWorktree`（生存中のworktree）。

    `pid`/`started_at`を持つ「生きている」状態が既定。ゾンビ・タイムアウト
    回収系テストは`pid=None`や過去の`started_at`を明示的に上書きする。
    """
    values: dict[str, Any] = {
        "issue_number": issue_number,
        "branch": f"claude/issue-{issue_number}-task-a",
        "worktree_path": "worktrees/w1",
        "pid": 111,
        "started_at": 1_699_999_000.0,
        "declared_footprint": (),
    }
    values.update(overrides)
    return ActiveWorktree(**values)


def make_test_cycle_context(
    *,
    state_root: Path | None = None,
    resolve_dependencies: bool = False,
    action_now: float | None = None,
    **overrides: Any,
) -> CycleContext:
    """観測コンテナが空の`CycleContext`。

    - `resolve_dependencies=True`: `tasks_by_issue`だけを渡したテストのために
      `dependency_resolution`を`resolve_all_dependencies`で自動導出する
      （明示的に`dependency_resolution`を渡した場合はそちらを優先）。
    - `action_now`を指定した場合のみ`CycleActionAdapter`を生成してbindする。
      action portを使わないテストでは未bindのままにし、誤って副作用APIを
      呼んだときに`ValueError`で気づけるようにしている。
    """
    defaults: dict[str, Any] = {
        "run_state": RunState(active_worktrees={}),
        "tasks_by_issue": {},
        "dependency_resolution": {},
        "ci_passed_pr_issue_numbers": set(),
        "changes_requested_issue_numbers": set(),
        "branch_by_issue_number": {},
        "prs": [],
        "config": make_test_dispatcher_config(state_root),
    }
    defaults.update(overrides)
    if (
        resolve_dependencies
        and "dependency_resolution" not in overrides
        and "tasks_by_issue" in overrides
    ):
        defaults["dependency_resolution"] = resolve_all_dependencies(
            overrides["tasks_by_issue"]
        )
    if action_now is None:
        return CycleContext(**defaults)
    actions = CycleActionAdapter(
        defaults["run_state"], defaults["config"], now=action_now
    )
    ctx = CycleContext(**defaults, actions=actions)
    actions.bind_context(ctx)
    return ctx


def make_footprint_issue(
    number: int,
    labels: tuple[str, ...] = ("status:queued",),
    footprint: tuple[str, ...] = ("src/foo.py",),
    symbols: tuple[str, ...] = ("foo.Foo",),
    subtask_id: str | None = "task-a",
    depends_on: tuple[str, ...] = (),
    created_at: str = DEFAULT_CREATED_AT,
    parent_number: int | None = 181,
) -> IssueRecord:
    """Footprint YAMLブロックを持つ`IssueRecord`。

    `run_dispatch_cycle`をエンドツーエンドで駆動する系のテストが要求する
    フィールド（footprint/symbols/subtask_id/depends_on/parent_number）を持つ。
    YAML本文の組み立ては`tests/conftest.py`の`make_issue`へ委譲し、旧テスト群が
    前提とする`title="t"`と`parent_number`既定181だけをここで揃えている。
    """
    parent = {"number": parent_number} if parent_number is not None else None
    return make_issue(
        number,
        title="t",
        labels=labels,
        footprint=footprint,
        symbols=symbols,
        subtask_id=subtask_id,
        depends_on=depends_on,
        created_at=created_at,
        parent=parent,
    )


def make_plain_issue(
    number: int, labels: tuple[str, ...] = (), state: str = "OPEN"
) -> IssueRecord:
    """Footprintを持たない最小の`IssueRecord`。

    ラベル集合や`state`だけを見るフィルタ系テスト向け。本文を空にすることで
    「Footprintが無いIssue」の経路も同時に表現している。
    """
    return IssueRecord(
        number=number,
        title=f"Issue {number}",
        body="",
        labels=labels,
        created_at=DEFAULT_CREATED_AT,
        state=state,
    )


@contextmanager
def patch_gc_process_alive(*, return_value: bool) -> Iterator[None]:
    """`is_process_alive`の全参照元（#479の分割先）を一括でpatchする。"""
    with ExitStack() as stack:
        for target in GC_PROCESS_ALIVE_TARGETS:
            stack.enter_context(patch(target, return_value=return_value))
        yield


def stub_label_actor_permission(
    fake_forge: MagicMock,
    *,
    actor: str = "trusted-actor",
    permission: str = "write",
) -> None:
    """#119のactor権限検証が実際の`gh api`を叩かないよう許可済みの値を返す。

    検証ロジック自体のテストは`tests/test_dispatch_actor_verification.py`に
    集約しているため、それ以外のテストは本ヘルパーでopt-inする。
    """
    fake_forge.get_label_actor.reset_mock(side_effect=True)
    fake_forge.get_label_actor.return_value = actor
    fake_forge.get_actor_permission.reset_mock(side_effect=True)
    fake_forge.get_actor_permission.return_value = permission


def stub_forge_check_auth(fake_forge: MagicMock) -> MagicMock:
    """`GitHubForge.check_auth()`が実際のgh認証エラーを投げないようにする。"""
    check_auth: MagicMock = fake_forge.check_auth
    check_auth.reset_mock(side_effect=True)
    return check_auth

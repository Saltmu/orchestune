"""#664: GitHubのIssue本文はCRLFで返るため、マーカーブロックの読み書きが
LF決め打ちのままだと「置換されず追記」され続けて本文が肥大化する。

改行の往復をLFで閉じること、マーカーブロックの更新が冪等であること、
既に重複した本文を自己修復できることを検証する。
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest
import yaml

from orchestune.forge import GitHubForge
from orchestune.issue_parsing import (
    DECOMPOSITION_PLAN_MARKER,
    LAUNCH_HISTORY_MARKER,
    PARENT_MARKER,
    backfill_launch_history,
    decomposition_plan_from_parent_body,
    embed_decomposition_plan_in_parent_body,
    launch_history_from_body,
    restore_plan_markdown_from_parent_body,
)
from orchestune.models import IssueRecord


def _plan(**overrides: Any) -> dict:
    plan = {
        "title": "Big rock",
        "parent_issue_number": 486,
        "subtasks": [{"id": "task-a", "issue_number": None}],
    }
    plan.update(overrides)
    return plan


def _plan_block(plan: dict) -> str:
    dumped = yaml.dump(
        plan, allow_unicode=True, default_flow_style=False, sort_keys=False
    )
    return f"{DECOMPOSITION_PLAN_MARKER}\n```yaml\n{dumped}```\n"


def _body_with_plan_blocks(*plans: dict) -> str:
    blocks = "\n".join(_plan_block(plan) for plan in plans)
    return f"EPIC prose\n\n{PARENT_MARKER}\n\n{blocks}"


def _plan_block_count(body: str) -> int:
    return body.count(DECOMPOSITION_PLAN_MARKER)


# --- 分解計画ブロック --------------------------------------------------------


def test_embed_replaces_block_in_crlf_body():
    """GitHubが返すCRLF本文でもブロックは置換され、追記されない。"""
    body = _body_with_plan_blocks(_plan()).replace("\n", "\r\n")

    updated = embed_decomposition_plan_in_parent_body(
        body, _plan(subtasks=[{"id": "task-a", "issue_number": 652}])
    )

    assert _plan_block_count(updated) == 1
    assert (
        decomposition_plan_from_parent_body(updated)["subtasks"][0]["issue_number"]
        == 652
    )


def test_embed_collapses_already_duplicated_blocks():
    """#486のように重複済みの本文は、次回の同期で1ブロックへ自己修復される。"""
    body = _body_with_plan_blocks(
        _plan(subtasks=[{"id": "task-a", "issue_number": None}]),
        _plan(subtasks=[{"id": "task-a", "issue_number": 652}]),
        _plan(subtasks=[{"id": "task-a", "issue_number": 652}]),
    )
    assert _plan_block_count(body) == 3

    updated = embed_decomposition_plan_in_parent_body(
        body, _plan(subtasks=[{"id": "task-a", "issue_number": 653}])
    )

    assert _plan_block_count(updated) == 1
    assert PARENT_MARKER in updated
    assert updated.startswith("EPIC prose")


def test_plan_extraction_prefers_the_last_block():
    """重複した本文からは最も新しい（最後の）ブロックを採用する。"""
    body = _body_with_plan_blocks(
        _plan(subtasks=[{"id": "task-a", "issue_number": None}]),
        _plan(subtasks=[{"id": "task-a", "issue_number": 652}]),
    )

    extracted = decomposition_plan_from_parent_body(body)

    assert extracted["subtasks"][0]["issue_number"] == 652


def test_decomposition_plan_from_crlf_body():
    body = _body_with_plan_blocks(_plan()).replace("\n", "\r\n")

    extracted = decomposition_plan_from_parent_body(body)

    assert extracted is not None
    assert extracted["title"] == "Big rock"


def test_restore_plan_markdown_from_crlf_parent_body():
    body = _body_with_plan_blocks(_plan()).replace("\n", "\r\n")

    restored = restore_plan_markdown_from_parent_body(body)

    assert restored is not None
    assert restored.startswith("---\n")
    assert "title: Big rock" in restored
    assert "\r" not in restored


# --- launch history ブロック（同型のバグ） -----------------------------------


def _launch_history_body(timestamps: list[float]) -> str:
    dumped = yaml.dump({"launch_history": timestamps}, default_flow_style=False)
    return f"EPIC prose\n\n{LAUNCH_HISTORY_MARKER}\n```yaml\n{dumped}```\n"


def test_launch_history_from_crlf_body():
    body = _launch_history_body([1000.0, 1001.0]).replace("\n", "\r\n")

    assert launch_history_from_body(body) == [1000.0, 1001.0]


def test_backfill_launch_history_does_not_duplicate_block_in_crlf_body():
    body = _launch_history_body([1000.0]).replace("\n", "\r\n")

    updated = backfill_launch_history(body, [1000.0, 1002.0])

    assert updated is not None
    assert updated.count(LAUNCH_HISTORY_MARKER) == 1
    assert launch_history_from_body(updated) == [1000.0, 1002.0]


def test_backfill_launch_history_returns_none_when_crlf_body_already_matches():
    """CRLF本文でも「同じ値なら書き込まない」最適化が効くこと。"""
    body = _launch_history_body([1000.0]).replace("\n", "\r\n")

    assert backfill_launch_history(body, [1000.0]) is None


# --- 改行の往復をLFで閉じる --------------------------------------------------


def test_issue_record_normalizes_body_newlines():
    record = IssueRecord(
        number=1,
        title="t",
        body="line1\r\nline2\rline3\n",
        labels=(),
        created_at="",
    )

    assert record.body == "line1\nline2\nline3\n"


def test_run_writes_stdin_without_os_newline_translation(monkeypatch):
    """Windowsのtext=Trueなstdin書き込みは`\\n`を`os.linesep`へ変換するため、
    LF正規化した本文がGitHub上でCRLFへ戻ってしまう。バイト列で渡して防ぐ。"""
    captured: dict[str, Any] = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    GitHubForge().update_issue_body(486, "line1\r\nline2\n")

    sent = captured["kwargs"]["input"]
    assert isinstance(sent, bytes), "stdinはバイト列で渡す（OSの改行変換を回避する）"
    assert b"\r" not in sent
    assert not captured["kwargs"].get("text", False)


if __name__ == "__main__":  # pragma: no cover - convenience for manual runs
    raise SystemExit(pytest.main([__file__]))

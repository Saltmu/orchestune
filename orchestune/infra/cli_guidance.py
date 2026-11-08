"""Render operator guidance as text. This module never runs a command."""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Sequence
from subprocess import list2cmdline

from orchestune.operator_guidance import (
    GuidanceAction,
    OperatorGuidance,
    ProgressOutlook,
)

_PLACEHOLDER = re.compile(r"<[A-Za-z0-9_-]+>")


def command_is_resolved(argv: Sequence[str]) -> bool:
    """True when every argument is present and free of unresolved placeholders."""
    return bool(argv) and all(_PLACEHOLDER.search(part) is None for part in argv)


def quote_command(argv: Sequence[str], *, posix: bool | None = None) -> str:
    """Quote an argv list for the current shell, or for the requested family."""
    use_posix = os.name != "nt" if posix is None else posix
    parts = [part.replace("\\", "/") for part in argv]
    if use_posix:
        return shlex.join(parts)
    return list2cmdline(parts)


def render_guidance(guidance: OperatorGuidance, *, posix: bool | None = None) -> str:
    """Render one guidance value. Unknown fields stay visible as unknown."""
    lines = [
        "案内:",
        f"  対象: {guidance.target}",
        f"  段階: {guidance.stage}",
        f"  理由: {_reason(guidance)}",
        f"  次の担当: {guidance.next_owner.value}",
        f"  自動進行: {_progress(guidance)}",
        f"  根拠: {guidance.evidence_state.value}（{guidance.evidence_note}）",
        *_action_lines(guidance.actions, posix=posix),
    ]
    if guidance.references:
        lines.append("  参照: " + ", ".join(guidance.references))
    return "\n".join(lines)


def _reason(guidance: OperatorGuidance) -> str:
    if guidance.reason_codes and guidance.explanation:
        codes = ", ".join(guidance.reason_codes)
        return f"{codes}（{guidance.explanation}）"
    if guidance.reason_codes:
        return ", ".join(guidance.reason_codes)
    if guidance.explanation:
        return guidance.explanation
    return "不明"


def _progress(guidance: OperatorGuidance) -> str:
    if guidance.progress is not ProgressOutlook.CONDITIONAL:
        return guidance.progress.value
    condition = guidance.progress_condition or "不明"
    return f"conditional（{condition}）"


def _action_lines(
    actions: tuple[GuidanceAction, ...], *, posix: bool | None
) -> list[str]:
    if not actions:
        return ["  次の操作: なし"]
    lines = ["  次の操作:"]
    for action in actions:
        lines.append(f"    - {action.description}")
        lines.extend(_argv_lines(action, posix=posix))
        if action.preconditions:
            lines.append("      先に確認: " + " / ".join(action.preconditions))
        if action.references:
            lines.append("      参照: " + ", ".join(action.references))
    return lines


def _argv_lines(action: GuidanceAction, *, posix: bool | None) -> list[str]:
    if not action.argv:
        return []
    if command_is_resolved(action.argv):
        quoted = quote_command(action.argv, posix=posix)
        return [f"      コマンド: {quoted}"]
    return ["      手順: 未解決の値があるためコマンドとしては表示しません"]

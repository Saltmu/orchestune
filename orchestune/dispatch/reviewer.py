"""Reviewer selection policy for autonomous dispatch runs."""

from __future__ import annotations

from typing import Literal

ReviewerBot = Literal["claude", "codex"]
ReviewerBotSetting = Literal["auto", "claude", "codex"]

_AUTO_REVIEWER_BY_TARGET: dict[str, ReviewerBot] = {
    "claude-cli": "codex",
    "cloud-routine": "codex",
    "codex-cli": "claude",
    "codex-cloud": "claude",
    "agy-cli": "claude",
}


def resolve_reviewer_bot(
    setting: ReviewerBotSetting, resolved_target_name: str
) -> ReviewerBot | None:
    """Resolve an explicit or cross-vendor reviewer without side effects.

    Generic targets such as ``local`` return ``None`` for ``auto`` because the
    execution vendor cannot be inferred reliably from an arbitrary command.
    """
    if setting == "claude" or setting == "codex":
        return setting
    if setting != "auto":
        raise ValueError(f"unsupported reviewer bot: {setting}")
    return _AUTO_REVIEWER_BY_TARGET.get(resolved_target_name)

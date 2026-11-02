"""Reviewer selection policy for autonomous dispatch runs."""

from __future__ import annotations

from collections.abc import Mapping

from orchestune.targets.contracts import (
    ReviewerBot as ReviewerBot,
)
from orchestune.targets.contracts import (
    ReviewerBotSetting as ReviewerBotSetting,
)

_AUTO_REVIEWER_BY_TARGET: dict[str, ReviewerBot] = {
    "claude-cli": "codex",
    "cloud-routine": "codex",
    "codex-cli": "claude",
    "codex-cloud": "claude",
    "agy-cli": "claude",
    "grok-cli": "claude",
}


def resolve_reviewer_bot(
    setting: ReviewerBotSetting,
    resolved_target_name: str,
    overrides: Mapping[str, ReviewerBot] | None = None,
) -> ReviewerBot | None:
    """Resolve an explicit or cross-vendor reviewer without side effects.

    An explicit ``claude``/``codex`` setting always wins. For ``auto``, a
    ``[reviewer_by_target]`` entry (``overrides``, #1560) replaces the built-in
    cross-vendor default of that one target; unlisted targets keep it.

    Generic targets such as ``local`` return ``None`` for ``auto`` unless the
    configuration names a reviewer, because the execution vendor cannot be
    inferred reliably from an arbitrary command.
    """
    if setting == "claude" or setting == "codex":
        return setting
    if setting != "auto":
        raise ValueError(f"unsupported reviewer bot: {setting}")
    if overrides and resolved_target_name in overrides:
        return overrides[resolved_target_name]
    return _AUTO_REVIEWER_BY_TARGET.get(resolved_target_name)

"""How a Layer-2 reviewer was chosen and what an operator is told (#1436)."""

from __future__ import annotations

import pytest

from orchestune.dispatch.semantic_review_diagnostics import describe_reviewer


@pytest.mark.parametrize(
    ("provider", "target", "reviewer", "origin_has"),
    [
        ("codex", "claude-cli", "codex", "explicit"),
        ("claude", "codex-cli", "claude", "explicit"),
        ("auto", "codex-cli", "claude", "dispatch target `codex-cli`"),
        ("auto", "claude-cli", "codex", "dispatch target `claude-cli`"),
        ("auto", "local", None, "cannot be resolved"),
        ("other", "local", None, "unsupported"),
    ],
)
def test_reviewer_origin_distinguishes_explicit_from_auto(
    provider: str, target: str, reviewer: str | None, origin_has: str
) -> None:
    resolution = describe_reviewer(provider, target)
    assert resolution.reviewer == reviewer
    assert origin_has in resolution.origin

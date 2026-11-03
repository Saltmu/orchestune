"""How a Layer-2 reviewer was chosen and what an operator is told (#1436)."""

from __future__ import annotations

import pytest

from orchestune.dispatch.semantic_review.diagnostics import describe_reviewer


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


def test_reviewer_origin_names_the_configured_override() -> None:
    resolution = describe_reviewer("auto", "grok-cli", {"grok-cli": "codex"})
    assert resolution.reviewer == "codex"
    assert "reviewer_by_target" in resolution.origin
    assert "grok-cli" in resolution.origin


def test_reviewer_origin_for_unlisted_target_stays_cross_vendor_default() -> None:
    resolution = describe_reviewer("auto", "codex-cli", {"grok-cli": "codex"})
    assert resolution.reviewer == "claude"
    assert "reviewer_by_target" not in resolution.origin


def test_override_can_resolve_a_target_auto_cannot() -> None:
    resolution = describe_reviewer("auto", "local", {"local": "claude"})
    assert resolution.reviewer == "claude"


def test_explicit_provider_ignores_overrides() -> None:
    resolution = describe_reviewer("codex", "grok-cli", {"grok-cli": "claude"})
    assert resolution.reviewer == "codex"
    assert "explicit" in resolution.origin

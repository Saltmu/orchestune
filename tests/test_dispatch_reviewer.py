import pytest

from orchestune.dispatch.reviewer import resolve_reviewer_bot


@pytest.mark.parametrize(
    ("target_name", "expected"),
    [
        ("claude-cli", "codex"),
        ("cloud-routine", "codex"),
        ("codex-cli", "claude"),
        ("codex-cloud", "claude"),
        ("agy-cli", "claude"),
    ],
)
def test_auto_reviewer_uses_cross_vendor_mapping(target_name, expected):
    assert resolve_reviewer_bot("auto", target_name) == expected


@pytest.mark.parametrize("explicit", ["claude", "codex"])
def test_explicit_reviewer_overrides_target_mapping(explicit):
    assert resolve_reviewer_bot(explicit, "claude-cli") == explicit


def test_auto_reviewer_is_unresolved_for_generic_local_target():
    assert resolve_reviewer_bot("auto", "local") is None


def test_invalid_reviewer_setting_is_rejected():
    with pytest.raises(ValueError, match="unsupported reviewer bot"):
        resolve_reviewer_bot("gemini", "claude-cli")  # type: ignore[arg-type]

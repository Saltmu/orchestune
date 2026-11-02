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
        ("grok-cli", "claude"),
    ],
)
def test_auto_reviewer_uses_cross_vendor_mapping(target_name, expected):
    assert resolve_reviewer_bot("auto", target_name) == expected


@pytest.mark.parametrize("explicit", ["claude", "codex"])
def test_explicit_reviewer_overrides_target_mapping(explicit):
    assert resolve_reviewer_bot(explicit, "claude-cli") == explicit


def test_auto_reviewer_is_unresolved_for_generic_local_target():
    assert resolve_reviewer_bot("auto", "local") is None


def test_unresolved_reviewer_instruction_records_skip_and_human_gate():
    from orchestune.targets.support import noninteractive_instruction

    text = noninteractive_instruction(resolve_reviewer_bot("auto", "local"))
    assert "--bot-name skip" in text
    assert "推測せず" in text
    assert "status:blocked-human-review" in text
    assert "既定required" in text


def test_invalid_reviewer_setting_is_rejected():
    with pytest.raises(ValueError, match="unsupported reviewer bot"):
        resolve_reviewer_bot("gemini", "claude-cli")  # type: ignore[arg-type]


# #1560: `[reviewer_by_target]` overrides the built-in auto mapping per target.
USER_MAPPING = {"agy-cli": "claude", "claude-cli": "codex", "grok-cli": "codex"}


@pytest.mark.parametrize(
    ("target_name", "expected"),
    [
        ("agy-cli", "claude"),
        ("claude-cli", "codex"),
        ("grok-cli", "codex"),
        # Not listed: the built-in cross-vendor default still applies.
        ("codex-cli", "claude"),
        ("codex-cloud", "claude"),
        ("cloud-routine", "codex"),
    ],
)
def test_auto_reviewer_overrides_are_partial(target_name, expected):
    assert resolve_reviewer_bot("auto", target_name, USER_MAPPING) == expected


def test_override_can_resolve_the_generic_local_target():
    assert resolve_reviewer_bot("auto", "local", {"local": "claude"}) == "claude"


def test_override_can_replace_a_builtin_default_with_the_same_vendor():
    assert resolve_reviewer_bot("auto", "claude-cli", {"claude-cli": "claude"}) == (
        "claude"
    )


@pytest.mark.parametrize("explicit", ["claude", "codex"])
def test_explicit_reviewer_beats_overrides(explicit):
    other = "codex" if explicit == "claude" else "claude"
    assert resolve_reviewer_bot(explicit, "grok-cli", {"grok-cli": other}) == explicit


@pytest.mark.parametrize("overrides", [None, {}])
def test_empty_overrides_keep_the_builtin_mapping(overrides):
    assert resolve_reviewer_bot("auto", "grok-cli", overrides) == "claude"
    assert resolve_reviewer_bot("auto", "local", overrides) is None


def test_overrides_do_not_mutate_the_builtin_mapping():
    resolve_reviewer_bot("auto", "grok-cli", USER_MAPPING)
    assert resolve_reviewer_bot("auto", "grok-cli") == "claude"

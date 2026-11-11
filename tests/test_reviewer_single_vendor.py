import pytest

from orchestune.dispatch.reviewer import (
    resolve_reviewer_bot,
    reviewer_for_installed_vendors,
)


@pytest.mark.parametrize(
    "installed, expected",
    [
        (None, "codex"),
        (frozenset(), "codex"),
        (frozenset({"claude", "codex"}), "codex"),
        (frozenset({"claude"}), "claude"),
        (frozenset({"codex"}), "codex"),
    ],
)
def test_single_vendor(installed, expected):
    assert resolve_reviewer_bot("auto", "claude-cli") == "codex"
    assert (
        reviewer_for_installed_vendors("auto", "claude-cli", {}, installed) == expected
    )


def test_explicit_override_and_unknown():
    installed = frozenset({"claude"})
    assert (
        reviewer_for_installed_vendors("codex", "claude-cli", {}, installed) == "codex"
    )
    assert (
        reviewer_for_installed_vendors(
            "auto", "claude-cli", {"claude-cli": "codex"}, installed
        )
        == "codex"
    )
    assert reviewer_for_installed_vendors("auto", "local", {}, installed) is None


@pytest.mark.parametrize("cli", ["claude", "agy", "codex", "grok", None])
def test_detection_injected(cli):
    from orchestune.dispatch.targets import detect_installed_local_cli

    assert (
        detect_installed_local_cli(which_fn=lambda name: name if name == cli else None)
        == cli
    )


def test_loader_snapshot_and_semantic_provider(tmp_path, monkeypatch, fake_forge):
    from orchestune.dispatch.config import loader
    from orchestune.dispatch.config.loader import load_and_resolve_config
    from orchestune.dispatch.postcycle import _final_acceptance_reviewer
    from orchestune.dispatch.semantic_review.diagnostics import describe_reviewer

    calls = []

    def which(name):
        calls.append(name)
        return name if name == "claude" else None

    monkeypatch.setattr(loader.shutil, "which", which)
    captured = []
    from types import SimpleNamespace

    config = load_and_resolve_config(
        ["--parent-issue", "1", "--dispatch-target", "claude-cli"],
        cwd=tmp_path,
        build_target_fn=lambda cfg: captured.append(cfg)
        or SimpleNamespace(target_name="claude-cli"),
        resolve_paths_fn=lambda args, cwd: (
            tmp_path / "state.json",
            tmp_path / "worktrees",
        ),
    )
    assert calls == ["claude", "codex"]
    assert captured[0].installed_review_vendors is config.installed_review_vendors
    assert _final_acceptance_reviewer(config) == "claude"
    assert (
        "PATH"
        in describe_reviewer(
            "auto", "claude-cli", {}, config.installed_review_vendors
        ).origin
    )
    config.semantic_review_provider = "codex"
    assert _final_acceptance_reviewer(config) == "codex"


@pytest.mark.parametrize(
    "setting, installed, changed",
    [
        ("auto", frozenset({"claude"}), True),
        ("auto", frozenset(), False),
        ("auto", frozenset({"claude", "codex"}), False),
        ("codex", frozenset({"claude"}), False),
    ],
)
def test_dispatch_warning_only_when_changed(
    tmp_path, capsys, setting, installed, changed
):
    from orchestune.dispatch.targets import TargetBuildConfig, build_dispatch_target

    config = TargetBuildConfig(
        "claude-cli",
        None,
        None,
        tmp_path,
        allow_unsafe_agent_execution=True,
        reviewer_bot=setting,
        installed_review_vendors=installed,
    )
    target = build_dispatch_target(config)
    assert target.target_name == "claude-cli"
    assert ("only that review CLI" in capsys.readouterr().err) is changed


def test_cloud_fallback_does_not_announce_unused_reviewer(tmp_path, capsys):
    from orchestune.dispatch.targets import TargetBuildConfig, build_dispatch_target

    target = build_dispatch_target(
        TargetBuildConfig(
            "cloud-routine",
            None,
            None,
            tmp_path,
            installed_review_vendors=frozenset({"claude"}),
        )
    )
    assert target.target_name == "local"
    assert "only that review CLI" not in capsys.readouterr().err

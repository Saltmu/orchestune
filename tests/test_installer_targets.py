from pathlib import Path

import pytest

from orchestune.installer.contracts import ScopeType, TargetResolutionError, TargetType
from orchestune.installer.targets import find_project_root, resolve_targets


def test_resolve_single_target_project_codex(tmp_path: Path):
    project_dir = tmp_path / "my_project"
    project_dir.mkdir()
    roots = resolve_targets(
        targets=[TargetType.CODEX],
        scope=ScopeType.PROJECT,
        project_dir=project_dir,
    )
    assert len(roots) == 1
    assert roots[0].target_types == [TargetType.CODEX]
    assert roots[0].scope == ScopeType.PROJECT
    assert roots[0].path == project_dir / ".agents" / "skills"


def test_resolve_single_target_project_claude(tmp_path: Path):
    project_dir = tmp_path / "my_project"
    project_dir.mkdir()
    roots = resolve_targets(
        targets=[TargetType.CLAUDE],
        scope=ScopeType.PROJECT,
        project_dir=project_dir,
    )
    assert len(roots) == 1
    assert roots[0].target_types == [TargetType.CLAUDE]
    assert roots[0].scope == ScopeType.PROJECT
    assert roots[0].path == project_dir / ".claude" / "skills"


def test_resolve_single_target_project_antigravity(tmp_path: Path):
    project_dir = tmp_path / "my_project"
    project_dir.mkdir()
    roots_ide = resolve_targets(
        targets=[TargetType.ANTIGRAVITY],
        scope=ScopeType.PROJECT,
        project_dir=project_dir,
    )
    assert len(roots_ide) == 1
    assert roots_ide[0].path == project_dir / ".agents" / "skills"

    roots_cli = resolve_targets(
        targets=[TargetType.ANTIGRAVITY_CLI],
        scope=ScopeType.PROJECT,
        project_dir=project_dir,
    )
    assert len(roots_cli) == 1
    assert roots_cli[0].path == project_dir / ".agents" / "skills"


def test_resolve_project_deduplication(tmp_path: Path):
    project_dir = tmp_path / "my_project"
    project_dir.mkdir()
    roots = resolve_targets(
        targets=[TargetType.CODEX, TargetType.ANTIGRAVITY, TargetType.ANTIGRAVITY_CLI],
        scope=ScopeType.PROJECT,
        project_dir=project_dir,
    )
    assert len(roots) == 1
    assert sorted(t.value for t in roots[0].target_types) == [
        "antigravity",
        "antigravity-cli",
        "codex",
    ]
    assert roots[0].path == project_dir / ".agents" / "skills"


def test_resolve_all_project_roots(tmp_path: Path):
    project_dir = tmp_path / "my_project"
    project_dir.mkdir()
    roots = resolve_targets(
        targets=[TargetType.ALL],
        scope=ScopeType.PROJECT,
        project_dir=project_dir,
    )
    assert len(roots) == 2
    paths = {r.path for r in roots}
    assert paths == {
        project_dir / ".agents" / "skills",
        project_dir / ".claude" / "skills",
    }


def test_resolve_user_targets(tmp_path: Path):
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()

    roots = resolve_targets(
        targets=[TargetType.CODEX],
        scope=ScopeType.USER,
        home=fake_home,
    )
    assert len(roots) == 1
    assert roots[0].path == fake_home / ".agents" / "skills"

    roots_claude = resolve_targets(
        targets=[TargetType.CLAUDE],
        scope=ScopeType.USER,
        home=fake_home,
    )
    assert len(roots_claude) == 1
    assert roots_claude[0].path == fake_home / ".claude" / "skills"

    roots_ag = resolve_targets(
        targets=[TargetType.ANTIGRAVITY],
        scope=ScopeType.USER,
        home=fake_home,
    )
    assert len(roots_ag) == 1
    assert roots_ag[0].path == fake_home / ".gemini" / "config" / "skills"

    roots_ag_cli = resolve_targets(
        targets=[TargetType.ANTIGRAVITY_CLI],
        scope=ScopeType.USER,
        home=fake_home,
    )
    assert len(roots_ag_cli) == 1
    assert roots_ag_cli[0].path == fake_home / ".gemini" / "antigravity-cli" / "skills"


def test_resolve_all_user_targets(tmp_path: Path):
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    roots = resolve_targets(
        targets=[TargetType.ALL],
        scope=ScopeType.USER,
        home=fake_home,
    )
    assert len(roots) == 4
    paths = {r.path for r in roots}
    assert paths == {
        fake_home / ".agents" / "skills",
        fake_home / ".claude" / "skills",
        fake_home / ".gemini" / "config" / "skills",
        fake_home / ".gemini" / "antigravity-cli" / "skills",
    }


def test_mixing_all_and_individual_targets_rejected():
    with pytest.raises(
        TargetResolutionError, match="Cannot mix 'all' with specific targets"
    ):
        resolve_targets(
            targets=[TargetType.ALL, TargetType.CODEX],
            scope=ScopeType.PROJECT,
        )


def test_skills_dir_override(tmp_path: Path):
    custom_dir = tmp_path / "custom" / "skills"
    roots = resolve_targets(
        targets=[TargetType.CODEX],
        scope=ScopeType.USER,
        skills_dir=custom_dir,
    )
    assert len(roots) == 1
    assert roots[0].path == custom_dir
    assert roots[0].is_custom_skills_dir is True


def test_skills_dir_override_rejected_for_multiple_targets(tmp_path: Path):
    custom_dir = tmp_path / "custom" / "skills"
    with pytest.raises(
        TargetResolutionError,
        match="--skills-dir can only be used with a single target",
    ):
        resolve_targets(
            targets=[TargetType.CODEX, TargetType.CLAUDE],
            scope=ScopeType.USER,
            skills_dir=custom_dir,
        )

    with pytest.raises(
        TargetResolutionError,
        match="--skills-dir can only be used with a single target",
    ):
        resolve_targets(
            targets=[TargetType.ALL],
            scope=ScopeType.PROJECT,
            skills_dir=custom_dir,
        )


def test_find_project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Git root simulation
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()
    sub_dir = repo_root / "subdir" / "nested"
    sub_dir.mkdir(parents=True)

    found = find_project_root(sub_dir)
    assert found == repo_root.resolve()

    # Non-git directory falls back to cwd
    non_git = tmp_path / "non_git"
    non_git.mkdir()
    # Model absent Git markers explicitly: the host's temporary directory may
    # itself belong to a repository, independently of this test's fixtures.
    with monkeypatch.context() as isolated:
        isolated.setattr(Path, "is_dir", lambda _: False)
        isolated.setattr(Path, "is_file", lambda _: False)
        assert find_project_root(non_git) == non_git.resolve()

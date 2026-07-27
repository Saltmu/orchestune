from pathlib import Path
from unittest.mock import patch


def test_create_skill_link_copies_on_windows_privilege_error(tmp_path):
    from orchestune.setup_skills import _create_skill_link

    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("skill contents", encoding="utf-8")
    destination = tmp_path / "destination"

    privilege_error = OSError("symlink privilege is not available")
    privilege_error.winerror = 1314

    with (
        patch("orchestune.setup_skills.sys.platform", "win32"),
        patch("pathlib.Path.symlink_to", side_effect=privilege_error),
    ):
        result = _create_skill_link(source, destination)

    assert result == "copied"
    assert not destination.is_symlink()
    assert (destination / "SKILL.md").read_text(encoding="utf-8") == "skill contents"


def test_create_skill_link_does_not_hide_other_errors(tmp_path):
    import pytest

    from orchestune.setup_skills import _create_skill_link

    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "destination"

    other_error = OSError("unexpected error")
    other_error.winerror = 5

    with (
        patch("orchestune.setup_skills.sys.platform", "win32"),
        patch("pathlib.Path.symlink_to", side_effect=other_error),
        pytest.raises(OSError, match="unexpected error"),
    ):
        _create_skill_link(source, destination)


def test_setup_skills_creates_links(tmp_path):
    from orchestune.setup_skills import setup_skills

    mock_home = tmp_path / "home"
    mock_home.mkdir()
    # 親フォルダがあらかじめ存在している場合のみシンボリックリンクを作成することを確認
    (mock_home / ".claude").mkdir()
    (mock_home / ".codex").mkdir()
    (mock_home / ".gemini").mkdir()

    mock_source = tmp_path / "orchestune_repo"
    mock_source.mkdir()
    skills_dir = mock_source / "skills"
    skills_dir.mkdir()
    (skills_dir / "orchestune").mkdir()
    (skills_dir / "orchestune" / "SKILL.md").touch()

    with (
        patch("pathlib.Path.home", return_value=mock_home),
        patch("pathlib.Path.cwd", return_value=mock_source),
    ):
        setup_skills()

    claude_target = mock_home / ".claude" / "skills" / "orchestune"
    codex_target = mock_home / ".codex" / "skills" / "orchestune"
    gemini_target = mock_home / ".gemini" / "config" / "skills" / "orchestune"

    for target in (claude_target, codex_target, gemini_target):
        assert target.is_dir()
        assert (target / "SKILL.md").is_file()
        if target.is_symlink():
            assert target.resolve() == skills_dir / "orchestune"


def test_setup_skills_skips_when_no_parent(tmp_path):
    from orchestune.setup_skills import setup_skills

    mock_home = tmp_path / "home"
    mock_home.mkdir()
    # 親フォルダを一切作成しない

    mock_source = tmp_path / "orchestune_repo"
    mock_source.mkdir()
    skills_dir = mock_source / "skills"
    skills_dir.mkdir()
    (skills_dir / "orchestune").mkdir()
    (skills_dir / "orchestune" / "SKILL.md").touch()

    with (
        patch("pathlib.Path.home", return_value=mock_home),
        patch("pathlib.Path.cwd", return_value=mock_source),
    ):
        setup_skills()

    # どのフォルダもシンボリックリンクも作成されていないことを検証
    assert not (mock_home / ".claude").exists()
    assert not (mock_home / ".codex").exists()
    assert not (mock_home / ".gemini").exists()


def test_setup_skills_already_exists(tmp_path, capsys):
    from orchestune.setup_skills import setup_skills

    mock_home = tmp_path / "home"
    mock_home.mkdir()
    (mock_home / ".claude").mkdir()

    mock_source = tmp_path / "orchestune_repo"
    mock_source.mkdir()
    skills_dir = mock_source / "skills"
    skills_dir.mkdir()
    (skills_dir / "orchestune").mkdir()
    (skills_dir / "orchestune" / "SKILL.md").touch()

    # すでにターゲットが存在している状態を作る
    claude_dir = mock_home / ".claude" / "skills"
    claude_dir.mkdir(parents=True)
    existing_link = claude_dir / "orchestune"
    existing_link.mkdir()

    with (
        patch("pathlib.Path.home", return_value=mock_home),
        patch("pathlib.Path.cwd", return_value=mock_source),
    ):
        setup_skills()

    captured = capsys.readouterr()
    assert "Skipped" in captured.out or "already exists" in captured.out


def test_get_skills_source_dir_fallback_parent(tmp_path):
    from orchestune.setup_skills import get_skills_source_dir

    mock_cwd = tmp_path / "other_dir"
    mock_cwd.mkdir()

    fake_pkg_file = tmp_path / "site-packages" / "orchestune" / "setup_skills.py"
    fake_pkg_file.parent.mkdir(parents=True)
    fake_pkg_file.touch()

    fake_parent_skills = tmp_path / "site-packages" / "skills"
    fake_parent_skills.mkdir()
    (fake_parent_skills / "orchestune").mkdir()

    with (
        patch("pathlib.Path.cwd", return_value=mock_cwd),
        patch("orchestune.setup_skills.__file__", str(fake_pkg_file)),
    ):
        result = get_skills_source_dir()
        assert result == fake_parent_skills


def test_get_skills_source_dir_fallback_pkg_dir(tmp_path):
    from orchestune.setup_skills import get_skills_source_dir

    mock_cwd = tmp_path / "other_dir"
    mock_cwd.mkdir()

    fake_pkg_file = tmp_path / "site-packages" / "orchestune" / "setup_skills.py"
    fake_pkg_file.parent.mkdir(parents=True)
    fake_pkg_file.touch()

    fake_pkg_skills = tmp_path / "site-packages" / "orchestune" / "skills"
    fake_pkg_skills.mkdir()
    (fake_pkg_skills / "orchestune").mkdir()

    with (
        patch("pathlib.Path.cwd", return_value=mock_cwd),
        patch("orchestune.setup_skills.__file__", str(fake_pkg_file)),
    ):
        result = get_skills_source_dir()
        assert result == fake_pkg_skills


def test_setup_skills_returns_0_on_full_success(tmp_path):
    from orchestune.setup_skills import setup_skills

    mock_home = tmp_path / "home"
    mock_home.mkdir()
    (mock_home / ".claude").mkdir()

    mock_source = tmp_path / "orchestune_repo"
    mock_source.mkdir()
    skills_dir = mock_source / "skills"
    skills_dir.mkdir()
    (skills_dir / "orchestune").mkdir()
    (skills_dir / "orchestune" / "SKILL.md").touch()

    with (
        patch("pathlib.Path.home", return_value=mock_home),
        patch("pathlib.Path.cwd", return_value=mock_source),
    ):
        exit_code = setup_skills()

    assert exit_code == 0


def test_setup_skills_returns_1_when_all_symlinks_fail_with_permission_error(
    tmp_path, capsys
):
    from orchestune.setup_skills import setup_skills

    mock_home = tmp_path / "home"
    mock_home.mkdir()
    (mock_home / ".claude").mkdir()

    mock_source = tmp_path / "orchestune_repo"
    mock_source.mkdir()
    skills_dir = mock_source / "skills"
    skills_dir.mkdir()
    (skills_dir / "orchestune").mkdir()
    (skills_dir / "orchestune" / "SKILL.md").touch()

    permission_error = PermissionError("Access is denied")

    with (
        patch("pathlib.Path.home", return_value=mock_home),
        patch("pathlib.Path.cwd", return_value=mock_source),
        patch("pathlib.Path.symlink_to", side_effect=permission_error),
    ):
        exit_code = setup_skills()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Setup completed." not in captured.out
    assert "Setup failed" in captured.out or "Setup failed" in captured.err


def test_setup_skills_returns_1_when_mkdir_fails_for_all_assistants(tmp_path, capsys):
    from orchestune.setup_skills import setup_skills

    mock_home = tmp_path / "home"
    mock_home.mkdir()
    (mock_home / ".claude").mkdir()

    mock_source = tmp_path / "orchestune_repo"
    mock_source.mkdir()
    skills_dir = mock_source / "skills"
    skills_dir.mkdir()
    (skills_dir / "orchestune").mkdir()
    (skills_dir / "orchestune" / "SKILL.md").touch()

    mkdir_error = PermissionError("Access is denied")

    with (
        patch("pathlib.Path.home", return_value=mock_home),
        patch("pathlib.Path.cwd", return_value=mock_source),
        patch("pathlib.Path.mkdir", side_effect=mkdir_error),
    ):
        exit_code = setup_skills()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Setup completed." not in captured.out
    assert "Setup failed" in captured.out or "Setup failed" in captured.err


def test_setup_skills_partial_failure_returns_1_with_summary(tmp_path, capsys):
    from orchestune.setup_skills import setup_skills

    mock_home = tmp_path / "home"
    mock_home.mkdir()
    (mock_home / ".claude").mkdir()

    mock_source = tmp_path / "orchestune_repo"
    mock_source.mkdir()
    skills_dir = mock_source / "skills"
    skills_dir.mkdir()
    (skills_dir / "skill-a").mkdir()
    (skills_dir / "skill-a" / "SKILL.md").touch()
    (skills_dir / "skill-b").mkdir()
    (skills_dir / "skill-b" / "SKILL.md").touch()

    real_symlink_to = Path.symlink_to
    call_count = {"n": 0}

    def flaky_symlink_to(self, target, target_is_directory=False):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise PermissionError("Access is denied")
        return real_symlink_to(self, target, target_is_directory=target_is_directory)

    with (
        patch("pathlib.Path.home", return_value=mock_home),
        patch("pathlib.Path.cwd", return_value=mock_source),
        patch("pathlib.Path.symlink_to", flaky_symlink_to),
    ):
        exit_code = setup_skills()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Setup completed with" in captured.out


def test_get_skills_source_dir_not_found(tmp_path):
    import pytest

    from orchestune.setup_skills import get_skills_source_dir

    mock_cwd = tmp_path / "other_dir"
    mock_cwd.mkdir()

    fake_pkg_file = tmp_path / "site-packages" / "orchestune" / "setup_skills.py"
    fake_pkg_file.parent.mkdir(parents=True)
    fake_pkg_file.touch()

    with (
        patch("pathlib.Path.cwd", return_value=mock_cwd),
        patch("orchestune.setup_skills.__file__", str(fake_pkg_file)),
    ):
        with pytest.raises(FileNotFoundError):
            get_skills_source_dir()


def test_setup_skills_dynamic_discovery(tmp_path):
    from orchestune.setup_skills import setup_skills

    mock_home = tmp_path / "home"
    mock_home.mkdir()
    (mock_home / ".claude").mkdir()

    mock_source = tmp_path / "orchestune_repo"
    mock_source.mkdir()
    skills_dir = mock_source / "skills"
    skills_dir.mkdir()

    # 検出されるべきスキル（SKILL.mdあり）
    (skills_dir / "orchestune").mkdir()
    (skills_dir / "orchestune" / "SKILL.md").touch()
    (skills_dir / "skill-a").mkdir()
    (skills_dir / "skill-a" / "SKILL.md").touch()
    (skills_dir / "skill-b").mkdir()
    (skills_dir / "skill-b" / "SKILL.md").touch()
    (skills_dir / "local-ci-developer").mkdir()
    (skills_dir / "local-ci-developer" / "SKILL.md").touch()

    # 検出されないべきスキル（SKILL.mdなし）
    (skills_dir / "ignored-folder").mkdir()

    with (
        patch("pathlib.Path.home", return_value=mock_home),
        patch("pathlib.Path.cwd", return_value=mock_source),
    ):
        setup_skills()

    # 検出されたスキルのみリンクまたはコピーされていることを検証
    assert (mock_home / ".claude" / "skills" / "orchestune" / "SKILL.md").is_file()
    assert (mock_home / ".claude" / "skills" / "skill-a" / "SKILL.md").is_file()
    assert (mock_home / ".claude" / "skills" / "skill-b" / "SKILL.md").is_file()
    assert not (mock_home / ".claude" / "skills" / "local-ci-developer").exists()
    assert not (mock_home / ".claude" / "skills" / "ignored-folder").exists()

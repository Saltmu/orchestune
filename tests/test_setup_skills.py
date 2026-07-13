from unittest.mock import patch


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

    assert claude_target.is_symlink()
    assert codex_target.is_symlink()
    assert gemini_target.is_symlink()

    assert claude_target.resolve() == skills_dir / "orchestune"
    assert codex_target.resolve() == skills_dir / "orchestune"
    assert gemini_target.resolve() == skills_dir / "orchestune"


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

    # 検出されたスキルのみリンクされていることを検証
    assert (mock_home / ".claude" / "skills" / "orchestune").is_symlink()
    assert (mock_home / ".claude" / "skills" / "skill-a").is_symlink()
    assert (mock_home / ".claude" / "skills" / "skill-b").is_symlink()
    assert not (mock_home / ".claude" / "skills" / "local-ci-developer").exists()
    assert not (mock_home / ".claude" / "skills" / "ignored-folder").exists()

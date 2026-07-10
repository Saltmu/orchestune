from unittest.mock import patch


def test_setup_skills_creates_links(tmp_path):
    from orchestune.setup_skills import setup_skills

    mock_home = tmp_path / "home"
    mock_home.mkdir()

    mock_source = tmp_path / "orchestune_repo"
    mock_source.mkdir()
    skills_dir = mock_source / "skills"
    skills_dir.mkdir()
    (skills_dir / "orchestune").mkdir()

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


def test_setup_skills_already_exists(tmp_path, capsys):
    from orchestune.setup_skills import setup_skills

    mock_home = tmp_path / "home"
    mock_home.mkdir()

    mock_source = tmp_path / "orchestune_repo"
    mock_source.mkdir()
    skills_dir = mock_source / "skills"
    skills_dir.mkdir()
    (skills_dir / "orchestune").mkdir()

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

    # 既に存在している場合はスキップされる、あるいは上書きされる（警告などが出る）
    # 実装では「すでに存在している場合はスキップしてメッセージを出す」とします
    captured = capsys.readouterr()
    assert "Skipped" in captured.out or "already exists" in captured.out

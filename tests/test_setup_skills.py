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


def test_setup_skills_already_exists_as_valid_copy(tmp_path, capsys):
    """占有先が有効なskillのコピー(SKILL.md有り)であれば成功として扱う。"""
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

    # すでにターゲットが有効なコピーとして存在している状態を作る
    # (Windowsでのsymlink権限不足によるcopyフォールバック後の再実行を想定)
    claude_dir = mock_home / ".claude" / "skills"
    claude_dir.mkdir(parents=True)
    existing_copy = claude_dir / "orchestune"
    existing_copy.mkdir()
    (existing_copy / "SKILL.md").touch()

    with (
        patch("pathlib.Path.home", return_value=mock_home),
        patch("pathlib.Path.cwd", return_value=mock_source),
    ):
        exit_code = setup_skills()

    captured = capsys.readouterr()
    assert "Skipped" in captured.out
    assert exit_code == 0


def test_setup_skills_occupied_by_unrelated_path_counts_as_failure(tmp_path, capsys):
    """占有先が無関係なfile/directory(SKILL.mdなし)であれば失敗として扱う。"""
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

    # すでにターゲットが無関係なディレクトリ(SKILL.mdなし)として存在している
    claude_dir = mock_home / ".claude" / "skills"
    claude_dir.mkdir(parents=True)
    unrelated_dir = claude_dir / "orchestune"
    unrelated_dir.mkdir()

    with (
        patch("pathlib.Path.home", return_value=mock_home),
        patch("pathlib.Path.cwd", return_value=mock_source),
    ):
        exit_code = setup_skills()

    captured = capsys.readouterr()
    assert "occupied by an unrelated" in captured.err
    assert exit_code == 1


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


def _make_source_and_home(tmp_path):
    """Common fixture-ish setup: a home dir with `.claude/` present, and a
    skills source dir containing a single `orchestune` skill."""
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    (mock_home / ".claude").mkdir()

    mock_source = tmp_path / "orchestune_repo"
    mock_source.mkdir()
    skills_dir = mock_source / "skills"
    skills_dir.mkdir()
    (skills_dir / "orchestune").mkdir()
    (skills_dir / "orchestune" / "SKILL.md").touch()

    return mock_home, mock_source, skills_dir


def test_setup_skills_skips_relinking_when_symlink_already_correct(tmp_path, capsys):
    """既存のsymlinkが既に正しいsrcを指している場合、貼り直さずスキップする。"""
    from orchestune.setup_skills import setup_skills

    mock_home, mock_source, skills_dir = _make_source_and_home(tmp_path)

    with (
        patch("pathlib.Path.home", return_value=mock_home),
        patch("pathlib.Path.cwd", return_value=mock_source),
    ):
        first_exit_code = setup_skills()
        assert first_exit_code == 0
        capsys.readouterr()  # discard the first run's output

        second_exit_code = setup_skills()

    captured = capsys.readouterr()
    assert second_exit_code == 0
    assert "already correctly linked" in captured.out


def test_setup_skills_relinks_when_symlink_points_elsewhere(tmp_path, capsys):
    """既存のsymlinkが別のsrcを指している場合、正しいsrcへ貼り直す。"""
    from orchestune.setup_skills import setup_skills

    mock_home, mock_source, skills_dir = _make_source_and_home(tmp_path)

    stale_target = tmp_path / "stale_skill_source"
    stale_target.mkdir()
    claude_skills = mock_home / ".claude" / "skills"
    claude_skills.mkdir(parents=True)
    stale_link = claude_skills / "orchestune"
    stale_link.symlink_to(stale_target, target_is_directory=True)

    with (
        patch("pathlib.Path.home", return_value=mock_home),
        patch("pathlib.Path.cwd", return_value=mock_source),
    ):
        exit_code = setup_skills()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Updating link" in captured.out
    assert stale_link.resolve() == (skills_dir / "orchestune").resolve()


def test_link_one_skill_recovers_when_readlink_raises(tmp_path, capsys):
    """既存linkのreadlink/resolveが失敗しても、除去して張り直すフォールバックで
    復旧できることを検証する。"""
    from orchestune.setup_skills import _link_one_skill

    src_skill = tmp_path / "source"
    src_skill.mkdir()
    dest_skill = tmp_path / "dest"
    other_target = tmp_path / "other_target"
    other_target.mkdir()
    dest_skill.symlink_to(other_target, target_is_directory=True)

    with patch(
        "pathlib.Path.readlink", side_effect=OSError("simulated readlink failure")
    ):
        result = _link_one_skill(src_skill, dest_skill, "orchestune")

    captured = capsys.readouterr()
    assert result is True
    assert "Warning: Failed to resolve existing link" in captured.out
    assert "Successfully linked" in captured.out
    assert dest_skill.resolve() == src_skill.resolve()


def test_link_one_skill_returns_false_when_stale_link_removal_fails(tmp_path, capsys):
    """readlinkの失敗に加え、フォールバックのunlinkも失敗した場合は、
    エラーを報告して失敗として扱う。"""
    from orchestune.setup_skills import _link_one_skill

    src_skill = tmp_path / "source"
    src_skill.mkdir()
    dest_skill = tmp_path / "dest"
    other_target = tmp_path / "other_target"
    other_target.mkdir()
    dest_skill.symlink_to(other_target, target_is_directory=True)

    with (
        patch(
            "pathlib.Path.readlink", side_effect=OSError("simulated readlink failure")
        ),
        patch("pathlib.Path.unlink", side_effect=OSError("simulated unlink failure")),
    ):
        result = _link_one_skill(src_skill, dest_skill, "orchestune")

    captured = capsys.readouterr()
    assert result is False
    assert "Error: Failed to remove stale link" in captured.err


def test_link_one_skill_reports_copy_success_message(tmp_path, capsys):
    """`_create_skill_link`が'copied'（Windowsのsymlink権限不足フォールバック）
    を返した場合、コピー完了メッセージが表示されることを検証する。"""
    from orchestune.setup_skills import _link_one_skill

    src_skill = tmp_path / "source"
    src_skill.mkdir()
    dest_skill = tmp_path / "dest"

    with patch("orchestune.setup_skills._create_skill_link", return_value="copied"):
        result = _link_one_skill(src_skill, dest_skill, "orchestune")

    captured = capsys.readouterr()
    assert result is True
    assert "Successfully copied" in captured.out
    assert "symlink privilege is not available" in captured.out


def test_setup_for_assistant_skips_missing_skill_source(tmp_path, capsys):
    """`skills_to_link`に、実際にはskills_dir配下に存在しないスキル名が
    含まれる場合、警告を出してスキップし、成功/失敗カウントに含めない。"""
    from orchestune.setup_skills import _setup_for_assistant

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "real-skill").mkdir()
    (skills_dir / "real-skill" / "SKILL.md").touch()

    target_dir = tmp_path / "target"

    result = _setup_for_assistant(
        "Claude Code",
        tmp_path,
        target_dir,
        skills_dir,
        ["real-skill", "missing-skill"],
    )

    captured = capsys.readouterr()
    assert result == (1, 0)
    assert "Skill source 'missing-skill' not found" in captured.out
    assert (target_dir / "real-skill" / "SKILL.md").is_file()
    assert not (target_dir / "missing-skill").exists()


def test_setup_skills_returns_1_when_skills_dir_not_found(tmp_path, capsys):
    """`get_skills_source_dir`がFileNotFoundErrorを送出した場合、
    `setup_skills`自体がそれを捕捉してエラーメッセージを表示し、
    終了コード1を返すことを検証する。"""
    from orchestune.setup_skills import setup_skills

    mock_cwd = tmp_path / "other_dir"
    mock_cwd.mkdir()

    fake_pkg_file = tmp_path / "site-packages" / "orchestune" / "setup_skills.py"
    fake_pkg_file.parent.mkdir(parents=True)
    fake_pkg_file.touch()

    with (
        patch("pathlib.Path.cwd", return_value=mock_cwd),
        patch("orchestune.setup_skills.__file__", str(fake_pkg_file)),
    ):
        exit_code = setup_skills()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Error:" in captured.err
    assert "Could not locate the 'skills' directory" in captured.err

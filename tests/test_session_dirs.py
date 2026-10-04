from pathlib import Path

import pytest

from orchestune.infra.session_dirs import SessionDirError, create_session_dir


def test_create_session_dir_success(tmp_path: Path):
    project_dir = tmp_path / "my_project"
    project_dir.mkdir()
    gitignore = project_dir / ".gitignore"
    gitignore.write_text(".orchestune/tmp/\n", encoding="utf-8")

    session_path = create_session_dir("planning", "1191", project_dir=project_dir)
    assert session_path.is_dir()
    assert session_path.parent.name == "tmp"
    assert session_path.parent.parent.name == ".orchestune"
    assert session_path.name.startswith("planning-1191-")


def test_create_session_dir_missing_gitignore(tmp_path: Path):
    project_dir = tmp_path / "my_project"
    project_dir.mkdir()

    with pytest.raises(SessionDirError, match="must be ignored in .gitignore"):
        create_session_dir("planning", "1191", project_dir=project_dir)


def test_create_session_dir_invalid_names(tmp_path: Path):
    project_dir = tmp_path / "my_project"
    project_dir.mkdir()
    gitignore = project_dir / ".gitignore"
    gitignore.write_text(".orchestune/tmp/\n", encoding="utf-8")

    with pytest.raises(SessionDirError, match="Invalid artifact or issue/task"):
        create_session_dir("../evil", "1191", project_dir=project_dir)

    with pytest.raises(SessionDirError, match="Invalid artifact or issue/task"):
        create_session_dir("task", "1191/evil", project_dir=project_dir)


@pytest.mark.parametrize(
    "pattern",
    [
        ".orchestune/",
        ".orchestune/*",
        ".orchestune",
        "/.orchestune/",
        "**/.orchestune/tmp/",
    ],
)
def test_create_session_dir_wildcard_patterns(tmp_path: Path, pattern: str):
    project_dir = tmp_path / f"project_{pattern.replace('/', '_').replace('*', 'star')}"
    project_dir.mkdir()
    gitignore = project_dir / ".gitignore"
    gitignore.write_text(f"{pattern}\n", encoding="utf-8")

    session_path = create_session_dir("task", "1191", project_dir=project_dir)
    assert session_path.is_dir()
    assert session_path.name.startswith("task-1191-")


def test_create_session_dir_git_check_ignore_fallback(tmp_path: Path, monkeypatch):
    from orchestune.infra import session_dirs

    project_dir = tmp_path / "git_project"
    project_dir.mkdir()
    # No .gitignore file exists on disk, but git check-ignore returns True
    monkeypatch.setattr(session_dirs, "is_git_ignored", lambda p, t: True)

    session_path = create_session_dir("task", "1191", project_dir=project_dir)
    assert session_path.is_dir()
    assert session_path.name.startswith("task-1191-")

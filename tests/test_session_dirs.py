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

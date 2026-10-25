from pathlib import Path

from orchestune.scratch.cli import main as scratch_main
from tests.cli_tty_support import patch_tty_stdout


def test_scratch_cli_create(tmp_path: Path, capsys):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / ".gitignore").write_text(".orchestune/tmp/\n", encoding="utf-8")

    assert (
        scratch_main(["create", "planning", "1191", "--project-dir", str(project_dir)])
        == 0
    )

    captured = capsys.readouterr()
    created_dir = Path(captured.out.strip())
    assert created_dir.is_dir()
    assert "planning-1191-" in created_dir.name


def test_scratch_cli_create_on_tty_prints_only_the_path_line(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / ".gitignore").write_text(".orchestune/tmp/\n", encoding="utf-8")
    stream = patch_tty_stdout(monkeypatch)

    assert (
        scratch_main(["create", "planning", "1191", "--project-dir", str(project_dir)])
        == 0
    )

    created_dir = Path(stream.getvalue().removesuffix("\n"))
    assert stream.getvalue() == f"{created_dir}\n"
    assert created_dir.is_dir()


def test_scratch_cli_missing_args():
    assert scratch_main(["create"]) == 2

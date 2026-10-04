from pathlib import Path

from orchestune.scratch.cli import main as scratch_main


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


def test_scratch_cli_missing_args():
    assert scratch_main(["create"]) == 2

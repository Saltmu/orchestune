import json
from pathlib import Path

from orchestune.installer.cli import main as skills_main


def test_cli_requires_subcommand(capsys):
    assert skills_main([]) == 2


def test_cli_workflow_skill_with_user_scope_rejected():
    assert (
        skills_main(
            ["install", "--target", "codex", "--scope", "user", "--with-workflow-skill"]
        )
        == 2
    )


def test_cli_install_dry_run_json_output(tmp_path: Path, capsys):
    test_args = [
        "install",
        "--target",
        "codex",
        "--scope",
        "project",
        "--project-dir",
        str(tmp_path),
        "--dry-run",
        "--json",
    ]
    assert skills_main(test_args) == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["schema_version"] == 1
    assert data["operation"] == "install"
    assert data["scope"] == "project"
    assert "codex" in data["logical_targets"]
    assert data["overall_status"] == "ok"

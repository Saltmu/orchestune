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


def test_cli_text_output_labels_results(tmp_path: Path, capsys):
    test_args = [
        "install",
        "--target",
        "codex",
        "--scope",
        "project",
        "--project-dir",
        str(tmp_path),
        "--dry-run",
    ]
    assert skills_main(test_args) == 0

    captured = capsys.readouterr()
    assert "Orchestune skills install completed:" in captured.out
    assert "[OK] " in captured.out
    assert "SUCCESS" not in captured.out


def test_cli_json_output_is_single_document_without_labels(tmp_path: Path, capsys):
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
    json.loads(captured.out)
    assert "[OK]" not in captured.out
    assert "completed:" not in captured.out


def test_cli_doctor_text_output_labels_each_check(tmp_path: Path, capsys):
    test_args = [
        "doctor",
        "--target",
        "codex",
        "--scope",
        "project",
        "--project-dir",
        str(tmp_path),
        "--offline",
    ]
    skills_main(test_args)

    lines = capsys.readouterr().out.splitlines()
    assert "=== Orchestune Skills Doctor ===" in lines
    check_lines = [line for line in lines if line.startswith("[")]
    assert check_lines
    assert all(
        line.split("] ", 1)[0] in {"[OK", "[WARN", "[FAIL", "[SKIPPED", "[UNVERIFIED"}
        for line in check_lines
    )

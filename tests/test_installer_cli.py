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


def test_cli_install_includes_readiness_skill(tmp_path: Path):
    test_args = [
        "install",
        "--target",
        "codex",
        "--scope",
        "project",
        "--project-dir",
        str(tmp_path),
    ]
    assert skills_main(test_args) == 0
    installed_skill = tmp_path / ".agents" / "skills" / "orchestune-readiness"
    assert (installed_skill / "SKILL.md").is_file()
    assert (installed_skill / "scripts" / "check_readiness.py").is_file()


def test_cli_install_auto_installs_workflow_template_when_missing(
    tmp_path: Path, capsys
):
    test_args = [
        "install",
        "--target",
        "codex",
        "--scope",
        "project",
        "--project-dir",
        str(tmp_path),
        "--json",
    ]
    assert skills_main(test_args) == 0

    data = json.loads(capsys.readouterr().out)
    assert data["overall_status"] == "ok"
    assert {r["bundle_name"] for r in data["results"]} == {"standard", "workflow"}
    assert data["workflow_guidance"]["status"] == "missing"
    assert data["workflow_guidance"]["action"] == "auto_installed"
    assert (tmp_path / ".agents" / "skills" / "orchestune" / "SKILL.md").is_file()
    assert (
        tmp_path / ".agents" / "skills" / "workflow-template" / "SKILL.md"
    ).is_file()


def test_cli_install_dry_run_previews_auto_workflow_template_without_writing(
    tmp_path: Path, capsys
):
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

    data = json.loads(capsys.readouterr().out)
    assert {r["bundle_name"] for r in data["results"]} == {"standard", "workflow"}
    assert data["workflow_guidance"]["action"] == "would_auto_install"
    assert not (tmp_path / ".agents" / "skills").exists()


def test_cli_install_proposes_merge_when_existing_discipline_lacks_complete(
    tmp_path: Path, capsys
):
    (tmp_path / "AGENTS.md").write_text("# Dev Rules\nRun pytest.\n", encoding="utf-8")

    test_args = [
        "install",
        "--target",
        "codex",
        "--scope",
        "project",
        "--project-dir",
        str(tmp_path),
        "--json",
    ]
    assert skills_main(test_args) == 0

    data = json.loads(capsys.readouterr().out)
    assert {r["bundle_name"] for r in data["results"]} == {"standard"}
    guidance = data["workflow_guidance"]
    assert guidance["status"] == "merge_recommended"
    assert guidance["action"] == "merge_proposed"
    assert guidance["detected_discipline_files"] == ["AGENTS.md"]
    assert "orchestune complete" in guidance["missing_procedures"]
    assert guidance["merge_checklist"]
    assert not (tmp_path / ".agents" / "skills" / "workflow-template").exists()
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == (
        "# Dev Rules\nRun pytest.\n"
    )


def test_cli_install_proposes_merge_when_existing_custom_skill_lacks_complete(
    tmp_path: Path, capsys
):
    custom_skill = tmp_path / ".agents" / "skills" / "repo-dev"
    custom_skill.mkdir(parents=True)
    (custom_skill / "SKILL.md").write_text("# Repo Dev\nRun tests.\n", encoding="utf-8")

    test_args = [
        "install",
        "--target",
        "codex",
        "--scope",
        "project",
        "--project-dir",
        str(tmp_path),
    ]
    assert skills_main(test_args) == 0

    out = capsys.readouterr().out
    assert "orchestune complete" in out
    assert ".agents/skills/repo-dev/SKILL.md" in out
    assert not (tmp_path / ".agents" / "skills" / "workflow-template").exists()


def test_cli_install_no_workflow_skill_skips_auto_install(tmp_path: Path, capsys):
    test_args = [
        "install",
        "--target",
        "codex",
        "--scope",
        "project",
        "--project-dir",
        str(tmp_path),
        "--no-workflow-skill",
        "--json",
    ]
    assert skills_main(test_args) == 0

    data = json.loads(capsys.readouterr().out)
    assert {r["bundle_name"] for r in data["results"]} == {"standard"}
    assert data["workflow_guidance"]["action"] == "skipped_by_flag"
    assert not (tmp_path / ".agents" / "skills" / "workflow-template").exists()


def test_cli_install_with_workflow_skill_installs_both_bundles(tmp_path: Path, capsys):
    (tmp_path / "AGENTS.md").write_text("# Existing\n", encoding="utf-8")
    test_args = [
        "install",
        "--target",
        "codex",
        "--scope",
        "project",
        "--project-dir",
        str(tmp_path),
        "--with-workflow-skill",
        "--json",
    ]
    assert skills_main(test_args) == 0

    data = json.loads(capsys.readouterr().out)
    assert {r["bundle_name"] for r in data["results"]} == {"standard", "workflow"}
    assert data["workflow_guidance"]["action"] == "explicitly_installed"
    assert (
        tmp_path / ".agents" / "skills" / "workflow-template" / "SKILL.md"
    ).is_file()


def test_cli_install_preserves_customized_workflow_template_on_rerun(
    tmp_path: Path, capsys
):
    first_args = [
        "install",
        "--target",
        "codex",
        "--scope",
        "project",
        "--project-dir",
        str(tmp_path),
    ]
    assert skills_main(first_args) == 0

    wf_skill_md = tmp_path / ".agents" / "skills" / "workflow-template" / "SKILL.md"
    wf_skill_md.write_text(
        "# Custom Workflow\nRun `orchestune claim 1 --apply`, pytest, `orchestune complete --apply`.\n",
        encoding="utf-8",
    )

    second_args = [
        "install",
        "--target",
        "all",
        "--scope",
        "project",
        "--project-dir",
        str(tmp_path),
        "--json",
    ]
    assert skills_main(second_args) == 0
    assert "Custom Workflow" in wf_skill_md.read_text(encoding="utf-8")
    assert (
        tmp_path / ".claude" / "skills" / "workflow-template" / "SKILL.md"
    ).is_file()


def test_cli_install_conflicting_workflow_flags_rejected():
    assert (
        skills_main(
            [
                "install",
                "--target",
                "codex",
                "--scope",
                "project",
                "--with-workflow-skill",
                "--no-workflow-skill",
            ]
        )
        == 2
    )
    assert (
        skills_main(
            ["install", "--target", "codex", "--scope", "user", "--no-workflow-skill"]
        )
        == 2
    )

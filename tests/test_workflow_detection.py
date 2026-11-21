from __future__ import annotations

from pathlib import Path

from orchestune.installer.workflow_detection import (
    detect_project_workflow,
    format_merge_guidance_lines,
)
from tests.symlink_support import create_test_symlink


def test_detect_missing_in_empty_repo(tmp_path: Path) -> None:
    detection = detect_project_workflow(tmp_path)
    assert detection.status == "missing"
    assert detection.discipline_files == ()
    assert detection.custom_skills == ()
    assert detection.workflow_templates == ()
    assert detection.has_claim is False
    assert detection.has_complete is False
    assert detection.missing_procedures == ("orchestune claim", "orchestune complete")
    assert detection.merge_checklist == ()


def test_detect_workflow_template_only_with_placeholders(tmp_path: Path) -> None:
    wf_dir = tmp_path / ".agents" / "skills" / "workflow-template"
    wf_dir.mkdir(parents=True)
    (wf_dir / "SKILL.md").write_text(
        "# Workflow\nRun `<CI_ENTRYPOINT>` then `orchestune complete`.\n",
        encoding="utf-8",
    )

    detection = detect_project_workflow(tmp_path)
    assert detection.status == "workflow_template"
    assert detection.workflow_templates == (
        ".agents/skills/workflow-template/SKILL.md",
    )
    assert detection.template_has_placeholders is True
    assert detection.has_complete is True


def test_detect_workflow_template_customized_without_placeholders(
    tmp_path: Path,
) -> None:
    wf_dir = tmp_path / ".claude" / "skills" / "workflow-template"
    wf_dir.mkdir(parents=True)
    (wf_dir / "SKILL.md").write_text(
        "# Workflow\nRun `orchestune claim <N> --apply`, `pytest`, `orchestune complete --apply`.\n",
        encoding="utf-8",
    )

    detection = detect_project_workflow(tmp_path)
    assert detection.status == "workflow_template"
    assert detection.template_has_placeholders is False
    assert detection.has_claim is True
    assert detection.has_complete is True


def test_detect_existing_custom_skill_missing_complete_proposes_merge(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / ".agents" / "skills" / "my-dev-workflow"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "# My Dev Workflow\nRun pytest and open PR.\n", encoding="utf-8"
    )

    detection = detect_project_workflow(tmp_path)
    assert detection.status == "merge_recommended"
    assert detection.custom_skills == (".agents/skills/my-dev-workflow/SKILL.md",)
    assert detection.has_complete is False
    assert "orchestune complete" in detection.missing_procedures
    assert detection.merge_checklist

    lines = format_merge_guidance_lines(detection)
    joined = "\n".join(lines)
    assert ".agents/skills/my-dev-workflow/SKILL.md" in joined
    assert "orchestune complete" in joined


def test_detect_custom_skill_complete_covered_in_references(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".claude" / "skills" / "team-dev"
    refs_dir = skill_dir / "references"
    refs_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "# Team Dev\nSee references.\n", encoding="utf-8"
    )
    (refs_dir / "finish.md").write_text(
        "Run `orchestune claim 1 --apply` and `orchestune complete --apply`.\n",
        encoding="utf-8",
    )

    detection = detect_project_workflow(tmp_path)
    assert detection.status == "custom_complete"
    assert detection.has_claim is True
    assert detection.has_complete is True
    assert detection.missing_procedures == ()


def test_detect_discipline_follows_relative_links_safely(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text(
        "Read [.agents/AGENTS.md](.agents/AGENTS.md) and [external](https://example.com/a.md).\n",
        encoding="utf-8",
    )
    agents_dir = tmp_path / ".agents"
    agents_dir.mkdir()
    (agents_dir / "AGENTS.md").write_text(
        "Follow [dev](../skills/dev/SKILL.md#steps).\n",
        encoding="utf-8",
    )
    dev_dir = tmp_path / "skills" / "dev"
    dev_dir.mkdir(parents=True)
    (dev_dir / "SKILL.md").write_text(
        "Use `orchestune claim` and `orchestune complete`.\n", encoding="utf-8"
    )

    detection = detect_project_workflow(tmp_path)
    assert detection.status == "custom_complete"
    assert ".agents/AGENTS.md" in detection.discipline_files
    assert "AGENTS.md" in detection.discipline_files
    assert detection.has_complete is True


def test_detect_discipline_rejects_root_escaping_and_handles_cycles(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("orchestune complete\n", encoding="utf-8")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text(
        "See [escape](../outside.md) and [b](b.md).\n", encoding="utf-8"
    )
    (repo / "b.md").write_text("Cycle back to [a](AGENTS.md).\n", encoding="utf-8")

    detection = detect_project_workflow(repo)
    assert detection.status == "merge_recommended"
    assert detection.has_complete is False


def test_detect_ignores_standard_orchestune_skills_and_outside_symlink(
    tmp_path: Path,
) -> None:
    outside_skill = tmp_path / "outside_skill"
    outside_skill.mkdir()
    (outside_skill / "SKILL.md").write_text("orchestune complete\n", encoding="utf-8")

    repo = tmp_path / "repo"
    skills_root = repo / ".agents" / "skills"
    (skills_root / "orchestune").mkdir(parents=True)
    (skills_root / "orchestune" / "SKILL.md").write_text(
        "orchestune complete\n", encoding="utf-8"
    )
    (skills_root / ".orchestune-installer").mkdir()
    (skills_root / ".orchestune-installer" / "SKILL.md").write_text(
        "orchestune complete\n", encoding="utf-8"
    )
    create_test_symlink(
        skills_root / "escaped-skill", outside_skill, target_is_directory=True
    )

    detection = detect_project_workflow(repo)
    assert detection.status == "missing"
    assert detection.custom_skills == ()


def test_detect_extra_skills_root_without_project_root(tmp_path: Path) -> None:
    custom_root = tmp_path / "custom-skills"
    (custom_root / "custom-dev").mkdir(parents=True)
    (custom_root / "custom-dev" / "SKILL.md").write_text(
        "# Custom\nNo complete command.\n", encoding="utf-8"
    )

    detection = detect_project_workflow(None, extra_skills_roots=[custom_root])
    assert detection.status == "merge_recommended"
    assert len(detection.custom_skills) == 1
    assert detection.has_complete is False

from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_codex_entrypoint_loads_canonical_agent_rules():
    entrypoint = REPOSITORY_ROOT / "AGENTS.md"

    assert entrypoint.is_file()
    instructions = entrypoint.read_text(encoding="utf-8")
    assert ".agents/AGENTS.md" in instructions
    assert "read" in instructions.lower()


def test_claude_entrypoint_loads_canonical_agent_rules():
    entrypoint = REPOSITORY_ROOT / "CLAUDE.md"

    assert entrypoint.is_file()
    assert not entrypoint.is_symlink()
    instructions = entrypoint.read_text(encoding="utf-8")
    assert ".agents/AGENTS.md" in instructions
    assert "read" in instructions.lower()


def test_local_ci_skill_reference_resolves_from_agent_rules():
    agent_rules = REPOSITORY_ROOT / ".agents" / "AGENTS.md"
    instructions = agent_rules.read_text(encoding="utf-8")
    relative_skill_path = "../skills/local-ci-developer/SKILL.md"

    assert relative_skill_path in instructions
    assert (agent_rules.parent / relative_skill_path).resolve().is_file()


def _extract_frontmatter(content: str) -> dict:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end_index = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_index = i
            break
    if end_index == -1:
        return {}
    frontmatter_yaml = "\n".join(lines[1:end_index])
    return yaml.safe_load(frontmatter_yaml) or {}


def test_project_local_skills_are_concrete_wrappers_referencing_sot():
    expected_skills = [
        "local-ci-developer",
        "orchestune",
        "orchestune-dispatch",
        "orchestune-provision",
    ]
    assistant_dirs = [
        REPOSITORY_ROOT / ".claude" / "skills",
        REPOSITORY_ROOT / ".codex" / "skills",
    ]

    for assistant_dir in assistant_dirs:
        assert assistant_dir.is_dir()
        for skill_name in expected_skills:
            skill_dir = assistant_dir / skill_name
            assert skill_dir.is_dir(), f"Expected directory {skill_dir} to exist"
            assert (
                not skill_dir.is_symlink()
            ), f"Expected {skill_dir} to be a real directory, not a symlink"

            skill_file = skill_dir / "SKILL.md"
            assert skill_file.is_file(), f"Expected {skill_file} to exist"
            assert (
                not skill_file.is_symlink()
            ), f"Expected {skill_file} to be a real file, not a symlink"

            content = skill_file.read_text(encoding="utf-8")
            sot_relative_ref = f"../../skills/{skill_name}/SKILL.md"
            assert (
                sot_relative_ref in content
            ), f"Expected {skill_file} to reference {sot_relative_ref}"

            sot_file = (skill_dir / sot_relative_ref).resolve()
            assert sot_file.is_file(), f"Referenced SoT file {sot_file} does not exist"

            # Check that frontmatter name and description match the SoT
            wrapper_meta = _extract_frontmatter(content)
            sot_meta = _extract_frontmatter(sot_file.read_text(encoding="utf-8"))

            assert wrapper_meta.get("name") == sot_meta.get("name") == skill_name
            assert wrapper_meta.get("description") == sot_meta.get("description")


def test_agent_rules_bloat_autonomous_refactoring():
    agent_rules = REPOSITORY_ROOT / ".agents" / "AGENTS.md"
    instructions = agent_rules.read_text(encoding="utf-8")

    assert "自律的リファクタリング" in instructions
    assert "承認" not in instructions
    assert "エスカレーション" in instructions


def test_agent_rules_define_collision_safe_repository_local_scratch_space():
    instructions = (REPOSITORY_ROOT / ".agents" / "AGENTS.md").read_text(
        encoding="utf-8"
    )

    assert ".orchestune/tmp/" in instructions
    assert "UTC" in instructions
    assert "random" in instructions.lower()
    assert "OS グローバルの `/tmp`" in instructions


def test_gitignore_excludes_the_agent_scratch_directory():
    ignore_rules = (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert ".orchestune/tmp/" in ignore_rules.splitlines()


def test_fixed_name_implementation_plan_is_not_tracked():
    tracked_plan = REPOSITORY_ROOT / "implementation_plan.md"

    assert not tracked_plan.exists()


def test_workflow_skills_use_unique_scratch_paths_instead_of_fixed_tmp_files():
    skill_paths = [
        REPOSITORY_ROOT / "skills" / "local-ci-developer" / "SKILL.md",
        REPOSITORY_ROOT / "skills" / "workflow-template" / "SKILL.md",
        REPOSITORY_ROOT / "skills" / "orchestune" / "SKILL.md",
    ]
    reference_paths = list(
        (REPOSITORY_ROOT / "skills" / "local-ci-developer" / "references").glob("*.md")
    ) + list(
        (REPOSITORY_ROOT / "skills" / "workflow-template" / "references").glob("*.md")
    )

    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in skill_paths + reference_paths
    )
    assert ".orchestune/tmp/" in combined
    assert "<UTC timestamp>" in combined
    assert "<random>" in combined
    assert "/tmp/pr_body.md" not in combined
    assert "/tmp/review_reply.md" not in combined


def test_orchestune_skill_verifies_target_ignore_before_writing_plan():
    instructions = (REPOSITORY_ROOT / "skills" / "orchestune" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "git check-ignore" in instructions
    assert "before creating" in instructions.lower()
    assert ".gitignore" in instructions


def test_local_ci_skill_migrates_preclaim_plan_into_task_worktree():
    instructions = (
        REPOSITORY_ROOT / "skills" / "local-ci-developer" / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "<planning-session-dir>" in instructions
    assert "worktree-local" in instructions
    assert "migrate" in instructions.lower()

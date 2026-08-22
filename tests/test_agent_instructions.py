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

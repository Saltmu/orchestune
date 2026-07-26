from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_codex_entrypoint_loads_canonical_agent_rules():
    entrypoint = REPOSITORY_ROOT / "AGENTS.md"

    assert entrypoint.is_file()
    instructions = entrypoint.read_text(encoding="utf-8")
    assert ".agents/AGENTS.md" in instructions
    assert "read" in instructions.lower()


def test_local_ci_skill_reference_resolves_from_agent_rules():
    agent_rules = REPOSITORY_ROOT / ".agents" / "AGENTS.md"
    instructions = agent_rules.read_text(encoding="utf-8")
    relative_skill_path = "../skills/local-ci-developer/SKILL.md"

    assert relative_skill_path in instructions
    assert (agent_rules.parent / relative_skill_path).resolve().is_file()

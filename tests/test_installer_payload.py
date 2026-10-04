from pathlib import Path

import pytest

from orchestune.installer.contracts import PayloadError
from orchestune.installer.payload import (
    STANDARD_SKILLS,
    calculate_file_sha256,
    resolve_payload,
)
from orchestune.version import get_version


def test_calculate_file_sha256(tmp_path: Path):
    f = tmp_path / "test.txt"
    f.write_bytes(b"hello world\n")
    digest = calculate_file_sha256(f)
    assert len(digest) == 64
    assert digest == "a948904f2f0f479b8f8197694b30184b0d2ed1c1cd2a1ec0fb85d299a192a447"


def test_resolve_payload_from_source_dir(tmp_path: Path):
    # Setup dummy checkout
    source_root = tmp_path / "checkout"
    source_root.mkdir()
    pyproject = source_root / "pyproject.toml"
    current_version = get_version()
    pyproject.write_text(
        f'[project]\nname = "orchestune"\nversion = "{current_version}"\n',
        encoding="utf-8",
    )

    skills_dir = source_root / "skills"
    skills_dir.mkdir()

    for skill in [
        "orchestune",
        "orchestune-provision",
        "orchestune-dispatch",
        "local-ci-developer",
        "workflow-template",
    ]:
        s_dir = skills_dir / skill
        s_dir.mkdir()
        (s_dir / "SKILL.md").write_text(
            f"---\nname: {skill}\ndescription: Test {skill}\n---\n# {skill}\n",
            encoding="utf-8",
        )
        ref_dir = s_dir / "references"
        ref_dir.mkdir()
        (ref_dir / "guide.md").write_text("# Guide\n", encoding="utf-8")

    payload = resolve_payload(source_dir=source_root, with_workflow_skill=False)
    assert payload.bundle_name == "standard"
    assert payload.package_version == current_version
    assert payload.source_kind == "source_directory"
    assert set(payload.skills.keys()) == set(STANDARD_SKILLS)
    assert "local-ci-developer" not in payload.skills
    assert "workflow-template" not in payload.skills
    assert len(payload.bundle_digest) == 64

    # Check skill details
    orch_skill = payload.skills["orchestune"]
    assert "SKILL.md" in orch_skill.files
    assert "references/guide.md" in orch_skill.files
    assert "references" in orch_skill.directories


def test_resolve_payload_with_workflow_template(tmp_path: Path):
    source_root = tmp_path / "checkout"
    source_root.mkdir()
    current_version = get_version()
    (source_root / "pyproject.toml").write_text(
        f'[project]\nname = "orchestune"\nversion = "{current_version}"\n',
        encoding="utf-8",
    )
    skills_dir = source_root / "skills"
    skills_dir.mkdir()

    for skill in [
        "orchestune",
        "orchestune-provision",
        "orchestune-dispatch",
        "workflow-template",
    ]:
        s_dir = skills_dir / skill
        s_dir.mkdir()
        (s_dir / "SKILL.md").write_text(
            f"---\nname: {skill}\ndescription: Test {skill}\n---\n# {skill}\n",
            encoding="utf-8",
        )

    wf_payload = resolve_payload(source_dir=source_root, with_workflow_skill=True)
    assert wf_payload.bundle_name == "workflow"
    assert set(wf_payload.skills.keys()) == {"workflow-template"}


def test_resolve_payload_version_mismatch(tmp_path: Path):
    source_root = tmp_path / "checkout"
    source_root.mkdir()
    (source_root / "pyproject.toml").write_text(
        '[project]\nname = "orchestune"\nversion = "9.9.9"\n', encoding="utf-8"
    )
    skills_dir = source_root / "skills"
    skills_dir.mkdir()

    with pytest.raises(PayloadError, match="Version mismatch"):
        resolve_payload(source_dir=source_root)


def test_resolve_payload_missing_skill_md(tmp_path: Path):
    source_root = tmp_path / "checkout"
    source_root.mkdir()
    current_version = get_version()
    (source_root / "pyproject.toml").write_text(
        f'[project]\nname = "orchestune"\nversion = "{current_version}"\n',
        encoding="utf-8",
    )
    skills_dir = source_root / "skills"
    skills_dir.mkdir()

    # Create skills without SKILL.md in orchestune
    (skills_dir / "orchestune").mkdir()
    for skill in ["orchestune-provision", "orchestune-dispatch"]:
        s_dir = skills_dir / skill
        s_dir.mkdir()
        (s_dir / "SKILL.md").write_text(
            f"---\nname: {skill}\ndescription: Test\n---\n", encoding="utf-8"
        )

    with pytest.raises(PayloadError, match="Missing SKILL.md"):
        resolve_payload(source_dir=source_root)


def test_resolve_payload_traversal_or_symlink_rejected(tmp_path: Path):
    source_root = tmp_path / "checkout"
    source_root.mkdir()
    current_version = get_version()
    (source_root / "pyproject.toml").write_text(
        f'[project]\nname = "orchestune"\nversion = "{current_version}"\n',
        encoding="utf-8",
    )
    skills_dir = source_root / "skills"
    skills_dir.mkdir()

    for skill in ["orchestune", "orchestune-provision", "orchestune-dispatch"]:
        s_dir = skills_dir / skill
        s_dir.mkdir()
        (s_dir / "SKILL.md").write_text(
            f"---\nname: {skill}\ndescription: Test\n---\n", encoding="utf-8"
        )

    # Add a symlink inside orchestune
    external = tmp_path / "external.txt"
    external.write_text("outside", encoding="utf-8")
    (skills_dir / "orchestune" / "link.txt").symlink_to(external)

    with pytest.raises(
        PayloadError, match="Symlinks are not allowed in payload source"
    ):
        resolve_payload(source_dir=source_root)


def test_resolve_distribution_skills_dir_relative_parts(tmp_path: Path, monkeypatch):
    import importlib.metadata

    from orchestune.installer.payload import _resolve_distribution_skills_dir

    fake_site = tmp_path / "venv" / "lib" / "site-packages"
    fake_skills = fake_site / "skills"
    fake_skills.mkdir(parents=True)
    (fake_skills / "orchestune").mkdir()
    (fake_skills / "orchestune" / "SKILL.md").write_text("---\nname: orchestune\n---\n")

    class FakeDistribution:
        version = get_version()
        files = [
            Path("skills/orchestune/SKILL.md"),
        ]

        def locate_file(self, rel_path):
            return fake_site / rel_path

    monkeypatch.setattr(
        importlib.metadata, "distribution", lambda name: FakeDistribution()
    )

    resolved, ver = _resolve_distribution_skills_dir()
    assert resolved == fake_skills
    assert ver == get_version()

"""Packaging regression tests validating real wheel and sdist contents."""

from __future__ import annotations

import configparser
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[1]
DISTRIBUTABLE_SKILLS = frozenset(
    {
        "orchestune",
        "orchestune-provision",
        "orchestune-dispatch",
        "workflow-template",
    }
)
EXCLUDED_SKILLS = frozenset({"local-ci-developer"})
EXPECTED_ENTRY_POINTS = {
    "orchestune": "orchestune.cli:main",
    "orchestune-dispatch": "orchestune.dispatch.dispatcher:main",
    "orchestune-dag": "orchestune.dag.cli:main",
}


def _expected_distributable_skill_files() -> set[str]:
    """Return relative POSIX paths of all files in distributable skills."""
    skills_dir = REPO_ROOT / "skills"
    result: set[str] = set()
    for skill in DISTRIBUTABLE_SKILLS:
        skill_dir = skills_dir / skill
        for path in skill_dir.rglob("*"):
            if path.is_file():
                result.add(path.relative_to(REPO_ROOT).as_posix())
    return result


@pytest.fixture(scope="module")
def built_artifacts(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    out_dir = tmp_path_factory.mktemp("dist")
    result = subprocess.run(
        ["uv", "build", "--out-dir", str(out_dir)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (
        result.returncode == 0
    ), f"uv build failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"

    wheels = sorted(out_dir.glob("*.whl"))
    sdists = sorted(out_dir.glob("*.tar.gz"))

    assert len(wheels) == 1, f"Expected 1 wheel file, found {wheels}"
    assert len(sdists) == 1, f"Expected 1 sdist file, found {sdists}"

    return wheels[0], sdists[0]


def test_wheel_contains_distributable_skills(
    built_artifacts: tuple[Path, Path],
) -> None:
    wheel_path, _ = built_artifacts
    with zipfile.ZipFile(wheel_path) as zf:
        namelist = set(zf.namelist())

    for skill in DISTRIBUTABLE_SKILLS:
        skill_manifest = f"skills/{skill}/SKILL.md"
        assert (
            skill_manifest in namelist
        ), f"Expected skill manifest {skill_manifest} in wheel, but it was missing."

    expected_files = _expected_distributable_skill_files()
    assert expected_files, "Expected distributable skill files to be non-empty"
    missing = expected_files - namelist
    assert not missing, f"Missing distributable skill files in wheel: {sorted(missing)}"


def test_sdist_contains_distributable_skills(
    built_artifacts: tuple[Path, Path],
) -> None:
    _, sdist_path = built_artifacts
    with tarfile.open(sdist_path) as tf:
        names = set(tf.getnames())

    # sdist entries have a top-level directory prefix (e.g. orchestune-0.5.0/skills/...)
    stripped_names = {name.split("/", 1)[1] for name in names if "/" in name}

    for skill in DISTRIBUTABLE_SKILLS:
        skill_manifest = f"skills/{skill}/SKILL.md"
        assert (
            skill_manifest in stripped_names
        ), f"Expected skill manifest {skill_manifest} in sdist, but it was missing."

    expected_files = _expected_distributable_skill_files()
    assert expected_files, "Expected distributable skill files to be non-empty"
    missing = expected_files - stripped_names
    assert not missing, f"Missing distributable skill files in sdist: {sorted(missing)}"


def test_wheel_and_sdist_exclude_local_ci_developer(
    built_artifacts: tuple[Path, Path],
) -> None:
    wheel_path, sdist_path = built_artifacts

    with zipfile.ZipFile(wheel_path) as zf:
        wheel_entries = zf.namelist()
    for skill in EXCLUDED_SKILLS:
        wheel_matching = [n for n in wheel_entries if f"skills/{skill}" in n]
        assert (
            not wheel_matching
        ), f"Expected {skill} to be excluded from wheel, but found: {wheel_matching}"

    with tarfile.open(sdist_path) as tf:
        sdist_entries = tf.getnames()
    for skill in EXCLUDED_SKILLS:
        sdist_matching = [n for n in sdist_entries if f"skills/{skill}" in n]
        assert (
            not sdist_matching
        ), f"Expected {skill} to be excluded from sdist, but found: {sdist_matching}"


def test_wheel_contains_package_and_entry_points(
    built_artifacts: tuple[Path, Path],
) -> None:
    wheel_path, _ = built_artifacts
    with zipfile.ZipFile(wheel_path) as zf:
        namelist = set(zf.namelist())

        assert "orchestune/__init__.py" in namelist
        for script_name, target in EXPECTED_ENTRY_POINTS.items():
            module_name = target.split(":")[0]
            module_file = module_name.replace(".", "/") + ".py"
            assert (
                module_file in namelist
            ), f"Expected console script '{script_name}' target module '{module_file}' in wheel."

        entry_point_files = [
            n for n in namelist if n.endswith(".dist-info/entry_points.txt")
        ]
        assert (
            len(entry_point_files) == 1
        ), f"Expected 1 entry_points.txt in dist-info, found {entry_point_files}"

        content = zf.read(entry_point_files[0]).decode("utf-8")

    parser = configparser.ConfigParser()
    parser.read_string(content)

    assert parser.has_section(
        "console_scripts"
    ), f"Missing [console_scripts] section in entry_points.txt:\n{content}"

    scripts = dict(parser.items("console_scripts"))
    assert scripts == EXPECTED_ENTRY_POINTS


def test_build_does_not_pollute_repo(tmp_path: Path) -> None:
    dist_dir = REPO_ROOT / "dist"
    assert (
        not dist_dir.exists()
    ), f"Repository root polluted with dist directory prior to build: {dist_dir}"

    out_dir = tmp_path / "dist"
    result = subprocess.run(
        ["uv", "build", "--out-dir", str(out_dir)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (
        result.returncode == 0
    ), f"uv build failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"

    assert (
        not dist_dir.exists()
    ), f"Repository root polluted with dist directory after build: {dist_dir}"


def test_bundled_issue_template_matches_canonical_repo_template() -> None:
    bundled = REPO_ROOT / "skills/orchestune-provision/resources/issue_template.md"
    canonical = REPO_ROOT / ".github/issue_template.md"
    assert bundled.is_file(), f"Missing bundled issue template at {bundled}"
    assert canonical.is_file(), f"Missing canonical issue template at {canonical}"
    assert bundled.read_text(encoding="utf-8") == canonical.read_text(encoding="utf-8")


def test_wheel_and_sdist_contain_nested_skill_assets(
    built_artifacts: tuple[Path, Path],
) -> None:
    wheel_path, sdist_path = built_artifacts
    expected_nested = {
        "skills/orchestune-provision/resources/issue_template.md",
        "skills/orchestune-dispatch/references/child-review-gate.md",
        "skills/workflow-template/references/scratch.md",
    }
    with zipfile.ZipFile(wheel_path) as zf:
        wheel_entries = set(zf.namelist())
    for rel_path in expected_nested:
        assert rel_path in wheel_entries, f"Missing {rel_path} in wheel"

    with tarfile.open(sdist_path) as tf:
        sdist_entries = {name.split("/", 1)[1] for name in tf.getnames() if "/" in name}
    for rel_path in expected_nested:
        assert rel_path in sdist_entries, f"Missing {rel_path} in sdist"


def test_isolated_installation_and_skills_portability(
    built_artifacts: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    wheel_path, _ = built_artifacts
    venv_dir = tmp_path / "test_venv"
    res_venv = subprocess.run(
        ["uv", "venv", str(venv_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert res_venv.returncode == 0, f"uv venv failed: {res_venv.stderr}"

    venv_python = (
        venv_dir / "Scripts" / "python.exe"
        if (venv_dir / "Scripts" / "python.exe").exists()
        else venv_dir / "bin" / "python"
    )
    res_install = subprocess.run(
        ["uv", "pip", "install", str(wheel_path), "--python", str(venv_python)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert res_install.returncode == 0, f"uv pip install failed: {res_install.stderr}"

    venv_orchestune = (
        venv_python.parent / "orchestune.exe"
        if venv_python.parent.name == "Scripts"
        else venv_python.parent / "orchestune"
    )
    assert venv_orchestune.is_file(), f"Missing console script: {venv_orchestune}"

    res_help = subprocess.run(
        [str(venv_orchestune), "skills", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert (
        res_help.returncode == 0
    ), f"orchestune skills --help failed: {res_help.stderr}"
    assert "install" in res_help.stdout
    assert "status" in res_help.stdout

    res_scratch_help = subprocess.run(
        [str(venv_orchestune), "scratch", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert (
        res_scratch_help.returncode == 0
    ), f"orchestune scratch --help failed: {res_scratch_help.stderr}"

    res_version = subprocess.run(
        [str(venv_orchestune), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert (
        res_version.returncode == 0
    ), f"orchestune --version failed: {res_version.stderr}"
    assert "orchestune" in res_version.stdout

    external_proj = tmp_path / "ext_project"
    external_proj.mkdir(parents=True)
    res_skills_install = subprocess.run(
        [
            str(venv_orchestune),
            "skills",
            "install",
            "--target",
            "codex",
            "--scope",
            "project",
            "--project-dir",
            str(external_proj),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert (
        res_skills_install.returncode == 0
    ), f"skills install failed: {res_skills_install.stderr}\n{res_skills_install.stdout}"

    installed_skills_dir = external_proj / ".agents" / "skills"
    assert (installed_skills_dir / "orchestune" / "SKILL.md").is_file()
    assert (installed_skills_dir / "orchestune-provision" / "SKILL.md").is_file()
    assert (installed_skills_dir / "orchestune-dispatch" / "SKILL.md").is_file()
    assert (
        installed_skills_dir
        / "orchestune-provision"
        / "resources"
        / "issue_template.md"
    ).is_file()

    res_status = subprocess.run(
        [
            str(venv_orchestune),
            "skills",
            "status",
            "--target",
            "codex",
            "--scope",
            "project",
            "--project-dir",
            str(external_proj),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert res_status.returncode == 0, f"skills status failed: {res_status.stderr}"
    assert "managed-current" in res_status.stdout

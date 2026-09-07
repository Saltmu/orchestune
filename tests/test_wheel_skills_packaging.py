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
        skill_entry = f"skills/{skill}/SKILL.md"
        assert skill_entry in namelist, (
            f"Expected {skill_entry} in wheel, but it was missing. "
            f"Matching entries: {[n for n in namelist if skill in n]}"
        )


def test_sdist_contains_distributable_skills(
    built_artifacts: tuple[Path, Path],
) -> None:
    _, sdist_path = built_artifacts
    with tarfile.open(sdist_path) as tf:
        names = set(tf.getnames())

    for skill in DISTRIBUTABLE_SKILLS:
        matching = [n for n in names if f"skills/{skill}/SKILL.md" in n]
        assert matching, (
            f"Expected skills/{skill}/SKILL.md in sdist, but no matching entry was found. "
            f"Sample entries: {sorted(names)[:10]}"
        )


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
        assert "orchestune/cli.py" in namelist

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


def test_build_does_not_pollute_repo() -> None:
    dist_dir = REPO_ROOT / "dist"
    assert (
        not dist_dir.exists()
    ), f"Repository root polluted with dist directory: {dist_dir}"

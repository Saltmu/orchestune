"""The Claude Code on the web SessionStart hook installs Node.js and Quint (#1276).

The hook runs only in the Linux web environment, so these tests drive the real
script with stubbed tools and a ``file://`` distribution; nothing is downloaded.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the hook runs only in the Linux web environment"
)

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / ".claude" / "hooks" / "session-start.sh"
VERSION = "v24.21.0"
ARCH = "arm64" if os.uname().machine in {"aarch64", "arm64"} else "x64"
ARCHIVE = f"node-{VERSION}-linux-{ARCH}.tar.gz"


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@dataclass
class Setup:
    root: Path
    calls: Path = field(init=False)
    project: Path = field(init=False)
    bin: Path = field(init=False)
    dist: Path = field(init=False)
    lib: Path = field(init=False)
    link: Path = field(init=False)
    env_file: Path = field(init=False)

    def __post_init__(self) -> None:
        self.calls = self.root / "calls.log"
        self.project = self.root / "project"
        self.bin = self.root / "bin"
        self.dist = self.root / "dist"
        self.lib = self.root / "lib"
        self.link = self.root / "link"
        self.env_file = self.root / "env"
        self.calls.write_text("", encoding="utf-8")
        (self.project / "scripts").mkdir(parents=True)
        (self.project / "package.json").write_text(
            json.dumps({"engines": {"node": ">=24 <25"}}), encoding="utf-8"
        )
        record = f'echo "$(basename "$0") $*" >> {self.calls}\n'
        _script(self.project / "scripts" / "setup-git-hooks.sh", record)
        _script(
            self.project / "scripts" / "quint-check.sh",
            f'echo "quint-check node=$(command -v node || echo none)" >> {self.calls}\n'
            'exit "${FAKE_QUINT_CHECK_EXIT:-0}"\n',
        )
        for tool in ("uv", "gh"):
            _script(self.bin / tool, record)

    def node_installed(self, version: str) -> None:
        for tool in ("node", "npm"):
            _script(self.bin / tool, f'echo "{version}"\n')

    def publish(self, *, corrupt: bool = False) -> None:
        """A fake ``<dist>/<version>/`` with an archive and its SHASUMS256.txt."""
        release = self.dist / VERSION
        release.mkdir(parents=True)
        top = self.root / "pack" / f"node-{VERSION}-linux-{ARCH}"
        for tool in ("node", "npm", "npx"):
            _script(top / "bin" / tool, f'echo "{VERSION}"\n')
        archive = release / ARCHIVE
        with tarfile.open(archive, "w:gz") as pack:
            pack.add(top, arcname=top.name)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if corrupt:
            digest = "0" * 64
        (release / "SHASUMS256.txt").write_text(f"{digest}  {ARCHIVE}\n", encoding="utf-8")

    def run(self, *, remote: bool = True, **extra: str) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.root),
            "CLAUDE_PROJECT_DIR": str(self.project),
            "CLAUDE_ENV_FILE": str(self.env_file),
            "ORCHESTUNE_NODE_DIST_BASE": self.dist.as_uri(),
            "ORCHESTUNE_NODE_LIB_DIR": str(self.lib),
            "ORCHESTUNE_NODE_BIN_DIR": str(self.link),
            **extra,
        }
        if remote:
            env["CLAUDE_CODE_REMOTE"] = "true"
        return subprocess.run(
            ["/bin/bash", str(HOOK)],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def logged(self) -> str:
        return self.calls.read_text(encoding="utf-8")


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return Setup(tmp_path)


def test_outside_the_web_environment_nothing_runs(setup: Setup) -> None:
    done = setup.run(remote=False)
    assert done.returncode == 0
    assert setup.logged() == ""
    assert not setup.link.exists()


def test_a_matching_node_is_kept_and_the_locked_tools_are_installed(setup: Setup) -> None:
    setup.node_installed("v24.1.0")  # no distribution exists: a download would fail
    done = setup.run()
    assert done.returncode == 0, done.stderr
    log = setup.logged()
    assert "quint-check node=" in log and "node=none" not in log
    assert "setup-git-hooks" in log
    assert not setup.link.exists() and not setup.env_file.exists()


def test_a_missing_node_is_installed_from_the_verified_archive(setup: Setup) -> None:
    setup.publish()
    done = setup.run()
    assert done.returncode == 0, done.stderr
    for tool in ("node", "npm", "npx"):
        assert (setup.link / tool).is_symlink()
    assert f"quint-check node={setup.link}/node" in setup.logged()
    assert str(setup.link) in setup.env_file.read_text(encoding="utf-8")


def test_a_node_of_another_major_is_replaced(setup: Setup) -> None:
    setup.node_installed("v22.9.0")
    setup.publish()
    done = setup.run()
    assert done.returncode == 0, done.stderr
    assert (setup.link / "node").is_symlink()


def test_a_checksum_mismatch_installs_nothing_but_the_rest_still_runs(
    setup: Setup,
) -> None:
    setup.publish(corrupt=True)
    done = setup.run()
    assert done.returncode != 0
    assert "Checksum mismatch" in done.stderr
    assert not (setup.link / "node").exists()
    assert "quint-check" not in setup.logged()
    assert "setup-git-hooks" in setup.logged()  # other tooling is still prepared


def test_an_unreachable_distribution_fails_the_hook(setup: Setup) -> None:
    done = setup.run()  # nothing published
    assert done.returncode != 0
    assert "could not download" in done.stderr
    assert "setup-git-hooks" in setup.logged()


def test_a_failing_quint_check_fails_the_hook(setup: Setup) -> None:
    setup.node_installed("v24.1.0")
    done = setup.run(FAKE_QUINT_CHECK_EXIT="1")
    assert done.returncode == 1
    assert "Node.js / Quint setup failed" in done.stderr


def test_a_malformed_engines_range_is_an_error(setup: Setup) -> None:
    (setup.project / "package.json").write_text(
        json.dumps({"engines": {"node": "24"}}), encoding="utf-8"
    )
    done = setup.run()
    assert done.returncode != 0
    assert "engines.node" in done.stderr


def test_the_pinned_release_satisfies_the_engines_of_package_json() -> None:
    text = HOOK.read_text(encoding="utf-8")
    pinned = re.search(r'ORCHESTUNE_NODE_VERSION:-v(\d+)\.\d+\.\d+', text)
    assert pinned, "the hook must pin a Node.js release"
    engines = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["engines"]
    lower, upper = map(int, re.fullmatch(r">=(\d+) <(\d+)", engines["node"]).groups())  # type: ignore[union-attr]
    assert lower <= int(pinned.group(1)) < upper

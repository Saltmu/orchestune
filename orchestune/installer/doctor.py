from __future__ import annotations

import re
import shutil
import subprocess
import sys

from orchestune.installer.contracts import DiagnosticItem, PhysicalRoot
from orchestune.installer.state import (
    TRANSACTIONS_DIR,
    get_installer_dir,
    load_manifest,
)
from orchestune.version import get_version

_TOKEN_PATTERN = re.compile(r"gh[opur]_[A-Za-z0-9_]+")


def _sanitize_output(text: str) -> str:
    return _TOKEN_PATTERN.sub("<REDACTED_TOKEN>", text)


def _check_python_version() -> DiagnosticItem:
    vi = sys.version_info
    if (3, 12) <= (vi.major, vi.minor) < (4, 0):
        return DiagnosticItem(
            check_name="python_version",
            status="ok",
            message=f"Python version {vi.major}.{vi.minor}.{vi.micro} satisfies requirement (>=3.12, <4.0)",
        )
    return DiagnosticItem(
        check_name="python_version",
        status="error",
        message=f"Python version {vi.major}.{vi.minor}.{vi.micro} does not satisfy requirement (>=3.12, <4.0)",
    )


def _check_binaries() -> tuple[DiagnosticItem, DiagnosticItem, str | None]:
    git_path = shutil.which("git")
    git_item = (
        DiagnosticItem(
            check_name="git_binary", status="ok", message=f"Git found at {git_path}"
        )
        if git_path
        else DiagnosticItem(
            check_name="git_binary", status="error", message="Git not found in PATH"
        )
    )

    gh_path = shutil.which("gh")
    gh_item = (
        DiagnosticItem(
            check_name="gh_binary",
            status="ok",
            message=f"GitHub CLI found at {gh_path}",
        )
        if gh_path
        else DiagnosticItem(
            check_name="gh_binary",
            status="warning",
            message="GitHub CLI (gh) not found in PATH (required for PR and review workflows)",
        )
    )
    return git_item, gh_item, gh_path


def _check_github_auth(gh_path: str | None, offline: bool) -> DiagnosticItem:
    if offline:
        return DiagnosticItem(
            check_name="github_auth",
            status="not_checked",
            message="GitHub authentication check skipped (--offline mode)",
        )
    if not gh_path:
        return DiagnosticItem(
            check_name="github_auth",
            status="warning",
            message="GitHub CLI (gh) is not available to verify authentication",
        )
    try:
        res = subprocess.run(
            [gh_path, "auth", "status"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5.0,
            check=False,
        )
        sanitized_out = _sanitize_output((res.stdout + "\n" + res.stderr).strip())
        if res.returncode == 0:
            return DiagnosticItem(
                check_name="github_auth",
                status="ok",
                message="GitHub CLI authenticated successfully",
                details={"output": sanitized_out},
            )
        return DiagnosticItem(
            check_name="github_auth",
            status="warning",
            message="GitHub CLI authentication check reported non-zero status",
            details={"output": sanitized_out},
        )
    except subprocess.TimeoutExpired:
        return DiagnosticItem(
            check_name="github_auth",
            status="warning",
            message="GitHub CLI authentication check timed out after 5.0 seconds",
        )
    except Exception as e:
        return DiagnosticItem(
            check_name="github_auth",
            status="warning",
            message=f"GitHub CLI authentication check failed: {e}",
        )


def _check_cli_version() -> DiagnosticItem:
    cli_path = shutil.which("orchestune")
    current_ver = get_version()
    if cli_path:
        return DiagnosticItem(
            check_name="orchestune_cli",
            status="ok",
            message=f"orchestune CLI found at {cli_path} (current package: {current_ver})",
        )
    return DiagnosticItem(
        check_name="orchestune_cli",
        status="warning",
        message="orchestune executable not found in PATH",
    )


def _check_physical_root(root: PhysicalRoot) -> list[DiagnosticItem]:
    items: list[DiagnosticItem] = []
    installer_dir = get_installer_dir(root.path)
    tx_dir = installer_dir / TRANSACTIONS_DIR
    if tx_dir.is_dir() and any(sub.is_dir() for sub in tx_dir.iterdir()):
        items.append(
            DiagnosticItem(
                check_name=f"root_transactions_{root.path.name}",
                status="warning",
                message=f"Root {root.path} has pending transaction journals",
            )
        )

    try:
        manifest = load_manifest(root.path)
        if manifest:
            items.append(
                DiagnosticItem(
                    check_name=f"root_manifest_{root.path.name}",
                    status="ok",
                    message=f"Valid manifest found at {root.path} (generation {manifest.generation})",
                )
            )
    except Exception as e:
        items.append(
            DiagnosticItem(
                check_name=f"root_manifest_{root.path.name}",
                status="error",
                message=f"Corrupted manifest at {root.path}: {e}",
            )
        )
    return items


def run_doctor_checks(
    roots: list[PhysicalRoot],
    offline: bool = False,
) -> list[DiagnosticItem]:
    diagnostics: list[DiagnosticItem] = [_check_python_version()]

    git_item, gh_item, gh_path = _check_binaries()
    diagnostics.extend([git_item, gh_item])
    diagnostics.append(_check_github_auth(gh_path, offline=offline))
    diagnostics.append(_check_cli_version())

    for root in roots:
        diagnostics.extend(_check_physical_root(root))

    return diagnostics

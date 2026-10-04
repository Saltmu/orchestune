import subprocess
from pathlib import Path
from unittest.mock import patch

from orchestune.installer.contracts import PhysicalRoot, ScopeType, TargetType
from orchestune.installer.doctor import run_doctor_checks


def test_doctor_offline_skips_auth(tmp_path: Path):
    root = PhysicalRoot(
        path=tmp_path / "skills",
        target_types=[TargetType.CODEX],
        scope=ScopeType.PROJECT,
    )
    diagnostics = run_doctor_checks([root], offline=True)

    gh_auth = next((d for d in diagnostics if d.check_name == "github_auth"), None)
    assert gh_auth is not None
    assert gh_auth.status == "not_checked"


def test_doctor_python_version():
    diagnostics = run_doctor_checks([], offline=True)
    py_check = next((d for d in diagnostics if d.check_name == "python_version"), None)
    assert py_check is not None
    assert py_check.status == "ok"


def test_doctor_gh_auth_no_token_leak():
    fake_completed = subprocess.CompletedProcess(
        args=["gh", "auth", "status"],
        returncode=0,
        stdout="Logged in to github.com account Saltmu (gho_secret123456789)\n",
        stderr="",
    )
    with patch("subprocess.run", return_value=fake_completed):
        diagnostics = run_doctor_checks([], offline=False)

    gh_auth = next((d for d in diagnostics if d.check_name == "github_auth"), None)
    assert gh_auth is not None
    # Ensure token string is sanitized
    assert "gho_secret123456789" not in gh_auth.message
    assert "gho_secret123456789" not in str(gh_auth.details)
    assert "<REDACTED_TOKEN>" in str(gh_auth.details)

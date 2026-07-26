from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_powershell_scripts_exist():
    local_ci_ps1 = PROJECT_ROOT / "scripts" / "local-ci.ps1"
    setup_hooks_ps1 = PROJECT_ROOT / "scripts" / "setup-git-hooks.ps1"

    assert local_ci_ps1.exists(), "scripts/local-ci.ps1 must exist"
    assert setup_hooks_ps1.exists(), "scripts/setup-git-hooks.ps1 must exist"


def test_powershell_local_ci_contract():
    local_ci_ps1 = PROJECT_ROOT / "scripts" / "local-ci.ps1"
    content = local_ci_ps1.read_text(encoding="utf-8")

    assert "poetry run ruff format --check" in content
    assert "poetry run ruff check" in content
    assert "poetry run mypy orchestune tests" in content
    assert "poetry run pytest" in content
    assert "gitleaks detect" in content


def test_powershell_setup_git_hooks_contract():
    setup_hooks_ps1 = PROJECT_ROOT / "scripts" / "setup-git-hooks.ps1"
    content = setup_hooks_ps1.read_text(encoding="utf-8")

    assert "pre-commit" in content
    assert "pre-push" in content
    assert "local-ci.ps1" in content

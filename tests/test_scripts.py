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


def test_gitleaks_installer_scripts_exist():
    install_sh = PROJECT_ROOT / "scripts" / "install-gitleaks.sh"
    install_ps1 = PROJECT_ROOT / "scripts" / "install-gitleaks.ps1"

    assert install_sh.exists(), "scripts/install-gitleaks.sh must exist"
    assert install_ps1.exists(), "scripts/install-gitleaks.ps1 must exist"


def test_local_ci_auto_installs_gitleaks_when_missing():
    local_ci_sh = PROJECT_ROOT / "scripts" / "local-ci.sh"
    content = local_ci_sh.read_text(encoding="utf-8")

    assert "install-gitleaks.sh" in content
    assert (
        "gitleaks is not installed locally and automatic installation failed" in content
    )


def test_powershell_local_ci_auto_installs_gitleaks_when_missing():
    local_ci_ps1 = PROJECT_ROOT / "scripts" / "local-ci.ps1"
    content = local_ci_ps1.read_text(encoding="utf-8")

    assert "install-gitleaks.ps1" in content
    assert (
        "gitleaks is not installed locally and automatic installation failed" in content
    )


def test_setup_git_hooks_proactively_installs_gitleaks():
    setup_hooks_sh = PROJECT_ROOT / "scripts" / "setup-git-hooks.sh"
    setup_hooks_ps1 = PROJECT_ROOT / "scripts" / "setup-git-hooks.ps1"

    assert "install-gitleaks.sh" in setup_hooks_sh.read_text(encoding="utf-8")
    assert "install-gitleaks.ps1" in setup_hooks_ps1.read_text(encoding="utf-8")

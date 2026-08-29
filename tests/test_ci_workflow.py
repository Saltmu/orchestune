import os

import yaml


def test_ci_workflow_has_explicit_permissions():
    ci_workflow_path = os.path.join(
        os.path.dirname(__file__), "..", ".github", "workflows", "ci.yml"
    )
    assert os.path.exists(ci_workflow_path), f"{ci_workflow_path} does not exist"

    with open(ci_workflow_path, encoding="utf-8") as f:
        workflow = yaml.safe_load(f)

    # ワークフローに permissions キーが存在することを確認
    assert (
        "permissions" in workflow
    ), "permissions block is missing in .github/workflows/ci.yml"

    # permissions が辞書型であることを確認
    permissions = workflow["permissions"]
    assert isinstance(permissions, dict), "permissions block must be a dictionary"

    # permissions に contents: read が含まれていることを確認
    assert (
        permissions.get("contents") == "read"
    ), "permissions.contents must be 'read' to restrict default token scope"


def test_pytest_addopts_caps_worker_count():
    """`-n auto` alone oversubscribes on many-core hosts (e.g. 16 workers on a
    16-core WSL2 box), which measurably increases both wall time and peak
    memory versus a capped worker count, and can trigger OS OOM kills under
    concurrent local-ci.sh runs. `--maxprocesses` caps `-n auto`'s worker
    count while still degrading gracefully (no-op) on hosts with fewer cores.
    """
    import tomllib

    pyproject_path = os.path.join(os.path.dirname(__file__), "..", "pyproject.toml")
    with open(pyproject_path, "rb") as f:
        pyproject = tomllib.load(f)

    addopts = pyproject["tool"]["pytest"]["ini_options"]["addopts"]
    assert "-n auto" in addopts, "addopts must keep auto-detecting worker count"
    assert "--maxprocesses=" in addopts, (
        "addopts must cap the worker count via --maxprocesses to avoid "
        "oversubscription/OOM on many-core hosts"
    )


def test_local_ci_sh_does_not_bypass_pytest_worker_cap():
    """scripts/local-ci.sh must not pass its own `-n` value: doing so would
    bypass the `--maxprocesses` cap defined in pyproject.toml's addopts and
    reintroduce the oversubscription/OOM risk the cap exists to prevent.
    """
    local_ci_path = os.path.join(
        os.path.dirname(__file__), "..", "scripts", "local-ci.sh"
    )
    with open(local_ci_path, encoding="utf-8") as f:
        content = f.read()

    pytest_lines = [
        line for line in content.splitlines() if "poetry run pytest" in line
    ]
    assert pytest_lines, "expected a `poetry run pytest` invocation in local-ci.sh"
    for line in pytest_lines:
        assert "-n " not in line and not line.rstrip().endswith("-n"), (
            f"local-ci.sh must not pass -n directly (bypasses the "
            f"--maxprocesses cap in pyproject.toml): {line!r}"
        )

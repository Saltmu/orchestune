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


def test_ci_workflow_uses_setup_uv_and_frozen_sync():
    ci_workflow_path = os.path.join(
        os.path.dirname(__file__), "..", ".github", "workflows", "ci.yml"
    )
    assert os.path.exists(ci_workflow_path), f"{ci_workflow_path} does not exist"

    with open(ci_workflow_path, encoding="utf-8") as f:
        workflow = yaml.safe_load(f)

    job = workflow["jobs"]["ci"]
    steps = job["steps"]

    # setup-uv ステップの検証
    setup_uv_steps = [
        s
        for s in steps
        if isinstance(s, dict) and "astral-sh/setup-uv" in s.get("uses", "")
    ]
    assert setup_uv_steps, "expected astral-sh/setup-uv step in ci.yml"
    setup_uv = setup_uv_steps[0]
    assert (
        setup_uv.get("with", {}).get("enable-cache") is True
    ), "enable-cache must be set to true for setup-uv"

    # uv sync --frozen ステップの検証
    uv_sync_steps = [
        s
        for s in steps
        if isinstance(s, dict) and "uv sync --frozen" in s.get("run", "")
    ]
    assert uv_sync_steps, "expected uv sync --frozen step in ci.yml"

    # poetry への依存が残っていないことの検証
    with open(ci_workflow_path, encoding="utf-8") as f:
        content = f.read()
    assert (
        "poetry" not in content.lower()
    ), "ci.yml must not contain any reference to poetry"


def test_ci_workflow_caches_gitleaks_binary():
    ci_workflow_path = os.path.join(
        os.path.dirname(__file__), "..", ".github", "workflows", "ci.yml"
    )
    assert os.path.exists(ci_workflow_path), f"{ci_workflow_path} does not exist"

    with open(ci_workflow_path, encoding="utf-8") as f:
        workflow = yaml.safe_load(f)

    job = workflow["jobs"]["ci"]
    steps = job["steps"]

    cache_steps = [
        s
        for s in steps
        if isinstance(s, dict)
        and "actions/cache" in s.get("uses", "")
        and "gitleaks" in s.get("name", "").lower()
    ]
    assert cache_steps, "expected actions/cache step for gitleaks in ci.yml"
    cache_step = cache_steps[0]
    with_block = cache_step.get("with", {})
    path = with_block.get("path", "")
    assert (
        ".local" in path and "gitleaks" in path
    ), f"cache path should target .local/bin/gitleaks*: {path}"
    key = with_block.get("key", "")
    assert (
        "runner.os" in key and "gitleaks" in key
    ), f"cache key should reference runner.os and gitleaks: {key}"


def test_pytest_addopts_uses_two_workers():
    """Ordinary pytest runs should use exactly two xdist workers by default."""
    import tomllib

    pyproject_path = os.path.join(os.path.dirname(__file__), "..", "pyproject.toml")
    with open(pyproject_path, "rb") as f:
        pyproject = tomllib.load(f)

    addopts = pyproject["tool"]["pytest"]["ini_options"]["addopts"].split()
    assert "-n" in addopts, "addopts must set the xdist worker count"
    worker_option_index = addopts.index("-n")
    assert (
        addopts[worker_option_index + 1] == "2"
    ), "ordinary pytest runs must use exactly two xdist workers"


def test_pytest_addopts_uses_loadgroup_distribution():
    """Default pytest addopts must co-locate grouped tests via --dist=loadgroup."""
    import tomllib

    pyproject_path = os.path.join(os.path.dirname(__file__), "..", "pyproject.toml")
    with open(pyproject_path, "rb") as f:
        pyproject = tomllib.load(f)

    addopts = pyproject["tool"]["pytest"]["ini_options"]["addopts"].split()
    assert (
        "--dist=loadgroup" in addopts
    ), "addopts must specify --dist=loadgroup for xdist test grouping"


def test_local_ci_sh_inherits_pytest_default():
    """scripts/local-ci.sh should inherit the two-worker pytest default."""
    local_ci_path = os.path.join(
        os.path.dirname(__file__), "..", "scripts", "local-ci.sh"
    )
    with open(local_ci_path, encoding="utf-8") as f:
        content = f.read()

    pytest_lines = [line for line in content.splitlines() if "uv run pytest" in line]
    assert pytest_lines, "expected a `uv run pytest` invocation in local-ci.sh"
    for line in pytest_lines:
        assert "-n " not in line and not line.rstrip().endswith("-n"), (
            f"local-ci.sh must not pass -n directly (bypasses the "
            f"two-worker default in pyproject.toml): {line!r}"
        )


def test_ci_workflow_disables_defender_only_on_windows_before_checks():
    """Only disposable Windows runners disable scanning, before CI starts."""
    workflow_path = os.path.join(
        os.path.dirname(__file__), "..", ".github", "workflows", "ci.yml"
    )
    with open(workflow_path, encoding="utf-8") as f:
        workflow = yaml.safe_load(f)

    steps = workflow["jobs"]["ci"]["steps"]
    defender_steps = [
        step for step in steps if "Set-MpPreference" in step.get("run", "")
    ]
    assert len(defender_steps) == 1
    defender_step = defender_steps[0]
    assert defender_step["if"] == "runner.os == 'Windows'"
    assert defender_step["shell"] == "pwsh"
    assert "Set-MpPreference -DisableRealtimeMonitoring $true" in defender_step["run"]
    windows_step = next(s for s in steps if s["name"] == "Run CI checks (Windows)")
    assert steps.index(defender_step) < steps.index(windows_step)


def test_ci_workflow_scopes_four_workers_and_durations_to_ci_steps():
    """Hosted Linux and Windows CI use four workers and durations without changing other jobs."""
    workflow_path = os.path.join(
        os.path.dirname(__file__), "..", ".github", "workflows", "ci.yml"
    )
    with open(workflow_path, encoding="utf-8") as f:
        workflow = yaml.safe_load(f)

    assert "ORCHESTUNE_TEST_WORKERS" not in workflow.get("env", {})
    assert "PYTEST_ADDOPTS" not in workflow.get("env", {})
    configured_steps = set()
    for job in workflow["jobs"].values():
        assert "ORCHESTUNE_TEST_WORKERS" not in job.get("env", {})
        assert "PYTEST_ADDOPTS" not in job.get("env", {})
        for step in job["steps"]:
            env = step.get("env", {})
            name = step.get("name")
            if name == "Run CI checks (Windows)":
                assert step["if"] == "runner.os == 'Windows'"
                assert env["ORCHESTUNE_TEST_WORKERS"] == "4"
                assert env["PYTEST_ADDOPTS"] == "--durations=30"
                configured_steps.add(name)
            elif name == "Run CI checks (Linux)":
                assert step["if"] == "runner.os == 'Linux'"
                assert env["ORCHESTUNE_TEST_WORKERS"] == "4"
                assert env["PYTEST_ADDOPTS"] == "--durations=30"
                configured_steps.add(name)
            else:
                assert "ORCHESTUNE_TEST_WORKERS" not in env
                assert "PYTEST_ADDOPTS" not in env
    assert configured_steps == {"Run CI checks (Linux)", "Run CI checks (Windows)"}

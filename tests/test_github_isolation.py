"""Regression tests for process-wide GitHub access isolation."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import urllib.request
from http.client import HTTPConnection, HTTPSConnection
from pathlib import Path

import pytest

from orchestune.forge import GitHubForge, LabelSpec
from tests.github_isolation import GitHubAccessBlocked, GitHubAccessMonitor

_ORIGINAL_POPEN = subprocess.Popen


@pytest.mark.uses_real_forge
@pytest.mark.parametrize("operation", ["run", "stdin", "auth", "labels"])
def test_real_forge_construction_cannot_communicate_with_github(
    github_access_isolation: GitHubAccessMonitor, operation: str
) -> None:
    github_access_isolation.expect("cli")
    forge = GitHubForge()

    with pytest.raises(GitHubAccessBlocked, match="blocked unmocked GitHub"):
        if operation == "run":
            forge._run(["gh", "api", "/user"])
        elif operation == "stdin":
            forge._run(
                ["gh", "api", "/repos/owner/repo/issues", "--method", "POST"],
                input_text='{"title":"test"}',
            )
        elif operation == "auth":
            forge.check_auth()
        else:
            forge.ensure_labels((LabelSpec("risk:flagged", "E11D21", "risky"),))


def test_popen_executable_override_cannot_launch_absolute_gh(
    github_access_isolation: GitHubAccessMonitor,
) -> None:
    github_access_isolation.expect("cli")
    gh_path = Path(os.sep) / "missing" / ("gh.exe" if os.name == "nt" else "gh")

    with pytest.raises(GitHubAccessBlocked, match="blocked unmocked GitHub"):
        subprocess.Popen([sys.executable, "-c", "pass"], executable=str(gh_path))


def test_popen_guard_preserves_the_popen_class_contract() -> None:
    assert isinstance(subprocess.Popen, type)
    assert issubclass(subprocess.Popen, _ORIGINAL_POPEN)

    process = subprocess.Popen([sys.executable, "-c", "pass"])

    assert isinstance(process, subprocess.Popen)
    assert isinstance(process, _ORIGINAL_POPEN)
    assert process.wait(timeout=10) == 0


def test_non_shell_argument_with_absolute_gh_path_is_not_a_cli_launch() -> None:
    gh_path = Path(os.sep) / "missing" / ("gh.exe" if os.name == "nt" else "gh")

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; print(sys.argv[1])",
            str(gh_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == str(gh_path)


def test_shell_cannot_launch_absolute_gh(
    github_access_isolation: GitHubAccessMonitor,
) -> None:
    github_access_isolation.expect("cli")
    gh_path = Path(os.sep) / "missing" / ("gh.exe" if os.name == "nt" else "gh")
    command = f'"{gh_path}" api /user'

    with pytest.raises(GitHubAccessBlocked, match="blocked unmocked GitHub"):
        subprocess.Popen(command, shell=True)


def test_shell_list_cannot_launch_absolute_gh(
    github_access_isolation: GitHubAccessMonitor,
) -> None:
    github_access_isolation.expect("cli")
    gh_path = Path(os.sep) / "missing" / ("gh.exe" if os.name == "nt" else "gh")
    if os.name == "nt":
        command = ["cmd.exe", "/c", f'"{gh_path}" api /user']
    else:
        command = ["sh", "-c", f"{gh_path} api /user"]

    with pytest.raises(GitHubAccessBlocked, match="blocked unmocked GitHub"):
        subprocess.Popen(command)


def test_shell_child_uses_the_guarded_path_shim(
    github_access_isolation: GitHubAccessMonitor,
) -> None:
    github_access_isolation.expect("cli")
    if os.name == "nt":
        command = ["cmd.exe", "/c", "gh api /user"]
    else:
        command = ["sh", "-c", "gh api /user"]

    result = subprocess.run(command, capture_output=True, text=True, check=False)

    assert result.returncode == 97
    assert "blocked unmocked GitHub CLI" in result.stderr


def test_shell_child_keeps_the_guarded_shim_with_explicit_environment(
    github_access_isolation: GitHubAccessMonitor,
) -> None:
    github_access_isolation.expect("cli")
    shim_dir = str(github_access_isolation.log_path.parent / "bin")
    path_without_shim = os.pathsep.join(
        part
        for part in os.environ.get("PATH", "").split(os.pathsep)
        if part != shim_dir
    )
    env = {"PATH": path_without_shim, "GH_TOKEN": "test-secret"}
    if os.name == "nt":
        command = ["cmd.exe", "/c", "gh api /user"]
    else:
        command = ["sh", "-c", "gh api /user"]

    result = subprocess.run(command, env=env, capture_output=True, text=True)

    assert result.returncode == 97
    assert "blocked unmocked GitHub CLI" in result.stderr


def test_python_child_inherits_the_process_guard(
    github_access_isolation: GitHubAccessMonitor,
) -> None:
    github_access_isolation.expect("cli")
    gh_path = Path(os.sep) / "missing" / ("gh.exe" if os.name == "nt" else "gh")
    child_code = f"import subprocess; subprocess.run([{str(gh_path)!r}])"

    result = subprocess.run(
        [sys.executable, "-c", child_code], capture_output=True, text=True
    )

    assert result.returncode != 0
    assert "GitHubAccessBlocked" in result.stderr


def test_python_child_with_explicit_environment_keeps_absolute_cli_guard(
    github_access_isolation: GitHubAccessMonitor,
) -> None:
    github_access_isolation.expect("cli")
    gh_path = Path(os.sep) / "missing" / ("gh.exe" if os.name == "nt" else "gh")
    child_code = f"import subprocess; subprocess.run([{str(gh_path)!r}])"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GH_TOKEN": "test-secret",
        "GH_CONFIG_DIR": str(github_access_isolation.log_path.parent),
    }

    result = subprocess.run(
        [sys.executable, "-c", child_code], env=env, capture_output=True, text=True
    )

    assert result.returncode != 0
    assert "GitHubAccessBlocked" in result.stderr


def test_python_child_with_empty_environment_does_not_inherit_parent_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ORCHESTUNE_TEST_EMPTY_ENV_SENTINEL", "parent-value")
    child_code = (
        "import json, os; "
        "print(json.dumps(os.environ.get('ORCHESTUNE_TEST_EMPTY_ENV_SENTINEL')))"
    )

    result = subprocess.run(
        [sys.executable, "-c", child_code],
        env={},
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(result.stdout) is None


def test_positional_child_environment_is_sanitized_and_guarded(
    github_access_isolation: GitHubAccessMonitor,
) -> None:
    github_access_isolation.expect("cli")
    gh_path = Path(os.sep) / "missing" / ("gh.exe" if os.name == "nt" else "gh")
    child_code = (
        "import json, os, subprocess\n"
        f"gh_path = {str(gh_path.parent)!r} + os.sep + {gh_path.name!r}\n"
        "blocked = None\n"
        "try:\n"
        "    subprocess.run([gh_path])\n"
        "except Exception as error:\n"
        "    blocked = type(error).__name__\n"
        "print(json.dumps({'token': os.environ.get('GH_TOKEN'), 'blocked': blocked}))\n"
    )
    explicit_env = {"PATH": os.environ.get("PATH", ""), "GH_TOKEN": "test-secret"}

    process = subprocess.Popen(
        [sys.executable, "-c", child_code],
        -1,
        None,
        subprocess.DEVNULL,
        subprocess.PIPE,
        subprocess.PIPE,
        None,
        True,
        False,
        None,
        explicit_env,
        text=True,
    )
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode == 0, stderr
    child_result = json.loads(stdout)
    assert child_result["token"] is None
    assert child_result["blocked"] == "GitHubAccessBlocked"


def test_child_processes_receive_no_github_credentials_or_user_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "GH_HOST",
    ):
        monkeypatch.setenv(name, "test-secret")
    child_code = (
        "import json, os; "
        "print(json.dumps({name: os.environ.get(name) for name in "
        "('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', "
        "'GITHUB_ENTERPRISE_TOKEN', 'GH_HOST')}))"
    )

    result = subprocess.run(
        [sys.executable, "-c", child_code], capture_output=True, text=True, check=True
    )

    assert json.loads(result.stdout) == {
        "GH_TOKEN": None,
        "GITHUB_TOKEN": None,
        "GH_ENTERPRISE_TOKEN": None,
        "GITHUB_ENTERPRISE_TOKEN": None,
        "GH_HOST": None,
    }
    config_dir = Path(os.environ["GH_CONFIG_DIR"])
    assert config_dir.is_dir()
    assert list(config_dir.iterdir()) == []


@pytest.mark.parametrize(
    "url", ["https://api.github.com/user", "https://github.com/Saltmu/orchestune"]
)
def test_python_http_clients_are_blocked_before_transport(
    github_access_isolation: GitHubAccessMonitor, url: str
) -> None:
    github_access_isolation.expect("http")

    with pytest.raises(GitHubAccessBlocked, match="blocked unmocked GitHub"):
        urllib.request.urlopen(url, timeout=0.01)


def test_https_proxy_tunnel_to_github_is_blocked_before_transport(
    github_access_isolation: GitHubAccessMonitor,
) -> None:
    github_access_isolation.expect("http")
    connection = HTTPSConnection("proxy.invalid")
    connection.set_tunnel("api.github.com", 443)

    with pytest.raises(GitHubAccessBlocked, match="blocked unmocked GitHub"):
        connection.connect()


def test_http_proxy_request_to_github_is_blocked_before_transport(
    github_access_isolation: GitHubAccessMonitor,
) -> None:
    github_access_isolation.expect("http")
    connection = HTTPConnection("proxy.invalid")

    with pytest.raises(GitHubAccessBlocked, match="blocked unmocked GitHub"):
        connection.request("GET", "http://github.com/Saltmu/orchestune")


def test_local_socket_connections_remain_available() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        with socket.create_connection(listener.getsockname(), timeout=1) as client:
            assert client.getpeername() == listener.getsockname()

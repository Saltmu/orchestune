"""Block unmocked GitHub CLI and HTTP calls throughout the test process tree.

The socket guard is best-effort: it can identify GitHub hostnames, not raw IP
addresses. Python children started with ``-I`` or ``-S`` also skip the
``sitecustomize`` startup hook used to install their process-local guards.
"""

from __future__ import annotations

import http.client
import inspect
import ntpath
import os
import shlex
import shutil
import socket
import subprocess
import uuid
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_LOG_ENV = "ORCHESTUNE_TEST_GITHUB_GUARD_LOG"
_PROCESS_ENV = "ORCHESTUNE_TEST_GITHUB_GUARD_PROCESS"
_BIN_ENV = "ORCHESTUNE_TEST_GITHUB_GUARD_BIN"
_PYTHON_ENV = "ORCHESTUNE_TEST_GITHUB_GUARD_PYTHON"
_PROJECT_ENV = "ORCHESTUNE_TEST_GITHUB_GUARD_PROJECT"
_SENSITIVE_GH_ENV = frozenset(
    {
        "gh_token",
        "github_token",
        "gh_enterprise_token",
        "github_enterprise_token",
        "gh_host",
    }
)
_GH_EXECUTABLES = frozenset({"gh", "gh.exe", "gh.cmd", "gh.bat"})
_GITHUB_SUFFIXES = ("github.com", "githubusercontent.com", "githubassets.com")
_CHILD_GUARDS_INSTALLED = False


class GitHubAccessBlocked(RuntimeError):
    """A test attempted an unmocked GitHub operation before transport."""


@dataclass
class GitHubAccessMonitor:
    """Record expected blocked calls without storing command arguments or secrets."""

    log_path: Path
    expected: Counter[str] = field(default_factory=Counter)

    def expect(self, kind: str, count: int = 1) -> None:
        self.expected[kind] += count

    def observed(self) -> Counter[str]:
        if not self.log_path.exists():
            return Counter()
        return Counter(self.log_path.read_text(encoding="utf-8").splitlines())

    def assert_expected(self) -> None:
        actual = self.observed()
        unexpected = actual - self.expected
        missing = self.expected - actual
        if unexpected or missing:
            raise AssertionError(
                "GitHub isolation event mismatch: "
                f"unexpected={dict(unexpected)}, missing={dict(missing)}"
            )


def _record_blocked(kind: str) -> None:
    log_path = os.environ.get(_LOG_ENV)
    if not log_path:
        return
    descriptor = os.open(log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, f"{kind}\n".encode("ascii"))
    finally:
        os.close(descriptor)


def _blocked(kind: str, operation: str) -> None:
    _record_blocked(kind)
    raise GitHubAccessBlocked(
        f"blocked unmocked GitHub {operation} before external transport"
    )


def _as_text(value: Any) -> str:
    try:
        return os.fsdecode(os.fspath(value))
    except TypeError:
        return str(value)


def _is_gh_executable(value: Any) -> bool:
    token = _as_text(value).strip().strip("\"'")
    basename = ntpath.basename(token.replace("/", "\\")).lower()
    return basename in _GH_EXECUTABLES


def _command_parts(args: Any) -> list[str]:
    if isinstance(args, bytes | str):
        return [_as_text(args)]
    if isinstance(args, Sequence):
        return [_as_text(part) for part in args]
    return [_as_text(args)]


def _shell_contains_absolute_gh(args: Any, shell: bool = False) -> bool:
    parts = _command_parts(args)
    if shell and isinstance(args, bytes | str):
        commands = parts
    else:
        commands = parts[1:]
    for command in commands:
        try:
            tokens = shlex.split(command, posix=os.name != "nt")
        except ValueError:
            tokens = [command]
        for token in tokens:
            if _is_gh_executable(token) and ("/" in token or "\\" in token):
                return True
    return False


def _would_launch_github_cli(
    args: Any, executable: Any = None, shell: bool = False
) -> bool:
    if executable is not None and _is_gh_executable(executable):
        return True
    parts = _command_parts(args)
    return bool(parts and _is_gh_executable(parts[0])) or _shell_contains_absolute_gh(
        args, shell=shell
    )


def _set_env_value(env: dict[str, str], name: str, value: str) -> None:
    for existing in tuple(env):
        if existing.lower() == name.lower():
            env[existing] = value
            return
    env[name] = value


def _environment_value(env: Mapping[str, Any], name: str) -> str:
    for key, value in env.items():
        if str(key).lower() == name.lower():
            return _as_text(value)
    return ""


def _child_environment(explicit: Mapping[str, Any] | None) -> dict[str, str]:
    source = os.environ if explicit is None else explicit
    env = {str(key): _as_text(value) for key, value in source.items()}
    env = {
        key: value for key, value in env.items() if key.lower() not in _SENSITIVE_GH_ENV
    }
    for name in (_LOG_ENV, _BIN_ENV, _PYTHON_ENV, _PROJECT_ENV, "GH_CONFIG_DIR"):
        value = os.environ.get(name)
        if value is not None:
            _set_env_value(env, name, value)

    original_path = _environment_value(env, "PATH") or os.environ.get("PATH", "")
    guarded_path = os.pathsep.join(
        part for part in (os.environ.get(_BIN_ENV, ""), original_path) if part
    )
    _set_env_value(env, "PATH", guarded_path)

    original_pythonpath = _environment_value(env, "PYTHONPATH")
    if not original_pythonpath:
        original_pythonpath = os.environ.get("PYTHONPATH", "")
    guarded_pythonpath = os.pathsep.join(
        part
        for part in (
            os.environ.get(_PYTHON_ENV, ""),
            os.environ.get(_PROJECT_ENV, ""),
            original_pythonpath,
        )
        if part
    )
    _set_env_value(env, "PYTHONPATH", guarded_pythonpath)
    _set_env_value(env, _PROCESS_ENV, "child")
    return env


def _make_popen_guard(
    original: type[subprocess.Popen[Any]],
) -> type[subprocess.Popen[Any]]:
    signature = inspect.signature(original.__init__)

    def guarded_init(self: Any, *popen_args: Any, **kwargs: Any) -> None:
        bound = signature.bind(self, *popen_args, **kwargs)
        values = bound.arguments
        if _would_launch_github_cli(
            values.get("args"),
            values.get("executable"),
            shell=values.get("shell", False),
        ):
            _blocked("cli", "CLI process")
        values["env"] = _child_environment(values.get("env"))
        original.__init__(*bound.args, **bound.kwargs)

    return type(
        original.__name__,
        (original,),
        {
            "__init__": guarded_init,
            "__module__": original.__module__,
            "__qualname__": original.__qualname__,
        },
    )


def _is_github_host(host: Any) -> bool:
    candidate = str(host).strip()
    normalized = (urlsplit(f"//{candidate}").hostname or candidate).rstrip(".").lower()
    return any(
        normalized == suffix or normalized.endswith(f".{suffix}")
        for suffix in _GITHUB_SUFFIXES
    )


def _is_github_target(target: Any) -> bool:
    parsed = urlsplit(_as_text(target))
    return parsed.scheme in {"http", "https"} and _is_github_host(parsed.hostname)


def _address_host(address: Any) -> str:
    if isinstance(address, tuple) and address:
        return str(address[0])
    return str(address)


def _install_guards(setter: Callable[[Any, str, Any], None]) -> None:
    original_popen = subprocess.Popen
    original_request = http.client.HTTPConnection.request

    def guarded_request(
        connection: Any, method: Any, url: Any, *args: Any, **kwargs: Any
    ) -> Any:
        if (
            _is_github_host(connection.host)
            or _is_github_host(getattr(connection, "_tunnel_host", None))
            or _is_github_target(url)
        ):
            _blocked("http", "HTTP request")
        return original_request(connection, method, url, *args, **kwargs)

    def guarded_connection(connect: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(connection: Any, *args: Any, **kwargs: Any) -> Any:
            if _is_github_host(connection.host) or _is_github_host(
                getattr(connection, "_tunnel_host", None)
            ):
                _blocked("http", "HTTP connection")
            return connect(connection, *args, **kwargs)

        return wrapped

    def guarded_create_connection(create: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(address: Any, *args: Any, **kwargs: Any) -> Any:
            if _is_github_host(_address_host(address)):
                _blocked("http", "socket connection")
            return create(address, *args, **kwargs)

        return wrapped

    def guarded_socket_connect(connect: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(sock: Any, address: Any, *args: Any, **kwargs: Any) -> Any:
            if _is_github_host(_address_host(address)):
                _blocked("http", "socket connection")
            return connect(sock, address, *args, **kwargs)

        return wrapped

    setter(subprocess, "Popen", _make_popen_guard(original_popen))
    setter(http.client.HTTPConnection, "request", guarded_request)
    setter(
        http.client.HTTPConnection,
        "connect",
        guarded_connection(http.client.HTTPConnection.connect),
    )
    setter(
        http.client.HTTPSConnection,
        "connect",
        guarded_connection(http.client.HTTPSConnection.connect),
    )
    setter(
        socket,
        "create_connection",
        guarded_create_connection(socket.create_connection),
    )
    setter(
        socket.socket,
        "connect",
        guarded_socket_connect(socket.socket.connect),
    )
    setter(
        socket.socket,
        "connect_ex",
        guarded_socket_connect(socket.socket.connect_ex),
    )


def install_child_guards() -> None:
    """Install stdlib boundaries in Python children through ``sitecustomize``."""
    global _CHILD_GUARDS_INSTALLED
    if _CHILD_GUARDS_INSTALLED:
        return
    _CHILD_GUARDS_INSTALLED = True
    os.environ[_PROCESS_ENV] = "child"
    _install_guards(setattr)


def _write_cli_shim(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True)
    if os.name == "nt":
        shim = bin_dir / "gh.cmd"
        shim.write_text(
            '@echo off\r\n>>"%ORCHESTUNE_TEST_GITHUB_GUARD_LOG%" echo cli\r\n'
            "echo blocked unmocked GitHub CLI invocation 1>&2\r\nexit /b 97\r\n",
            encoding="utf-8",
        )
        return

    shim = bin_dir / "gh"
    shim.write_text(
        "#!/bin/sh\nprintf '%s\\n' cli >> \"$ORCHESTUNE_TEST_GITHUB_GUARD_LOG\"\n"
        "echo 'blocked unmocked GitHub CLI invocation' >&2\nexit 97\n",
        encoding="utf-8",
    )
    shim.chmod(0o700)


def _write_sitecustomize(python_dir: Path) -> None:
    # This startup hook intentionally precedes any existing sitecustomize.
    # Python children using -I or -S skip it; see the module limitation above.
    python_dir.mkdir(parents=True)
    (python_dir / "sitecustomize.py").write_text(
        "from tests.github_isolation import install_child_guards\n"
        "install_child_guards()\n",
        encoding="utf-8",
    )


@contextmanager
def configure_test_isolation(
    setenv: Callable[[str, str], None],
    setattr: Callable[[Any, str, Any], None],
    project_root: Path,
) -> Iterator[GitHubAccessMonitor]:
    """Install parent and child guards and monitor blocked calls for one test."""
    scratch = (
        project_root
        / ".orchestune"
        / "tmp"
        / (f"github-isolation-{os.getpid()}-{uuid.uuid4().hex}")
    )
    bin_dir = scratch / "bin"
    python_dir = scratch / "python"
    scratch.mkdir(parents=True)
    config_dir = scratch / "gh-config"
    config_dir.mkdir()
    _write_cli_shim(bin_dir)
    _write_sitecustomize(python_dir)
    log_path = scratch / "blocked.log"
    log_path.touch()
    for name, value in (
        (_LOG_ENV, str(log_path)),
        (_PROCESS_ENV, "parent"),
        (_BIN_ENV, str(bin_dir)),
        (_PYTHON_ENV, str(python_dir)),
        (_PROJECT_ENV, str(project_root)),
        ("GH_CONFIG_DIR", str(config_dir)),
    ):
        setenv(name, value)

    current_path = os.environ.get("PATH", "")
    setenv("PATH", os.pathsep.join(filter(None, (str(bin_dir), current_path))))
    current_pythonpath = os.environ.get("PYTHONPATH", "")
    setenv(
        "PYTHONPATH",
        os.pathsep.join(
            filter(None, (str(python_dir), str(project_root), current_pythonpath))
        ),
    )
    _install_guards(setattr)

    monitor = GitHubAccessMonitor(log_path)
    try:
        yield monitor
    finally:
        try:
            monitor.assert_expected()
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

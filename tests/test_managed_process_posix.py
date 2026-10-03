"""#820: real-process verification of POSIX process-group ownership."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from orchestune.infra.execution_deadline import CleanupBudget
from orchestune.infra.managed_process import (
    ManagedProcessSpec,
    ProcessOutcome,
    run_managed,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX process groups only"
)

PY = sys.executable


def _write_scripts(tmp_path: Path, *, parent_body: str) -> tuple[Path, Path]:
    """Write a recording grandchild script and a parent script that spawns it."""
    pids = tmp_path / "pids"
    child = tmp_path / "child.py"
    child.write_text(
        "import os, time\n"
        f"open({str(pids)!r}, 'a').write(str(os.getpid()) + ' ')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import os, subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(child)!r}])\n" + parent_body,
        encoding="utf-8",
    )
    return parent, pids


def _run_script(script: Path, timeout: float):  # type: ignore[no-untyped-def]
    return run_managed(
        ManagedProcessSpec(
            args=[PY, str(script)],
            stage="ci",
            timeout_seconds=timeout,
            term_grace_seconds=0.3,
        )
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie awaiting its reaper is not running code.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    return stat.rpartition(")")[2].split()[0] not in ("Z", "X")


def _wait_gone(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _spec(code: str, timeout: float) -> ManagedProcessSpec:
    return ManagedProcessSpec(
        args=[PY, "-c", code],
        stage="ci",
        timeout_seconds=timeout,
        term_grace_seconds=0.3,
        cleanup=CleanupBudget(0.6),
    )


def _read_pids(path: Path, count: int) -> list[int]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.exists():
            pids = [int(p) for p in path.read_text().split() if p]
            if len(pids) >= count:
                return pids
        time.sleep(0.05)
    raise AssertionError(f"expected {count} pids in {path}")


def test_timeout_stops_child_and_grandchild(tmp_path: Path) -> None:
    pid_file = tmp_path / "pids"
    parent, pids = _write_scripts(
        tmp_path,
        parent_body=(
            f"open({str(pid_file)!r}, 'a').write(str(os.getpid()) + ' ')\n"
            "time.sleep(60)\n"
        ),
    )

    result = _run_script(parent, timeout=1.0)

    assert result.outcome is ProcessOutcome.TIMED_OUT
    assert result.stop_confirmed is True
    for pid in _read_pids(pids, 2):
        assert _wait_gone(pid), f"pid {pid} survived the group stop"


def test_child_ignoring_sigterm_is_killed_after_the_grace_period(
    tmp_path: Path,
) -> None:
    pids = tmp_path / "pids"
    script = tmp_path / "stubborn.py"
    script.write_text(
        "import os, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"open({str(pids)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    result = _run_script(script, timeout=1.0)

    assert result.outcome is ProcessOutcome.TIMED_OUT
    assert result.stop_confirmed is True
    assert _wait_gone(_read_pids(pids, 1)[0])


def test_descendant_holding_the_pipe_does_not_block_collection(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "pids"
    parent, pids = _write_scripts(
        tmp_path,
        parent_body=(
            f"while not os.path.exists({str(pid_file)!r}): time.sleep(0.05)\n"
            "print('parent done')\n"
        ),
    )
    started = time.monotonic()

    result = _run_script(parent, timeout=20.0)

    # The parent exited 0, but its pipe-holding child was still ours: stop it, and
    # never report success while a managed descendant survives.
    assert result.outcome is ProcessOutcome.SUCCESS
    assert result.stop_confirmed is True
    assert "descendant" in result.detail
    assert "parent done" in result.stdout_tail
    assert time.monotonic() - started < 15
    assert _wait_gone(_read_pids(pids, 1)[0])


def test_unconfirmed_group_is_never_reported_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orchestune.infra import managed_process_posix

    monkeypatch.setattr(
        managed_process_posix.PosixProcessGroup, "alive", lambda self: True
    )

    result = run_managed(_spec("print('done')", timeout=5.0))

    assert result.outcome is ProcessOutcome.STOP_UNCONFIRMED
    assert result.stop_confirmed is False


def test_a_timed_out_leader_is_reaped_so_liveness_works_without_proc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without /proc (macOS) a zombie leader keeps ``killpg(pgid, 0)`` succeeding.

    The runner must reap the leader while it waits for the group to disappear,
    otherwise every real timeout burns the cleanup budget and ends as STOP_UNCONFIRMED.
    """
    from orchestune.infra import managed_process_posix

    monkeypatch.setattr(managed_process_posix, "_linux_live_members", lambda _p: None)

    result = run_managed(_spec("import time; time.sleep(60)", timeout=0.5))

    assert result.outcome is ProcessOutcome.TIMED_OUT
    assert result.stop_confirmed is True

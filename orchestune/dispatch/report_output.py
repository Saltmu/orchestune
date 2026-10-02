"""Reserve a per-execution result without changing shared atomic-write behavior."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from orchestune.dispatch.config import DispatcherConfig
from orchestune.infra.json_state import write_json_atomic
from orchestune.infra.process_utils import file_lock


def _business_paths(config: DispatcherConfig) -> set[Path]:
    state = Path(config.run_state_path)
    paths = {
        state,
        Path(config.events_log_path),
        Path(config.not_needed_review_state_path),
        state.with_suffix(".status-intents.json"),
        state.with_suffix(".lock"),
    }
    return {p.resolve() for p in paths | {p.with_suffix(".lock") for p in paths}}


def _check_target(path: Path, config: DispatcherConfig) -> None:
    if path.is_symlink():
        raise ValueError(f"report target is a symlink: {path}")
    normalized = path.resolve()
    lock = normalized.with_name(normalized.name + ".report.lock").resolve()
    if normalized.with_name(normalized.name + ".report.lock").is_symlink():
        raise ValueError(f"report lock is a symlink: {path}")
    if normalized in _business_paths(config) or lock in _business_paths(config):
        raise ValueError(f"report target overlaps business state or lock: {path}")
    if path.is_dir():
        raise ValueError(f"report target is a directory: {path}")


def _auto_target(config: DispatcherConfig, run_id: str) -> Path:
    root = Path(config.report_dir) / f"parent-{config.parent_issue_number}"
    root.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        identity = run_id if attempt == 0 else f"{run_id}-{uuid4()}"
        directory = root / identity
        try:
            directory.mkdir()
        except FileExistsError:
            continue
        return directory / "result.json"
    raise RuntimeError("could not allocate a unique report directory")


def new_run_id() -> str:
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4()}"


@dataclass(frozen=True)
class ReportOutput:
    path: Path

    def save(self, data: dict) -> None:
        if self.path.exists() or self.path.is_symlink():
            raise ValueError(f"report target already exists: {self.path}")
        write_json_atomic(self.path, data)


@contextmanager
def reserve_report(config: DispatcherConfig, run_id: str) -> Iterator[ReportOutput]:
    path = (
        Path(config.report_path)
        if config.report_path is not None
        else _auto_target(config, run_id)
    )
    _check_target(path, config)
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path.with_name(path.name + ".report.lock"), timeout=0):
        if path.exists() or path.is_symlink():
            raise ValueError(f"report target already exists: {path}")
        fd, probe = tempfile.mkstemp(dir=path.parent, prefix=".report-probe-")
        os.close(fd)
        Path(probe).unlink()
        yield ReportOutput(path)

"""Plan and append repository-local gitignore rules for Orchestune paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast

from orchestune.config_contracts import ConfigError
from orchestune.dispatch.config.loader import read_resolved_runtime_paths
from orchestune.dispatch.config.model import DispatcherConfig
from orchestune.dispatch.reporting.promotion import promotion_report_journal_path
from orchestune.dispatch.status_repair import status_intent_journal_path
from orchestune.infra.git_cli import query_path_ignore
from orchestune.infra.process_utils import FileLock

FileKind = Literal["absent", "regular", "symlink", "other", "unreadable"]
Action = Literal["append", "present", "manual", "held"]
_SPECIAL = set("\\*?[] #")
_BASE = (
    ("orchestune.toml", False),
    (".orchestune", True),
    (".orchestune-installer", True),
)


@dataclass(frozen=True)
class IgnoreDecision:
    path: str
    action: Action
    reason: str
    rule: str | None = None


@dataclass(frozen=True)
class IgnorePlan:
    decisions: tuple[IgnoreDecision, ...]
    original: bytes | None
    file_kind: FileKind


class IgnoreApplyError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def base_candidates(root: Path) -> tuple[tuple[Path, bool], ...]:
    return tuple((root / name, directory) for name, directory in _BASE)


def runtime_candidates(
    root: Path,
) -> tuple[tuple[Path, bool], ...] | None:
    """Effective runtime files and directories, or ``None`` if config is unreadable."""
    try:
        paths = read_resolved_runtime_paths(root)
    except (ConfigError, OSError):
        return None
    state = Path(paths["run_state_path"])
    holder = cast(DispatcherConfig, SimpleNamespace(run_state_path=state))
    files = (
        state,
        state.with_suffix(".lock"),
        status_intent_journal_path(holder),
        promotion_report_journal_path(holder),
        Path(paths["events_log_path"]),
        Path(paths["not_needed_review_state_path"]),
    )
    directories = (
        Path(paths["log_dir"]),
        Path(paths["worktree_root"]),
        Path(paths["report_dir"]),
    )
    return tuple((path, False) for path in files) + tuple(
        (path, True) for path in directories
    )


def build_ignore_plan(
    root: Path, candidates: tuple[tuple[Path, bool], ...]
) -> IgnorePlan:
    kind, original = _gitignore_state(root / ".gitignore")
    decisions = tuple(
        _decide(root, path, directory, kind) for path, directory in candidates
    )
    return IgnorePlan(decisions, original, kind)


def plan_diff(plan: IgnorePlan) -> str:
    rules = [item.rule for item in plan.decisions if item.action == "append"]
    if not rules:
        return "(no .gitignore changes)"
    return "\n".join(f"+{rule}" for rule in rules if rule is not None)


def apply_ignore_plan(root: Path, plan: IgnorePlan) -> None:
    """Append approved rules if the gitignore snapshot is unchanged."""
    rules = [item.rule for item in plan.decisions if item.action == "append"]
    if not rules or plan.file_kind in {"symlink", "other", "unreadable"}:
        if rules and plan.file_kind != "absent":
            raise IgnoreApplyError(1, "gitignore cannot be updated automatically")
        return
    lock = FileLock(root / ".git" / "orchestune-init-gitignore.lock", timeout=5)
    with lock:
        _apply_locked(root / ".gitignore", plan, [rule for rule in rules if rule])


def _decide(
    root: Path, candidate: Path, directory: bool, kind: FileKind
) -> IgnoreDecision:
    shown = _display(root, candidate)
    if kind in {"symlink", "other", "unreadable"}:
        return IgnoreDecision(shown, "held", "gitignore is not a regular file")
    relative = _contained(root, candidate)
    if relative is None:
        return IgnoreDecision(shown, "manual", "path leaves the repository")
    if relative == Path("."):
        return IgnoreDecision(shown, "manual", "path covers the repository")
    probe = relative.as_posix()
    if directory:
        probe = f"{probe}/"
    query = query_path_ignore(root, probe)
    return _from_query(shown, relative, directory, query)


def _from_query(
    shown: str,
    relative: Path,
    directory: bool,
    query: object,
) -> IgnoreDecision:
    ignored = getattr(query, "ignored", None)
    tracked = getattr(query, "tracked", None)
    pattern = getattr(query, "pattern", None)
    if ignored is None or tracked is None:
        return IgnoreDecision(shown, "held", "git could not classify the path")
    if isinstance(pattern, str) and pattern.startswith("!"):
        return IgnoreDecision(shown, "manual", "a negation rule matches this path")
    if tracked:
        return IgnoreDecision(shown, "manual", "the path or a child is tracked")
    if ignored:
        return IgnoreDecision(shown, "present", "an existing rule already ignores it")
    return IgnoreDecision(
        shown, "append", "missing from .gitignore", _rule(relative, directory)
    )


def _contained(root: Path, candidate: Path) -> Path | None:
    try:
        resolved = candidate.resolve()
        return resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None


def _rule(relative: Path, directory: bool) -> str:
    escaped = "".join(
        f"\\{char}" if char in _SPECIAL else char for char in relative.as_posix()
    )
    rule = f"/{escaped}"
    if directory and not rule.endswith("/"):
        rule += "/"
    return rule


def _display(root: Path, candidate: Path) -> str:
    try:
        return candidate.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return candidate.as_posix()


def _gitignore_state(path: Path) -> tuple[FileKind, bytes | None]:
    if path.is_symlink():
        return "symlink", None
    if not path.exists():
        return "absent", None
    if not path.is_file():
        return "other", None
    try:
        data = path.read_bytes()
    except OSError:
        return "unreadable", None
    if b"\0" in data:
        return "other", None
    return "regular", data


def _apply_locked(path: Path, plan: IgnorePlan, rules: list[str]) -> None:
    if not _snapshot_matches(path, plan):
        raise IgnoreApplyError(3, ".gitignore changed after the diff was shown")
    payload = _render(plan.original or b"", rules)
    if plan.file_kind == "absent":
        _create_exclusive(path, payload)
        return
    _replace(path, payload)


def _snapshot_matches(path: Path, plan: IgnorePlan) -> bool:
    if plan.file_kind == "absent":
        return not path.exists()
    if plan.file_kind != "regular" or plan.original is None:
        return False
    if path.is_symlink() or not path.is_file():
        return False
    try:
        return path.read_bytes() == plan.original
    except OSError:
        return False


def _render(existing: bytes, rules: list[str]) -> bytes:
    newline = _newline(existing)
    addition = newline.join(rule.encode() for rule in rules) + newline
    if not existing:
        return addition
    body = existing if existing.endswith((b"\n", b"\r\n")) else existing + newline
    return body + addition


def _newline(existing: bytes) -> bytes:
    if b"\r\n" in existing and b"\n" not in existing.replace(b"\r\n", b""):
        return b"\r\n"
    return b"\n"


def _create_exclusive(path: Path, payload: bytes) -> None:
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError as exc:
        raise IgnoreApplyError(3, ".gitignore appeared after the diff") from exc
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


def _replace(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.orchestune-tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)

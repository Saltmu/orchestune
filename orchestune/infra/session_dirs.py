from __future__ import annotations

import datetime
import re
import uuid
from pathlib import Path

_VALID_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]+$")


class SessionDirError(Exception):
    """Raised when session directory creation fails."""


def find_project_root(start_dir: Path | None = None) -> Path:
    current = (start_dir or Path.cwd()).resolve()
    for parent in [current] + list(current.parents):
        if (parent / ".git").is_dir() or (parent / ".git").is_file():
            return parent
    return current


def _is_ignored_in_gitignore(project_dir: Path) -> bool:
    gitignore_path = project_dir / ".gitignore"
    if not gitignore_path.is_file():
        return False

    content = gitignore_path.read_text(encoding="utf-8")
    for line in content.splitlines():
        stripped = line.strip()
        if stripped in (
            ".orchestune/tmp",
            ".orchestune/tmp/",
            "/.orchestune/tmp",
            "/.orchestune/tmp/",
        ):
            return True
    return False


def create_session_dir(
    artifact: str,
    issue_or_task: str,
    project_dir: Path | None = None,
) -> Path:
    if not _VALID_NAME_PATTERN.match(artifact) or not _VALID_NAME_PATTERN.match(
        issue_or_task
    ):
        raise SessionDirError(
            f"Invalid artifact or issue/task name: '{artifact}', '{issue_or_task}'. "
            "Must contain only alphanumeric characters, underscores, or hyphens."
        )

    base_project = (project_dir or find_project_root()).resolve()

    if not _is_ignored_in_gitignore(base_project):
        raise SessionDirError(
            f"Directory .orchestune/tmp/ must be ignored in .gitignore at {base_project}. "
            "Please add '.orchestune/tmp/' to .gitignore."
        )

    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    random_suffix = uuid.uuid4().hex[:8]
    dir_name = f"{artifact}-{issue_or_task}-{timestamp}-{random_suffix}"

    session_dir = base_project / ".orchestune" / "tmp" / dir_name
    session_dir.mkdir(parents=True, exist_ok=False)
    return session_dir.resolve()

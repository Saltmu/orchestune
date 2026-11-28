#!/usr/bin/env python3
"""Muse bridge supervisor for Orchestune local-cmd dispatch.

Orchestune's built-in dispatch targets launch a local CLI process per task.
Agents without a CLI entry point (such as Muse, the user's personal agent)
cannot be launched that way. This supervisor is the missing middle piece:
configured as a custom ``local-cmd`` for the ``local`` dispatch target, it

1. writes a request file describing the claimed task (issue, branch,
   worktree, launch-context path) into a shared inbox directory, and
2. stays alive, polling for a completion marker written by the external
   agent, so the dispatcher's process-liveness tracking does not treat the
   task as an early death.

Task completion itself is NOT signalled by this process exiting. The
external agent does the work in the given worktree, opens the pull request
and runs ``orchestune complete``; only after that does it write the done
marker that lets this supervisor exit 0. A ``blocked`` marker exits 1 and a
timeout (disabled by default) exits 2.

Inbox layout (default ``~/.orchestune-muse/inbox``, override with
``--inbox-dir`` or the ``ORCHESTUNE_MUSE_INBOX_DIR`` environment variable):

- ``request-<issue>.json``   written by this supervisor (atomically)
- ``heartbeat-<issue>.txt``  refreshed on every poll while waiting
- ``done-<issue>.json``      written by the external agent:
  ``{"status": "done"}`` or ``{"status": "blocked", "reason": "..."}``
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
ENV_INBOX_DIR = "ORCHESTUNE_MUSE_INBOX_DIR"
DEFAULT_POLL_SECONDS = 5.0


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_inbox_dir(explicit: str | None) -> Path:
    """Resolve the inbox directory: flag, then env, then the user default."""
    if explicit:
        return Path(explicit).expanduser()
    from_env = os.environ.get(ENV_INBOX_DIR)
    if from_env:
        return Path(from_env).expanduser()
    return Path.home() / ".orchestune-muse" / "inbox"


def request_path(inbox_dir: Path, issue: int) -> Path:
    return inbox_dir / f"request-{issue}.json"


def marker_path(inbox_dir: Path, issue: int) -> Path:
    return inbox_dir / f"done-{issue}.json"


def heartbeat_path(inbox_dir: Path, issue: int) -> Path:
    return inbox_dir / f"heartbeat-{issue}.txt"


def build_request(args: argparse.Namespace, inbox_dir: Path) -> dict[str, Any]:
    worktree = Path(args.worktree).expanduser()
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pending",
        "issue_number": args.issue,
        "branch_name": args.branch,
        "worktree_path": str(worktree),
        "repo_root": str(worktree),
        "context_json": args.context,
        "inbox_dir": str(inbox_dir),
        "pid": os.getpid(),
        "created_at": _utc_now(),
    }


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_request(inbox_dir: Path, request: dict[str, Any]) -> Path:
    path = request_path(inbox_dir, int(request["issue_number"]))
    _write_atomic(path, json.dumps(request, indent=2, ensure_ascii=False) + "\n")
    return path


def write_heartbeat(inbox_dir: Path, issue: int) -> Path:
    path = heartbeat_path(inbox_dir, issue)
    _write_atomic(path, f"{_utc_now()} pid={os.getpid()}\n")
    return path


def read_marker(inbox_dir: Path, issue: int) -> dict[str, Any] | None:
    path = marker_path(inbox_dir, issue)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "blocked", "reason": f"unreadable marker file: {path}"}
    if not isinstance(data, dict) or "status" not in data:
        return {"status": "blocked", "reason": f"marker file lacks a status: {path}"}
    return data


def wait_for_marker(
    inbox_dir: Path,
    issue: int,
    *,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    timeout_seconds: float = 0,
) -> dict[str, Any] | None:
    """Poll for the completion marker; return it, or None on timeout."""
    started = time.monotonic()
    while True:
        write_heartbeat(inbox_dir, issue)
        marker = read_marker(inbox_dir, issue)
        if marker is not None:
            return marker
        if timeout_seconds > 0 and time.monotonic() - started >= timeout_seconds:
            return None
        time.sleep(poll_seconds)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bridge an Orchestune local dispatch to an external agent (Muse)."
    )
    parser.add_argument("--issue", type=int, required=True, help="GitHub issue number")
    parser.add_argument("--branch", default="", help="Task branch name")
    parser.add_argument(
        "--worktree",
        default=".",
        help="Task worktree path (default: current directory)",
    )
    parser.add_argument("--context", default=None, help="Launch context_json path")
    parser.add_argument("--inbox-dir", default=None, help="Shared inbox directory")
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=DEFAULT_POLL_SECONDS,
        help="Marker poll interval (default: 5)",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=0,
        help="Give up after this many seconds; 0 waits indefinitely (default: 0)",
    )
    args = parser.parse_args(argv)
    if args.issue <= 0:
        parser.error("--issue must be a positive integer")
    if args.poll_seconds <= 0:
        parser.error("--poll-seconds must be positive")
    if args.timeout_seconds < 0:
        parser.error("--timeout-seconds must not be negative")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    inbox_dir = resolve_inbox_dir(args.inbox_dir)
    inbox_dir.mkdir(parents=True, exist_ok=True)
    request = build_request(args, inbox_dir)
    path = write_request(inbox_dir, request)
    print(f"muse-bridge: request written to {path}", flush=True)
    print(
        f"muse-bridge: waiting for marker {marker_path(inbox_dir, args.issue)}",
        flush=True,
    )
    try:
        marker = wait_for_marker(
            inbox_dir,
            args.issue,
            poll_seconds=args.poll_seconds,
            timeout_seconds=args.timeout_seconds,
        )
    except KeyboardInterrupt:
        print("muse-bridge: interrupted", file=sys.stderr)
        return 130
    if marker is None:
        print(
            f"muse-bridge: timed out after {args.timeout_seconds} seconds",
            file=sys.stderr,
        )
        return 2
    status = marker.get("status")
    if status == "done":
        print(f"muse-bridge: issue #{args.issue} marked done", flush=True)
        return 0
    reason = marker.get("reason") or f"unexpected marker status: {status!r}"
    print(f"muse-bridge: issue #{args.issue} blocked: {reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

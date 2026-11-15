"""Rebuild and export the local context index."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from orchestune.context.fetch import FetchRequest, fetch_context
from orchestune.context.storage import (
    IndexedRecord,
    IndexView,
    open_context_index,
    rebuild_context_index,
)
from orchestune.forge import GitHubForge

_DESCRIPTION = "rebuild and export the local context index."
_DEFAULT_DATABASE = Path(".orchestune/context-index.sqlite3")
_OPERATION_ERRORS = (OSError, ValueError, RuntimeError, sqlite3.Error)


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``rebuild`` or ``export``. Usage errors return 2."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "export":
        return _export(args.db)
    return _rebuild(args)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchestune context", description=_DESCRIPTION
    )
    commands = parser.add_subparsers(dest="command", required=True)
    rebuild = commands.add_parser("rebuild", help="rebuild", description=_DESCRIPTION)
    export = commands.add_parser("export", help="export", description=_DESCRIPTION)
    _add_database(rebuild)
    _add_database(export)
    rebuild.add_argument("--repository", required=True)
    rebuild.add_argument("--parent-issue", type=int)
    rebuild.add_argument("--issue", action="append", type=int)
    rebuild.add_argument("--since")
    rebuild.add_argument("--max-pages", type=_pages)
    rebuild.add_argument("--indexed-at")
    return parser


def _add_database(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", type=Path, default=_DEFAULT_DATABASE)


def _pages(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("max-pages must be at least 1")
    return number


def _rebuild(args: argparse.Namespace) -> int:
    if args.parent_issue is None and not args.issue and args.since is None:
        print("specify --parent-issue, --issue, or --since", file=sys.stderr)
        return 2
    try:
        snapshot = fetch_context(GitHubForge(), _request(args))
        result = rebuild_context_index(
            args.db, snapshot, indexed_at=_indexed_at(args.indexed_at)
        )
    except _OPERATION_ERRORS as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _print(
        {
            "published": result.published,
            "preserved_existing": result.preserved_existing,
            "coverage_status": result.coverage_status,
            "reason": snapshot.reason,
            "manifest": snapshot.manifest,
        }
    )
    return 0


def _indexed_at(value: str | None) -> str:
    if value:
        return value
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _request(args: argparse.Namespace) -> FetchRequest:
    return FetchRequest(
        repository=args.repository,
        parent_issue=args.parent_issue,
        issue_numbers=tuple(args.issue or ()),
        since=args.since,
        max_pages=args.max_pages,
    )


def _export(database: Path) -> int:
    try:
        view = open_context_index(database)
    except _OPERATION_ERRORS as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _print(_export_payload(view))
    return 0


def _export_payload(view: IndexView) -> dict[str, object]:
    return {
        "status": view.status,
        "reason": view.reason,
        "coverage_status": view.coverage_status,
        "schema_version": view.schema_version,
        "tokenizer_version": view.tokenizer_version,
        "bm25_params_version": view.bm25_params_version,
        "manifest": view.manifest,
        "artifacts": [_artifact_payload(record) for record in view.artifacts],
    }


def _artifact_payload(record: IndexedRecord) -> dict[str, object]:
    artifact = record.artifact
    return {
        "title": record.title,
        "description": record.description,
        "completion_id": artifact.completion_id,
        "issue_number": artifact.issue_number,
        "source_comment_id": artifact.source_comment_id,
    }


def _print(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))

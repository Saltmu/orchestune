"""Publish and read one local context-index snapshot.

Readers open only a replaced file. A failed build leaves the previous file
in place. The database is created by a rebuild, not by opening it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from orchestune.context.contracts import (
    ArtifactStaleStatus,
    EvidenceRefs,
    SharedArtifactKey,
    SharedArtifactV1,
    VerificationSummary,
)
from orchestune.context.search import BM25_PARAMS_VERSION, TOKENIZER_VERSION
from orchestune.infra.process_utils import FileLock

INDEX_SCHEMA_VERSION = "context-index-v1"
_ARTIFACT_COLUMNS = (
    "repository",
    "issue_number",
    "source_comment_id",
    "source_revision_digest",
    "artifact_schema",
    "parent_issue",
    "subtask_id",
    "task_kind",
    "depends_on_json",
    "footprint_json",
    "symbols_json",
    "dependency_issue_numbers_json",
    "result",
    "attempt",
    "review_rounds",
    "review_verdict",
    "ci",
    "claim_id",
    "recorded_at",
    "completion_id",
    "base_sha",
    "target_commit",
    "summary",
    "assumptions_json",
    "supersedes_json",
    "superseded_by_json",
    "stale_status",
    "source_url",
    "plan_revision_digest",
    "indexed_at",
    "title",
    "description",
    "evidence_json",
)
_SCHEMA_SQL = """
CREATE TABLE index_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version TEXT NOT NULL,
    tokenizer_version TEXT NOT NULL,
    bm25_params_version TEXT NOT NULL,
    coverage_status TEXT NOT NULL,
    collection_manifest_json TEXT NOT NULL,
    indexed_at TEXT NOT NULL
);
CREATE TABLE artifacts (
    repository TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    source_comment_id TEXT NOT NULL,
    source_revision_digest TEXT NOT NULL,
    artifact_schema TEXT NOT NULL,
    parent_issue INTEGER,
    subtask_id TEXT,
    task_kind TEXT NOT NULL,
    depends_on_json TEXT,
    footprint_json TEXT,
    symbols_json TEXT,
    dependency_issue_numbers_json TEXT,
    result TEXT NOT NULL,
    attempt INTEGER,
    review_rounds INTEGER,
    review_verdict TEXT,
    ci TEXT,
    claim_id TEXT,
    recorded_at TEXT,
    completion_id TEXT,
    base_sha TEXT,
    target_commit TEXT,
    summary TEXT,
    assumptions_json TEXT,
    supersedes_json TEXT,
    superseded_by_json TEXT NOT NULL,
    stale_status TEXT NOT NULL,
    source_url TEXT,
    plan_revision_digest TEXT,
    indexed_at TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY (
        repository, issue_number, source_comment_id, source_revision_digest
    )
);
"""


@dataclass(frozen=True, slots=True)
class IndexedRecord:
    """One stored artifact plus the issue title and body beside it."""

    artifact: SharedArtifactV1
    title: str
    description: str

    def __getattr__(self, name: str) -> Any:
        return getattr(self.artifact, name)


@dataclass(frozen=True, slots=True)
class CollectionSnapshot:
    """Records from one fetch and whether they may replace the index."""

    publish: bool
    coverage_status: str
    reason: str | None
    records: tuple[IndexedRecord, ...]
    manifest: dict[str, Any]


@dataclass(frozen=True, slots=True)
class IndexView:
    """What a reader can see in one finished snapshot."""

    status: str
    reason: str | None
    artifacts: tuple[IndexedRecord, ...]
    schema_version: str | None
    coverage_status: str | None
    manifest: dict[str, Any] | None
    tokenizer_version: str | None = None
    bm25_params_version: str | None = None


@dataclass(frozen=True, slots=True)
class RebuildResult:
    """Whether the rebuild replaced artifact rows."""

    published: bool
    preserved_existing: bool
    coverage_status: str


def index_lock_path(db_path: Path) -> Path:
    """Return the writer lock beside the snapshot, not the snapshot itself."""
    return Path(f"{db_path}.lock")


def integrity_ok(connection: sqlite3.Connection) -> bool:
    """Return whether SQLite reports this connection's file as intact."""
    return _pragma_ok(connection)


def _pragma_ok(connection: sqlite3.Connection) -> bool:
    row = connection.execute("PRAGMA integrity_check").fetchone()
    return row is not None and row[0] == "ok"


def open_context_index(path: str | Path) -> IndexView:
    """Open one finished snapshot. A missing file is left missing."""
    database = Path(path)
    if not database.is_file():
        return _unavailable("missing", None)
    try:
        view, quarantine = _read_snapshot(database)
    except sqlite3.DatabaseError:
        _quarantine(database)
        return _unavailable("corrupt", None)
    if quarantine:
        _quarantine(database)
        return _unavailable("corrupt", None)
    return view


def rebuild_context_index(
    path: str | Path,
    snapshot: CollectionSnapshot,
    *,
    indexed_at: str,
    before_replace: Callable[[], None] | None = None,
) -> RebuildResult:
    """Replace the snapshot, or record coverage when publishing is refused.

    ``before_replace`` runs after the new file passes its integrity check and
    before that file becomes the published path.
    """
    database = Path(path)
    with FileLock(index_lock_path(database), timeout=0):
        if snapshot.publish:
            return _publish(database, snapshot, indexed_at, before_replace)
        return _keep_previous(database, snapshot)


def _read_snapshot(database: Path) -> tuple[IndexView, bool]:
    connection = _connect_readonly(database)
    try:
        if not _pragma_ok(connection) or not _tables_present(connection):
            return _unavailable("corrupt", None), True
        meta = _meta_row(connection)
        if meta is None:
            return _unavailable("corrupt", None), True
        if not _versions_match(meta):
            return _mismatch(meta), False
        return _ready_view(connection, meta), False
    finally:
        connection.close()


def _connect_readonly(database: Path) -> sqlite3.Connection:
    uri = f"{database.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _unavailable(reason: str, schema_version: str | None) -> IndexView:
    return IndexView("unavailable", reason, (), schema_version, None, None)


def _mismatch(meta: sqlite3.Row) -> IndexView:
    return _unavailable("schema-mismatch", str(meta["schema_version"]))


def _ready_view(connection: sqlite3.Connection, meta: sqlite3.Row) -> IndexView:
    rows = connection.execute(
        """
        SELECT * FROM artifacts
        ORDER BY repository, issue_number, source_comment_id, source_revision_digest
        """
    ).fetchall()
    return IndexView(
        status="ready",
        reason=None,
        artifacts=tuple(_record_from_row(row) for row in rows),
        schema_version=str(meta["schema_version"]),
        coverage_status=str(meta["coverage_status"]),
        manifest=_object(meta["collection_manifest_json"]),
        tokenizer_version=str(meta["tokenizer_version"]),
        bm25_params_version=str(meta["bm25_params_version"]),
    )


def _tables_present(connection: sqlite3.Connection) -> bool:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {"index_meta", "artifacts"} <= {row[0] for row in rows}


def _meta_row(connection: sqlite3.Connection) -> sqlite3.Row | None:
    row = connection.execute(
        """
        SELECT schema_version, tokenizer_version, bm25_params_version,
               coverage_status, collection_manifest_json
        FROM index_meta WHERE id = 1
        """
    ).fetchone()
    if not isinstance(row, sqlite3.Row):
        return None
    return row


def _versions_match(meta: sqlite3.Row) -> bool:
    return bool(
        meta["schema_version"] == INDEX_SCHEMA_VERSION
        and meta["tokenizer_version"] == TOKENIZER_VERSION
        and meta["bm25_params_version"] == BM25_PARAMS_VERSION
    )


def _publish(
    database: Path,
    snapshot: CollectionSnapshot,
    indexed_at: str,
    before_replace: Callable[[], None] | None,
) -> RebuildResult:
    temporary = Path(f"{database}.building")
    temporary.unlink(missing_ok=True)
    try:
        _write_database(temporary, snapshot, indexed_at)
        if not _built_ok(temporary):
            return RebuildResult(False, database.is_file(), snapshot.coverage_status)
        if before_replace is not None:
            before_replace()
        database.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, database)
    finally:
        temporary.unlink(missing_ok=True)
    return RebuildResult(True, False, snapshot.coverage_status)


def _built_ok(temporary: Path) -> bool:
    connection = sqlite3.connect(temporary)
    try:
        return integrity_ok(connection)
    finally:
        connection.close()


def _write_database(
    temporary: Path, snapshot: CollectionSnapshot, indexed_at: str
) -> None:
    temporary.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(temporary)
    try:
        _use_delete_journal(connection)
        connection.executescript(_SCHEMA_SQL)
        _use_delete_journal(connection)
        _insert_meta(connection, snapshot, indexed_at)
        _insert_artifacts(connection, snapshot.records, indexed_at)
        connection.commit()
    finally:
        connection.close()


def _use_delete_journal(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=DELETE")


def _insert_meta(
    connection: sqlite3.Connection, snapshot: CollectionSnapshot, indexed_at: str
) -> None:
    connection.execute(
        """
        INSERT INTO index_meta (
            id, schema_version, tokenizer_version, bm25_params_version,
            coverage_status, collection_manifest_json, indexed_at
        ) VALUES (1, ?, ?, ?, ?, ?, ?)
        """,
        (
            INDEX_SCHEMA_VERSION,
            TOKENIZER_VERSION,
            BM25_PARAMS_VERSION,
            snapshot.coverage_status,
            _dump(snapshot.manifest),
            indexed_at,
        ),
    )


def _insert_artifacts(
    connection: sqlite3.Connection,
    records: tuple[IndexedRecord, ...],
    indexed_at: str,
) -> None:
    columns = ", ".join(_ARTIFACT_COLUMNS)
    placeholders = ", ".join("?" for _ in _ARTIFACT_COLUMNS)
    statement = f"INSERT INTO artifacts ({columns}) VALUES ({placeholders})"
    for record in sorted(records, key=_sort_key):
        row = _artifact_row(record, indexed_at)
        connection.execute(
            statement, tuple(row[column] for column in _ARTIFACT_COLUMNS)
        )


def _keep_previous(database: Path, snapshot: CollectionSnapshot) -> RebuildResult:
    if not _writable_current(database):
        return RebuildResult(False, False, snapshot.coverage_status)
    _update_coverage(database, snapshot)
    return RebuildResult(False, True, snapshot.coverage_status)


def _writable_current(database: Path) -> bool:
    if not database.is_file():
        return False
    try:
        connection = sqlite3.connect(database)
    except sqlite3.DatabaseError:
        return False
    connection.row_factory = sqlite3.Row
    try:
        if not integrity_ok(connection) or not _tables_present(connection):
            return False
        meta = _meta_row(connection)
        return meta is not None and _versions_match(meta)
    finally:
        connection.close()


def _update_coverage(database: Path, snapshot: CollectionSnapshot) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """
            UPDATE index_meta
            SET coverage_status = ?, collection_manifest_json = ?
            WHERE id = 1
            """,
            (snapshot.coverage_status, _dump(snapshot.manifest)),
        )
        connection.commit()
    finally:
        connection.close()


def _quarantine(database: Path) -> None:
    target = Path(f"{database}.corrupt.{time.time_ns()}")
    try:
        os.replace(database, target)
    except FileNotFoundError:
        return


def _sort_key(record: IndexedRecord) -> tuple[str, int, str, str]:
    artifact = record.artifact
    return (
        artifact.repository,
        artifact.issue_number,
        artifact.source_comment_id,
        artifact.source_revision_digest,
    )


def _artifact_row(record: IndexedRecord, indexed_at: str) -> dict[str, Any]:
    artifact = replace(record.artifact, indexed_at=indexed_at)
    verification = artifact.verification_summary
    return {
        "repository": artifact.repository,
        "issue_number": artifact.issue_number,
        "source_comment_id": artifact.source_comment_id,
        "source_revision_digest": artifact.source_revision_digest,
        "artifact_schema": artifact.schema_version,
        "parent_issue": artifact.parent_issue,
        "subtask_id": artifact.subtask_id,
        "task_kind": artifact.task_kind,
        "depends_on_json": _dump_optional(artifact.depends_on),
        "footprint_json": _dump_optional(artifact.footprint),
        "symbols_json": _dump_optional(artifact.symbols),
        "dependency_issue_numbers_json": _dump_optional(
            artifact.dependency_issue_numbers
        ),
        "result": artifact.result,
        "attempt": artifact.attempt,
        "review_rounds": verification.review_rounds,
        "review_verdict": verification.review_verdict,
        "ci": verification.ci,
        "claim_id": artifact.claim_id,
        "recorded_at": artifact.recorded_at,
        "completion_id": artifact.completion_id,
        "base_sha": artifact.base_sha,
        "target_commit": artifact.target_commit,
        "summary": artifact.summary,
        "assumptions_json": _dump_optional(artifact.assumptions),
        "supersedes_json": _dump_key(artifact.supersedes),
        "superseded_by_json": _dump(
            [_key_payload(key) for key in artifact.superseded_by]
        ),
        "stale_status": artifact.stale_status.value,
        "source_url": artifact.source_url,
        "plan_revision_digest": artifact.plan_revision_digest,
        "indexed_at": artifact.indexed_at,
        "title": record.title,
        "description": record.description,
        "evidence_json": _dump(_evidence_payload(artifact.evidence_refs)),
    }


def _record_from_row(row: sqlite3.Row) -> IndexedRecord:
    return IndexedRecord(
        _artifact_from_row(row), str(row["title"]), str(row["description"])
    )


def _artifact_from_row(row: sqlite3.Row) -> SharedArtifactV1:
    return SharedArtifactV1(
        schema_version=str(row["artifact_schema"]),
        repository=str(row["repository"]),
        issue_number=int(row["issue_number"]),
        source_comment_id=str(row["source_comment_id"]),
        source_revision_digest=str(row["source_revision_digest"]),
        parent_issue=row["parent_issue"],
        subtask_id=row["subtask_id"],
        claim_id=row["claim_id"],
        attempt=row["attempt"],
        completion_id=row["completion_id"],
        base_sha=row["base_sha"],
        target_commit=row["target_commit"],
        task_kind=str(row["task_kind"]),
        result=str(row["result"]),
        summary=row["summary"],
        assumptions=_load_tuple(row["assumptions_json"]),
        evidence_refs=_evidence_from(str(row["evidence_json"])),
        verification_summary=VerificationSummary(
            ci=row["ci"],
            review_verdict=row["review_verdict"],
            review_rounds=row["review_rounds"],
        ),
        supersedes=_key_from(row["supersedes_json"]),
        superseded_by=_keys_from(str(row["superseded_by_json"])),
        stale_status=ArtifactStaleStatus(str(row["stale_status"])),
        recorded_at=row["recorded_at"],
        indexed_at=row["indexed_at"],
        source_url=row["source_url"],
        plan_revision_digest=row["plan_revision_digest"],
        depends_on=_load_tuple(row["depends_on_json"]),
        footprint=_load_tuple(row["footprint_json"]),
        symbols=_load_tuple(row["symbols_json"]),
        dependency_issue_numbers=_load_tuple(row["dependency_issue_numbers_json"]),
    )


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _dump_optional(value: Sequence[Any] | None) -> str | None:
    if value is None:
        return None
    return _dump(list(value))


def _dump_key(key: SharedArtifactKey | None) -> str | None:
    if key is None:
        return None
    return _dump(_key_payload(key))


def _load_tuple(raw: str | None) -> tuple[Any, ...] | None:
    if raw is None:
        return None
    loaded = json.loads(raw)
    if not isinstance(loaded, list):
        raise sqlite3.DatabaseError("index column is not a JSON list")
    return tuple(loaded)


def _object(raw: str) -> dict[str, Any]:
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        raise sqlite3.DatabaseError("collection manifest is not an object")
    return loaded


def _key_payload(key: SharedArtifactKey) -> dict[str, object]:
    return {
        "repository": key.repository,
        "issue_number": key.issue_number,
        "source_comment_id": key.source_comment_id,
        "source_revision_digest": key.source_revision_digest,
    }


def _key_from(raw: str | None) -> SharedArtifactKey | None:
    if raw is None:
        return None
    return _key_from_payload(json.loads(raw))


def _keys_from(raw: str) -> tuple[SharedArtifactKey, ...]:
    loaded = json.loads(raw)
    if not isinstance(loaded, list):
        raise sqlite3.DatabaseError("superseded_by is not a JSON list")
    return tuple(_key_from_payload(item) for item in loaded)


def _key_from_payload(payload: object) -> SharedArtifactKey:
    if not isinstance(payload, Mapping):
        raise sqlite3.DatabaseError("artifact key is not an object")
    repository = payload.get("repository")
    number = payload.get("issue_number")
    comment = payload.get("source_comment_id")
    digest = payload.get("source_revision_digest")
    if (
        not isinstance(repository, str)
        or isinstance(number, bool)
        or not isinstance(number, int)
        or not isinstance(comment, str)
        or not isinstance(digest, str)
    ):
        raise sqlite3.DatabaseError("artifact key is unusable")
    return SharedArtifactKey(repository, number, comment, digest)


def _evidence_payload(evidence: EvidenceRefs) -> dict[str, object]:
    changed = evidence.changed_files
    return {
        "pr_url": evidence.pr_url,
        "comment_url": evidence.comment_url,
        "changed_files": None if changed is None else list(changed),
        "read_evidence_digest": evidence.read_evidence_digest,
    }


def _evidence_from(raw: str) -> EvidenceRefs:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise sqlite3.DatabaseError("evidence is not an object")
    return EvidenceRefs(
        pr_url=_optional_text(payload.get("pr_url")),
        comment_url=_optional_text(payload.get("comment_url")),
        changed_files=_text_tuple(payload.get("changed_files")),
        read_evidence_digest=_optional_text(payload.get("read_evidence_digest")),
    )


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise sqlite3.DatabaseError("index text column is unusable")


def _text_tuple(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    raise sqlite3.DatabaseError("index text list is unusable")

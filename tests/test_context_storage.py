"""Local SQLite snapshots stay atomic, readable, and free of FTS5."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from orchestune.context import storage as context_storage
from orchestune.context.budget import fit_context_budget
from orchestune.context.contracts import (
    SCHEMA_VERSION,
    ArtifactStaleStatus,
    EvidenceRefs,
    SharedArtifactV1,
    VerificationSummary,
)
from orchestune.context.search import search_artifacts
from orchestune.context.storage import (
    INDEX_SCHEMA_VERSION,
    CollectionSnapshot,
    IndexedRecord,
    index_lock_path,
    open_context_index,
    rebuild_context_index,
)
from orchestune.infra.process_utils import FileLock, FileLockContentionError


def _artifact(issue: int, title: str) -> IndexedRecord:
    return IndexedRecord(
        artifact=SharedArtifactV1(
            schema_version=SCHEMA_VERSION,
            repository="Saltmu/orchestune",
            issue_number=issue,
            source_comment_id=f"c{issue}",
            source_revision_digest="ab" * 32,
            parent_issue=1841,
            subtask_id="context-index-search",
            claim_id=None,
            attempt=1,
            completion_id=f"completion-{issue}",
            base_sha=None,
            target_commit="c" * 40,
            task_kind="feat",
            result="done",
            summary="再構築",
            assumptions=None,
            evidence_refs=EvidenceRefs(comment_url="https://example.test/c"),
            verification_summary=VerificationSummary(ci="pass", review_rounds=1),
            supersedes=None,
            superseded_by=(),
            stale_status=ArtifactStaleStatus.UNKNOWN,
            recorded_at="2026-01-01T00:00:00Z",
            indexed_at=None,
            source_url="https://example.test/issue",
            plan_revision_digest="d" * 64,
            depends_on=(),
            footprint=("orchestune/context/search.py",),
            symbols=("search_artifacts",),
            dependency_issue_numbers=(),
        ),
        title=title,
        description="説明",
    )


def _snapshot(
    *records: IndexedRecord, coverage: str = "complete"
) -> CollectionSnapshot:
    return CollectionSnapshot(
        publish=True,
        coverage_status=coverage,
        reason=None,
        records=records,
        manifest={"coverage_status": coverage, "reason": None},
    )


def _signature(path: Path) -> list[tuple[object, ...]]:
    view = open_context_index(path)
    assert view.status == "ready"
    return [
        (
            record.artifact.issue_number,
            record.artifact.source_comment_id,
            record.artifact.source_revision_digest,
            record.artifact.indexed_at,
            record.artifact.summary,
            record.artifact.footprint,
            record.artifact.symbols,
            record.artifact.verification_summary.ci,
            record.artifact.verification_summary.review_rounds,
            record.title,
            record.description,
            view.schema_version,
            view.coverage_status,
        )
        for record in view.artifacts
    ]


def test_missing_index_searches_nothing_and_budget_still_fits(tmp_path: Path) -> None:
    path = tmp_path / "context-index.sqlite3"

    view = open_context_index(path)

    assert not path.exists()
    assert view.status == "unavailable"
    assert view.reason == "missing"
    assert view.artifacts == ()
    assert search_artifacts(view.artifacts, "検索") == ()
    assert fit_context_budget("索引が無い").fits is True


def test_same_snapshot_rebuilds_the_same_logical_records(tmp_path: Path) -> None:
    left = tmp_path / "left.sqlite3"
    right = tmp_path / "right.sqlite3"
    snapshot = _snapshot(_artifact(2, "索引検索"), _artifact(1, "予算"))

    rebuild_context_index(left, snapshot, indexed_at="2026-01-02T00:00:00Z")
    rebuild_context_index(right, snapshot, indexed_at="2026-01-02T00:00:00Z")

    assert _signature(left) == _signature(right)
    assert [row[0] for row in _signature(left)] == [1, 2]
    assert open_context_index(left).schema_version == INDEX_SCHEMA_VERSION


def test_corrupt_index_is_quarantined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "context-index.sqlite3"
    path.write_bytes(b"this is not a database")
    monkeypatch.setattr(context_storage.time, "time_ns", lambda: 123)

    view = open_context_index(path)

    assert view.status == "unavailable"
    assert view.reason == "corrupt"
    assert view.artifacts == ()
    assert not path.exists()
    quarantined = tmp_path / "context-index.sqlite3.corrupt.123"
    assert quarantined.read_bytes() == b"this is not a database"


def test_version_mismatch_is_unavailable_without_quarantine(tmp_path: Path) -> None:
    path = tmp_path / "context-index.sqlite3"
    rebuild_context_index(
        path, _snapshot(_artifact(1, "索引検索")), indexed_at="2026-01-02T00:00:00Z"
    )
    connection = sqlite3.connect(path)
    connection.execute("UPDATE index_meta SET schema_version = 'context-index-v0'")
    connection.commit()
    connection.close()

    view = open_context_index(path)

    assert path.exists()
    assert list(tmp_path.glob("*.corrupt.*")) == []
    assert view.status == "unavailable"
    assert view.reason == "schema-mismatch"
    assert view.artifacts == ()
    assert view.schema_version == "context-index-v0"


def test_reader_sees_the_previous_snapshot_until_replace(tmp_path: Path) -> None:
    path = tmp_path / "context-index.sqlite3"
    rebuild_context_index(
        path, _snapshot(_artifact(1, "旧索引")), indexed_at="2026-01-02T00:00:00Z"
    )
    seen: dict[str, str] = {}

    def before_replace() -> None:
        seen["title"] = open_context_index(path).artifacts[0].title

    rebuild_context_index(
        path,
        _snapshot(_artifact(1, "新索引")),
        indexed_at="2026-01-03T00:00:00Z",
        before_replace=before_replace,
    )

    assert seen["title"] == "旧索引"
    assert open_context_index(path).artifacts[0].title == "新索引"


def test_a_second_writer_is_excluded(tmp_path: Path) -> None:
    path = tmp_path / "context-index.sqlite3"
    with FileLock(index_lock_path(path), timeout=0.0):
        with pytest.raises(FileLockContentionError):
            rebuild_context_index(
                path,
                _snapshot(_artifact(1, "索引検索")),
                indexed_at="2026-01-02T00:00:00Z",
            )

    assert not path.exists()


def test_failed_integrity_check_keeps_the_old_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "context-index.sqlite3"
    rebuild_context_index(
        path, _snapshot(_artifact(1, "旧索引")), indexed_at="2026-01-02T00:00:00Z"
    )
    before = _signature(path)
    monkeypatch.setattr(context_storage, "integrity_ok", lambda _connection: False)

    result = rebuild_context_index(
        path, _snapshot(_artifact(1, "新索引")), indexed_at="2026-01-03T00:00:00Z"
    )

    assert result.published is False
    assert _signature(path) == before
    assert list(tmp_path.glob("*.building")) == []


def test_abandoned_building_file_is_replaced(tmp_path: Path) -> None:
    path = tmp_path / "context-index.sqlite3"
    abandoned = Path(f"{path}.building")
    connection = sqlite3.connect(abandoned)
    connection.execute("CREATE TABLE index_meta (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()

    result = rebuild_context_index(
        path, _snapshot(_artifact(1, "索引検索")), indexed_at="2026-01-02T00:00:00Z"
    )

    assert result.published is True
    assert not abandoned.exists()
    assert open_context_index(path).artifacts[0].title == "索引検索"


def test_published_index_has_no_fts5_table(tmp_path: Path) -> None:
    path = tmp_path / "context-index.sqlite3"
    rebuild_context_index(
        path, _snapshot(_artifact(1, "索引検索")), indexed_at="2026-01-02T00:00:00Z"
    )
    source = Path("orchestune/context/storage.py").read_text(encoding="utf-8")
    connection = sqlite3.connect(path)
    schemas = connection.execute(
        "SELECT COALESCE(sql, '') FROM sqlite_master"
    ).fetchall()
    connection.close()

    assert "fts5" not in source.lower()
    assert "read_json_with_recovery" not in source
    assert all("fts5" not in sql.lower() for (sql,) in schemas)

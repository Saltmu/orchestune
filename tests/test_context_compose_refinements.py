"""Launch-context refinements: self exclusion, acceptance fallback, score floor."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orchestune.context import cli as context_cli
from orchestune.context.cli import main
from orchestune.context.compose import compose_for_dispatch, compose_launch_context
from orchestune.context.contracts import (
    SCHEMA_VERSION,
    ArtifactStaleStatus,
    EvidenceRefs,
    SharedArtifactV1,
    VerificationSummary,
)
from orchestune.context.storage import (
    CollectionSnapshot,
    IndexedRecord,
    rebuild_context_index,
)
from orchestune.models import IssueRecord
from orchestune.outcome_record import OutcomeRecord
from tests.fake_forge_support import FakeForge

_QUERY = "索引検索の予算を注入する"
_FOOTPRINT = """
## Footprint
- [ ] フッター内
```yaml
subtask_id: child
footprint:
  - orchestune/context/compose.py
symbols: []
depends_on: []
```
"""


def _issue(number: int, body: str, *, title: str = _QUERY) -> IssueRecord:
    return IssueRecord(
        number=number,
        title=title,
        body=body,
        labels=(),
        created_at="2026-01-01T00:00:00Z",
    )


class _Forge:
    def __init__(self, issues: dict[int, IssueRecord]) -> None:
        self.issues = issues

    def get_issue(self, number: int) -> IssueRecord | None:
        return self.issues.get(int(number))

    def get_repository_slug(self) -> str:
        return "Saltmu/orchestune"


def _record(issue: int, title: str) -> IndexedRecord:
    return IndexedRecord(
        artifact=SharedArtifactV1(
            schema_version=SCHEMA_VERSION,
            repository="Saltmu/orchestune",
            issue_number=issue,
            source_comment_id=f"c{issue}",
            source_revision_digest="ab" * 32,
            parent_issue=None,
            subtask_id="dep",
            claim_id=None,
            attempt=1,
            completion_id=f"completion-{issue}",
            base_sha=None,
            target_commit="c" * 40,
            task_kind="feat",
            result="done",
            summary=title,
            assumptions=None,
            evidence_refs=EvidenceRefs(),
            verification_summary=VerificationSummary(ci="pass"),
            supersedes=None,
            superseded_by=(),
            stale_status=ArtifactStaleStatus.UNKNOWN,
            recorded_at="2026-01-01T00:00:00Z",
            indexed_at=None,
            source_url=f"https://example.test/issue/{issue}",
            plan_revision_digest="d" * 64,
            depends_on=(),
            footprint=(),
            symbols=(),
            dependency_issue_numbers=(),
        ),
        title=title,
        description=title,
    )


def _index(path: Path, *records: IndexedRecord) -> Path:
    rebuild_context_index(
        path,
        CollectionSnapshot(
            publish=True,
            coverage_status="complete",
            reason=None,
            records=records,
            manifest={"coverage_status": "complete", "reason": None},
        ),
        indexed_at="2026-01-02T00:00:00Z",
    )
    return path


def _acceptance(body: str, *, parent_body: str = "") -> tuple[str, str]:
    child = _issue(8, body)
    issues: dict[int, IssueRecord] = {8: child}
    parent_number = None
    if parent_body:
        parent_number = 100
        issues[100] = _issue(100, parent_body, title="親")
        child = _issue(8, body)
        issues[8] = IssueRecord(
            number=8,
            title=_QUERY,
            body=body,
            labels=(),
            created_at="2026-01-01T00:00:00Z",
            parent={"number": 100},
        )
    composed = compose_launch_context(
        _Forge(issues),
        8,
        parent_issue_number=parent_number,
        index_path=Path("missing-context-index.sqlite3"),
    )
    assert composed.tier1 is not None
    return composed.tier1.acceptance, composed.tier1.parent_acceptance


def test_standard_heading_beats_fallback_and_checklist() -> None:
    acceptance, parent = _acceptance(
        "## 受け入れ条件\n定型の受入\n\n## 期待される挙動\n代替\n\n- [ ] リスト\n"
        + _FOOTPRINT,
        parent_body="## 受け入れ\n親の定型\n\n## 期待される挙動\n親の代替\n",
    )

    assert acceptance == "定型の受入"
    assert parent == "親の定型"


def test_fallback_headings_fill_acceptance() -> None:
    feature, _parent = _acceptance(
        "## 概要\n何か\n\n## 設計案・実装アプローチ\nこの方針で実装する\n" + _FOOTPRINT
    )
    bug, parent = _acceptance(
        "## バグの内容\n落ちる\n" + _FOOTPRINT,
        parent_body="## 期待される挙動\nこう動く\n",
    )

    assert feature == "この方針で実装する"
    assert bug == ""
    assert parent == "こう動く"


def test_checklist_ignores_fences_and_footprint_and_empty_stays_empty() -> None:
    checklist, _parent = _acceptance(
        "## 目的\n目的だけ\n\n- [ ] 最初の条件\n- [x] 次の条件\n\n"
        "```\n- [ ] フェンス内は無視\n```\n"
        "~~~\n- [ ] チルダ内は無視\n~~~\n" + _FOOTPRINT
    )
    empty, _parent = _acceptance("## 目的\n目的だけ\n" + _FOOTPRINT)

    assert checklist == "- [ ] 最初の条件\n- [x] 次の条件"
    assert empty == ""
    assert "目的だけ" not in empty


def test_tier2_excludes_the_target_issue_and_low_scores(tmp_path: Path) -> None:
    index = _index(
        tmp_path / "context-index.sqlite3",
        _record(8, _QUERY * 4),
        _record(11, _QUERY * 4),
        _record(12, "引検"),
    )
    forge = _Forge({8: _issue(8, "## 目的\n目的だけ\n" + _FOOTPRINT)})

    composed = compose_launch_context(forge, 8, index_path=index)
    dispatched = compose_for_dispatch(forge, 8, index_path=index)

    assert composed.context_status == "ready"
    assert composed.tier2_status == "ready"
    assert [hit.issue_number for hit in composed.tier2] == [11]
    assert dispatched is not None
    assert [hit.issue_number for hit in dispatched.tier2] == [11]


def test_tier2_is_empty_when_only_the_target_issue_is_indexed(tmp_path: Path) -> None:
    index = _index(tmp_path / "only-self.sqlite3", _record(8, _QUERY * 4))
    composed = compose_launch_context(
        _Forge({8: _issue(8, "## 目的\n目的だけ\n" + _FOOTPRINT)}),
        8,
        index_path=index,
    )

    assert composed.tier2 == ()
    assert composed.tier2_status == "empty"
    assert composed.context_status == "ready"
    assert composed.reduction_reason is None


def test_export_omits_the_requested_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    forge = FakeForge()
    body = (
        "## 目的\n検索を絞る\n\n## 受け入れ条件\n予算内\n\n## Footprint\n```yaml\n"
        "subtask_id: s7\nfootprint:\n  - orchestune/context/cli.py\nsymbols: []\n"
        "depends_on: []\n```\n"
    )
    forge.issues[7] = IssueRecord(
        number=7,
        title=_QUERY,
        body=body,
        labels=(),
        created_at="2026-01-01T00:00:00Z",
    )
    forge.issues[9] = IssueRecord(
        number=9,
        title=_QUERY,
        body="関連する記録\n",
        labels=(),
        created_at="2026-01-01T00:00:00Z",
    )
    forge.create_issue_comment(
        7, OutcomeRecord(result="done", issue=7, completion_id="completion-7").render()
    )
    forge.create_issue_comment(
        9, OutcomeRecord(result="done", issue=9, completion_id="completion-9").render()
    )
    monkeypatch.setattr(context_cli, "GitHubForge", lambda: forge)
    database = tmp_path / "context-index.sqlite3"
    assert (
        main(
            [
                "rebuild",
                "--repository",
                "Saltmu/orchestune",
                "--issue",
                "7",
                "--issue",
                "9",
                "--db",
                str(database),
                "--indexed-at",
                "2026-01-05T00:00:00Z",
            ]
        )
        == 0
    )
    capsys.readouterr()

    status = main(["export", "--db", str(database), "--issue", "7"])
    exported: dict[str, Any] = json.loads(capsys.readouterr().out)

    assert status == 0
    assert exported["context_status"] == "ready"
    assert 7 not in [hit["issue_number"] for hit in exported["tier2"]]

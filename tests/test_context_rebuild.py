"""Rebuild collects one snapshot and keeps the previous index on failure."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from orchestune.context.fetch import FetchRequest, fetch_context
from orchestune.context.storage import open_context_index, rebuild_context_index
from orchestune.forge import GitHubForge
from orchestune.models import IssueRecord
from orchestune.outcome_record import OutcomeRecord
from tests.fake_forge_support import FakeForge


def _issue(number: int, title: str, *, parent: int | None = None) -> IssueRecord:
    footprint = (
        "```yaml\n"
        f"subtask_id: task-{number}\n"
        "footprint:\n"
        "  - orchestune/context/search.py\n"
        "symbols:\n"
        "  - search_artifacts\n"
        "task_kind: feat\n"
        "depends_on: []\n"
        "```\n"
    )
    return IssueRecord(
        number=number,
        title=title,
        body=f"{title}\n{footprint}",
        labels=(),
        created_at=f"2026-01-0{number}T00:00:00Z",
        parent=None if parent is None else {"number": parent},
    )


def _forge(*issues: IssueRecord) -> FakeForge:
    forge = FakeForge()
    for issue in issues:
        forge.issues[issue.number] = issue
        forge.create_issue_comment(
            issue.number,
            OutcomeRecord(
                result="done",
                issue=issue.number,
                completion_id=f"completion-{issue.number}",
                ci="pass",
            ).render(),
        )
    return forge


def _publish(tmp_path: Path, forge: FakeForge, **kwargs: object) -> Path:
    path = tmp_path / "context-index.sqlite3"
    fetched = fetch_context(
        forge,
        FetchRequest(repository="Saltmu/orchestune", **kwargs),  # type: ignore[arg-type]
    )
    rebuild_context_index(path, fetched, indexed_at="2026-01-05T00:00:00Z")
    return path


def _keys(path: Path) -> list[tuple[int, str]]:
    view = open_context_index(path)
    return [
        (record.artifact.issue_number, record.artifact.completion_id or "")
        for record in view.artifacts
    ]


def test_the_same_fetch_snapshot_rebuilds_identical_records(tmp_path: Path) -> None:
    forge = _forge(_issue(1, "索引検索"), _issue(2, "予算"))
    request = FetchRequest(repository="Saltmu/orchestune", issue_numbers=(2, 1))

    first = fetch_context(forge, request)
    second = fetch_context(forge, request)
    left = tmp_path / "left.sqlite3"
    right = tmp_path / "right.sqlite3"
    rebuild_context_index(left, first, indexed_at="2026-01-05T00:00:00Z")
    rebuild_context_index(right, second, indexed_at="2026-01-05T00:00:00Z")

    assert open_context_index(left).coverage_status == "complete"
    assert _keys(left) == _keys(right) == [(1, "completion-1"), (2, "completion-2")]
    record = open_context_index(left).artifacts[0]
    assert record.title == "索引検索"
    assert record.artifact.footprint == ("orchestune/context/search.py",)
    assert record.artifact.symbols == ("search_artifacts",)
    assert record.artifact.task_kind == "feat"
    assert record.artifact.verification_summary.ci == "pass"


def test_parent_and_explicit_issues_are_both_collected(tmp_path: Path) -> None:
    forge = _forge(
        _issue(10, "親"),
        _issue(11, "子", parent=10),
        _issue(12, "明示"),
    )

    path = _publish(tmp_path, forge, parent_issue=10, issue_numbers=(12,))

    assert _keys(path) == [
        (10, "completion-10"),
        (11, "completion-11"),
        (12, "completion-12"),
    ]


def test_max_pages_publishes_a_partial_snapshot(tmp_path: Path) -> None:
    forge = _forge(_issue(1, "一"), _issue(2, "二"))

    path = _publish(tmp_path, forge, since="2026-01-01T00:00:00Z", max_pages=1)
    view = open_context_index(path)

    assert view.coverage_status == "partial"
    assert view.manifest is not None
    assert view.manifest["reason"] == "stopped at max_pages"
    assert _keys(path) == [(1, "completion-1")]


def test_comment_page_failure_keeps_the_previous_records(tmp_path: Path) -> None:
    forge = _forge(_issue(1, "索引検索"))
    path = _publish(tmp_path, forge, issue_numbers=(1,))
    before = _keys(path)

    class Flaky(FakeForge):
        def list_all_issue_comments(self, issue_number: int | str) -> list[dict]:
            if int(issue_number) == 2:
                raise RuntimeError("page 2 failed")
            return super().list_all_issue_comments(issue_number)

    flaky = Flaky()
    flaky.issues = dict(forge.issues)
    flaky.issues[2] = _issue(2, "途中失敗")
    flaky.event_comments.extend(forge.event_comments)
    fetched = fetch_context(
        flaky, FetchRequest(repository="Saltmu/orchestune", issue_numbers=(1, 2))
    )
    result = rebuild_context_index(path, fetched, indexed_at="2026-01-06T00:00:00Z")
    view = open_context_index(path)

    assert fetched.publish is False
    assert fetched.coverage_status == "partial"
    assert result.published is False
    assert result.preserved_existing is True
    assert "page 2 failed" in (view.manifest or {})["reason"]
    assert view.coverage_status == "partial"
    assert _keys(path) == before


def test_subprocess_failure_stays_inside_the_snapshot(tmp_path: Path) -> None:
    forge = _forge(_issue(1, "索引検索"))
    path = _publish(tmp_path, forge, issue_numbers=(1,))
    before = _keys(path)

    class Broken(FakeForge):
        def list_all_issue_comments(self, issue_number: int | str) -> list[dict]:
            if int(issue_number) == 2:
                raise subprocess.CalledProcessError(1, ["gh", "api"])
            return super().list_all_issue_comments(issue_number)

    broken = Broken()
    broken.issues = dict(forge.issues)
    broken.issues[2] = _issue(2, "途中失敗")
    broken.event_comments.extend(forge.event_comments)
    fetched = fetch_context(
        broken, FetchRequest(repository="Saltmu/orchestune", issue_numbers=(1, 2))
    )
    result = rebuild_context_index(path, fetched, indexed_at="2026-01-06T00:00:00Z")

    assert fetched.publish is False
    assert fetched.coverage_status == "partial"
    assert "non-zero" in (fetched.reason or "")
    assert result.preserved_existing is True
    assert _keys(path) == before


def test_range_listing_keeps_a_parent_already_loaded(tmp_path: Path) -> None:
    child = _issue(11, "子", parent=10)
    forge = _forge(_issue(10, "親"), child)
    ranged = IssueRecord(
        number=11,
        title=child.title,
        body=child.body,
        labels=(),
        created_at=child.created_at,
        parent=None,
        blocked_by=(),
    )

    class RangeWithoutParent(FakeForge):
        def list_issues_in_range(
            self, **kwargs: object
        ) -> tuple[list[IssueRecord], bool]:
            return [ranged], True

    listed = RangeWithoutParent()
    listed.issues = dict(forge.issues)
    listed.event_comments.extend(forge.event_comments)
    fetched = fetch_context(
        listed,
        FetchRequest(
            repository="Saltmu/orchestune",
            parent_issue=10,
            since="2026-01-01T00:00:00Z",
        ),
    )
    path = tmp_path / "context-index.sqlite3"
    rebuild_context_index(path, fetched, indexed_at="2026-01-05T00:00:00Z")
    stored = {
        record.artifact.issue_number: record.artifact.parent_issue
        for record in open_context_index(path).artifacts
    }

    assert stored[11] == 10


def test_listing_failure_with_no_prior_index_stays_unavailable(tmp_path: Path) -> None:
    class Broken(FakeForge):
        def list_issues_in_range(
            self, **kwargs: object
        ) -> tuple[list[IssueRecord], bool]:
            raise RuntimeError("range failed")

    path = tmp_path / "context-index.sqlite3"
    fetched = fetch_context(
        Broken(),
        FetchRequest(repository="Saltmu/orchestune", since="2026-01-01T00:00:00Z"),
    )
    result = rebuild_context_index(path, fetched, indexed_at="2026-01-06T00:00:00Z")

    assert fetched.coverage_status == "unavailable"
    assert result.published is False
    assert result.preserved_existing is False
    assert not path.exists()


def _raw_issue(number: int, *, pull_request: bool = False) -> dict[str, object]:
    payload: dict[str, object] = {
        "number": number,
        "title": f"t{number}",
        "body": "b",
        "state": "open",
        "created_at": "2026-01-01T00:00:00Z",
        "labels": [{"name": "status:done"}],
    }
    if pull_request:
        payload["pull_request"] = {"url": "https://example.test/pr"}
    return payload


def test_range_listing_pages_issues_and_skips_pull_requests(
    forge: GitHubForge, gh_run
) -> None:
    full = json.dumps([_raw_issue(number) for number in range(1, 101)])
    short = json.dumps([_raw_issue(101), _raw_issue(102, pull_request=True)])
    gh_run.stdout_sequence(full, short)

    issues, exhausted = forge.list_issues_in_range(
        since="2026-01-01T00:00:00Z", state="all", max_pages=2
    )

    assert exhausted is True
    assert [issue.number for issue in issues] == [*range(1, 101), 101]
    assert issues[0].labels == ("status:done",)
    calls = [call.args[0] for call in gh_run.call_args_list]
    assert calls[0][:2] == ["gh", "api"]
    assert "per_page=100" in calls[0][-1]
    assert "since=2026-01-01T00%3A00%3A00Z" in calls[0][-1]
    assert "page=2" in calls[1][-1]


def test_range_listing_reports_truncation(forge: GitHubForge, gh_run) -> None:
    gh_run.stdout(json.dumps([_raw_issue(number) for number in range(1, 101)]))

    _issues, exhausted = forge.list_issues_in_range(max_pages=1)

    assert exhausted is False
    gh_run.assert_called_once()


def test_range_listing_rejects_bad_arguments_before_gh(
    forge: GitHubForge, gh_run
) -> None:
    with pytest.raises(ValueError):
        forge.list_issues_in_range(state="bogus")
    with pytest.raises(ValueError):
        forge.list_issues_in_range(max_pages=0)
    gh_run.assert_not_called()

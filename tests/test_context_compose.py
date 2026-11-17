"""Launch context keeps Tier 1 and spends the remaining budget on Tier 2."""

from __future__ import annotations

import ast
import json
import subprocess
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from orchestune.context.compose import (
    append_cloud_context,
    compose_for_dispatch,
    compose_launch_context,
    format_cloud_context,
    truncate_utf8,
)
from orchestune.context.contracts import (
    LAUNCH_CONTEXT_SCHEMA,
    SCHEMA_VERSION,
    STATEMENTS_MARKER,
    ArtifactStaleStatus,
    DependencyContext,
    EvidenceRefs,
    LaunchContext,
    SharedArtifactV1,
    Tier1Context,
    Tier2Hit,
    VerificationSummary,
)
from orchestune.context.storage import (
    CollectionSnapshot,
    IndexedRecord,
    rebuild_context_index,
)
from orchestune.dispatch.attempt_record import LaunchAttempt
from orchestune.dispatch.launch import attempts as launch_attempts
from orchestune.dispatch.launch.attempts import JournaledDispatchTarget
from orchestune.dispatch.targets import (
    GROK_CLI_LOCAL_CMD_TEMPLATE,
    CodexCloudDispatchTarget,
    LocalProcessDispatchTarget,
)
from orchestune.dispatch.worktree import _provision_and_launch
from orchestune.infra.detached_process import DetachedProcess
from orchestune.models import IssueRecord, Task
from orchestune.outcome_record import OutcomeRecord, ReviewSummary
from orchestune.targets.cloud_routine import ClaudeCodeCloudRoutineDispatchTarget
from orchestune.targets.contracts import DispatchHandle, DispatchTarget
from orchestune.task_metadata import TaskMetadata

_CHILD = """
## 目的
子の目的

## 受け入れ条件
子の受け入れ

## Footprint
```yaml
subtask_id: child
footprint:
  - orchestune/context/compose.py
symbols:
  - compose_launch_context
depends_on:
  - dep-a
```
"""

_PARENT = """
## 目的
親の目的

## 受け入れ
親の受け入れ
"""

_DEP = """
```yaml
subtask_id: dep-a
footprint:
  - orchestune/context/search.py
symbols:
  - search_artifacts
```
"""


def _issue(
    number: int,
    body: str,
    *,
    title: str = "索引予算",
    parent: dict[str, Any] | None = None,
) -> IssueRecord:
    return IssueRecord(
        number=number,
        title=title,
        body=body,
        labels=(),
        created_at="2026-01-01T00:00:00Z",
        parent=parent,
    )


def _task(issue_number: int = 7) -> Task:
    return Task(
        issue_number=issue_number,
        subtask_id="task-a",
        footprint=("src/foo.py",),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=("status:queued",),
        created_at="2026-01-01T00:00:00+00:00",
    )


class _Forge:
    def __init__(
        self,
        issues: dict[int, IssueRecord],
        comments: dict[int, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.issues = issues
        self.comments = comments or {}

    def get_issue(self, number: int) -> IssueRecord | None:
        return self.issues.get(int(number))

    def list_sub_issues(self, parent_issue_number: int) -> list[IssueRecord]:
        return [
            issue
            for issue in self.issues.values()
            if issue.number != int(parent_issue_number)
        ]

    def list_comments(self, number: int) -> list[dict[str, Any]]:
        return list(self.comments.get(int(number), []))


def _done_comment(summary: str) -> dict[str, str]:
    record = OutcomeRecord(
        result="done",
        issue=50,
        pr=9,
        base_sha="a" * 40,
        head_sha="b" * 40,
        claim_id="claim-1",
        completion_id="completion-1",
        attempt=2,
        ci="pass",
        review=ReviewSummary(verdict="approve"),
    )
    statements = json.dumps({"summary": summary}, ensure_ascii=False)
    body = f"{record.render()}{STATEMENTS_MARKER}\n```json\n{statements}\n```\n"
    return {"body": body, "created_at": "2026-01-02T00:00:00Z"}


def _tier1(**overrides: object) -> Tier1Context:
    values: dict[str, object] = {
        "purpose": "PURPOSE",
        "acceptance": "受け入れる",
        "footprint": ("orchestune/context/compose.py",),
        "symbols": ("compose_launch_context",),
        "parent_purpose": "親",
        "parent_acceptance": "親の受け入れ",
        "dependencies": (),
    }
    values.update(overrides)
    return Tier1Context(**values)  # type: ignore[arg-type]


def _context(**overrides: object) -> LaunchContext:
    values: dict[str, object] = {
        "schema_version": LAUNCH_CONTEXT_SCHEMA,
        "context_status": "ready",
        "tier2_status": "ready",
        "reduction_reason": None,
        "tier1": _tier1(),
        "tier2": (
            Tier2Hit(issue_number=3, title="関連", summary="あいう", source_url=None),
        ),
        "tier3_urls": ("https://example.test/one", "https://example.test/two"),
        "size_bytes": 1,
        "max_bytes": 32768,
    }
    values.update(overrides)
    return LaunchContext(**values)  # type: ignore[arg-type]


def _artifact(issue: int, title: str) -> IndexedRecord:
    return IndexedRecord(
        artifact=SharedArtifactV1(
            schema_version=SCHEMA_VERSION,
            repository="Saltmu/orchestune",
            issue_number=issue,
            source_comment_id=f"c{issue}",
            source_revision_digest="ab" * 32,
            parent_issue=100,
            subtask_id="dep",
            claim_id=None,
            attempt=1,
            completion_id=f"completion-{issue}",
            base_sha=None,
            target_commit="c" * 40,
            task_kind="feat",
            result="done",
            summary="あ" * 200,
            assumptions=None,
            evidence_refs=EvidenceRefs(
                pr_url=f"https://example.test/pr/{issue}",
                comment_url=f"https://example.test/c/{issue}",
            ),
            verification_summary=VerificationSummary(ci="pass"),
            supersedes=None,
            superseded_by=(),
            stale_status=ArtifactStaleStatus.UNKNOWN,
            recorded_at="2026-01-01T00:00:00Z",
            indexed_at=None,
            source_url=f"https://example.test/issue/{issue}",
            plan_revision_digest="d" * 64,
            depends_on=(),
            footprint=("orchestune/context/search.py",),
            symbols=("compose_launch_context",),
            dependency_issue_numbers=(),
        ),
        title=title,
        description="索引予算 注入する compose_launch_context",
    )


def test_compose_module_does_not_import_dispatch_or_targets() -> None:
    source = Path("orchestune/context/compose.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    modules.extend(
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )
    assert all(not name.startswith("orchestune.dispatch") for name in modules)
    assert all(not name.startswith("orchestune.targets") for name in modules)


def test_tier1_keeps_parent_dependency_outcome_and_summary(tmp_path: Path) -> None:
    dep = _issue(50, _DEP, title="依存")
    forge = _Forge(
        {1: _issue(1, _CHILD), 100: _issue(100, _PARENT, title="親"), 50: dep},
        {50: [_done_comment("依存は完了")]},
    )

    composed = compose_launch_context(
        forge, 1, parent_issue_number=100, index_path=tmp_path / "missing.sqlite3"
    )

    assert composed.context_status == "ready"
    assert composed.schema_version == "launch-context-v1"
    assert composed.tier2_status == "unavailable"
    assert composed.tier1 is not None
    assert composed.tier1.purpose == "子の目的"
    assert composed.tier1.acceptance == "子の受け入れ"
    assert composed.tier1.footprint == ("orchestune/context/compose.py",)
    assert composed.tier1.symbols == ("compose_launch_context",)
    assert composed.tier1.parent_purpose == "親の目的"
    assert composed.tier1.parent_acceptance == "親の受け入れ"
    dependency = composed.tier1.dependencies[0]
    assert dependency == DependencyContext(
        issue_number=50,
        subtask_id="dep-a",
        result="done",
        summary="依存は完了",
        pr=9,
        base_sha="a" * 40,
        head_sha="b" * 40,
        claim_id="claim-1",
        completion_id="completion-1",
        attempt=2,
        ci="pass",
        review_verdict="approve",
    )


def test_blocked_dependency_makes_context_unavailable_with_reason(
    tmp_path: Path,
) -> None:
    blocked = OutcomeRecord(result="blocked", issue=50, reason="base-branch-red")
    dep = _issue(50, _DEP, title="依存")
    forge = _Forge(
        {1: _issue(1, _CHILD), 100: _issue(100, _PARENT, title="親"), 50: dep},
        {50: [{"body": blocked.render(), "created_at": "2026-01-03T00:00:00Z"}]},
    )

    composed = compose_launch_context(
        forge, 1, parent_issue_number=100, index_path=tmp_path / "missing.sqlite3"
    )

    assert composed.context_status == "unavailable"
    assert composed.tier1 is None
    assert composed.unavailable_reason is not None
    assert "50" in composed.unavailable_reason
    assert (
        compose_for_dispatch(
            forge, 1, parent_issue_number=100, index_path=tmp_path / "missing.sqlite3"
        )
        is None
    )


def test_magicmock_and_raising_forge_degrade_without_raising(tmp_path: Path) -> None:
    class _Boom:
        def get_issue(self, number: int) -> IssueRecord:
            raise RuntimeError(f"down {number}")

    mocked = compose_launch_context(
        MagicMock(), 1, index_path=tmp_path / "missing.sqlite3"
    )
    raised = compose_launch_context(_Boom(), 1, index_path=tmp_path / "missing.sqlite3")

    assert mocked.context_status == "unavailable"
    assert mocked.tier1 is None
    assert raised.context_status == "unavailable"
    assert compose_for_dispatch(_Boom(), 1, index_path=tmp_path / "x.sqlite3") is None


def test_missing_parent_or_dependency_makes_tier1_unavailable(tmp_path: Path) -> None:
    missing_parent = compose_launch_context(
        _Forge({1: _issue(1, _CHILD)}),
        1,
        parent_issue_number=100,
        index_path=tmp_path / "missing.sqlite3",
    )
    unresolved = compose_launch_context(
        _Forge({1: _issue(1, _CHILD), 100: _issue(100, _PARENT, title="親")}),
        1,
        parent_issue_number=100,
        index_path=tmp_path / "missing.sqlite3",
    )

    assert missing_parent.context_status == "unavailable"
    assert unresolved.context_status == "unavailable"


def test_no_parent_number_leaves_parent_sections_empty(tmp_path: Path) -> None:
    body = _CHILD.replace("depends_on:\n  - dep-a\n", "depends_on: []\n")
    composed = compose_launch_context(
        _Forge({1: _issue(1, body, parent=None)}),
        1,
        parent_issue_number=None,
        index_path=tmp_path / "missing.sqlite3",
    )

    assert composed.context_status == "ready"
    assert composed.tier1 is not None
    assert composed.tier1.parent_purpose == ""
    assert composed.tier1.dependencies == ()


def test_provisioning_template_headings_fill_purpose_and_acceptance(
    tmp_path: Path,
) -> None:
    templates = (
        Path(".github/issue_template.md"),
        Path("skills/orchestune-provision/resources/issue_template.md"),
    )
    for path in templates:
        text = path.read_text(encoding="utf-8")
        assert "## 概要 (Overview)" in text
        assert "## 受け入れ基準 (Acceptance Criteria)" in text
    body = """
## 概要 (Overview)
テンプレートの目的

## 変更内容 (Proposed Changes)
変更

## 受け入れ基準 (Acceptance Criteria)
テンプレートの受け入れ

## Footprint
```yaml
subtask_id: child
footprint:
  - orchestune/context/compose.py
symbols:
  - compose_launch_context
depends_on: []
```
"""
    composed = compose_launch_context(
        _Forge({1: _issue(1, body)}),
        1,
        index_path=tmp_path / "missing.sqlite3",
    )

    assert composed.context_status == "ready"
    assert composed.tier1 is not None
    assert composed.tier1.purpose == "テンプレートの目的"
    assert composed.tier1.acceptance == "テンプレートの受け入れ"


def test_corrupt_index_keeps_tier1(tmp_path: Path) -> None:
    index = tmp_path / "context-index.sqlite3"
    index.write_bytes(b"this is not a database")
    body = _CHILD.replace("depends_on:\n  - dep-a\n", "depends_on: []\n")

    composed = compose_launch_context(
        _Forge({1: _issue(1, body), 100: _issue(100, _PARENT, title="親")}),
        1,
        parent_issue_number=100,
        index_path=index,
    )

    assert composed.context_status == "ready"
    assert composed.tier2_status == "unavailable"
    assert composed.tier1 is not None
    assert composed.tier1.purpose == "子の目的"


def test_tier2_tail_drop_keeps_a_prefix_and_tier1_over_budget_stays_whole(
    tmp_path: Path,
) -> None:
    body = """
## 目的
注入する

## Footprint
```yaml
subtask_id: child
footprint:
  - orchestune/context/compose.py
symbols:
  - compose_launch_context
depends_on: []
```
"""
    index = tmp_path / "context-index.sqlite3"
    title = "索引予算 注入する compose_launch_context"
    rebuild_context_index(
        index,
        CollectionSnapshot(
            publish=True,
            coverage_status="complete",
            reason=None,
            records=(
                _artifact(11, title),
                _artifact(12, title),
                _artifact(13, title),
            ),
            manifest={"coverage_status": "complete", "reason": None},
        ),
        indexed_at="2026-01-02T00:00:00Z",
    )
    forge = _Forge({8: _issue(8, body, title="索引予算")})
    full = compose_launch_context(forge, 8, index_path=index, max_bytes=10**9)
    shrunk = compose_launch_context(
        forge, 8, index_path=index, max_bytes=full.size_bytes - 50
    )
    over = compose_launch_context(forge, 8, index_path=index, max_bytes=1)

    assert [hit.issue_number for hit in full.tier2] == [11, 12, 13]
    assert shrunk.context_status == "ready"
    assert shrunk.tier2_status == "reduced"
    assert shrunk.reduction_reason == "tier2_tail_dropped"
    assert [hit.issue_number for hit in shrunk.tier2] == [
        hit.issue_number for hit in full.tier2
    ][: len(shrunk.tier2)]
    assert len(shrunk.tier2) < len(full.tier2)
    assert over.context_status == "over_budget"
    assert over.reduction_reason == "tier1_exceeds_budget"
    assert over.tier1 is not None
    assert over.tier1.purpose == "注入する"
    assert compose_for_dispatch(forge, 8, index_path=index, max_bytes=1) is not None


def test_truncate_utf8_stops_on_a_character_boundary() -> None:
    assert truncate_utf8("あいう", 4) == "あ"
    assert truncate_utf8("あいう", 3) == "あ"
    assert truncate_utf8("aあ", 2) == "a"
    assert truncate_utf8("ab", 2) == "ab"
    assert truncate_utf8("あ", 0) == ""


def test_cloud_format_keeps_tier1_and_drops_whole_urls() -> None:
    context = _context()
    over = format_cloud_context(context, max_bytes=1)

    assert "PURPOSE" in over
    assert "over_budget" in over
    assert len(over.encode("utf-8")) > 1
    assert append_cloud_context("元の指示\n", None) == "元の指示\n"


def test_cloud_targets_append_context_after_the_existing_prompt() -> None:
    task = _task()
    routine = ClaudeCodeCloudRoutineDispatchTarget("routine", "token")
    codex = CodexCloudDispatchTarget("env")
    context = _context()

    assert routine._build_text(task, "branch", launch_context=context).startswith(
        routine._build_text(task, "branch")
    )
    assert "PURPOSE" in routine._build_text(task, "branch", launch_context=context)
    assert codex._build_prompt(task, "branch", launch_context=context).startswith(
        codex._build_prompt(task, "branch")
    )
    assert "PURPOSE" in codex._build_prompt(task, "branch", launch_context=context)


def _worktree(tmp_path: Path) -> Path:
    worktree = tmp_path / f"wt-{uuid.uuid4().hex[:8]}"
    worktree.mkdir()
    (worktree / ".gitignore").write_text(".orchestune/\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=worktree, check=True, capture_output=True)
    return worktree


def _capture_launch(
    tmp_path: Path, local_cmd: str | None, launch_context: LaunchContext | None
) -> tuple[list[str], list[Path]]:
    worktree = _worktree(tmp_path)
    seen: dict[str, list[str]] = {}

    def start(args: list[str], *, cwd: str | Path, log_path: Path) -> DetachedProcess:
        seen["args"] = args
        return DetachedProcess(pid=1, log_path=log_path, log_offset=0)

    target = LocalProcessDispatchTarget(
        log_dir=tmp_path / "logs", local_cmd=local_cmd, start_process=start
    )
    target.launch(
        _task(), "claude/issue-7-task-a", worktree, launch_context=launch_context
    )
    return seen["args"], list(worktree.glob(".orchestune/tmp/**/context.json"))


def test_builtin_grok_writes_context_without_changing_flags(tmp_path: Path) -> None:
    cmd, written = _capture_launch(tmp_path, GROK_CLI_LOCAL_CMD_TEMPLATE, _context())

    assert cmd[0] == "grok"
    assert cmd[3:] == [
        "--always-approve",
        "--no-plan",
        "--no-auto-update",
        "--output-format",
        "streaming-json",
    ]
    assert "起動コンテキスト" in cmd[2]
    assert "context.json" in cmd[2]
    assert len(written) == 1
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["tier1"]["purpose"] == "PURPOSE"
    assert payload["schema_version"] == "launch-context-v1"
    assert "size_bytes" in payload


def test_custom_placeholder_dry_run_and_absent_context_keep_their_argv(
    tmp_path: Path,
) -> None:
    placeholder, files = _capture_launch(tmp_path, 'echo "{context_json}"', _context())
    plain, plain_files = _capture_launch(
        tmp_path, 'echo "GitHub Issue #{issue_number}"', _context()
    )
    dry, dry_files = _capture_launch(tmp_path, None, _context())
    untouched, untouched_files = _capture_launch(
        tmp_path, GROK_CLI_LOCAL_CMD_TEMPLATE, None
    )

    assert placeholder[1].endswith("context.json")
    assert files
    assert "context.json" not in " ".join(plain)
    assert plain_files
    assert dry == ["true"]
    assert dry_files == []
    assert "起動コンテキスト" not in untouched[2]
    assert untouched_files == []


def test_provision_forwards_context_only_when_the_target_accepts_it(
    tmp_path: Path,
) -> None:
    class _Accepting:
        def launch(
            self,
            task: TaskMetadata,
            branch_name: str,
            worktree_path: Path,
            *,
            force_push: bool = False,
            execution_selection: object = None,
            base_branch: str | None = None,
            launch_context: object | None = None,
        ) -> DispatchHandle:
            self.seen = launch_context
            return DispatchHandle(pid=1)

    class _Legacy:
        def launch(
            self,
            task: TaskMetadata,
            branch_name: str,
            worktree_path: Path,
            *,
            force_push: bool = False,
        ) -> DispatchHandle:
            self.seen = "called"
            return DispatchHandle(pid=1)

    context = _context()
    accepting = _Accepting()
    legacy = _Legacy()
    _provision_and_launch(
        accepting,  # type: ignore[arg-type]
        _task(),
        "branch",
        tmp_path,
        launch_context=context,
    )
    _provision_and_launch(
        legacy,  # type: ignore[arg-type]
        _task(),
        "branch",
        tmp_path,
        launch_context=context,
    )
    mocked = MagicMock()
    _provision_and_launch(mocked, _task(), "branch", tmp_path)

    assert accepting.seen is context
    assert legacy.seen == "called"
    assert "launch_context" not in mocked.launch.call_args.kwargs


class _Inner(DispatchTarget):
    def __init__(self, *, accept: bool) -> None:
        self.accept = accept
        self.seen: object = None

    def launch(
        self,
        task: TaskMetadata,
        branch_name: str,
        worktree_path: Path,
        *,
        force_push: bool = False,
        execution_selection: object = None,
        base_branch: str | None = None,
        launch_context: object | None = None,
    ) -> DispatchHandle:
        return DispatchHandle(external_id="e1")

    def launch_attempt(  # type: ignore[override]
        self,
        attempt_id: str,
        task: TaskMetadata,
        branch_name: str,
        worktree_path: Path,
        *,
        force_push: bool = False,
        execution_selection: object = None,
        base_branch: str | None = None,
        launch_context: object | None = None,
    ) -> DispatchHandle:
        self.seen = launch_context
        return DispatchHandle(external_id="e1")

    def is_complete(self, handle: DispatchHandle, forge: object = None) -> bool:
        return False


class _NoContextAttempt(DispatchTarget):
    def __init__(self) -> None:
        self.called = False

    def launch(
        self,
        task: TaskMetadata,
        branch_name: str,
        worktree_path: Path,
        *,
        force_push: bool = False,
        execution_selection: object = None,
        base_branch: str | None = None,
        launch_context: object | None = None,
    ) -> DispatchHandle:
        return DispatchHandle(external_id="e1")

    def launch_attempt(  # type: ignore[override]
        self,
        attempt_id: str,
        task: TaskMetadata,
        branch_name: str,
        worktree_path: Path,
        *,
        force_push: bool = False,
        execution_selection: object = None,
        base_branch: str | None = None,
    ) -> DispatchHandle:
        self.called = True
        return DispatchHandle(external_id="e1")

    def is_complete(self, handle: DispatchHandle, forge: object = None) -> bool:
        return False


def _journal(inner: DispatchTarget) -> JournaledDispatchTarget:
    attempt = LaunchAttempt(
        attempt_id="attempt-1",
        phase="prepared",
        target="local",
        branch="branch",
        base_branch="main",
        started_at=1.0,
    )
    return JournaledDispatchTarget(inner, attempt, MagicMock(), lambda: None)


def test_journaled_target_forwards_context_only_when_attempt_accepts_it(
    tmp_path: Path,
) -> None:
    accepting = _Inner(accept=True)
    rejecting = _NoContextAttempt()
    context = _context()
    with patch.object(launch_attempts, "write_attempt"):
        _journal(accepting).launch(_task(), "branch", tmp_path, launch_context=context)
        _journal(rejecting).launch(_task(), "branch", tmp_path, launch_context=context)

    assert accepting.seen is context
    assert rejecting.called is True


def test_1841_parent_heading_and_missing_bodies_are_handled(tmp_path: Path) -> None:
    parent_1841 = """
## 目的
ScaleMASを参考に限定コンテキストを構成する

## 企画の受入条件
Tier 1と出典を必ず維持する
"""
    child_body = _CHILD.replace("depends_on:\n  - dep-a\n", "depends_on: []\n")
    composed = compose_launch_context(
        _Forge({1: _issue(1, child_body), 1841: _issue(1841, parent_1841, title="親")}),
        1,
        parent_issue_number=1841,
        index_path=tmp_path / "missing.sqlite3",
    )
    missing_child_body = compose_launch_context(
        _Forge({1: _issue(1, "   "), 1841: _issue(1841, parent_1841, title="親")}),
        1,
        parent_issue_number=1841,
        index_path=tmp_path / "missing.sqlite3",
    )
    missing_parent_body = compose_launch_context(
        _Forge({1: _issue(1, child_body), 1841: _issue(1841, "", title="親")}),
        1,
        parent_issue_number=1841,
        index_path=tmp_path / "missing.sqlite3",
    )

    assert composed.context_status == "ready"
    assert composed.tier1 is not None
    assert composed.tier1.parent_purpose == "ScaleMASを参考に限定コンテキストを構成する"
    assert composed.tier1.parent_acceptance == "Tier 1と出典を必ず維持する"
    assert missing_child_body.context_status == "unavailable"
    assert missing_child_body.unavailable_reason is not None
    assert "1" in missing_child_body.unavailable_reason
    assert missing_parent_body.context_status == "unavailable"
    assert missing_parent_body.unavailable_reason is not None
    assert "1841" in missing_parent_body.unavailable_reason


def test_native_dependency_and_later_blocked_outcome_and_comment_failure(
    tmp_path: Path,
) -> None:
    child = IssueRecord(
        number=1,
        title="子",
        body="## 目的\n目的\n\n## 受け入れ条件\n受入\n\n## Footprint\n```yaml\nsubtask_id: child\nfootprint:\n  - a.py\ndepends_on: []\n```\n",
        labels=(),
        created_at="2026-01-01T00:00:00Z",
        blocked_by=(50,),
    )
    dep = _issue(50, _DEP, title="依存")
    later_blocked = OutcomeRecord(result="blocked", issue=50, reason="stale-read")
    forge_later_blocked = _Forge(
        {1: child, 50: dep},
        {
            50: [
                _done_comment("古い完了"),
                {"body": later_blocked.render(), "created_at": "2026-01-05T00:00:00Z"},
            ]
        },
    )

    class _CommentFailForge(_Forge):
        def list_comments(self, number: int) -> list[dict[str, Any]]:
            raise RuntimeError(f"comments down {number}")

    forge_done = _Forge({1: child, 50: dep}, {50: [_done_comment("native完了")]})
    ready = compose_launch_context(
        forge_done, 1, index_path=tmp_path / "missing.sqlite3"
    )
    blocked_res = compose_launch_context(
        forge_later_blocked, 1, index_path=tmp_path / "missing.sqlite3"
    )
    failed_res = compose_launch_context(
        _CommentFailForge({1: child, 50: dep}),
        1,
        index_path=tmp_path / "missing.sqlite3",
    )

    assert ready.context_status == "ready"
    assert ready.tier1 is not None
    assert ready.tier1.dependencies[0].issue_number == 50
    assert ready.tier1.dependencies[0].summary == "native完了"
    assert blocked_res.context_status == "unavailable"
    assert blocked_res.unavailable_reason is not None
    assert "50" in blocked_res.unavailable_reason
    assert failed_res.context_status == "unavailable"
    assert failed_res.unavailable_reason is not None
    assert "50" in failed_res.unavailable_reason


def test_tier2_filters_repository_and_preserves_provenance_in_local_and_cloud(
    tmp_path: Path,
) -> None:
    same_repo = _artifact(11, "索引予算 注入する compose_launch_context")
    other_repo = IndexedRecord(
        artifact=SharedArtifactV1(
            schema_version=SCHEMA_VERSION,
            repository="OtherOrg/other-repo",
            issue_number=99,
            source_comment_id="c99",
            source_revision_digest="ef" * 32,
            parent_issue=100,
            subtask_id="other",
            claim_id=None,
            attempt=3,
            completion_id="completion-99",
            base_sha=None,
            target_commit="9" * 40,
            task_kind="feat",
            result="done",
            summary="別リポジトリの成果",
            assumptions=None,
            evidence_refs=EvidenceRefs(),
            verification_summary=VerificationSummary(ci="pass"),
            supersedes=None,
            superseded_by=(),
            stale_status=ArtifactStaleStatus.CONFIRMED_STALE,
            recorded_at="2026-01-03T00:00:00Z",
            indexed_at=None,
            source_url="https://example.test/other/99",
            plan_revision_digest="d" * 64,
            depends_on=(),
            footprint=("orchestune/context/compose.py",),
            symbols=("compose_launch_context",),
            dependency_issue_numbers=(),
        ),
        title="索引予算 注入する compose_launch_context",
        description="索引予算 注入する compose_launch_context",
    )
    index = tmp_path / "context-index.sqlite3"
    rebuild_context_index(
        index,
        CollectionSnapshot(
            publish=True,
            coverage_status="complete",
            reason=None,
            records=(same_repo, other_repo),
            manifest={"coverage_status": "complete", "reason": None},
        ),
        indexed_at="2026-01-04T00:00:00Z",
    )
    body = _CHILD.replace("depends_on:\n  - dep-a\n", "depends_on: []\n")

    class _RepoForge(_Forge):
        def get_repository_slug(self) -> str:
            return "Saltmu/orchestune"

    composed = compose_launch_context(
        _RepoForge({8: _issue(8, body, title="索引予算")}),
        8,
        index_path=index,
    )

    assert [hit.issue_number for hit in composed.tier2] == [11]
    hit = composed.tier2[0]
    assert hit.target_commit == "c" * 40
    assert hit.recorded_at == "2026-01-01T00:00:00Z"
    assert hit.attempt == 1
    assert hit.stale_status == "unknown"

    _cmd, written = _capture_launch(tmp_path, GROK_CLI_LOCAL_CMD_TEMPLATE, composed)
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    tier2_entry = payload["tier2"][0]
    assert tier2_entry["target_commit"] == "c" * 40
    assert tier2_entry["recorded_at"] == "2026-01-01T00:00:00Z"
    assert tier2_entry["attempt"] == 1
    assert tier2_entry["stale_status"] == "unknown"
    assert tier2_entry["source_url"] == "https://example.test/issue/11"

    routine = ClaudeCodeCloudRoutineDispatchTarget("routine", "token")
    codex = CodexCloudDispatchTarget("env")
    for prompt in (
        routine._build_text(_task(), "branch", launch_context=composed),
        codex._build_prompt(_task(), "branch", launch_context=composed),
    ):
        assert "c" * 40 in prompt
        assert "2026-01-01T00:00:00Z" in prompt
        assert "https://example.test/issue/11" in prompt
        assert "unknown" in prompt


def test_local_and_cloud_actual_utf8_byte_budget_and_over_budget(
    tmp_path: Path,
) -> None:
    hit1 = Tier2Hit(
        issue_number=11,
        title="日本語ヒット1",
        summary="あ" * 40,
        source_url="https://example.test/issue/11",
        target_commit="a" * 40,
        recorded_at="2026-01-01T00:00:00Z",
        attempt=1,
        stale_status="unknown",
    )
    hit2 = Tier2Hit(
        issue_number=12,
        title="日本語ヒット2",
        summary="い" * 40,
        source_url="https://example.test/issue/12",
        target_commit="b" * 40,
        recorded_at="2026-01-02T00:00:00Z",
        attempt=2,
        stale_status="candidate_stale",
    )
    context = _context(
        tier2=(hit1, hit2),
        tier3_urls=("https://example.test/issue/11", "https://example.test/issue/12"),
    )

    routine = ClaudeCodeCloudRoutineDispatchTarget("routine", "token")
    base_prompt = routine._build_text(_task(), "branch", launch_context=None)
    one_hit_context = _context(
        tier2=(hit1,),
        tier3_urls=("https://example.test/issue/11",),
    )
    exact_one_hit = append_cloud_context(base_prompt, one_hit_context, max_bytes=10**6)
    exact_cap = len(exact_one_hit.encode("utf-8"))

    fitted = append_cloud_context(base_prompt, context, max_bytes=exact_cap)
    assert len(fitted.encode("utf-8")) <= exact_cap
    assert "日本語ヒット1" in fitted
    assert "https://example.test/issue/11" in fitted
    assert "日本語ヒット2" not in fitted
    assert "https://example.test/issue/12" not in fitted

    over = append_cloud_context(
        base_prompt, context, max_bytes=len(base_prompt.encode("utf-8")) + 10
    )
    assert "PURPOSE" in over
    assert "over_budget" in over
    assert "日本語ヒット1" not in over

    cmd, written = _capture_launch(
        tmp_path,
        GROK_CLI_LOCAL_CMD_TEMPLATE,
        _context(
            context_status="over_budget",
            reduction_reason="tier1_exceeds_budget",
            tier2=(),
            tier3_urls=(),
            max_bytes=1,
        ),
    )
    assert written
    local_payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert local_payload["context_status"] == "over_budget"
    assert local_payload["reduction_reason"] == "tier1_exceeds_budget"
    assert "context.json" in cmd[2]


def test_numeric_depends_on_scalar_depends_on_and_repo_mismatch(
    tmp_path: Path,
) -> None:
    numeric_child = _issue(
        1,
        "## 目的\n目的\n\n## Footprint\n```yaml\nsubtask_id: c1\nfootprint:\n  - a.py\ndepends_on:\n  - 50\n```\n",
    )
    scalar_child = _issue(
        2,
        "## 目的\n目的\n\n## Footprint\n```yaml\nsubtask_id: c2\nfootprint:\n  - a.py\ndepends_on: dep-a\n```\n",
    )
    dep = _issue(50, _DEP, title="依存")

    class _RepoForge(_Forge):
        def get_repository_slug(self) -> str:
            return "Saltmu/orchestune"

    forge = _RepoForge(
        {1: numeric_child, 2: scalar_child, 50: dep}, {50: [_done_comment("数値完了")]}
    )
    numeric_res = compose_launch_context(
        forge, 1, index_path=tmp_path / "missing.sqlite3"
    )
    scalar_res = compose_launch_context(
        forge, 2, index_path=tmp_path / "missing.sqlite3"
    )
    mismatch_res = compose_launch_context(
        forge,
        1,
        index_path=tmp_path / "missing.sqlite3",
        repository="OtherOrg/other",
    )

    assert numeric_res.context_status == "ready"
    assert numeric_res.tier1 is not None
    assert numeric_res.tier1.dependencies[0].issue_number == 50
    assert numeric_res.tier1.dependencies[0].summary == "数値完了"
    assert scalar_res.context_status == "unavailable"
    assert mismatch_res.context_status == "unavailable"
    assert mismatch_res.unavailable_reason is not None
    assert "repository mismatch" in mismatch_res.unavailable_reason

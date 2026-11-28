"""親ダッシュボード集計（parent_status）の読み取り専用ドメインテスト。"""

import json
import os
from pathlib import Path
from typing import Any

import pytest

import orchestune.parent_status as parent_status
from orchestune.integrator.attempt import OpenAttempt
from orchestune.issue_parsing import ChildDiscoveryResult
from orchestune.labels import StatusLabel
from orchestune.ledger.run_state import RunState
from orchestune.models import IssueRecord
from orchestune.operator_guidance import EvidenceState, NextOwner, ProgressOutlook
from orchestune.parent_status import (
    ParentObservationCache,
    ParentStatusRequest,
    build_parent_status_snapshot,
    compact_parent_attention,
    discover_latest_dispatch_report,
    format_parent_status_report,
    resolve_report_dir,
)
from orchestune.review.semantic_records import (
    SemanticReviewPhase,
    SemanticReviewStateV1,
)
from tests.dispatch_test_support import UNUSED_PID
from tests.dispatch_test_support import make_test_active_worktree as make_worktree
from tests.dispatch_test_support import save_locked_run_state as save_run_state

_REPO = "Saltmu/orchestune"
_NOW = 1_700_000_000.0


def _issue(
    number: int, labels: tuple[str, ...] = (), state: str = "OPEN"
) -> IssueRecord:
    return IssueRecord(
        number, f"child {number}", "", labels, "2020-01-01T00:00:00Z", state
    )


def _children(*issues: IssueRecord) -> ChildDiscoveryResult:
    return ChildDiscoveryResult(issues=list(issues), metadata_search_supported=True)


def _request(root: Path, **overrides: object) -> ParentStatusRequest:
    values: dict[str, object] = {
        "parent_issue": 1918,
        "repo_root": root,
        "run_state_path": root / "run_state.json",
        "log_dir": root / "logs",
        "now": _NOW,
        "repository_id": _REPO,
    }
    values.update(overrides)
    return ParentStatusRequest(**values)  # type: ignore[arg-type]


def _review_state(
    phase: SemanticReviewPhase, pr_number: int, **overrides: object
) -> SemanticReviewStateV1:
    values: dict[str, object] = {
        "attempt_id": f"attempt-{pr_number}",
        "parent_issue_number": 1918,
        "pr_number": pr_number,
        "phase": phase,
        "round": 1,
        "head_sha": "abc",
        "base_sha": "def",
        "child_sources": (),
    }
    values.update(overrides)
    return SemanticReviewStateV1(**values)  # type: ignore[arg-type]


def _attempt(
    pr_number: int,
    phase: SemanticReviewPhase | None = None,
    *,
    state_error: str | None = None,
    escalation_reason: str | None = None,
) -> OpenAttempt:
    state = None
    if phase is not None:
        state = _review_state(phase, pr_number, escalation_reason=escalation_reason)
    return OpenAttempt(
        pr_number,
        f"tmp-{pr_number}",
        "parent/issue-1894",
        "abc",
        state,
        state_error=state_error,
    )


def _install_readers(
    monkeypatch: pytest.MonkeyPatch, children: object, attempts: object
) -> dict[str, int]:
    calls = {"children": 0, "attempts": 0}

    def children_reader(forge: object, parent: int) -> object:
        calls["children"] += 1
        if isinstance(children, Exception):
            raise children
        return children

    def attempts_reader(forge: object, parent: int) -> object:
        calls["attempts"] += 1
        if isinstance(attempts, Exception):
            raise attempts
        return attempts

    monkeypatch.setattr(parent_status, "find_children_by_parent", children_reader)
    monkeypatch.setattr(parent_status, "find_open_attempts", attempts_reader)
    return calls


def _forge(recorded: list[int] | None = None) -> Any:
    from unittest.mock import MagicMock

    forge = MagicMock()

    def labels(number: int) -> tuple[str, ...]:
        if recorded is not None:
            recorded.append(number)
        return ("status:in-progress",)

    forge.get_issue_labels.side_effect = labels
    return forge


def _save(root: Path, **worktrees: object) -> None:
    save_run_state(RunState(active_worktrees=worktrees), root / "run_state.json")  # type: ignore[arg-type]


def _write_report(root: Path, parent: int, run_id: str, payload: object) -> Path:
    directory = (
        root / ".orchestune" / "reports" / "dispatch" / f"parent-{parent}" / run_id
    )
    directory.mkdir(parents=True)
    path = directory / "result.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_explicit_report_dir_ignores_broken_config(tmp_path: Path) -> None:
    (tmp_path / "orchestune.toml").write_text("[\n", encoding="utf-8")
    explicit = tmp_path / "explicit-reports"
    resolved = resolve_report_dir(tmp_path, explicit)
    assert resolved.path == explicit.resolve()
    assert resolved.note is None


def test_corrupt_config_uses_the_default_report_dir(tmp_path: Path) -> None:
    (tmp_path / "orchestune.toml").write_text("[\n", encoding="utf-8")
    resolved = resolve_report_dir(tmp_path)
    assert (
        resolved.path == (tmp_path / ".orchestune" / "reports" / "dispatch").resolve()
    )
    assert (
        resolved.note
        == "設定ファイルを読めないため既定のレポートディレクトリを使います"
    )


def test_hyphen_key_and_pyproject_key_resolve_relatively(tmp_path: Path) -> None:
    (tmp_path / "orchestune.toml").write_text(
        'report-dir = "custom/reports"\n', encoding="utf-8"
    )
    assert (
        resolve_report_dir(tmp_path).path == (tmp_path / "custom" / "reports").resolve()
    )
    pyproject = tmp_path / "other"
    pyproject.mkdir()
    (pyproject / "pyproject.toml").write_text(
        '[tool.orchestune]\nreport_dir = "from-pyproject"\n',
        encoding="utf-8",
    )
    assert (
        resolve_report_dir(pyproject).path == (pyproject / "from-pyproject").resolve()
    )


def test_underscore_report_dir_wins_and_invalid_values_fall_back(
    tmp_path: Path,
) -> None:
    (tmp_path / "orchestune.toml").write_text(
        'report_dir = "from-underscore"\nreport-dir = "from-hyphen"\n',
        encoding="utf-8",
    )
    assert resolve_report_dir(tmp_path).path == (tmp_path / "from-underscore").resolve()
    (tmp_path / "orchestune.toml").write_text("report_dir = 3\n", encoding="utf-8")
    invalid = resolve_report_dir(tmp_path)
    assert invalid.path == (tmp_path / ".orchestune" / "reports" / "dispatch").resolve()
    assert (
        invalid.note
        == "report_dir の値がパスではないため既定のレポートディレクトリを使います"
    )
    (tmp_path / "orchestune.toml").write_text('report_dir = "   "\n', encoding="utf-8")
    assert resolve_report_dir(tmp_path).note == invalid.note


def test_missing_config_uses_the_default_without_a_note(tmp_path: Path) -> None:
    resolved = resolve_report_dir(tmp_path)
    assert resolved.note is None
    assert (
        resolved.path == (tmp_path / ".orchestune" / "reports" / "dispatch").resolve()
    )


def test_discover_skips_symlinks_and_prefers_the_highest_run_name(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent-7"
    parent.mkdir()
    older = parent / "run-a"
    newer_name = parent / "run-b"
    linked_dir = parent / "run-c"
    linked_file = parent / "run-d"
    for directory in (older, newer_name, linked_file):
        directory.mkdir()
        (directory / "result.json").write_text("{}", encoding="utf-8")
    linked_dir.symlink_to(older, target_is_directory=True)
    (linked_file / "result.json").unlink()
    (linked_file / "result.json").symlink_to(older / "result.json")
    os.utime(older, (1_000, 1_000))
    os.utime(newer_name, (10, 10))
    found = discover_latest_dispatch_report(tmp_path, 7)
    assert found == newer_name / "result.json"
    alias = tmp_path / "alias-parent"
    alias.symlink_to(parent, target_is_directory=True)
    assert discover_latest_dispatch_report(tmp_path / "missing", 7) is None
    root = tmp_path / "linked-root"
    root.mkdir()
    (root / "parent-7").symlink_to(parent, target_is_directory=True)
    assert discover_latest_dispatch_report(root, 7) is None


def test_discover_treats_unreadable_directories_as_missing(tmp_path: Path) -> None:
    parent = tmp_path / "parent-1"
    parent.mkdir()
    (parent / "run").mkdir()
    parent.chmod(0)
    try:
        assert discover_latest_dispatch_report(tmp_path, 1) is None
    finally:
        parent.chmod(0o700)


def test_cache_ttl_serves_fresh_values_and_keeps_stale_ones_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = ParentObservationCache()
    seen = {"children": 0}

    def fetch(forge: object, parent: int) -> ChildDiscoveryResult:
        seen["children"] += 1
        if seen["children"] > 1:
            raise RuntimeError("down")
        return _children()

    monkeypatch.setattr(parent_status, "find_children_by_parent", fetch)
    forge = _forge()
    first, first_state = cache.load_children(forge, 1, 0.0)
    second, second_state = cache.load_children(forge, 1, 29.0)
    third, third_state = cache.load_children(forge, 1, 30.0)
    assert first is second
    assert first_state is EvidenceState.CURRENT
    assert second_state is EvidenceState.CURRENT
    assert seen["children"] == 2
    assert third is first
    assert third_state is EvidenceState.STALE


def test_cache_without_a_prior_value_is_unavailable_and_rejects_bad_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(forge: object, parent: int) -> list[OpenAttempt]:
        raise RuntimeError("down")

    monkeypatch.setattr(parent_status, "find_open_attempts", fail)
    value, state = ParentObservationCache().load_attempts(_forge(), 1, 0.0)
    assert value is None
    assert state is EvidenceState.UNAVAILABLE
    for ttl in (0, -1, True, False):
        with pytest.raises(ValueError):
            ParentObservationCache(ttl)  # type: ignore[arg-type]


def test_parent_issue_must_be_a_positive_integer(tmp_path: Path) -> None:
    for number in (True, False, 0, -5):
        with pytest.raises(ValueError):
            _request(tmp_path, parent_issue=number)


def test_label_cache_is_prefilled_for_parent_children_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = _issue(11, (StatusLabel.IN_PROGRESS.value,))
    outside_calls: list[int] = []
    _install_readers(monkeypatch, _children(child), [])
    _save(
        tmp_path,
        child=make_worktree(issue_number=11, pid=os.getpid(), started_at=_NOW - 5),
        outside=make_worktree(issue_number=99, pid=os.getpid(), started_at=_NOW - 5),
    )
    snapshot = build_parent_status_snapshot(_request(tmp_path), _forge(outside_calls))
    assert outside_calls == [99]
    assert snapshot.sessions.membership_known is True
    assert [item.issue_number for item in snapshot.sessions.worktrees] == [11]
    assert snapshot.sessions.outside_active_count == 1
    assert snapshot.children.open_count == 1
    assert snapshot.children.closed_count == 0


def test_status_counts_follow_label_order_and_ignore_non_status_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issues = _children(
        _issue(1, (StatusLabel.IN_PROGRESS.value, "priority:high", "status:custom")),
        _issue(2, (StatusLabel.QUEUED.value, StatusLabel.BLOCKED.value), "CLOSED"),
    )
    _install_readers(monkeypatch, issues, [])
    snapshot = build_parent_status_snapshot(_request(tmp_path), _forge())
    assert snapshot.children.open_count == 1
    assert snapshot.children.closed_count == 1
    assert snapshot.children.by_status == (
        (StatusLabel.QUEUED.value, 1),
        (StatusLabel.BLOCKED.value, 1),
        (StatusLabel.IN_PROGRESS.value, 1),
    )
    assert snapshot.children.other == 1


def test_next_actions_follow_the_contract_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issues = _children(
        _issue(30, (StatusLabel.BLOCKED_HUMAN_REVIEW.value,)),
        _issue(4, (StatusLabel.IN_PROGRESS.value,)),
        _issue(8, (StatusLabel.QUEUED.value,), "CLOSED"),
        _issue(9, (StatusLabel.QUEUED.value,)),
    )
    attempts = [
        _attempt(20, SemanticReviewPhase.PASSED),
        _attempt(8, SemanticReviewPhase.REVIEW_PENDING),
        _attempt(3, SemanticReviewPhase.NOT_STARTED),
        _attempt(11, SemanticReviewPhase.INVALIDATED),
        _attempt(12, SemanticReviewPhase.FAST_PATH_PASSED),
        _attempt(13, SemanticReviewPhase.SKIPPED_BY_HUMAN),
        _attempt(14, SemanticReviewPhase.ESCALATED),
        _attempt(15, SemanticReviewPhase.INVALIDATED, escalation_reason="stuck"),
        _attempt(16, state_error="unreadable"),
        _attempt(17),
    ]
    _install_readers(monkeypatch, issues, attempts)
    _save(
        tmp_path,
        stopped=make_worktree(issue_number=4, pid=UNUSED_PID, started_at=_NOW - 3661),
    )
    _write_log(tmp_path, 4)
    _write_report(
        tmp_path,
        1918,
        "run-1",
        {"parent_integration": {"status": "review_gate_blocked"}},
    )
    snapshot = build_parent_status_snapshot(
        _request(tmp_path), _forge(), cache=ParentObservationCache()
    )
    argv = [action.argv for action in snapshot.next_actions.actions]
    assert argv == [
        ("orchestune", "blocked", "-p", "1918", "--detail"),
        ("orchestune", "review", "status", "--pr", "3"),
        ("orchestune", "review", "status", "--pr", "8"),
        ("orchestune", "review", "status", "--pr", "14"),
        ("orchestune", "review", "status", "--pr", "15"),
        ("orchestune", "review", "status", "--pr", "16"),
        ("orchestune", "review", "status", "--pr", "17"),
        ("orchestune", "recover", "--issue", "4"),
        None,
        ("orchestune", "dispatch", "-p", "1918"),
    ]
    assert snapshot.next_actions.actions[8].description
    assert snapshot.next_actions.next_owner is NextOwner.USER
    assert snapshot.next_actions.progress is ProgressOutlook.MANUAL
    assert snapshot.integration.review_gate_blocked is True
    assert "現在の状態は不明" in snapshot.integration.text
    report = format_parent_status_report(snapshot, _NOW)
    assert "Orchestune parent status (#1918, " in report
    assert "1h01m01s" in report
    assert "tail-line" in report
    assert "親外" not in report
    compact = compact_parent_attention(snapshot)
    assert compact == (
        "親 #1918 / 要対応:blocked-human-review、PR #3、PR #8、PR #14、PR #15、"
        "PR #16、PR #17、停止 #4、証跡確認、queued。詳細は orchestune status -p 1918"
    )


def _write_log(root: Path, issue: int) -> None:
    log_dir = root / "logs"
    log_dir.mkdir(exist_ok=True)
    (log_dir / f"claude-issue-{issue}-task-a.log").write_text(
        "old\ntail-line\n", encoding="utf-8"
    )


def test_closed_queued_passed_and_invalidated_attempts_do_not_suggest_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issues = _children(_issue(2, (StatusLabel.QUEUED.value,), "closed"))
    attempts = [
        _attempt(4, SemanticReviewPhase.PASSED),
        _attempt(5, SemanticReviewPhase.INVALIDATED),
    ]
    _install_readers(monkeypatch, issues, attempts)
    snapshot = build_parent_status_snapshot(_request(tmp_path), _forge())
    assert snapshot.next_actions.actions == ()
    assert snapshot.next_actions.next_owner is NextOwner.NONE
    assert snapshot.next_actions.progress is ProgressOutlook.TERMINAL
    assert (
        compact_parent_attention(snapshot)
        == "親 #1918 / 要対応:なし。詳細は orchestune status -p 1918"
    )


def test_running_session_suppresses_dispatch_and_recover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issues = _children(
        _issue(4, (StatusLabel.IN_PROGRESS.value, StatusLabel.QUEUED.value))
    )
    _install_readers(monkeypatch, issues, [])
    _save(
        tmp_path,
        alive=make_worktree(issue_number=4, pid=os.getpid(), started_at=_NOW - 5),
    )
    snapshot = build_parent_status_snapshot(_request(tmp_path), _forge())
    assert snapshot.next_actions.actions == ()
    assert snapshot.next_actions.next_owner is NextOwner.ENGINE
    assert snapshot.next_actions.progress is ProgressOutlook.IN_PROGRESS


def test_external_session_counts_as_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issues = _children(
        _issue(4, (StatusLabel.IN_PROGRESS.value,)),
        _issue(9, (StatusLabel.QUEUED.value,)),
    )
    _install_readers(monkeypatch, issues, [])
    _save(
        tmp_path,
        external=make_worktree(
            issue_number=4, pid=None, external_id="ext-1", started_at=_NOW - 5
        ),
    )
    snapshot = build_parent_status_snapshot(_request(tmp_path), _forge())
    assert snapshot.sessions.worktrees[0].state.value == "EXTERNAL"
    assert snapshot.next_actions.actions == ()


def test_unavailable_children_do_not_classify_membership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_readers(
        monkeypatch,
        RuntimeError("down"),
        [_attempt(9, SemanticReviewPhase.FIX_IN_PROGRESS)],
    )
    recorded: list[int] = []
    _save(
        tmp_path,
        child=make_worktree(issue_number=4, pid=UNUSED_PID, started_at=_NOW - 5),
    )
    snapshot = build_parent_status_snapshot(_request(tmp_path), _forge(recorded))
    assert recorded == []
    assert snapshot.sessions.membership_known is False
    assert snapshot.sessions.outside_active_count is None
    assert snapshot.children.evidence is EvidenceState.UNAVAILABLE
    assert snapshot.next_actions.evidence_state is EvidenceState.UNAVAILABLE
    assert [action.argv for action in snapshot.next_actions.actions] == [
        ("orchestune", "review", "status", "--pr", "9")
    ]
    assert "所属" in (snapshot.sessions.note or "")
    report = format_parent_status_report(snapshot, _NOW)
    assert "所属は不明" in report
    assert calls["children"] == 1


def test_unavailable_observation_without_actions_stays_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_readers(monkeypatch, RuntimeError("down"), RuntimeError("down"))
    snapshot = build_parent_status_snapshot(_request(tmp_path), _forge())
    assert snapshot.next_actions.actions == ()
    assert snapshot.next_actions.next_owner is NextOwner.UNKNOWN
    assert snapshot.next_actions.progress is ProgressOutlook.UNKNOWN
    assert "断定しません" in snapshot.next_actions.explanation
    assert snapshot.next_actions.evidence_state is EvidenceState.UNAVAILABLE


def test_corrupt_run_state_degrades_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_readers(monkeypatch, _children(_issue(4, (StatusLabel.QUEUED.value,))), [])
    ledger = tmp_path / "run_state.json"
    ledger.write_text("{", encoding="utf-8")
    snapshot = build_parent_status_snapshot(_request(tmp_path), _forge())
    assert snapshot.sessions.membership_known is False
    assert "run_state" in (snapshot.sessions.note or "")
    assert snapshot.next_actions.actions == ()
    assert snapshot.next_actions.progress is ProgressOutlook.UNKNOWN
    assert ledger.read_text(encoding="utf-8") == "{"
    assert list(tmp_path.glob("run_state.json.corrupt.*")) == []
    again = build_parent_status_snapshot(_request(tmp_path), _forge())
    assert again.sessions.membership_known is False
    assert again.next_actions.actions == ()
    assert again.next_actions.progress is ProgressOutlook.UNKNOWN
    assert ledger.read_text(encoding="utf-8") == "{"


def test_schema_invalid_run_state_stays_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_readers(monkeypatch, _children(_issue(4, (StatusLabel.QUEUED.value,))), [])
    ledger = tmp_path / "run_state.json"
    text = '{"launch_history": null}'
    ledger.write_text(text, encoding="utf-8")
    snapshot = build_parent_status_snapshot(_request(tmp_path), _forge())
    assert snapshot.sessions.membership_known is False
    assert snapshot.next_actions.actions == ()
    assert snapshot.next_actions.progress is ProgressOutlook.UNKNOWN
    assert ledger.read_text(encoding="utf-8") == text


def test_missing_and_corrupt_reports_are_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_readers(monkeypatch, _children(), [])
    missing = build_parent_status_snapshot(_request(tmp_path), _forge())
    assert "未検出" in missing.integration.text
    assert missing.integration.review_gate_blocked is False
    assert missing.integration.path is None
    path = _write_report(tmp_path, 1918, "run-9", {"not-json": True})
    path.write_text("{", encoding="utf-8")
    corrupt = build_parent_status_snapshot(_request(tmp_path), _forge())
    assert "現在の状態は不明" in corrupt.integration.text
    assert corrupt.integration.review_gate_blocked is False
    report = format_parent_status_report(missing, _NOW)
    assert "未検出" in report
    assert "統合・レビュー状態" in report
    assert "次の操作案内" in report
    assert "案内:" in report


def test_config_note_is_not_repeated_when_the_integration_text_contains_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "orchestune.toml").write_text("[\n", encoding="utf-8")
    _install_readers(monkeypatch, _children(), [])
    snapshot = build_parent_status_snapshot(_request(tmp_path), _forge())
    report = format_parent_status_report(snapshot, _NOW)
    assert (
        report.count("設定ファイルを読めないため既定のレポートディレクトリを使います")
        == 1
    )
    assert "注意:" in report


def test_repository_id_comes_from_the_caller_forge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forge = _forge()
    seen: dict[str, object] = {}

    def observe(cwd: Path, forge_arg: object = None) -> str:
        seen["forge"] = forge_arg
        seen["cwd"] = cwd
        return _REPO

    monkeypatch.setattr(parent_status, "observe_repository_id", observe)
    _install_readers(monkeypatch, _children(), [])
    _write_report(
        tmp_path,
        1918,
        "run-1",
        {
            "parent_integration": {
                "version": 1,
                "repository_id": _REPO,
                "parent_issue": 1918,
                "observed_at": "2020-01-01T00:00:00Z",
                "mode": "apply",
                "executed": True,
                "status": "review_gate_blocked",
                "failures": [
                    {"kind": "child_review_gate", "reason": "absent", "issue_number": 4}
                ],
            }
        },
    )
    snapshot = build_parent_status_snapshot(
        _request(tmp_path, repository_id=None), forge
    )
    assert seen["forge"] is forge
    assert seen["cwd"] == tmp_path
    assert snapshot.integration.review_gate_blocked is True
    assert "レビュー証跡を確認してください" in snapshot.integration.text
    assert snapshot.next_actions.actions[-1].argv is None


def test_outside_running_session_does_not_block_parent_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_readers(monkeypatch, _children(_issue(9, (StatusLabel.QUEUED.value,))), [])
    _save(
        tmp_path,
        outside=make_worktree(issue_number=99, pid=os.getpid(), started_at=_NOW - 5),
    )
    snapshot = build_parent_status_snapshot(_request(tmp_path), _forge())
    assert snapshot.sessions.outside_active_count == 1
    assert [action.argv for action in snapshot.next_actions.actions] == [
        ("orchestune", "dispatch", "-p", "1918")
    ]
    assert "親外の active worktree: 1" in format_parent_status_report(snapshot, _NOW)


def test_source_does_not_offer_gate_override_or_apply() -> None:
    source = Path("orchestune/parent_status.py").read_text(encoding="utf-8")
    assert "--child-review-gate" not in source
    assert "--apply" not in source


def test_higher_run_name_beats_a_newer_mtime(tmp_path: Path) -> None:
    parent = tmp_path / "parent-3"
    low = parent / "001"
    high = parent / "002"
    low.mkdir(parents=True)
    high.mkdir()
    (low / "result.json").write_text("{}", encoding="utf-8")
    (high / "result.json").write_text("{}", encoding="utf-8")
    os.utime(low, (5_000, 5_000))
    os.utime(high, (10, 10))
    assert discover_latest_dispatch_report(tmp_path, 3) == high / "result.json"

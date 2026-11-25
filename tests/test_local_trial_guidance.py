"""Trial guidance stays bound to the current view and its ids."""

from dataclasses import replace
from pathlib import Path

import pytest

from orchestune.infra.cli_guidance import render_guidance
from orchestune.infra.managed_process import ManagedProcessResult, ManagedProcessSpec
from orchestune.local_trial.contracts import (
    TrialError,
    format_dirty_workspace,
)
from orchestune.local_trial.guidance import guidance_from_trial
from orchestune.local_trial.models import (
    SCHEMA_VERSION,
    AttemptInput,
    AttemptKind,
    AttemptResult,
    Budget,
    Change,
    DirtyWorkspace,
    ProcessRecord,
    ProcessStatus,
    TrialPhase,
    WorktreeKind,
)
from orchestune.local_trial.planning import validate_plan
from orchestune.local_trial.policy import verify_clean_review
from orchestune.local_trial.service import (
    ResumeRequest,
    StartRequest,
    _reconcile_attempt_by_kind,
    resume_trial,
    start_trial,
)
from orchestune.operator_guidance import NextOwner, ProgressOutlook
from tests.test_local_trial_cli import _dummy_state, _dummy_view
from tests.test_local_trial_service import FakeProcessRunner, _make_fake_deps


def _text(view, repo: str = "/work/repo") -> str:
    return render_guidance(guidance_from_trial(view, repo))


def test_planned_approval_uses_the_current_digest() -> None:
    state = _dummy_state("a" * 32, TrialPhase.PLANNED)
    text = _text(_dummy_view(state))
    assert "計画の承認が必要です" in text
    assert "--approve-plan" in text
    assert "次の担当: user" in text
    assert "自動進行: manual" in text


def test_missing_plan_is_not_an_approval_command() -> None:
    state = replace(_dummy_state("a" * 32), plan=None)
    text = _text(_dummy_view(state, reason="approval_required"))
    assert "コマンド:" not in text
    assert "--approve-plan" not in text


def test_stop_is_unconfirmed_before_any_confirm_command() -> None:
    attempt = AttemptInput(
        trial_id="a" * 32,
        attempt_id="b" * 32,
        kind=AttemptKind.IMPLEMENT,
        input_head_sha="c" * 40,
        base_sha="d" * 40,
        plan_digest="e" * 64,
        plan_revision=0,
        cwd="/work/repo",
        artifact_dir="/work/artifacts",
        started_at="2026-01-01T00:00:00Z",
    )
    state = replace(
        _dummy_state("a" * 32, TrialPhase.NEEDS_ATTENTION),
        pending_attempt=attempt,
        reason="execution_unknown",
    )
    text = _text(
        _dummy_view(
            state,
            effective_phase=TrialPhase.NEEDS_ATTENTION,
            reason="execution_unknown",
        )
    )
    assert "プロセスと成果物の確認が先" in text
    assert "--confirm-stopped" in text
    assert "コマンド:" not in text


def test_missing_attempt_id_is_not_executable() -> None:
    state = replace(
        _dummy_state("a" * 32, TrialPhase.NEEDS_ATTENTION),
        pending_attempt=None,
        reason="execution_unknown",
    )
    text = _text(
        _dummy_view(
            state,
            effective_phase=TrialPhase.NEEDS_ATTENTION,
            reason="execution_unknown",
        )
    )
    assert "コマンド:" not in text
    assert "b" * 32 not in text


def test_dirty_confirmation_uses_only_the_view_digest() -> None:
    state = _dummy_state("a" * 32, TrialPhase.NEEDS_ATTENTION)
    view = _dummy_view(state, reason="worktree_dirty")
    text = _text(view, repo="/work/my dir")
    assert "--confirm-worktree" in text
    assert "c" * 64 in text
    assert "'/work/my dir'" in text or "/work/my dir" in text

    missing = _dummy_view(state, reason="worktree_dirty")
    missing = replace(missing, worktree_digest=None)
    assert "コマンド:" not in _text(missing)


def test_base_change_and_budget_do_not_offer_generic_resume() -> None:
    state = _dummy_state("a" * 32, TrialPhase.NEEDS_ATTENTION)
    base = _dummy_view(state, reason="base_changed")
    base_text = _text(base)
    assert "orchestune trial resume" not in base_text
    assert "コマンド:" not in base_text
    assert "汎用の resume では解消しません" in base_text

    limited = replace(
        state, budget=Budget(plan_revision=1, cycles_started=3, reviews_started=3)
    )
    text = _text(_dummy_view(limited, reason="budget_exhausted"))
    assert "修正回数" in text and "レビュー回数" in text
    assert "orchestune trial resume" not in text


def test_ready_adoption_requires_ready_is_current() -> None:
    state = _dummy_state("a" * 32, TrialPhase.READY)
    current = _dummy_view(state, effective_phase=TrialPhase.READY)
    text = _text(current)
    assert "採用を記録する" in text
    assert "--decision adopted" in text

    stale = replace(current, ready_is_current=False)
    held = _text(stale)
    assert "採用候補にしません" in held
    assert "--decision" not in held


def test_closed_and_unknown_reason_do_not_invent_a_command() -> None:
    state = _dummy_state("a" * 32, TrialPhase.CLOSED)
    closed = guidance_from_trial(
        _dummy_view(state, effective_phase=TrialPhase.CLOSED, reason="closed"),
        "/work/repo",
    )
    assert closed.next_owner is NextOwner.NONE
    assert closed.progress is ProgressOutlook.TERMINAL
    assert closed.actions == ()

    unknown = _dummy_view(
        replace(state, phase=TrialPhase.NEEDS_ATTENTION),
        effective_phase=TrialPhase.NEEDS_ATTENTION,
        reason="something_new",
    )
    text = _text(unknown)
    assert "理由: something_new（不明）" in text
    assert "次の担当: unknown" in text
    assert "resume" not in text


def _detail(
    kind: WorktreeKind, changes: tuple[Change, ...], root: str = "/work/wt"
) -> DirtyWorkspace:
    return DirtyWorkspace(kind=kind, root=root, changes=changes)


def test_dirty_guidance_renders_bounds_without_a_confirm_for_review() -> None:
    state = _dummy_state("a" * 32, TrialPhase.NEEDS_ATTENTION)
    tracked = _detail(
        WorktreeKind.IMPLEMENTATION,
        (Change(status="A", path="kept.py"), Change(status="M", path="edit.py")),
    )
    tracked_text = _text(
        replace(
            _dummy_view(state, reason="worktree_dirty"),
            dirty_workspace=tracked,
        )
    )
    assert "実装 worktree:" in tracked_text
    assert "- A kept.py" in tracked_text
    assert "- M edit.py" in tracked_text
    assert ".gitignore" not in tracked_text
    assert "--confirm-worktree" in tracked_text
    assert "c" * 64 in tracked_text

    weird = Change(status="M", path="a b\n\t\x01\\", old_path="")
    escaped = format_dirty_workspace(
        _detail(WorktreeKind.SOURCE, (weird,), root="/work/my dir")
    )
    line = next(item for item in escaped.splitlines() if item.startswith("- "))
    assert line == '- M a\\ b\\n\\t\\x01\\\\ <- ""'
    assert "開始元 worktree:" in escaped
    assert "/work/my\\ dir" in escaped
    assert "--confirm-worktree" not in escaped

    omitted = tuple(Change(status="M", path=f"f{index:02d}.py") for index in range(20))
    omitted += (Change(status="??", path="gen.out"),)
    many = format_dirty_workspace(_detail(WorktreeKind.PRIMARY, omitted))
    assert "主 worktree:" in many
    assert "ほか 1 件は省略しました" in many
    assert "gen.out" not in many
    assert many.count(".gitignore") == 1
    assert len([item for item in many.splitlines() if item.startswith("- ")]) == 20

    empty = format_dirty_workspace(_detail(WorktreeKind.REVIEW, ()))
    assert "観測した変更パスはありません。パスは断定しません。" in empty
    assert "git status --short" in empty
    assert ".gitignore" not in empty

    review = replace(
        _dummy_view(state, reason="worktree_dirty"),
        dirty_workspace=_detail(
            WorktreeKind.REVIEW, (Change(status="??", path="leak.py"),), root="/rev"
        ),
    )
    review_text = _text(review)
    assert "レビュー worktree:" in review_text
    assert "コマンド:" not in review_text
    assert "--confirm-worktree" not in review_text
    assert "実装 worktree の表示 digest" in review_text


def test_plan_stage_dirty_view_shows_the_judgment_snapshot(tmp_path: Path) -> None:
    deps, _store, ws, agents, _proc = _make_fake_deps(tmp_path)

    def on_run(req_arg: object) -> None:
        kind = str(getattr(getattr(req_arg, "input", None), "kind", ""))
        if kind == "plan":
            ws.head_sha = "b" * 40
            ws.changes = (Change(status="M", path="plan dirty.py"),)

    agents.on_run = on_run
    view = start_trial(
        StartRequest(
            repo=str(tmp_path),
            request_text="implement something",
            agent_name="codex",
        ),
        deps,
    )
    assert view.reason == "worktree_dirty"
    assert view.dirty_workspace is not None
    assert view.dirty_workspace.changes == (Change(status="M", path="plan dirty.py"),)
    text = _text(view, repo=str(tmp_path))
    assert "実装 worktree:" in text
    assert "plan\\ dirty.py" in text
    assert view.worktree_digest is not None and view.worktree_digest in text
    assert "--confirm-worktree" in text

    fresh, _store, fresh_ws, fresh_agents, _fresh_proc = _make_fake_deps(tmp_path)

    def on_head_only(req_arg: object) -> None:
        kind = str(getattr(getattr(req_arg, "input", None), "kind", ""))
        if kind == "plan":
            fresh_ws.head_sha = "c" * 40
            fresh_ws.changes = ()

    fresh_agents.on_run = on_head_only
    empty = start_trial(
        StartRequest(
            repo=str(tmp_path),
            request_text="another request",
            agent_name="codex",
        ),
        fresh,
    )
    assert empty.reason == "worktree_dirty"
    assert empty.dirty_workspace is not None
    assert empty.dirty_workspace.changes == ()
    empty_text = _text(empty, repo=str(tmp_path))
    assert "観測した変更パスはありません。パスは断定しません。" in empty_text
    assert "plan dirty.py" not in empty_text


def test_ci_and_review_head_drift_do_not_invent_paths(tmp_path: Path) -> None:
    deps, _store, ws, _agents, proc = _make_fake_deps(tmp_path)
    view = start_trial(
        StartRequest(
            repo=str(tmp_path),
            request_text="implement something",
            agent_name="codex",
        ),
        deps,
    )
    assert view.state is not None and view.state.plan is not None
    plan_dig = validate_plan(
        view.state.plan, str(tmp_path), "implement something", ws.base_sha
    )

    def run(spec: ManagedProcessSpec) -> ManagedProcessResult:
        ws.head_sha = "b" * 40
        ws.changes = ()
        return FakeProcessRunner.run(proc, spec)

    proc.run = run  # type: ignore[method-assign, assignment]
    with pytest.raises(TrialError, match="Worktree modified during CI") as caught:
        resume_trial(
            ResumeRequest(trial_id=view.state.trial_id, approve_plan=plan_dig), deps
        )
    assert "観測した変更パスはありません。パスは断定しません。" in caught.value.message
    assert "実装 worktree:" in caught.value.message
    assert "--confirm-worktree" not in caught.value.message

    state = view.state
    snap = replace(
        ws.inspect(state.repository, state.workspace),
        head_sha="d" * 40,
        changes=(),
    )
    with pytest.raises(
        TrialError, match="Review worktree modified during review"
    ) as review_exc:
        verify_clean_review(snap, state)
    assert state.workspace.review_path in review_exc.value.message
    assert "レビュー worktree:" in review_exc.value.message
    assert (
        "観測した変更パスはありません。パスは断定しません。" in review_exc.value.message
    )
    assert state.workspace.implementation_path not in review_exc.value.message


def test_reconcile_dirty_attempt_uses_the_observed_changes(tmp_path: Path) -> None:
    deps, _store, ws, _agents, _proc = _make_fake_deps(tmp_path)
    view = start_trial(
        StartRequest(repo=str(tmp_path), request_text="req", agent_name="codex"),
        deps,
    )
    assert view.state is not None
    state = view.state
    ws.changes = (Change(status="R", path="new.py", old_path="old.py"),)
    snap = ws.inspect(state.repository, state.workspace)
    attempt = AttemptInput(
        trial_id=state.trial_id,
        attempt_id="att1",
        kind=AttemptKind.PLAN,
        input_head_sha=state.head_sha,
        base_sha=state.repository.base_sha,
        plan_digest=None,
        plan_revision=0,
        cwd=state.workspace.implementation_path,
        artifact_dir="/art",
        started_at="2026-01-01T00:00:00Z",
    )
    process = ProcessRecord(
        outcome=ProcessStatus.SUCCESS,
        returncode=0,
        stop_confirmed=True,
        elapsed_seconds=1.0,
        timeout_seconds=60.0,
        stdout_tail="",
        stderr_tail="",
        detail="",
    )
    result = AttemptResult(
        schema_version=SCHEMA_VERSION,
        input=attempt,
        process=process,
        agent_payload=None,
        ended_at="2026-01-01T00:00:00Z",
    )
    with pytest.raises(
        TrialError, match="Worktree modified during planning phase"
    ) as planned:
        _reconcile_attempt_by_kind(state, result, snap, deps)
    assert "- R new.py <- old.py" in planned.value.message
    assert state.workspace.implementation_path in planned.value.message

    ci_result = replace(result, input=replace(attempt, kind=AttemptKind.CI))
    with pytest.raises(TrialError, match="Worktree modified during CI") as ci_exc:
        _reconcile_attempt_by_kind(state, ci_result, snap, deps)
    assert "- R new.py <- old.py" in ci_exc.value.message

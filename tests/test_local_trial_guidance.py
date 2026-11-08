"""Trial guidance stays bound to the current view and its ids."""

from dataclasses import replace

from orchestune.infra.cli_guidance import render_guidance
from orchestune.local_trial.guidance import guidance_from_trial
from orchestune.local_trial.models import (
    AttemptInput,
    AttemptKind,
    Budget,
    TrialPhase,
)
from orchestune.operator_guidance import NextOwner, ProgressOutlook
from tests.test_local_trial_cli import _dummy_state, _dummy_view


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

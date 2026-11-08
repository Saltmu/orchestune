"""Project a trial view into operator guidance. Commands stay bound to that view."""

from __future__ import annotations

from orchestune.local_trial.contracts import plan_digest
from orchestune.local_trial.models import TrialPhase, TrialState, TrialView
from orchestune.operator_guidance import (
    EvidenceState,
    GuidanceAction,
    NextOwner,
    OperatorGuidance,
    ProgressOutlook,
)

_NO_RESUME = {
    "base_changed": "base が変わっています。汎用の resume では解消しません",
    "head_changed": "head が変わっています。汎用の resume では解消しません",
    "identity_mismatch": "識別子が一致しません。汎用の resume では解消しません",
    "findings_pending": "指摘が残っています。汎用の resume だけでは解消しません",
    "ci_failed": "CI が失敗しています。汎用の resume だけでは解消しません",
    "review_failed": "レビューが失敗しています。汎用の resume だけでは解消しません",
    "ci_missing": "CI コマンドが未設定です。承認や resume では補えません",
}
_CURRENT = EvidenceState.CURRENT
_NOTE = "この表示回の trial view です"


def guidance_from_trial(view: TrialView, repo: str) -> OperatorGuidance:
    """Map the view's phase and reason. Do not offer an unbound command."""
    if view.state is None:
        return _without_state(view)
    mapped = (
        _closed(view)
        or _stop_unconfirmed(view)
        or _dirty(view, repo)
        or _budget(view)
        or _blocked_reason(view)
        or _approval(view, repo)
        or _ready(view)
    )
    return mapped if mapped is not None else _other(view)


def _phase_name(view: TrialView) -> str:
    phase = view.effective_phase
    return phase.value if isinstance(phase, TrialPhase) else str(phase)


def _target(state: TrialState | None) -> str:
    if state is None:
        return "trial (unknown)"
    return f"trial {state.trial_id}"


def _guidance(
    view: TrialView,
    *,
    explanation: str,
    owner: NextOwner,
    progress: ProgressOutlook,
    actions: tuple[GuidanceAction, ...] = (),
    condition: str | None = None,
    evidence: EvidenceState = _CURRENT,
    note: str = _NOTE,
) -> OperatorGuidance:
    codes = (view.reason,) if view.reason else ()
    return OperatorGuidance(
        target=_target(view.state),
        stage=_phase_name(view),
        reason_codes=codes,
        explanation=explanation,
        next_owner=owner,
        progress=progress,
        progress_condition=condition,
        actions=actions,
        references=(),
        evidence_state=evidence,
        evidence_note=note,
    )


def _without_state(view: TrialView) -> OperatorGuidance:
    return _guidance(
        view,
        explanation="試行の状態がありません",
        owner=NextOwner.UNKNOWN,
        progress=ProgressOutlook.UNKNOWN,
        evidence=EvidenceState.UNAVAILABLE,
        note="state が無いため完了や再開は断定しません",
    )


def _closed(view: TrialView) -> OperatorGuidance | None:
    if _phase_name(view) != TrialPhase.CLOSED.value:
        return None
    return _guidance(
        view,
        explanation="試行は閉じています",
        owner=NextOwner.NONE,
        progress=ProgressOutlook.TERMINAL,
    )


def _stop_unconfirmed(view: TrialView) -> OperatorGuidance | None:
    if view.reason != "execution_unknown":
        return None
    action = GuidanceAction(
        description=(
            "プロセスと成果物の確認が先です。確認前の --confirm-stopped は実行しません"
        ),
        preconditions=("プロセスの停止", "成果物"),
    )
    return _guidance(
        view,
        explanation="停止は未確認です",
        owner=NextOwner.USER,
        progress=ProgressOutlook.MANUAL,
        actions=(action,),
    )


def _dirty(view: TrialView, repo: str) -> OperatorGuidance | None:
    if view.reason != "worktree_dirty" or view.state is None:
        return None
    argv = _dirty_argv(view.state, view.worktree_digest, repo)
    description = (
        "表示した作業ツリー digest で変更を確認します"
        if argv is not None
        else "作業ツリー digest が無いため確認コマンドは実行できません"
    )
    return _guidance(
        view,
        explanation="作業ツリーの変更確認が必要です",
        owner=NextOwner.USER,
        progress=ProgressOutlook.MANUAL,
        actions=(GuidanceAction(description=description, argv=argv),),
    )


def _dirty_argv(
    state: TrialState, digest: str | None, repo: str
) -> tuple[str, ...] | None:
    if not digest or not repo:
        return None
    return (
        "orchestune",
        "trial",
        "resume",
        state.trial_id,
        "--repo",
        repo,
        "--confirm-worktree",
        digest,
    )


def _budget(view: TrialView) -> OperatorGuidance | None:
    state = view.state
    if state is None or view.reason != "budget_exhausted":
        return None
    parts = []
    if state.budget.cycles_started >= 3:
        parts.append("修正回数")
    if state.budget.reviews_started >= 3:
        parts.append("レビュー回数")
    detail = "と".join(parts) if parts else "修正またはレビューの回数"
    return _guidance(
        view,
        explanation=f"{detail}が上限です。汎用の resume では解消しません",
        owner=NextOwner.USER,
        progress=ProgressOutlook.MANUAL,
    )


def _blocked_reason(view: TrialView) -> OperatorGuidance | None:
    explanation = _NO_RESUME.get(view.reason or "")
    if explanation is None:
        return None
    return _guidance(
        view,
        explanation=explanation,
        owner=NextOwner.USER,
        progress=ProgressOutlook.MANUAL,
    )


def _approval(view: TrialView, repo: str) -> OperatorGuidance | None:
    if _phase_name(view) != TrialPhase.PLANNED.value or view.state is None:
        return None
    if view.reason not in (None, "approval_required"):
        return None
    argv = _approval_argv(view.state, repo)
    description = (
        "表示した計画を確認し、その digest で承認します"
        if argv is not None
        else "計画 digest が無いため承認コマンドは実行できません"
    )
    return _guidance(
        view,
        explanation="計画の承認が必要です",
        owner=NextOwner.USER,
        progress=ProgressOutlook.MANUAL,
        actions=(GuidanceAction(description=description, argv=argv),),
    )


def _approval_argv(state: TrialState, repo: str) -> tuple[str, ...] | None:
    if state.plan is None or not repo:
        return None
    digest = plan_digest(state.plan, state.request.digest, state.repository.base_sha)
    return (
        "orchestune",
        "trial",
        "resume",
        state.trial_id,
        "--repo",
        repo,
        "--approve-plan",
        digest,
    )


def _ready(view: TrialView) -> OperatorGuidance | None:
    if _phase_name(view) != TrialPhase.READY.value or view.state is None:
        return None
    if not view.ready_is_current:
        return _guidance(
            view,
            explanation="ready_is_current を満たさないため採用候補にしません",
            owner=NextOwner.USER,
            progress=ProgressOutlook.MANUAL,
        )
    return _guidance(
        view,
        explanation="ローカルの採用候補です",
        owner=NextOwner.USER,
        progress=ProgressOutlook.MANUAL,
        actions=_adoption_actions(view.state),
    )


def _adoption_actions(state: TrialState) -> tuple[GuidanceAction, ...]:
    root = state.repository.source_root
    branch = state.workspace.branch
    if not root or not branch:
        return (
            GuidanceAction(
                description="採用元のパスが無いため採用コマンドは実行できません"
            ),
        )
    tid = state.trial_id
    return (
        GuidanceAction(
            description="リポジトリが clean かつ base であることを確認する",
            argv=("git", "-C", root, "status"),
        ),
        GuidanceAction(
            description="作業ブランチを fast-forward で取り込む",
            argv=("git", "-C", root, "merge", "--ff-only", branch),
        ),
        GuidanceAction(
            description="採用を記録する",
            argv=(
                "orchestune",
                "trial",
                "result",
                tid,
                "--decision",
                "adopted",
                "--repo",
                root,
            ),
        ),
        GuidanceAction(
            description="採用せず保持する",
            argv=(
                "orchestune",
                "trial",
                "result",
                tid,
                "--decision",
                "keep",
                "--repo",
                root,
            ),
        ),
    )


def _other(view: TrialView) -> OperatorGuidance:
    if view.reason:
        return _guidance(
            view,
            explanation="不明",
            owner=NextOwner.UNKNOWN,
            progress=ProgressOutlook.UNKNOWN,
        )
    return _guidance(
        view,
        explanation="試行の処理が進行中です",
        owner=NextOwner.ENGINE,
        progress=ProgressOutlook.IN_PROGRESS,
    )

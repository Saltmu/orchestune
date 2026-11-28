"""親 Issue の読み取り専用ステータス集計。

子 Issue、稼働中セッション、dispatch レポート、オープンな統合試行をまとめる。
CLI の引数解析と画面ループは持たない。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from orchestune.claim.workspace import resolve_relative_to
from orchestune.config_contracts import ConfigError
from orchestune.forge import Forge
from orchestune.infra.cli_guidance import render_guidance
from orchestune.infra.repository_config import find_and_load_config_file
from orchestune.integrator.attempt import OpenAttempt, find_open_attempts
from orchestune.issue_parsing import ChildDiscoveryResult, find_children_by_parent
from orchestune.labels import StatusLabel
from orchestune.ledger.run_state import load_run_state_readonly
from orchestune.operator_guidance import (
    EvidenceState,
    GuidanceAction,
    NextOwner,
    OperatorGuidance,
    ProgressOutlook,
)
from orchestune.review.semantic_records import (
    ACTIVE_PENDING_PHASES,
    TERMINAL_PASSED_PHASES,
    SemanticReviewPhase,
)
from orchestune.status_snapshot import (
    MonitorState,
    WorktreeStatus,
    build_status_snapshot,
    format_parent_integration,
    guidance_from_status,
    observe_repository_id,
)

_DEFAULT_REPORT_DIR = ".orchestune/reports/dispatch"
_CONFIG_NOTE = "設定ファイルを読めないため既定のレポートディレクトリを使います"
_PATH_NOTE = "report_dir の値がパスではないため既定のレポートディレクトリを使います"
_REPORT_MISSING = "dispatch レポート未検出"
_CHILD_UNKNOWN = "子 Issue を取得できないため、所属は不明です"
_CHILD_STALE = "子 Issue の観測は期限切れのキャッシュです"
_ATTEMPT_UNKNOWN = "オープンな統合試行を取得できません"
_ATTEMPT_STALE = "統合試行の観測は期限切れのキャッシュです"
_RUN_STATE_UNKNOWN = "run_state を読めないため稼働中セッションを判定できません"
_CURRENT_NOTE = "現在の観測です"
_T = TypeVar("_T")


@dataclass(frozen=True)
class ReportDirResolution:
    """解決したレポートディレクトリと、既定へ戻した理由。"""

    path: Path
    note: str | None


@dataclass(frozen=True)
class ParentStatusRequest:
    """1回の親ステータス観測に必要な入力。"""

    parent_issue: int
    repo_root: Path
    run_state_path: Path
    log_dir: Path
    now: float
    report_dir: str | Path | None = None
    tail_lines: int = 3
    repository_id: str | None = None

    def __post_init__(self) -> None:
        _require_parent(self.parent_issue)
        object.__setattr__(self, "repo_root", Path(self.repo_root))
        object.__setattr__(self, "run_state_path", Path(self.run_state_path))
        object.__setattr__(self, "log_dir", Path(self.log_dir))


@dataclass(frozen=True)
class ChildRollup:
    """子 Issue の状態ラベルと OPEN/CLOSED の件数。"""

    by_status: tuple[tuple[str, int], ...]
    other: int
    open_count: int
    closed_count: int
    evidence: EvidenceState
    note: str | None


@dataclass(frozen=True)
class SessionRollup:
    """親に属する稼働中ワークツリーと、親外の件数。"""

    worktrees: tuple[WorktreeStatus, ...]
    outside_active_count: int | None
    membership_known: bool
    evidence: EvidenceState
    note: str | None


@dataclass(frozen=True)
class IntegrationRollup:
    """最新の dispatch レポートから作った統合テキスト。"""

    text: str
    path: Path | None
    notes: tuple[str, ...]
    review_gate_blocked: bool


@dataclass(frozen=True)
class AttemptView:
    """オープンな統合試行の表示用要約。"""

    pr_number: int
    phase: str | None
    state_error: str | None
    escalation_reason: str | None
    needs_attention: bool


@dataclass(frozen=True)
class AttemptRollup:
    """オープンな統合試行と、その観測の新しさ。"""

    views: tuple[AttemptView, ...]
    evidence: EvidenceState
    note: str | None


@dataclass(frozen=True)
class ParentStatusSnapshot:
    """CLI がプレーンテキストにも Rich にも描ける親ステータス。"""

    parent_issue: int
    now: float
    children: ChildRollup
    sessions: SessionRollup
    integration: IntegrationRollup
    attempts: AttemptRollup
    next_actions: OperatorGuidance
    report_dir: Path
    report_dir_note: str | None


def _require_parent(number: object) -> int:
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise ValueError("parent_issue must be a positive integer")
    return number


def _positive_ttl(ttl_seconds: object) -> float:
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int | float):
        raise ValueError("ttl_seconds must be a positive number")
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be a positive number")
    return float(ttl_seconds)


class ParentObservationCache:
    """子 Issue と統合試行を、既定 30 秒の TTL のあいだ再取得しない。"""

    def __init__(self, ttl_seconds: float = 30.0) -> None:
        self._ttl = _positive_ttl(ttl_seconds)
        self._children: tuple[float, ChildDiscoveryResult] | None = None
        self._attempts: tuple[float, tuple[OpenAttempt, ...]] | None = None

    def load_children(
        self, forge: Forge, parent_issue: int, now: float
    ) -> tuple[ChildDiscoveryResult | None, EvidenceState]:
        def remember(slot: tuple[float, ChildDiscoveryResult]) -> None:
            self._children = slot

        return self._load(
            self._children,
            lambda: find_children_by_parent(forge, parent_issue),
            now,
            remember,
        )

    def load_attempts(
        self, forge: Forge, parent_issue: int, now: float
    ) -> tuple[tuple[OpenAttempt, ...] | None, EvidenceState]:
        def remember(slot: tuple[float, tuple[OpenAttempt, ...]]) -> None:
            self._attempts = slot

        def fetch() -> tuple[OpenAttempt, ...]:
            return tuple(find_open_attempts(forge, parent_issue))

        return self._load(self._attempts, fetch, now, remember)

    def _load(
        self,
        slot: tuple[float, _T] | None,
        fetch: Callable[[], _T],
        now: float,
        remember: Callable[[tuple[float, _T]], None],
    ) -> tuple[_T | None, EvidenceState]:
        if slot is not None and now - slot[0] < self._ttl:
            return slot[1], EvidenceState.CURRENT
        try:
            value = fetch()
        except Exception:  # noqa: BLE001 - boundary: a read-only snapshot keeps stale or unavailable evidence
            if slot is None:
                return None, EvidenceState.UNAVAILABLE
            return slot[1], EvidenceState.STALE
        remember((now, value))
        return value, EvidenceState.CURRENT


def resolve_report_dir(
    repo_root: Path, report_dir: str | Path | None = None
) -> ReportDirResolution:
    """明示パス、設定、既定の順でレポートディレクトリを決める。"""
    root = Path(repo_root)
    if report_dir is not None:
        return ReportDirResolution(_resolved(root, report_dir), None)
    try:
        config = find_and_load_config_file(root)
    except ConfigError:
        return ReportDirResolution(_resolved(root, None), _CONFIG_NOTE)
    value, present = _configured_report_dir(config)
    if not present or value is None:
        return ReportDirResolution(_resolved(root, None), None)
    if isinstance(value, str) and value.strip():
        return ReportDirResolution(_resolved(root, value), None)
    if isinstance(value, Path):
        return ReportDirResolution(_resolved(root, value), None)
    return ReportDirResolution(_resolved(root, None), _PATH_NOTE)


def _resolved(root: Path, value: str | Path | None) -> Path:
    return resolve_relative_to(root, value, _DEFAULT_REPORT_DIR).resolve()


def _configured_report_dir(config: dict[str, object]) -> tuple[object, bool]:
    if "report_dir" in config:
        return config["report_dir"], True
    if "report-dir" in config:
        return config["report-dir"], True
    return None, False


def discover_latest_dispatch_report(report_dir: Path, parent_issue: int) -> Path | None:
    """`<report_dir>/parent-<N>/*/result.json` のうち最新の実ファイルを返す。"""
    parent_dir = Path(report_dir) / f"parent-{parent_issue}"
    if parent_dir.is_symlink() or not parent_dir.is_dir():
        return None
    candidates = _report_candidates(parent_dir)
    if not candidates:
        return None
    return max(candidates, key=_report_sort_key) / "result.json"


def _report_candidates(parent_dir: Path) -> list[Path] | None:
    try:
        return [path for path in parent_dir.iterdir() if _is_report_dir(path)]
    except OSError:
        return None


def _is_report_dir(path: Path) -> bool:
    if path.is_symlink() or not path.is_dir():
        return False
    result = path / "result.json"
    return not result.is_symlink() and result.is_file()


def _report_sort_key(path: Path) -> tuple[str, int]:
    try:
        modified = path.stat().st_mtime_ns
    except OSError:
        modified = -1
    return (path.name, modified)


def build_parent_status_snapshot(
    request: ParentStatusRequest,
    forge: Forge,
    *,
    cache: ParentObservationCache | None = None,
    label_cache: dict[int, tuple[float, tuple[str, ...]]] | None = None,
) -> ParentStatusSnapshot:
    """親ダッシュボード 1 画面分の観測を組み立てる。読み取りに失敗しても例外にしない。"""
    observation_cache = cache if cache is not None else ParentObservationCache()
    labels: dict[int, tuple[float, tuple[str, ...]]] = (
        {} if label_cache is None else label_cache
    )
    report_dir = resolve_report_dir(request.repo_root, request.report_dir)
    children, child_evidence = observation_cache.load_children(
        forge, request.parent_issue, request.now
    )
    attempts, attempt_evidence = observation_cache.load_attempts(
        forge, request.parent_issue, request.now
    )
    _prefill_labels(children, labels, request.now)
    sessions = _sessions(request, forge, children, labels)
    child_rollup = _child_rollup(children, child_evidence)
    attempt_rollup = _attempt_rollup(attempts, attempt_evidence)
    integration = _integration(request, forge, report_dir)
    guidance = _guidance(
        request.parent_issue,
        children,
        child_rollup,
        sessions,
        attempt_rollup,
        integration,
    )
    return _snapshot(
        request,
        report_dir,
        child_rollup,
        sessions,
        integration,
        attempt_rollup,
        guidance,
    )


def _snapshot(
    request: ParentStatusRequest,
    report_dir: ReportDirResolution,
    children: ChildRollup,
    sessions: SessionRollup,
    integration: IntegrationRollup,
    attempts: AttemptRollup,
    guidance: OperatorGuidance,
) -> ParentStatusSnapshot:
    return ParentStatusSnapshot(
        parent_issue=request.parent_issue,
        now=request.now,
        children=children,
        sessions=sessions,
        integration=integration,
        attempts=attempts,
        next_actions=guidance,
        report_dir=report_dir.path,
        report_dir_note=report_dir.note,
    )


def _prefill_labels(
    children: ChildDiscoveryResult | None,
    label_cache: dict[int, tuple[float, tuple[str, ...]]],
    now: float,
) -> None:
    if children is None:
        return
    for issue in children.issues:
        label_cache[issue.number] = (now, issue.labels)


def _sessions(
    request: ParentStatusRequest,
    forge: Forge,
    children: ChildDiscoveryResult | None,
    label_cache: dict[int, tuple[float, tuple[str, ...]]],
) -> SessionRollup:
    if children is None:
        return _unknown_sessions(_CHILD_UNKNOWN)
    if not _ledger_readable(request.run_state_path):
        return _unknown_sessions(_RUN_STATE_UNKNOWN)
    try:
        snapshot = build_status_snapshot(
            request.run_state_path,
            request.log_dir,
            request.now,
            tail_lines=request.tail_lines,
            label_cache=label_cache,
            forge=forge,
        )
    except (OSError, ValueError, UnicodeError):
        return _unknown_sessions(_RUN_STATE_UNKNOWN)
    return _known_sessions(snapshot.worktrees, children)


def _ledger_readable(path: Path) -> bool:
    try:
        load_run_state_readonly(path)
    except Exception:  # noqa: BLE001 - boundary: an unreadable ledger stays unknown and is not quarantined
        return False
    return True


def _unknown_sessions(note: str) -> SessionRollup:
    return SessionRollup((), None, False, EvidenceState.UNAVAILABLE, note)


def _known_sessions(
    worktrees: list[WorktreeStatus], children: ChildDiscoveryResult
) -> SessionRollup:
    numbers = {issue.number for issue in children.issues}
    parent = tuple(item for item in worktrees if item.issue_number in numbers)
    return SessionRollup(
        parent, len(worktrees) - len(parent), True, EvidenceState.CURRENT, None
    )


def _child_rollup(
    children: ChildDiscoveryResult | None, evidence: EvidenceState
) -> ChildRollup:
    if children is None:
        return ChildRollup((), 0, 0, 0, evidence, _CHILD_UNKNOWN)
    counts = {label.value: 0 for label in StatusLabel}
    other = 0
    open_count = 0
    closed_count = 0
    for issue in children.issues:
        if issue.state.upper() == "CLOSED":
            closed_count += 1
        else:
            open_count += 1
        other += _count_labels(issue.labels, counts)
    visible = tuple((name, count) for name, count in counts.items() if count)
    return ChildRollup(
        visible, other, open_count, closed_count, evidence, _child_note(evidence)
    )


def _count_labels(labels: tuple[str, ...], counts: dict[str, int]) -> int:
    other = 0
    for label in labels:
        if label in counts:
            counts[label] += 1
        elif label.startswith("status:"):
            other += 1
    return other


def _child_note(evidence: EvidenceState) -> str | None:
    if evidence is EvidenceState.STALE:
        return _CHILD_STALE
    return None


def _attempt_rollup(
    attempts: tuple[OpenAttempt, ...] | None, evidence: EvidenceState
) -> AttemptRollup:
    if attempts is None:
        return AttemptRollup((), evidence, _ATTEMPT_UNKNOWN)
    views = tuple(
        sorted((_view(item) for item in attempts), key=lambda item: item.pr_number)
    )
    note = _ATTEMPT_STALE if evidence is EvidenceState.STALE else None
    return AttemptRollup(views, evidence, note)


def _view(attempt: OpenAttempt) -> AttemptView:
    state = attempt.state
    phase = None if state is None else state.phase.value
    reason = None if state is None else state.escalation_reason
    return AttemptView(
        attempt.pr_number,
        phase,
        attempt.state_error,
        reason,
        _needs_attention(attempt),
    )


def _needs_attention(attempt: OpenAttempt) -> bool:
    if attempt.state_error or attempt.state is None:
        return True
    phase = attempt.state.phase
    if phase in TERMINAL_PASSED_PHASES:
        return False
    if phase in ACTIVE_PENDING_PHASES or phase is SemanticReviewPhase.ESCALATED:
        return True
    return bool(attempt.state.escalation_reason)


def _integration(
    request: ParentStatusRequest, forge: Forge, report: ReportDirResolution
) -> IntegrationRollup:
    path = discover_latest_dispatch_report(report.path, request.parent_issue)
    notes = [report.note] if report.note else []
    if path is None:
        notes.append(_REPORT_MISSING)
        return IntegrationRollup(
            _REPORT_MISSING, None, tuple(dict.fromkeys(notes)), False
        )
    payload, blocked = _read_report(path)
    text = format_parent_integration(
        payload,
        parent_issue=request.parent_issue,
        repository_id=_repository_id(request, forge),
        evidence_path=path.name,
    )
    return IntegrationRollup(text, path, tuple(notes), blocked)


def _repository_id(request: ParentStatusRequest, forge: Forge) -> str | None:
    if request.repository_id is not None:
        return request.repository_id
    return observe_repository_id(request.repo_root, forge)


def _read_report(path: Path) -> tuple[object, bool]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, False
    return payload, _review_gate_blocked(payload)


def _review_gate_blocked(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    record = payload.get("parent_integration")
    return isinstance(record, dict) and record.get("status") == "review_gate_blocked"


def _guidance(
    parent: int,
    children: ChildDiscoveryResult | None,
    child_rollup: ChildRollup,
    sessions: SessionRollup,
    attempts: AttemptRollup,
    integration: IntegrationRollup,
) -> OperatorGuidance:
    actions = _action_list(parent, children, sessions, attempts, integration)
    evidence = _guidance_evidence(child_rollup, attempts, sessions)
    owner, progress, explanation = _outlook(
        bool(actions), evidence, _parent_running(sessions)
    )
    return OperatorGuidance(
        target=f"parent issue #{parent}",
        stage="parent-status",
        reason_codes=tuple(_reason_for(action) for action in actions),
        explanation=explanation,
        next_owner=owner,
        progress=progress,
        progress_condition=None,
        actions=actions,
        references=(),
        evidence_state=evidence,
        evidence_note=_guidance_note(child_rollup, attempts, sessions),
    )


def _action_list(
    parent: int,
    children: ChildDiscoveryResult | None,
    sessions: SessionRollup,
    attempts: AttemptRollup,
    integration: IntegrationRollup,
) -> tuple[GuidanceAction, ...]:
    actions: list[GuidanceAction] = []
    blocked = _blocked_action(parent, children)
    if blocked is not None:
        actions.append(blocked)
    actions.extend(_review_actions(attempts))
    actions.extend(_recover_actions(sessions))
    gate = _gate_action(integration)
    if gate is not None:
        actions.append(gate)
    dispatch = _dispatch_action(parent, children, sessions)
    if dispatch is not None:
        actions.append(dispatch)
    return tuple(actions)


def _blocked_action(
    parent: int, children: ChildDiscoveryResult | None
) -> GuidanceAction | None:
    label = StatusLabel.BLOCKED_HUMAN_REVIEW.value
    if children is None or not any(label in issue.labels for issue in children.issues):
        return None
    return GuidanceAction(
        description=f"親 #{parent} の blocked-human-review を確認してください。",
        argv=("orchestune", "blocked", "-p", str(parent), "--detail"),
    )


def _review_actions(attempts: AttemptRollup) -> tuple[GuidanceAction, ...]:
    return tuple(
        GuidanceAction(
            description=f"PR #{view.pr_number} のレビュー状態を確認してください。",
            argv=("orchestune", "review", "status", "--pr", str(view.pr_number)),
        )
        for view in attempts.views
        if view.needs_attention
    )


def _recover_actions(sessions: SessionRollup) -> tuple[GuidanceAction, ...]:
    if not sessions.membership_known:
        return ()
    numbers = sorted(
        {
            item.issue_number
            for item in sessions.worktrees
            if item.state is MonitorState.PROCESS_EXITED
        }
    )
    return tuple(_recover_action(number) for number in numbers)


def _recover_action(number: int) -> GuidanceAction:
    return GuidanceAction(
        description=f"Issue #{number} の停止したセッションを確認してください。",
        argv=("orchestune", "recover", "--issue", str(number)),
    )


def _gate_action(integration: IntegrationRollup) -> GuidanceAction | None:
    if not integration.review_gate_blocked:
        return None
    return GuidanceAction(description="統合レポートのレビュー証跡を確認してください。")


def _dispatch_action(
    parent: int,
    children: ChildDiscoveryResult | None,
    sessions: SessionRollup,
) -> GuidanceAction | None:
    if children is None or not sessions.membership_known or _parent_running(sessions):
        return None
    if not _open_queued(children):
        return None
    return GuidanceAction(
        description=f"親 #{parent} の queued な子 Issue を dispatch してください。",
        argv=("orchestune", "dispatch", "-p", str(parent)),
    )


def _open_queued(children: ChildDiscoveryResult) -> bool:
    queued = StatusLabel.QUEUED.value
    return any(
        issue.state.upper() != "CLOSED" and queued in issue.labels
        for issue in children.issues
    )


def _parent_running(sessions: SessionRollup) -> bool:
    if not sessions.membership_known:
        return False
    return any(_running(item) for item in sessions.worktrees)


def _running(item: WorktreeStatus) -> bool:
    return item.alive is True or item.state in {
        MonitorState.RUNNING,
        MonitorState.EXTERNAL,
    }


def _guidance_evidence(
    children: ChildRollup, attempts: AttemptRollup, sessions: SessionRollup
) -> EvidenceState:
    evidence = _worst(children.evidence, attempts.evidence)
    if sessions.membership_known:
        return evidence
    return _worst(evidence, EvidenceState.UNAVAILABLE)


def _worst(left: EvidenceState, right: EvidenceState) -> EvidenceState:
    rank = {
        EvidenceState.CURRENT: 0,
        EvidenceState.STALE: 1,
        EvidenceState.UNAVAILABLE: 2,
    }
    return left if rank[left] >= rank[right] else right


def _outlook(
    has_actions: bool, evidence: EvidenceState, running: bool
) -> tuple[NextOwner, ProgressOutlook, str]:
    if has_actions:
        return (
            NextOwner.USER,
            ProgressOutlook.MANUAL,
            "観測された状態に対する次の操作です",
        )
    if evidence is EvidenceState.UNAVAILABLE:
        return (
            NextOwner.UNKNOWN,
            ProgressOutlook.UNKNOWN,
            "観測が不足しているため、操作の有無は断定しません",
        )
    if running:
        return (
            NextOwner.ENGINE,
            ProgressOutlook.IN_PROGRESS,
            "親の作業ツリーでセッションが稼働中です",
        )
    return (NextOwner.NONE, ProgressOutlook.TERMINAL, "今すぐ必要な操作はありません")


def _guidance_note(
    children: ChildRollup, attempts: AttemptRollup, sessions: SessionRollup
) -> str:
    parts: list[str] = []
    for part in (children.note, attempts.note, sessions.note):
        if part and part not in parts:
            parts.append(part)
    if parts:
        return "。".join(parts)
    return _CURRENT_NOTE


def _reason_for(action: GuidanceAction) -> str:
    argv = action.argv
    if argv is None:
        return "review-gate-blocked"
    if argv[1] == "blocked":
        return "blocked-human-review"
    if argv[1] == "review":
        return f"review-status:{argv[-1]}"
    if argv[1] == "recover":
        return f"process-exited:{argv[-1]}"
    return "queued"


def format_parent_status_report(snapshot: ParentStatusSnapshot, now: float) -> str:
    """プレーンテキストの親ステータス。Rich 表示も同じ観測を使う。"""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    lines = [
        f"Orchestune parent status (#{snapshot.parent_issue}, {timestamp})",
        "",
        "親 Issue サマリー",
        *_child_lines(snapshot.children),
        "",
        "稼働中セッション",
        *_session_lines(snapshot.sessions),
        "",
        "統合・レビュー状態",
        *_integration_lines(snapshot),
        "",
        "次の操作案内",
        render_guidance(snapshot.next_actions),
    ]
    return "\n".join(lines)


def _child_lines(children: ChildRollup) -> list[str]:
    if children.evidence is EvidenceState.UNAVAILABLE:
        return [f"  {children.note or '子 Issue の所属は不明です。'}"]
    lines = [f"  OPEN: {children.open_count} / CLOSED: {children.closed_count}"]
    lines.extend(f"  {name}: {count}" for name, count in children.by_status)
    if children.other:
        lines.append(f"  other: {children.other}")
    if children.note:
        lines.append(f"  注意: {children.note}")
    return lines


def _session_lines(sessions: SessionRollup) -> list[str]:
    if not sessions.membership_known:
        return [f"  {sessions.note or '稼働中セッションの所属は不明です。'}"]
    lines = _known_session_lines(sessions)
    if sessions.outside_active_count:
        lines.append(f"  親外の active worktree: {sessions.outside_active_count}")
    return lines


def _known_session_lines(sessions: SessionRollup) -> list[str]:
    if not sessions.worktrees:
        return ["  親に属する稼働中セッションはありません。"]
    lines: list[str] = []
    for status in sessions.worktrees:
        lines.extend(_worktree_lines(status))
    return lines


def _worktree_lines(status: WorktreeStatus) -> list[str]:
    lines = [
        f"  [{status.state.value}] Issue #{status.issue_number} - {status.branch}",
        f"    PID: {status.pid} / 経過時間: {_elapsed(status.elapsed_seconds)}",
        f"    worktree: {status.worktree_path}",
    ]
    if status.log_tail:
        lines.append("    ログ末尾:")
        lines.extend(f"      {line}" for line in status.log_tail)
    rendered = render_guidance(guidance_from_status(status))
    lines.extend(f"    {line}" for line in rendered.splitlines())
    return lines


def _elapsed(seconds: float | None) -> str:
    if seconds is None:
        return "不明"
    total_seconds = int(seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes > 0:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _integration_lines(snapshot: ParentStatusSnapshot) -> list[str]:
    integration = snapshot.integration
    lines = integration.text.splitlines() or [_REPORT_MISSING]
    lines.extend(_fresh_notes(integration.notes, integration.text))
    lines.extend(_attempt_lines(snapshot.attempts, integration.text))
    return [f"  {line}" for line in lines]


def _fresh_notes(notes: tuple[str, ...], text: str) -> list[str]:
    return [f"注意: {note}" for note in notes if note and note not in text]


def _attempt_lines(attempts: AttemptRollup, text: str) -> list[str]:
    if attempts.evidence is EvidenceState.UNAVAILABLE:
        note = attempts.note or "統合試行を取得できません"
        return [] if note in text else [note]
    lines = [_attempt_line(view) for view in attempts.views]
    if attempts.note and attempts.note not in text:
        lines.append(f"注意: {attempts.note}")
    return lines


def _attempt_line(view: AttemptView) -> str:
    phase = view.phase or "不明"
    if view.state_error:
        return f"PR #{view.pr_number}: {phase} / エラー: {view.state_error}"
    return f"PR #{view.pr_number}: {phase}"


def compact_parent_attention(snapshot: ParentStatusSnapshot) -> str:
    """監視 1 行。要対応の内訳と、詳細を開く入口だけを返す。"""
    pieces = [
        piece
        for code in snapshot.next_actions.reason_codes
        if (piece := _compact_piece(code))
    ]
    body = "、".join(pieces) if pieces else "なし"
    number = snapshot.parent_issue
    return f"親 #{number} / 要対応:{body}。詳細は orchestune status -p {number}"


def _compact_piece(code: str) -> str:
    fixed = {
        "blocked-human-review": "blocked-human-review",
        "review-gate-blocked": "証跡確認",
        "queued": "queued",
    }
    if code in fixed:
        return fixed[code]
    if code.startswith("review-status:"):
        return f"PR #{code.split(':', 1)[1]}"
    if code.startswith("process-exited:"):
        return f"停止 #{code.split(':', 1)[1]}"
    return ""

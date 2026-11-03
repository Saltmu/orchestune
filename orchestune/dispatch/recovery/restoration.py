"""Rebuild an ActiveWorktree from an Issue body and its open PR.

``recovery`` imports the two helpers its restoration projection calls.
This module does not import ``recovery``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from orchestune.branch_naming import build_task_branch_name
from orchestune.claim.ownership import with_claim
from orchestune.claim.preflight import resolve_claim_subtask_id
from orchestune.dependencies.resolution import (
    EMPTY_DEPENDENCIES,
    TaskDependencies,
)
from orchestune.dispatch.attempt_record import (
    LaunchAttempt,
    attempt_from_body,
)
from orchestune.dispatch.execution_profiles import (
    resolve_task_execution_selection,
)
from orchestune.dispatch.launch_attempts import (
    _completed_claim,
    active_from_attempt,
    restored_active_record,
)
from orchestune.dispatch.recovery.counters import merged_recovery_counters
from orchestune.issue_parsing import (
    FOOTPRINT_BLOCK_PATTERN,
    parse_task_from_issue,
)
from orchestune.labels import StatusLabel
from orchestune.ledger.run_state import (
    ActiveWorktree,
    recovered_claim_id,
    recovered_owner_token_digest,
    recovery_repository_id,
)
from orchestune.models import IssueRecord
from orchestune.task_branch_resolution import (
    BranchCapability,
    CanonicalBranchState,
    TaskBranchResolution,
    TaskBranchResolver,
    probe_canonical_state,
)
from orchestune.task_metadata import TaskMetadata

if TYPE_CHECKING:
    from orchestune.dispatch.config import DispatcherConfig

_YAML_SAFE_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _yaml_safe_load(stream: str) -> Any:
    """Parse YAML with LibYAML's CSafeLoader when available, else SafeLoader."""
    return yaml.load(stream, Loader=_YAML_SAFE_LOADER)


class ClaimInfo(tuple[str, str | None, str]):
    recompute_count: int
    forced_serial: bool

    def __new__(
        cls,
        owner: str,
        cid: str | None,
        rk: str,
        *,
        recompute_count: int = 0,
        forced_serial: bool = False,
    ) -> ClaimInfo:
        inst = super().__new__(cls, (owner, cid, rk))
        inst.recompute_count, inst.forced_serial = recompute_count, forced_serial
        return inst

    owner_kind = property(lambda self: self[0])
    claim_id = property(lambda self: self[1])
    reservation_kind = property(lambda self: self[2])


def _parse_claim_info_from_issue(issue: IssueRecord) -> ClaimInfo:
    """Issueの本文から owner_kind, claim_id, reservation_kind を抽出する。
    欠落時は (owner_kind='dispatch', claim_id=None, reservation_kind='footprint') を返す。
    """
    owner, cid, rk, rc, fs = "dispatch", None, "footprint", 0, False
    if match := FOOTPRINT_BLOCK_PATTERN.search(issue.body):
        try:
            d = _yaml_safe_load(match.group(1))
            if isinstance(d, dict):
                n = d.get("recompute_count")
                rc = (
                    n
                    if isinstance(n, int) and not isinstance(n, bool) and n >= 0
                    else 0
                )
                fs = d.get("forced_serial") is True
                if isinstance(k := d.get("owner_kind"), str) and k in {
                    "interactive",
                    "dispatch",
                }:
                    owner = k
                if isinstance(c := d.get("claim_id"), str) and c:
                    cid = c
                if isinstance(r := d.get("reservation_kind"), str) and r in {
                    "footprint",
                    "repository",
                }:
                    rk = r
        except Exception:
            pass
    return ClaimInfo(owner, cid, rk, recompute_count=rc, forced_serial=fs)


def _parse_subtask_info_from_issue(
    issue: IssueRecord,
    task: TaskMetadata | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Issueの本文から subtask_id と declared_footprint を抽出する。"""
    parsed = task if task is not None else parse_task_from_issue(issue)
    declared_footprint = () if parsed.footprint_error else parsed.footprint
    subtask_id = parsed.subtask_id
    if not subtask_id:
        owner_kind, _, _ = _parse_claim_info_from_issue(issue)
        subtask_id = (
            resolve_claim_subtask_id(issue)
            if owner_kind == "interactive"
            else f"issue-{issue.number}"
        )
    return subtask_id, declared_footprint


def _dependency_issue_numbers(
    issue: IssueRecord,
    dependency_resolution: dict[int, TaskDependencies],
) -> tuple[int, ...]:
    """自己修復に使う依存Issue番号を決定する。

    ネイティブ`blocked_by`は`issue`から直接、Issue番号のまま採用する
    （resolverの`.resolved`は経由しない）。startup recoveryの母集団
    （`_refresh_snapshot`）はin-progress/queued-attemptのIssueに限られる
    ため、既に`status:done`等で外れたblockerは`tasks_by_issue`に存在せず、
    resolverの`_resolve_native`はそれを（状態確認不能という別の理由で）
    未解決として扱う——しかしここでは単にPRのbase branchを探す手掛かり
    として使うだけなので、母集団に無くても番号として信頼してよい（旧実装が
    `issue.blocked_by`をそのまま使っていたのと同じ前提）。

    本文`depends_on`はEPICスコープの共通resolver（#799）で解決し、native
    に無いものだけ追加で合流する（旧実装は`blocked_by`があれば本文を一切
    見ない早期returnで、body側のambiguityを黙って落としていた——`resolved`
    は独立解決の結果を保つのでこの合流だけで両立する）。native優先の順序は
    `_restored_base_branch`のPR探索順として保たれる。
    """
    native = issue.blocked_by
    resolved = dependency_resolution.get(issue.number, EMPTY_DEPENDENCIES).resolved
    body_only = tuple(dep for dep in resolved if dep not in native)
    return native + body_only


def _restored_base_branch(
    issue: IssueRecord,
    issue_to_subtask_id: dict[int, str],
    dependency_resolution: dict[int, TaskDependencies],
    resolver: TaskBranchResolver,
    resolutions: dict[int, TaskBranchResolution],
    config: DispatcherConfig,
) -> str:
    """Issueの親・依存関係から自己修復時のbase branchを決定する。

    依存Issue番号は解決順（ネイティブ優先）で1件ずつ試し、最初に一致した
    open PRのhead_refを採用する。この順序が「ネイティブ依存が本文depends_on
    より優先される」という既存契約を保つ——旧実装の「ネイティブがあれば
    本文を一切見ない」早期returnとは異なり、本文側の未解決診断も
    （呼出側の他用途のために）resolver内では失われない。
    """
    base_branch = "origin/main"
    if issue.parent and issue.parent.get("number") is not None:
        base_branch = f"parent/issue-{issue.parent['number']}"

    for dep_num in _dependency_issue_numbers(issue, dependency_resolution):
        subtask_id = issue_to_subtask_id.get(dep_num)
        if subtask_id is None:
            continue
        resolution = _resolve_recovery_branch(
            dep_num, subtask_id, resolver, resolutions, config
        )
        if resolution.allows(BranchCapability.FETCH_MERGE):
            return resolution.branch_name

    return base_branch


def _recovery_counters_for_issue(issue: IssueRecord) -> tuple[int, bool]:
    """#516レビュー指摘: Issue本文（Footprintフェンス）を第一のソースとしつつ、
    `status:force-serial`ラベルが付いているのに本文フィールドが無い/false
    のケースをラベル側の権威で補う。想定されるケースは2つ:
    (1) 本フィールド導入前からforced_serialだった移行時のIssue（本文に
        フィールドが無い）、(2) `_persist_recovery_counters`の本文書き込みは
        成功したがその後の`add_label`が失敗し、次のイベントまで本文が
        更新されないまま残ったケースの逆——ここでは扱わない（本文が
        `true`で確定していればそちらが優先される）。ラベルは`recompute_count`
        を持たないためforced_serial側のみのフォールバックとする。
    """
    return merged_recovery_counters(issue)


def _resolve_recovery_pr_and_branch(
    issue: IssueRecord,
    subtask_id: str,
    resolver: TaskBranchResolver,
    resolutions: dict[int, TaskBranchResolution],
    config: DispatcherConfig,
) -> tuple[str, str | None, str | None]:
    canonical = build_task_branch_name(issue.number, subtask_id)
    resolution = _resolve_recovery_branch(
        issue.number, subtask_id, resolver, resolutions, config
    )
    if resolution.allows(BranchCapability.LINK_PR) and resolution.pr is not None:
        return (
            resolution.branch_name,
            str(resolution.pr.number),
            f"PR#{resolution.pr.number}",
        )
    return canonical, None, None


def _resolve_recovery_branch(
    issue_number: int,
    subtask_id: str,
    resolver: TaskBranchResolver,
    resolutions: dict[int, TaskBranchResolution],
    config: DispatcherConfig,
) -> TaskBranchResolution:
    """Resolve once per Issue for one recovery pass, including its API probe."""
    cached = resolutions.get(issue_number)
    if cached is not None:
        return cached
    canonical = build_task_branch_name(issue_number, subtask_id)
    state = (
        probe_canonical_state(canonical, config.resolved_forge.branch_exists)
        if resolver.has_verified_candidate(issue_number, subtask_id)
        else CanonicalBranchState.INDETERMINATE
    )
    resolution = resolver.resolve(issue_number, subtask_id, state)
    resolutions[issue_number] = resolution
    return resolution


def _build_restored_from_attempt(
    issue: IssueRecord,
    attempt: LaunchAttempt,
    owner_kind: str,
    claim_id: str | None,
    reservation_kind: str,
    config: DispatcherConfig,
    task: TaskMetadata | None = None,
) -> ActiveWorktree:
    parsed = task if task is not None else parse_task_from_issue(issue)
    active = active_from_attempt(attempt, parsed, config)
    effective_claim_id = claim_id or recovered_claim_id(issue.number)
    return with_claim(
        active,
        _completed_claim(
            owner_kind=owner_kind,
            claim_id=effective_claim_id,
            base_ref=attempt.base_branch,
            reservation_kind=reservation_kind,
            repository_id=recovery_repository_id(config.run_state_path),
            claimed_at=attempt.started_at,
            owner_token_digest=recovered_owner_token_digest(issue.number, claim_id),
        ),
    )


def _restored_worktree_workspace(
    issue: IssueRecord,
    subtask_id: str,
    resolver: TaskBranchResolver,
    resolutions: dict[int, TaskBranchResolution],
    config: DispatcherConfig,
) -> tuple[str, str, str | None, str | None]:
    branch_name, external_id, external_url = _resolve_recovery_pr_and_branch(
        issue, subtask_id, resolver, resolutions, config
    )
    slug = branch_name.replace("/", "-")
    worktree_path = Path(config.worktree_root) / slug
    return branch_name, str(worktree_path), external_id, external_url


def _counters_from_claim_or_issue(
    issue: IssueRecord, claim_info: ClaimInfo | None
) -> tuple[int, bool]:
    if claim_info is not None:
        return (
            claim_info.recompute_count,
            claim_info.forced_serial or StatusLabel.FORCE_SERIAL in issue.labels,
        )
    return _recovery_counters_for_issue(issue)


def _build_restored_standard_worktree(
    issue: IssueRecord,
    subtask_id: str,
    declared_footprint: tuple[str, ...],
    resolver: TaskBranchResolver,
    resolutions: dict[int, TaskBranchResolution],
    issue_to_subtask_id: dict[int, str],
    dependency_resolution: dict[int, TaskDependencies],
    owner_kind: str,
    claim_id: str | None,
    reservation_kind: str,
    config: DispatcherConfig,
    task: TaskMetadata | None = None,
    claim_info: ClaimInfo | None = None,
) -> ActiveWorktree:
    if owner_kind == "interactive" and subtask_id == f"issue-{issue.number}":
        subtask_id = resolve_claim_subtask_id(issue)
    branch, worktree_path, external_id, external_url = _restored_worktree_workspace(
        issue, subtask_id, resolver, resolutions, config
    )
    restored_base = _restored_base_branch(
        issue, issue_to_subtask_id, dependency_resolution, resolver, resolutions, config
    )
    rc, fs = _counters_from_claim_or_issue(issue, claim_info)
    selection = resolve_task_execution_selection(
        task if task is not None else parse_task_from_issue(issue), config
    )
    return restored_active_record(
        issue_number=issue.number,
        branch=branch,
        worktree_path=worktree_path,
        footprint=declared_footprint,
        recompute_count=rc,
        forced_serial=fs,
        external_id=external_id,
        external_url=external_url,
        base_branch=restored_base,
        profile=selection.profile,
        model=selection.model,
        reasoning_effort=selection.reasoning_effort,
        selection_reason=selection.reason,
        owner_kind=owner_kind,
        claim_id=claim_id or recovered_claim_id(issue.number),
        reservation_kind=reservation_kind,
        repository_id=recovery_repository_id(config.run_state_path),
        owner_token_digest=recovered_owner_token_digest(issue.number, claim_id),
    )


def _build_restored_active_worktree(
    issue: IssueRecord,
    subtask_id: str,
    declared_footprint: tuple[str, ...],
    resolver: TaskBranchResolver,
    resolutions: dict[int, TaskBranchResolution],
    issue_to_subtask_id: dict[int, str],
    dependency_resolution: dict[int, TaskDependencies],
    config: DispatcherConfig,
    task: TaskMetadata | None = None,
) -> ActiveWorktree:
    claim_info = _parse_claim_info_from_issue(issue)
    owner_kind, claim_id, reservation_kind = claim_info
    parsed = task if task is not None else parse_task_from_issue(issue)
    if (
        parsed.footprint_error or parsed.yaml_error
    ) and reservation_kind == "footprint":
        reservation_kind = "repository"
        declared_footprint = ()
    attempt = attempt_from_body(issue.body)
    if (
        owner_kind != "interactive"
        and attempt is not None
        and attempt.phase == "launched"
    ):
        return _build_restored_from_attempt(
            issue,
            attempt,
            owner_kind,
            claim_id,
            reservation_kind,
            config,
            task=parsed,
        )
    return _build_restored_standard_worktree(
        issue,
        subtask_id,
        declared_footprint,
        resolver,
        resolutions,
        issue_to_subtask_id,
        dependency_resolution,
        owner_kind,
        claim_id,
        reservation_kind,
        config,
        task=parsed,
        claim_info=claim_info,
    )

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]

from orchestune import github
from orchestune.dispatch_gc import (
    _collect_zombies_and_timeouts,
    _finalize_completed_worktree,
    _finalize_not_needed_worktree,
    is_process_alive,
    remove_worktree,
    worktree_has_uncommitted_changes,
)
from orchestune.dispatch_locks import (
    ExternalLockScanResult,
    _strip_remote_prefix,
    check_footprint_deviation,
    scan_external_locks,
)
from orchestune.dispatch_rebase import (
    _handle_footprint_deviation,
    _try_auto_rebase,
    notify_force_serial,
    notify_recompute,
)
from orchestune.dispatch_scoring import (
    Task,
    compute_priority_score,
    parse_task_from_issue,
    quota_available,
    select_next_tasks,
)
from orchestune.dispatch_state import (
    ActiveWorktree,
    CompletedWorktree,
    RunState,
    load_run_state,
    save_run_state,
)
from orchestune.dispatch_targets import (
    ClaudeCodeCloudRoutineDispatchTarget,
    DispatchHandle,
    DispatchTarget,
    LocalProcessDispatchTarget,
    build_dispatch_target,
    default_dry_run_command_builder,
)
from orchestune.github import IssueRecord, PrRecord, _validate_ref_name

__all__ = [
    "ActiveWorktree",
    "CompletedWorktree",
    "DispatchHandle",
    "DispatchTarget",
    "ExternalLockScanResult",
    "LocalProcessDispatchTarget",
    "RunState",
    "Task",
    "build_dispatch_target",
    "check_footprint_deviation",
    "compute_priority_score",
    "default_dry_run_command_builder",
    "is_process_alive",
    "load_run_state",
    "notify_force_serial",
    "notify_recompute",
    "parse_task_from_issue",
    "quota_available",
    "remove_worktree",
    "save_run_state",
    "scan_external_locks",
    "select_next_tasks",
    "worktree_has_uncommitted_changes",
]


@dataclass
class LaunchResult:
    issue_number: int
    branch: str
    worktree_path: str
    pid: int | None
    launched: bool
    error_message: str | None = None
    external_id: str | None = None
    external_url: str | None = None


def _branch_exists(branch_name: str) -> bool:
    """指定されたブランチがローカルまたはリモート追跡ブランチとして存在するか確認する。"""
    res_local = subprocess.run(
        ["git", "show-ref", "--verify", f"refs/heads/{branch_name}"],
        capture_output=True,
    )
    if res_local.returncode == 0:
        return True

    res_remote = subprocess.run(
        ["git", "show-ref", "--verify", f"refs/remotes/origin/{branch_name}"],
        capture_output=True,
    )
    if res_remote.returncode == 0:
        return True

    return False


def create_worktree_and_launch(
    task: Task,
    branch_name: str,
    worktree_root: str | Path,
    dispatch_target: DispatchTarget,
    apply: bool,
    base_branch: str | None = None,
) -> LaunchResult:
    _validate_ref_name(branch_name)
    worktree_root = Path(worktree_root)
    slug = branch_name.replace("/", "-")
    worktree_path = worktree_root / slug

    pid: int | None = None
    external_id: str | None = None
    external_url: str | None = None
    launched = False
    error_message: str | None = None

    if apply:
        try:
            # 1. 無効なworktreeの整理
            subprocess.run(["git", "worktree", "prune"], capture_output=True, text=True)

            # 2. すでにディレクトリが存在する場合のクリーンアップ
            if worktree_path.exists():
                try:
                    shutil.rmtree(worktree_path)
                except Exception:
                    pass

            worktree_root.mkdir(parents=True, exist_ok=True)

            # 3. ブランチがすでに存在する場合はそのまま利用し、存在しない場合は新規作成する
            if _branch_exists(branch_name):
                cmd = ["git", "worktree", "add", str(worktree_path), branch_name]
            else:
                cmd = ["git", "worktree", "add", "-b", branch_name, str(worktree_path)]
                if base_branch:
                    cmd.append(base_branch)
            subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
            )
            handle = dispatch_target.launch(task, branch_name, worktree_path)
            pid = handle.pid
            external_id = handle.external_id
            external_url = handle.external_url
            launched = True
        except (subprocess.CalledProcessError, OSError) as e:
            error_details = ""
            if isinstance(e, subprocess.CalledProcessError):
                error_details = f" (stderr: {e.stderr.strip() if e.stderr else ''})"
            print(
                f"Error: Failed to create worktree or launch for issue #{task.issue_number}: {e}{error_details}",
                file=sys.stderr,
            )
            error_message = f"{e}{error_details}"

    return LaunchResult(
        issue_number=task.issue_number,
        branch=branch_name,
        worktree_path=str(worktree_path),
        pid=pid,
        launched=launched,
        error_message=error_message,
        external_id=external_id,
        external_url=external_url,
    )


@dataclass
class DispatcherConfig:
    max_concurrent: int = 2
    max_launches_per_window: int = 1
    window_seconds: int = 3600
    run_state_path: Path = Path("run_state.json")
    worktree_root: Path = Path("worktrees")
    log_dir: Path = Path("logs")
    events_log_path: Path = Path("events.jsonl")
    parent_issue_number: int | None = None
    apply: bool = False
    dispatch_target: DispatchTarget | None = None
    deviation_buffer_lines: int = 5
    max_recompute_retries: int = 2
    task_timeout_seconds: int = 0
    # #282: status:not-needed判定の独立検証レビュー（保留分）の永続化先。
    not_needed_review_state_path: Path = Path("not_needed_review_state.json")

    def __post_init__(self) -> None:
        if self.dispatch_target is None:
            self.dispatch_target = LocalProcessDispatchTarget(log_dir=self.log_dir)


@dataclass
class CycleReport:
    selected: list[Task]
    quota_slots_available: int
    lock_changes: dict[str, list[Task]]
    deviation_events: list[dict]
    completion_events: list[dict]
    promotion_events: list[dict]
    applied: bool


def build_event_log_entry(report: CycleReport, now: float) -> dict:
    """#239: KPI A1〜A4/C2/C3集計用に、1サイクル分のイベントをJSON Lines化する。"""
    return {
        "timestamp": now,
        "quota_slots_available": report.quota_slots_available,
        "selected": [
            {"issue_number": t.issue_number, "subtask_id": t.subtask_id}
            for t in report.selected
        ],
        "deviation_events": report.deviation_events,
        "completion_events": report.completion_events,
        "promotion_events": report.promotion_events,
    }


def append_event_log(entry: dict, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _process_active_worktrees(  # noqa: C901
    run_state: RunState,
    tasks_by_issue: dict[int, Task],
    issue_number_by_subtask_id: dict[str, int],
    ci_passed_pr_subtask_ids: set[str],
    changes_requested_subtask_ids: set[str],
    subtask_branch_map: dict[str, str],
    config: DispatcherConfig,
) -> tuple[list[dict], list[dict], bool, set[str]]:
    """#192/#193/#200: active worktreeごとの完了検知・footprint逸脱処理。

    完了と判定したエントリは（apply時）run_state.active_worktreesから
    除去してクオータを解放し、以後のfootprint逸脱チェックはスキップする。
    """
    completion_events: list[dict] = []
    deviation_events: list[dict] = []
    any_forced_serial = False
    completed_subtask_ids: set[str] = set()

    for key, active in list(run_state.active_worktrees.items()):
        active_task = tasks_by_issue.get(active.issue_number)

        if active_task is not None and "status:not-needed" in active_task.status_labels:
            # #280: セッションが「対応不要」と判断した場合、コミット・PRを作らない
            # ためclosingIssuesReferences等の完了シグナルが発生せず、
            # _is_worktree_complete()（PID/PR存在ベース）は永遠にFalseを返し続ける。
            # ラベル検知を最優先の完了シグナルとして扱い、下のstale判定より先に処理する。
            completion_event = _finalize_not_needed_worktree(
                active, active_task, config
            )
            completion_events.append(completion_event)
            # #282: 即時クローズ・検証レビューへの委譲のどちらの経路でも、
            # 対応不要の根拠自体は「mainに既に実装されている」ことなので、
            # Issueクローズの可否とは独立に依存関係は解決済みとして扱ってよい。
            if completion_event["action"] in (
                "not_needed",
                "not_needed_review_dispatched",
            ):
                if active_task.subtask_id:
                    completed_subtask_ids.add(active_task.subtask_id)
                if config.apply:
                    del run_state.active_worktrees[key]
            continue

        if (
            active_task is not None
            and "status:in-progress" not in active_task.status_labels
        ):
            # run_stateへの登録(save_run_state)は起動成功直後に、GitHubラベルの
            # status:in-progress付与はその後に行う順序になっているため、この間で
            # クラッシュした場合（あるいは完了/エスカレーション処理でラベルだけ
            # 先に更新されてクラッシュした場合）、GitHub側のラベルは
            # status:in-progressでなくなっているのにrun_state側にだけ古い
            # エントリが残ることがある。GitHubラベルを正として、この古い帳簿
            # エントリを破棄する（ゾンビGCの拡張）。
            completion_events.append(
                {
                    "issue_number": active.issue_number,
                    "subtask_id": active_task.subtask_id,
                    "action": "stale_active_entry_discarded",
                    "reason": (
                        "issue label is no longer status:in-progress "
                        f"(labels={sorted(active_task.status_labels)})"
                    ),
                }
            )
            if config.apply:
                del run_state.active_worktrees[key]
            continue

        if active.forced_serial:
            any_forced_serial = True

        if _is_worktree_complete(active, config):
            completion_event = _finalize_completed_worktree(active, active_task, config)
            completion_events.append(completion_event)
            if completion_event["action"] == "completed":
                if active_task is not None and active_task.subtask_id:
                    completed_subtask_ids.add(active_task.subtask_id)
                if config.apply:
                    run_state.completed_worktrees.append(
                        CompletedWorktree(
                            issue_number=active.issue_number,
                            subtask_id=active_task.subtask_id if active_task else "",
                            branch=active.branch,
                            started_at=active.started_at,
                            completed_at=time.time(),
                            recompute_count=active.recompute_count,
                            forced_serial=active.forced_serial,
                            commit_sha=completion_event.get("commit_sha"),
                        )
                    )
                    del run_state.active_worktrees[key]
                continue

        # 自動リベースや逸脱判定の前に、CHANGES_REQUESTED になった親を持つかチェックする (#185)
        if active_task and active_task.depends_on:
            if any(
                dep in changes_requested_subtask_ids for dep in active_task.depends_on
            ):
                if config.apply:
                    if active.pid:
                        try:
                            os.kill(active.pid, 9)
                        except OSError:
                            pass
                    github.remove_label(active.issue_number, "status:in-progress")
                    github.add_label(active.issue_number, "status:blocked-human-review")
                    github.add_comment(
                        active.issue_number,
                        "依存元PRが変更要求（Request Changes）を受けたため、スタックされたタスクを一時停止しました。",
                    )
                    del run_state.active_worktrees[key]
                completion_events.append(
                    {
                        "issue_number": active.issue_number,
                        "subtask_id": active_task.subtask_id,
                        "action": "escalated_due_to_changes_requested",
                    }
                )
                continue

        # 自動リベース判定＆実行 (#201)
        process_alive = is_process_alive(active.pid)
        if process_alive and _try_auto_rebase(
            active,
            active_task,
            key,
            run_state,
            ci_passed_pr_subtask_ids,
            subtask_branch_map,
            config,
        ):
            continue

        deviated = check_footprint_deviation(
            active.worktree_path,
            active.declared_footprint,
            base=active.base_branch,
            min_changed_lines=config.deviation_buffer_lines,
        )
        if not deviated:
            continue

        event = _handle_footprint_deviation(
            active, deviated, tasks_by_issue, issue_number_by_subtask_id, config
        )
        if event["action"] in ("forced_serial", "already_forced_serial"):
            any_forced_serial = True
        deviation_events.append(event)

    return completion_events, deviation_events, any_forced_serial, completed_subtask_ids


def _promote_blocked_tasks(
    blocked_issues: list[IssueRecord],
    done_issues: list[IssueRecord],
    completed_subtask_ids: set[str],
    tasks_by_issue: dict[int, Task],
    config: DispatcherConfig,
) -> list[dict]:
    """#193: 依存先が全て解決したstatus:blockedタスクをstatus:queuedへ昇格する。

    #280: `done_issues`には`status:done`と`status:not-needed`の両方を
    呼び出し側で合流させて渡すことで、対応不要と判定された依存先も
    「解決済み」として扱われる（このタスク自体は依存先の状態を区別しない）。
    """
    done_subtask_ids = {
        tasks_by_issue[issue.number].subtask_id
        for issue in done_issues
        if issue.number in tasks_by_issue and tasks_by_issue[issue.number].subtask_id
    } | completed_subtask_ids

    events: list[dict] = []
    for issue in blocked_issues:
        task = tasks_by_issue.get(issue.number)
        if task is None or not task.depends_on:
            continue
        if not all(dep in done_subtask_ids for dep in task.depends_on):
            continue
        if config.apply:
            github.remove_label(task.issue_number, "status:blocked")
            github.add_label(task.issue_number, "status:queued")
        events.append(
            {"issue_number": task.issue_number, "subtask_id": task.subtask_id}
        )
    return events


@contextlib.contextmanager
def file_lock(lock_path: Path) -> Iterator[None]:
    if fcntl is None:
        raise RuntimeError(
            "fcntl is not supported on this platform. File locking is required."
        )

    lock_fd = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = open(lock_path, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if lock_fd:
            lock_fd.close()
        raise RuntimeError(
            f"Another instance is already running (locked on {lock_path})"
        ) from None
    except Exception:
        if lock_fd:
            lock_fd.close()
        raise

    # #227: ロック取得成功後のbody実行は別のtry/finallyに分離する。
    # ロック取得(mkdir/open/flock)の例外処理と同じtry内でyieldしていると、
    # body側で発生した例外がこのgeneratorへ再スローされ、下のexcept Exceptionに
    # 捕捉されて再度yieldしてしまい、Pythonが
    # `RuntimeError: generator didn't stop after throw()` を送出して
    # 元の例外を握り潰してしまう（body側の例外はロック取得の失敗ではないため
    # ここで処理すべきではない）。
    try:
        yield
    finally:
        if lock_fd:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except Exception:
                pass
            lock_fd.close()


def _is_worktree_complete(active: ActiveWorktree, config: DispatcherConfig) -> bool:
    """#215: `external_id`が設定されている（ローカルpid以外でディスパッチされた）
    active worktreeは、設定されたdispatch_targetの`is_complete`に完了判定を委譲する。
    それ以外（従来通りのローカルsubprocess起動）は`is_process_alive`ベースのまま。"""
    if active.external_id is not None:
        handle = DispatchHandle(
            pid=active.pid,
            external_id=active.external_id,
            external_url=active.external_url,
            branch_name=active.branch,
            issue_number=active.issue_number,
        )
        assert config.dispatch_target is not None
        return config.dispatch_target.is_complete(handle)
    return not is_process_alive(active.pid)


def _parse_subtask_info_from_issue(
    issue: github.IssueRecord,
) -> tuple[str, tuple[str, ...]]:
    """Issueの本文から subtask_id と declared_footprint を抽出する。"""
    import yaml

    from orchestune.dispatch_scoring import _FOOTPRINT_BLOCK_PATTERN

    match = _FOOTPRINT_BLOCK_PATTERN.search(issue.body)
    subtask_id = None
    declared_footprint = ()
    if match:
        try:
            data = yaml.safe_load(match.group(1))
            if isinstance(data, dict):
                subtask_id = data.get("subtask_id")
                footprint = data.get("footprint", [])
                if isinstance(footprint, list):
                    declared_footprint = tuple(footprint)
        except Exception:
            pass

    if not subtask_id:
        subtask_id = f"issue-{issue.number}"

    return subtask_id, declared_footprint


def _restore_missing_active_worktrees(
    run_state: RunState,
    in_progress_issues: list[github.IssueRecord],
    config: DispatcherConfig,
) -> bool:
    """in-progressなIssueからActiveWorktreeを復元する。"""
    missing_issues = []
    for issue in in_progress_issues:
        subtask_id, declared_footprint = _parse_subtask_info_from_issue(issue)
        if str(issue.number) not in run_state.active_worktrees:
            missing_issues.append((issue, subtask_id, declared_footprint))

    if not missing_issues:
        return False

    print(
        f"Self-healing: Found {len(missing_issues)} active issues missing from run_state.",
        file=sys.stderr,
    )

    try:
        open_prs = github.list_open_prs()
    except Exception as e:
        print(
            f"Self-healing warning: Failed to list open PRs: {e}",
            file=sys.stderr,
        )
        open_prs = []

    for issue, subtask_id, declared_footprint in missing_issues:
        associated_pr = None
        for pr in open_prs:
            if issue.number in pr.closes_issue_numbers:
                associated_pr = pr
                break

        if associated_pr:
            branch_name = associated_pr.head_ref
            external_id = str(associated_pr.number)
            external_url = f"PR#{associated_pr.number}"
        else:
            branch_name = f"claude/issue-{issue.number}-{subtask_id}"
            external_id = None
            external_url = None

        slug = branch_name.replace("/", "-")
        worktree_path = Path(config.worktree_root) / slug

        started_at = time.time()
        try:
            from datetime import datetime

            dt = datetime.fromisoformat(issue.created_at.replace("Z", "+00:00"))
            started_at = dt.timestamp()
        except Exception:
            pass

        restored_base_branch = "origin/main"
        if config.parent_issue_number is not None:
            restored_base_branch = f"parent/issue-{config.parent_issue_number}"

        if issue.blocked_by:
            dep_pr = None
            for pr in open_prs:
                if any(
                    dep_num in pr.closes_issue_numbers for dep_num in issue.blocked_by
                ):
                    dep_pr = pr
                    break
            if dep_pr:
                restored_base_branch = dep_pr.head_ref

        run_state.active_worktrees[str(issue.number)] = ActiveWorktree(
            issue_number=issue.number,
            branch=branch_name,
            worktree_path=str(worktree_path),
            pid=None,
            started_at=started_at,
            declared_footprint=declared_footprint,
            recompute_count=0,
            forced_serial=False,
            external_id=external_id,
            external_url=external_url,
            base_branch=restored_base_branch,
        )
        print(
            f"Self-healing: Restored active worktree state for subtask '{subtask_id}' (Issue #{issue.number})",
            file=sys.stderr,
        )

    return True


def _warn_missing_physical_worktrees(run_state: RunState) -> None:
    """物理的な git worktree が存在しない場合に警告ログを出す。"""
    try:
        res = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
        existing_worktree_paths = set()
        for line in res.stdout.splitlines():
            if line.startswith("worktree "):
                existing_worktree_paths.add(Path(line.split(" ", 1)[1]).resolve())
    except Exception as e:
        print(
            f"Self-healing warning: Failed to list git worktrees: {e}",
            file=sys.stderr,
        )
        existing_worktree_paths = None

    if existing_worktree_paths is not None:
        for subtask_id, active in run_state.active_worktrees.items():
            active_path = Path(active.worktree_path).resolve()
            if active_path not in existing_worktree_paths:
                print(
                    f"Self-healing warning: Physical worktree for subtask '{subtask_id}' not found at '{active.worktree_path}'.",
                    file=sys.stderr,
                )


def recover_run_state(
    run_state: RunState,
    in_progress_issues: list[github.IssueRecord],
    config: DispatcherConfig,
) -> bool:
    """run_state.jsonが失われたり不整合が起きている場合に、GitHub API (in_progress_issues / open_prs)
    およびローカルの物理的な git worktree から RunState を自動復元する。
    """
    modified = _restore_missing_active_worktrees(run_state, in_progress_issues, config)
    _warn_missing_physical_worktrees(run_state)
    return modified


def run_dispatch_cycle(config: DispatcherConfig) -> CycleReport:  # noqa: C901
    lock_path = Path(config.run_state_path).with_suffix(".lock")
    with file_lock(lock_path):
        run_state = load_run_state(config.run_state_path)
        now = time.time()

        if config.parent_issue_number is not None and config.apply:
            github.ensure_parent_branch(config.parent_issue_number)

        queued_issues = github.list_issues_by_label("status:queued")
        locked_issues = github.list_issues_by_label("status:external-lock")
        in_progress_issues = github.list_issues_by_label("status:in-progress")
        blocked_issues = github.list_issues_by_label("status:blocked")
        # #236: 完了Issueは人間が通常のGitHub運用でCloseすることが多いため、
        # 依存解決判定はclosedなIssueも含めて検索する。
        done_issues = github.list_issues_by_label("status:done", state="all")
        # #280: セッションがstatus:not-neededを付与すると同時にstatus:in-progressを
        # 外すため、in_progress_issuesの一覧には現れなくなる。tasks_by_issueに含めて
        # おかないと_process_active_worktrees側で完了検知できず、依存解決からも
        # 漏れてしまう（closedなIssueもクローズ後の依存解決に必要なためstate="all"）。
        not_needed_issues = github.list_issues_by_label(
            "status:not-needed", state="all"
        )

        # 自己修復（ステート復元・不整合修復）
        # run_state.json が存在しない場合、かつ apply=True の場合のみ復元処理を実行する
        if config.apply and not Path(config.run_state_path).exists():
            if recover_run_state(run_state, in_progress_issues, config):
                save_run_state(run_state, config.run_state_path)

        # parent_issue_number が指定されている場合、親Issueが一致する子Issueのみにフィルタリングする
        if config.parent_issue_number is not None:
            queued_issues = [
                i
                for i in queued_issues
                if i.parent and i.parent.get("number") == config.parent_issue_number
            ]
            locked_issues = [
                i
                for i in locked_issues
                if i.parent and i.parent.get("number") == config.parent_issue_number
            ]
            in_progress_issues = [
                i
                for i in in_progress_issues
                if i.parent and i.parent.get("number") == config.parent_issue_number
            ]
            blocked_issues = [
                i
                for i in blocked_issues
                if i.parent and i.parent.get("number") == config.parent_issue_number
            ]
            done_issues = [
                i
                for i in done_issues
                if i.parent and i.parent.get("number") == config.parent_issue_number
            ]
            not_needed_issues = [
                i
                for i in not_needed_issues
                if i.parent and i.parent.get("number") == config.parent_issue_number
            ]

        all_issues = [
            *queued_issues,
            *locked_issues,
            *in_progress_issues,
            *blocked_issues,
            *done_issues,
            *not_needed_issues,
        ]

        import yaml

        from orchestune.dispatch_scoring import _FOOTPRINT_BLOCK_PATTERN

        issue_to_subtask_id = {}
        for issue in all_issues:
            match = _FOOTPRINT_BLOCK_PATTERN.search(issue.body)
            if match:
                try:
                    data = yaml.safe_load(match.group(1))
                    if isinstance(data, dict):
                        sub_id = data.get("subtask_id")
                        if sub_id:
                            issue_to_subtask_id[issue.number] = str(sub_id)
                except Exception:
                    pass

        tasks_by_issue = {
            issue.number: parse_task_from_issue(issue, issue_to_subtask_id)
            for issue in all_issues
        }
        issue_number_by_subtask_id = {
            task.subtask_id: task.issue_number
            for task in tasks_by_issue.values()
            if task.subtask_id
        }

        prs = github.list_open_prs()

        done_subtask_ids = {
            task.subtask_id
            for task in tasks_by_issue.values()
            if "status:done" in task.status_labels and task.subtask_id
        }

        pr_by_branch = {pr.head_ref: pr for pr in prs}
        ci_passed_pr_subtask_ids = set()
        changes_requested_subtask_ids = set()
        subtask_branch_map = {}

        for task in tasks_by_issue.values():
            if not task.subtask_id:
                continue
            branch_name = f"claude/issue-{task.issue_number}-{task.subtask_id}"
            subtask_branch_map[task.subtask_id] = branch_name

            pr = pr_by_branch.get(branch_name)
            if pr:
                if pr.review_decision == "CHANGES_REQUESTED":
                    changes_requested_subtask_ids.add(task.subtask_id)
                elif pr.is_ci_passing:
                    ci_passed_pr_subtask_ids.add(task.subtask_id)

        (
            completion_events,
            deviation_events,
            any_forced_serial,
            completed_subtask_ids,
        ) = _process_active_worktrees(
            run_state,
            tasks_by_issue,
            issue_number_by_subtask_id,
            ci_passed_pr_subtask_ids,
            changes_requested_subtask_ids,
            subtask_branch_map,
            config,
        )

        gc_events = _collect_zombies_and_timeouts(
            run_state,
            tasks_by_issue,
            config,
        )
        completion_events.extend(gc_events)

        promotion_events = _promote_blocked_tasks(
            blocked_issues,
            done_issues + not_needed_issues,
            completed_subtask_ids,
            tasks_by_issue,
            config,
        )

        lock_result = _sync_external_locks(tasks_by_issue, prs, run_state, config)

        newly_locked = {t.issue_number for t in lock_result.to_lock}
        queued_candidates = [
            tasks_by_issue[issue.number]
            for issue in queued_issues
            if issue.number not in newly_locked
        ]

        stack_eligible_tasks, task_to_base_branch = _get_stack_eligible_tasks(
            blocked_issues,
            tasks_by_issue,
            done_subtask_ids,
            ci_passed_pr_subtask_ids,
            subtask_branch_map,
        )

        candidate_tasks = queued_candidates + stack_eligible_tasks

        # 重複起動の防止: 既にオープンなPRが存在するcandidate_tasksを検知し、
        # 起動対象から除外して status:blocked-human-review へ移行させる。
        valid_candidate_tasks = []
        for task in candidate_tasks:
            expected_branch = f"claude/issue-{task.issue_number}-{task.subtask_id}"

            existing_pr = pr_by_branch.get(expected_branch)
            if not existing_pr:
                for pr in prs:
                    if task.issue_number in pr.closes_issue_numbers:
                        existing_pr = pr
                        break

            is_duplicate = False
            if existing_pr:
                # 重複起動をスキップする条件の判定
                last_completed = None
                for cw in reversed(run_state.completed_worktrees):
                    if cw.issue_number == task.issue_number:
                        last_completed = cw
                        break

                if not last_completed:
                    # 過去の完了履歴がないのにPRが存在する場合は、人間が作成したとみなしてスキップ
                    is_duplicate = True
                else:
                    # リモートブランチの最新コミットSHAを取得
                    remote_sha = None
                    ls_remote_failed = False
                    try:
                        ref_name = f"refs/heads/{existing_pr.head_ref}"
                        res = subprocess.run(
                            ["git", "ls-remote", "origin", ref_name],
                            capture_output=True,
                            text=True,
                            check=True,
                        )
                        output = res.stdout.strip()
                        if output:
                            remote_sha = output.split()[0]
                    except Exception:
                        ls_remote_failed = True

                    # 履歴のSHAとリモートのSHAが両方取得でき、かつそれらが異なる場合のみ重複（人間介入）と判定。
                    # ただし、ls-remoteが例外等で失敗した場合は、安全のため重複とみなして起動をスキップする。
                    if ls_remote_failed:
                        is_duplicate = True
                    elif last_completed.commit_sha and remote_sha:
                        if last_completed.commit_sha != remote_sha:
                            is_duplicate = True

            if is_duplicate and existing_pr:
                print(
                    f"Skipping task {task.subtask_id} (Issue #{task.issue_number}) because an open PR #{existing_pr.number} already exists on branch '{existing_pr.head_ref}' and has been updated.",
                    file=sys.stderr,
                )
                if config.apply:
                    if "status:queued" in task.status_labels:
                        github.remove_label(task.issue_number, "status:queued")
                    if "status:blocked" in task.status_labels:
                        github.remove_label(task.issue_number, "status:blocked")
                    github.add_label(task.issue_number, "status:blocked-human-review")
                    github.add_comment(
                        task.issue_number,
                        f"重複起動防止: このサブタスクに対応するオープンなPR #{existing_pr.number} (ブランチ: `{existing_pr.head_ref}`) が既に検出され、更新されています。\n"
                        f"重複したエージェントセッションの起動を防ぐため、自動起動をスキップし、ステータスを `status:blocked-human-review` に変更しました。\n"
                        f"必要に応じて手動でPRをマージするか、再起動したい場合は既存のPRをクローズした上で再度 `status:queued` に設定してください。",
                    )
            else:
                valid_candidate_tasks.append(task)

        candidate_tasks = valid_candidate_tasks

        if any_forced_serial:
            quota_slots = 0
            selected: list[Task] = []
        else:
            quota_slots = quota_available(
                run_state,
                now,
                config.max_concurrent,
                config.max_launches_per_window,
                config.window_seconds,
            )
            selected = select_next_tasks(
                candidate_tasks,
                run_state,
                now,
                config.max_concurrent,
                config.max_launches_per_window,
                config.window_seconds,
            )

        if config.apply:
            selected = _launch_selected_tasks(
                selected,
                task_to_base_branch,
                candidate_tasks,
                run_state,
                now,
                config,
            )

        report = CycleReport(
            selected=selected,
            quota_slots_available=quota_slots,
            lock_changes={
                "to_lock": lock_result.to_lock,
                "to_unlock": lock_result.to_unlock,
            },
            deviation_events=deviation_events,
            completion_events=completion_events,
            promotion_events=promotion_events,
            applied=config.apply,
        )

        if config.apply:
            append_event_log(build_event_log_entry(report, now), config.events_log_path)

        return report


def write_github_step_summary(
    cycle_report: CycleReport | None,
    integrator_report: dict | None,
    summary_path: str,
) -> None:
    lines = ["## 🤖 Orchestune Dispatch Summary\n"]

    if integrator_report:
        lines.append("### 🔍 仮マージ検証（Integrator）結果")
        status = integrator_report.get("status", "unknown")
        lines.append(f"全体ステータス: **{status}**\n")

        merged = integrator_report.get("merged", [])
        failed = integrator_report.get("failed", [])
        failed_reasons = integrator_report.get("failed_reasons", {})
        integration_pr_number = integrator_report.get("integration_pr_number")

        if not merged and not failed:
            lines.append("検証対象の完了タスク（`status:done`）はありませんでした。\n")
        else:
            lines.append("| サブタスクID | 結果 | 詳細 / 理由 |")
            lines.append("| --- | --- | --- |")
            for task_id in merged:
                lines.append(
                    f"| `{task_id}` | ✅ 成功 | 仮マージCI通過またはマージ済みスキップ |"
                )
            for task_id in failed:
                reason = failed_reasons.get(task_id, "不明なエラー")
                reason_short = reason.split("\n")[0]
                lines.append(f"| `{task_id}` | ❌ 失敗 | {reason_short} |")
            lines.append("")

        # Integratorの仕事は統合PRの作成までで、最終マージは常に人間が行うため、
        # そのPRへのリンクをサマリー上で必ず可視化する（run #68のように、成功していても
        # 誰にも気づかれず放置されるのを防ぐ）。
        if integration_pr_number:
            repo_slug = os.environ.get("GITHUB_REPOSITORY")
            pr_ref = (
                f"https://github.com/{repo_slug}/pull/{integration_pr_number}"
                if repo_slug
                else f"#{integration_pr_number}"
            )
            lines.append(
                f"➡️ **統合PR #{integration_pr_number}** が作成/検出されました。"
                f"最終マージには人間によるレビューが必要です: {pr_ref}\n"
            )

    if cycle_report:
        lines.append("### 🚀 新規起動タスク")
        if not cycle_report.selected:
            lines.append("今回新たに起動されたタスクはありません。\n")
        else:
            lines.append("| サブタスクID | Issue番号 | 優先度 |")
            lines.append("| --- | --- | --- |")
            for task in cycle_report.selected:
                lines.append(
                    f"| `{task.subtask_id}` | #{task.issue_number} | {task.priority} |"
                )
            lines.append("")

        lines.append("### 🔒 外部ロック（External Lock）変更")
        to_lock = cycle_report.lock_changes.get("to_lock", [])
        to_unlock = cycle_report.lock_changes.get("to_unlock", [])

        if not to_lock and not to_unlock:
            lines.append("外部ロックの変更はありませんでした。\n")
        else:
            lines.append("| サブタスクID | Issue番号 | アクション |")
            lines.append("| --- | --- | --- |")
            for task in to_lock:
                lines.append(
                    f"| `{task.subtask_id}` | #{task.issue_number} | 🔒 ロック付与 (`status:external-lock`) |"
                )
            for task in to_unlock:
                lines.append(
                    f"| `{task.subtask_id}` | #{task.issue_number} | 🔓 ロック解除 |"
                )
            lines.append("")

    try:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        print(f"Warning: Failed to write to GITHUB_STEP_SUMMARY: {e}", file=sys.stderr)


def _sync_external_locks(
    tasks_by_issue: dict[int, Task],
    prs: list[PrRecord],
    run_state: RunState,
    config: DispatcherConfig,
) -> ExternalLockScanResult:
    remote_branch_names = github.list_remote_branches()
    active_branches = [aw.branch for aw in run_state.active_worktrees.values()]
    pr_head_refs = {pr.head_ref for pr in prs}
    bare_branches = [
        b
        for b in remote_branch_names
        if _strip_remote_prefix(b) not in pr_head_refs
        and _strip_remote_prefix(b) not in active_branches
    ]
    remote_branch_footprints = [
        (
            _strip_remote_prefix(branch),
            tuple(github.branch_changed_files(branch)),
        )
        for branch in bare_branches
    ]

    all_tasks = list(tasks_by_issue.values())
    lock_result = scan_external_locks(
        all_tasks, remote_branch_footprints, prs, active_branches
    )

    if config.apply:
        for task in lock_result.to_lock:
            github.add_label(task.issue_number, "status:external-lock")
        for task in lock_result.to_unlock:
            github.remove_label(task.issue_number, "status:external-lock")
            if "status:done" not in task.status_labels:
                github.add_label(task.issue_number, "status:queued")

    return lock_result


def _get_stack_eligible_tasks(
    blocked_issues: list[IssueRecord],
    tasks_by_issue: dict[int, Task],
    done_subtask_ids: set[str],
    ci_passed_pr_subtask_ids: set[str],
    subtask_branch_map: dict[str, str],
) -> tuple[list[Task], dict[int, str]]:
    stack_eligible_tasks = []
    task_to_base_branch = {}

    for issue in blocked_issues:
        task = parse_task_from_issue(issue)
        if not task.subtask_id or not task.depends_on:
            continue

        all_resolved_or_stackable = True
        stackable_deps = []
        for dep in task.depends_on:
            if dep in done_subtask_ids:
                continue
            elif dep in ci_passed_pr_subtask_ids:
                dep_task = None
                for t in tasks_by_issue.values():
                    if t.subtask_id == dep:
                        dep_task = t
                        break
                if dep_task:
                    if not all(
                        grand_dep in done_subtask_ids
                        for grand_dep in dep_task.depends_on
                    ):
                        all_resolved_or_stackable = False
                        break
                stackable_deps.append(dep)
            else:
                all_resolved_or_stackable = False
                break

        # スタッキング可能な未マージ依存先が「ちょうど1つ」の場合のみスタッキング起動を許可する
        # （複数ある場合は、両方の変更をベースブランチとして同時に取り込めないためマージされるまでブロックする）
        if all_resolved_or_stackable and len(stackable_deps) == 1:
            stack_eligible_tasks.append(task)
            dep = stackable_deps[0]
            task_to_base_branch[task.issue_number] = subtask_branch_map[dep]

    return stack_eligible_tasks, task_to_base_branch


def _launch_selected_tasks(
    selected: list[Task],
    task_to_base_branch: dict[int, str],
    candidate_tasks: list[Task],
    run_state: RunState,
    now: float,
    config: DispatcherConfig,
) -> list[Task]:
    for task in candidate_tasks:
        if task.yaml_error:
            github.remove_label(task.issue_number, "status:queued")
            github.add_label(task.issue_number, "status:blocked")
            github.add_comment(
                task.issue_number,
                "YAMLのパースに失敗したため、タスクをブロックしました。フォーマットを確認してください。",
            )

    actually_selected = []
    for task in selected:
        branch_name = f"claude/issue-{task.issue_number}-{task.subtask_id or 'task'}"
        base_branch = task_to_base_branch.get(task.issue_number)
        if base_branch is None:
            if config.parent_issue_number is not None:
                base_branch_for_launch = f"parent/issue-{config.parent_issue_number}"
                base_branch_for_state = base_branch_for_launch
            else:
                base_branch_for_launch = None
                base_branch_for_state = "origin/main"
        else:
            base_branch_for_launch = base_branch
            base_branch_for_state = base_branch

        assert config.dispatch_target is not None
        launch = create_worktree_and_launch(
            task,
            branch_name,
            config.worktree_root,
            config.dispatch_target,
            apply=True,
            base_branch=base_branch_for_launch,
        )
        if not launch.launched:
            if "status:queued" in task.status_labels:
                github.remove_label(task.issue_number, "status:queued")
            if "status:blocked" in task.status_labels:
                github.remove_label(task.issue_number, "status:blocked")
            github.add_label(task.issue_number, "status:blocked")
            github.add_comment(
                task.issue_number,
                f"Git worktreeの作成またはエージェントの起動に失敗しました。\n"
                f"エラー内容:\n```\n{launch.error_message}\n```",
            )
            continue

        # run_stateへの登録・永続化を先に行い、GitHubラベルの更新は後で行う。
        # 起動(create_worktree_and_launch)は既に成功しているため、この順序なら
        # この後でクラッシュしても「run_stateには記録済みだがGitHubラベルは
        # まだstatus:queuedのまま」という、次回サイクルの冒頭でラベルを見て
        # 機械的に検出・破棄できる非対称にしかならない（逆順だと「GitHub側は
        # 確定・run_state側は空」という検出不能な非対称になってしまう）。
        run_state.active_worktrees[str(task.issue_number)] = ActiveWorktree(
            issue_number=task.issue_number,
            branch=branch_name,
            worktree_path=launch.worktree_path,
            pid=launch.pid,
            started_at=now,
            declared_footprint=task.footprint,
            external_id=launch.external_id,
            external_url=launch.external_url,
            base_branch=base_branch_for_state,
        )
        run_state.launch_history.append(now)
        save_run_state(run_state, config.run_state_path)

        if "status:queued" in task.status_labels:
            github.remove_label(task.issue_number, "status:queued")
        if "status:blocked" in task.status_labels:
            github.remove_label(task.issue_number, "status:blocked")
        github.add_label(task.issue_number, "status:in-progress")
        actually_selected.append(task)

    save_run_state(run_state, config.run_state_path)
    return actually_selected


def _report_to_dict(report: CycleReport) -> dict:
    return {
        "applied": report.applied,
        "quota_slots_available": report.quota_slots_available,
        "selected": [dataclasses.asdict(t) for t in report.selected],
        "lock_changes": {
            "to_lock": [dataclasses.asdict(t) for t in report.lock_changes["to_lock"]],
            "to_unlock": [
                dataclasses.asdict(t) for t in report.lock_changes["to_unlock"]
            ],
        },
        "deviation_events": report.deviation_events,
        "completion_events": report.completion_events,
        "promotion_events": report.promotion_events,
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="スケジューラ駆動ディスパッチャー: 1サイクル分の選出・dispatchを実行する"
        "（既定でラベル更新・worktree作成・エージェント起動まで行う。dry-runには--no-applyを指定）"
    )
    parser.add_argument(
        "--apply",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="実際にラベル更新・worktree作成・エージェント起動を行う（既定）。"
        "--no-applyでdry-run（何も変更しない）にできる。",
    )
    parser.add_argument("--max-concurrent", type=int, default=2)
    parser.add_argument("--max-launches-per-window", type=int, default=1)
    parser.add_argument("--window-seconds", type=int, default=3600)
    parser.add_argument("--run-state-path", type=Path, default=Path("run_state.json"))
    parser.add_argument("--worktree-root", type=Path, default=Path("worktrees"))
    parser.add_argument("--log-dir", type=Path, default=Path("logs"))
    parser.add_argument(
        "--events-log-path",
        type=Path,
        default=Path("events.jsonl"),
        help="#239: KPI集計用の構造化イベントログ（JSON Lines）の出力先",
    )
    parser.add_argument("--parent-issue", type=int, default=None)
    parser.add_argument(
        "--deviation-buffer-lines",
        type=int,
        default=5,
        help="footprint逸脱として扱わない変更行数の許容バッファ（#200: ライブロック防止）",
    )
    parser.add_argument(
        "--max-recompute-retries",
        type=int,
        default=2,
        help="DAG再計算のリトライ上限。超過時は強制直列化にフォールバックする（#200）",
    )
    parser.add_argument(
        "--dispatch-target",
        choices=["local", "cloud-routine"],
        default="local",
        help="#215: エージェントの実ディスパッチ先。'cloud-routine'はClaude Codeクラウド"
        "ルーチンのfire APIへディスパッチする（要 --routine-id/--routine-token または"
        "ORCHESTUNE_ROUTINE_ID/ORCHESTUNE_ROUTINE_TOKEN環境変数）",
    )
    parser.add_argument(
        "--local-cmd",
        default=None,
        help="ローカルのCLI（agyなど）にディスパッチする際のコマンドテンプレート。"
        "例: 'agy --issue {issue_number}' や 'agy'。"
        "使用可能な変数: {issue_number}, {subtask_id}, {branch_name}, {worktree_path}",
    )
    parser.add_argument(
        "--routine-id",
        default=None,
        help="#215: クラウドルーチンのID（未指定時はORCHESTUNE_ROUTINE_ID環境変数を使用）",
    )
    parser.add_argument(
        "--routine-token",
        default=None,
        help="#215: クラウドルーチンのAPIトークン（未指定時はORCHESTUNE_ROUTINE_TOKEN環境変数を使用）",
    )
    parser.add_argument(
        "--not-needed-review-state-path",
        type=Path,
        default=Path("not_needed_review_state.json"),
        help="#282: 保留中のstatus:not-needed検証レビュー（合否ポーリング・自動クローズ待ち）の永続化先",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    config = DispatcherConfig(
        max_concurrent=args.max_concurrent,
        max_launches_per_window=args.max_launches_per_window,
        window_seconds=args.window_seconds,
        run_state_path=args.run_state_path,
        worktree_root=args.worktree_root,
        log_dir=args.log_dir,
        events_log_path=args.events_log_path,
        parent_issue_number=args.parent_issue,
        apply=args.apply,
        dispatch_target=build_dispatch_target(
            args.dispatch_target,
            args.routine_id,
            args.routine_token,
            args.log_dir,
            local_cmd=args.local_cmd,
        ),
        deviation_buffer_lines=args.deviation_buffer_lines,
        max_recompute_retries=args.max_recompute_retries,
        not_needed_review_state_path=args.not_needed_review_state_path,
    )
    report = None
    integrator_run_report = None
    try:
        report = run_dispatch_cycle(config)
        print(json.dumps(_report_to_dict(report), ensure_ascii=False, indent=2))

        if config.apply:
            # 統合コーディネーターによる意味的レビュー（LLMによる統合PRのバグ検知、結果は
            # PRコメントのみで完結）と、#282のstatus:not-needed独立検証レビューの
            # 両方を、`ORCHESTUNE_SEMANTIC_REVIEW=0`でまとめて無効化できる。
            semantic_review_enabled = (
                os.environ.get("ORCHESTUNE_SEMANTIC_REVIEW", "1") != "0"
            )

            if semantic_review_enabled:
                # #282: status:not-needed判定の独立検証レビュー（保留分）のポーリング。
                try:
                    from orchestune.integration_coordinator import (
                        process_pending_not_needed_reviews,
                    )

                    not_needed_review_report = process_pending_not_needed_reviews(
                        args.not_needed_review_state_path
                    )
                    print("Pending Not-Needed Review Report:")
                    print(
                        json.dumps(
                            not_needed_review_report, ensure_ascii=False, indent=2
                        )
                    )
                except Exception as re:
                    print(
                        f"Warning: failed to process pending not-needed reviews: {re}",
                        file=sys.stderr,
                    )

            try:
                from orchestune.integrator import Integrator, IntegratorConfig

                integrator_config = IntegratorConfig(
                    parent_issue_number=config.parent_issue_number,
                    apply=config.apply,
                )
                # レビューはdispatcherと同一のクラウドルーチンを再利用して起動するため、
                # 実ディスパッチ先がクラウドルーチンのときのみ意味的レビューを有効化する。
                # レビューセッションは統合PRへコメントを残すのみで、自動マージ等の
                # 後続処理はPython側では一切行わない。
                if semantic_review_enabled and isinstance(
                    config.dispatch_target, ClaudeCodeCloudRoutineDispatchTarget
                ):
                    from orchestune.integration_coordinator import (
                        IntegrationCoordinator,
                    )

                    integrator_config.enable_semantic_review = True
                    integrator_config.coordinator = IntegrationCoordinator(
                        config.dispatch_target
                    )
                else:
                    integrator_config.enable_semantic_review = False
                integrator = Integrator(integrator_config)
                integrator_run_report = integrator.run()
                print("Integrator Report:")
                print(json.dumps(integrator_run_report, ensure_ascii=False, indent=2))
            except Exception as ie:
                print(f"Warning: Integrator failed to run: {ie}", file=sys.stderr)

        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            write_github_step_summary(
                cycle_report=report,
                integrator_report=integrator_run_report,
                summary_path=summary_path,
            )
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

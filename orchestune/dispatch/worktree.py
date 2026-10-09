from __future__ import annotations

import inspect
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from orchestune.dispatch import gc as dispatch_gc
from orchestune.dispatch.targets import (
    BranchReachabilityError,
    DispatchHandle,
    DispatchTarget,
)
from orchestune.infra.git_cli import resolve_local_or_remote_branch, run_git
from orchestune.infra.process_utils import file_lock as file_lock
from orchestune.task_metadata import TaskMetadata
from orchestune.worktree_ops.claim_marker import (
    claim_lock_path,
    claim_marker_path,
    read_claim_marker,
    remove_claim_marker,
    write_claim_marker,
)
from orchestune.worktree_ops.preparation import (
    WorktreePreparation,
)
from orchestune.worktree_ops.preparation import (
    _branch_exists as _common_branch_exists,
)
from orchestune.worktree_ops.preparation import (
    _create_worktree as _common_create_worktree,
)
from orchestune.worktree_ops.preparation import (
    _git_common_dir as _common_git_common_dir,
)
from orchestune.worktree_ops.preparation import (
    _git_toplevel as _common_git_toplevel,
)
from orchestune.worktree_ops.preparation import (
    _resolve_worktree_head_sha as _common_resolve_worktree_head_sha,
)
from orchestune.worktree_ops.preparation import (
    _resolve_worktree_path as _common_resolve_worktree_path,
)
from orchestune.worktree_ops.preparation import (
    _verify_worktree_identity as _common_verify_worktree_identity,
)
from orchestune.worktree_ops.preparation import (
    _worktree_checked_out_branch as _common_worktree_checked_out_branch,
)
from orchestune.worktree_ops.preparation import (
    prepare_task_worktree as _prepare_shared_task_worktree,
)

if TYPE_CHECKING:
    from orchestune.dispatch.execution_profiles import ExecutionSelection


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
    validation_error: bool = False
    # #262レビュー対応: worktreeのprune/backup/addにかかる時間を含めず、
    # 実際にdispatch_target.launch()を呼び出す直前の時刻。呼び出し元が
    # PR完了判定のstale境界（started_at）としてこれを使うことで、
    # worktree準備中に作成された無関係な既存PRを新sessionの成果物と
    # 誤認する窓を最小化する。launchが行われなかった場合はNone。
    dispatch_started_at: float | None = None
    execution_selection: ExecutionSelection | None = None
    launch_attempt_id: str | None = None
    # #943: dispatch launchがclaim_task経由になった際、共通着手サービスの結果
    # （実際のbase_ref/claim_id/reservation_kind）を運ぶ。dispatch以外の既存
    # 呼び出し元・テストは常にNoneのままで、既存挙動に影響しない。
    base_ref: str | None = None
    claim_id: str | None = None
    reservation_kind: str | None = None
    # #943レビュー対応(Codex P1): claim_task自体がholdされた（provider=agentは
    # 一度も呼ばれていない: STATE_LOCK_FAILEDや、ACTIVE_SAVED段階での
    # ラベル更新/所有権メタデータ公開失敗）ケースを表す。`launched=False`だが
    # `held=True`の場合、`_apply_single_task_launch`はlaunch_history（quota消費）
    # への計上も`_handle_launch_failure`によるエスカレーションも行わない
    # ——providerを一度も呼んでいない以上、quotaは消費していないため。
    held: bool = False
    # #1270: 起動attributionをhandleからledgerのLaunchInfoへ運ぶ。
    target_name: str | None = None
    log_path: str | None = None
    log_offset: int | None = None


def _branch_exists(branch_name: str, cwd: str | Path | None = None) -> bool:
    """指定されたブランチがローカルまたはリモート追跡ブランチとして存在するか確認する。

    #830: 本関数はテストから`patch("orchestune.dispatch.worktree._branch_exists")`
    で直接差し替えられることを許容された注入境界である（呼び出し側の分岐選択を
    検証するための正当な手段。`CONTRIBUTING.md`/`CONTRIBUTING.ja.md`の
    `autospec=True`解説サンプルとしても掲載済み）。local/remote判定ロジック
    自体の検証は、本関数が実際に経由する`run_git`境界のpatchのみで完結する
    `tests/test_dispatch_worktree.py::TestBranchExists`に一本化している。
    """
    return _common_branch_exists(branch_name, cwd=cwd, git_runner=run_git)


def _resolve_worktree_path(worktree_root: str | Path, branch_name: str) -> Path:
    """ブランチ名のバリデーションを行い、worktreeの対象パスを計算する。"""
    return _common_resolve_worktree_path(worktree_root, branch_name)


def _cleanup_existing_worktree(
    worktree_path: Path, issue_number: int, cwd: str | Path | None = None
) -> str | None:
    """すでにディレクトリが存在する場合、WIPコミットとして退避した上で削除する。
    退避に失敗した場合はエラー文字列を返し、削除を行わない（fail-closed）。"""
    if not worktree_path.exists():
        return None

    # #213: 未コミットの変更が残ったまま削除すると、前回のエージェント
    # 作業が黙って消失する。削除前にWIPコミットとして退避を試みる。
    # `backup_wip_commit`はfail-closed（dirty判定自体に失敗した場合も
    # 退避未完了扱い）なので、None以外が返れば削除せず起動を失敗させる。
    backup_error = dispatch_gc.backup_wip_commit(
        worktree_path,
        "WIP: backup by Orchestune before worktree recreation",
    )
    if backup_error is not None:
        return backup_error

    try:
        run_git(
            ["worktree", "remove", "--force", str(worktree_path)],
            cwd=cwd,
            check=False,
        )
        if worktree_path.exists():
            shutil.rmtree(worktree_path)
    except Exception:
        pass
    return None


def _create_worktree(
    worktree_path: Path,
    worktree_root: Path,
    branch_name: str,
    base_branch: str | None = None,
    cwd: str | Path | None = None,
) -> None:
    """無効なworktreeを整理し、指定のブランチ/ベースブランチでworktreeを作成する。"""
    _common_create_worktree(
        worktree_path,
        worktree_root,
        branch_name,
        base_branch,
        cwd=cwd,
        git_runner=run_git,
        branch_exists=_branch_exists,
        branch_resolver=resolve_local_or_remote_branch,
    )


_claim_marker_path = claim_marker_path
_read_claim_marker = read_claim_marker
_write_claim_marker = write_claim_marker
_remove_claim_marker = remove_claim_marker
_claim_lock_path = claim_lock_path


def clear_claim_ownership_marker(worktree_path: str | Path) -> None:
    """#943: worktree撤去時（GC完了処理）に、claim_task（`allow_force=False`の
    安全経路）が発行した所有権マーカーも一緒に取り除く公開ラッパー。

    マーカーはworktree本体のsibling（`claim_marker_path`参照）に置かれるため、
    `git worktree remove`ではworktree自体しか消えず、マーカーだけが残り続ける。
    残ったマーカーは以後の`claim_id`（呼び出しごとに新規生成）と一致しなくなり、
    完了・撤去済みのIssueを再度claimしようとした際に`claim_id_mismatch`として
    永久に拒否されてしまう——このシステムがdispatch起動をclaim_task経由に
    一本化したことで新たに露見した経路のため、撤去側でも対で片付ける。
    """
    remove_claim_marker(Path(worktree_path))


def _resolve_worktree_head_sha(worktree_path: Path) -> str:
    return _common_resolve_worktree_head_sha(worktree_path, git_runner=run_git)


def _target_supports_param(dispatch_target: DispatchTarget, param_name: str) -> bool:
    try:
        sig = inspect.signature(dispatch_target.launch)
        return param_name in sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
    except (ValueError, TypeError):
        return False


def _provision_and_launch(
    dispatch_target: DispatchTarget,
    task: TaskMetadata,
    branch_name: str,
    worktree_path: Path,
    *,
    force_push: bool = False,
    execution_selection: ExecutionSelection | None = None,
    base_branch: str | None = None,
) -> tuple[DispatchHandle, float]:
    """#262レビュー対応: dispatch_target.launch()直前の時刻をstarted_atとして取得し起動する。"""
    dispatch_started_at = time.time()
    kwargs: dict[str, Any] = {"force_push": force_push}
    if _target_supports_param(dispatch_target, "execution_selection"):
        kwargs["execution_selection"] = execution_selection
    if _target_supports_param(dispatch_target, "base_branch"):
        kwargs["base_branch"] = base_branch
    handle = dispatch_target.launch(task, branch_name, worktree_path, **kwargs)
    return handle, dispatch_started_at


def _cleanup_failed_worktree(worktree_path: Path) -> None:
    """launch失敗時の補償処理: 作成したworktreeをGit管理および物理ディスクから回収する。"""
    try:
        run_git(
            ["worktree", "remove", "--force", str(worktree_path)],
            cwd=None,
            check=False,
        )
        if worktree_path.exists():
            shutil.rmtree(worktree_path)
        run_git(["worktree", "prune"], cwd=None, check=False)
    except Exception:
        pass


def _handle_launch_error(
    e: Exception,
    task: TaskMetadata,
    branch_name: str,
    worktree_path: Path,
    worktree_created: bool,
    execution_selection: ExecutionSelection | None,
) -> LaunchResult:
    if worktree_created:
        _cleanup_failed_worktree(worktree_path)
    error_details = ""
    if isinstance(e, subprocess.CalledProcessError) and e.stderr:
        error_details = f" (stderr: {e.stderr.strip()})"
    print(
        f"Error: Failed to create worktree or launch for issue #{task.issue_number}: {e}{error_details}",
        file=sys.stderr,
    )
    return LaunchResult(
        issue_number=task.issue_number,
        branch=branch_name,
        worktree_path=str(worktree_path),
        pid=None,
        launched=False,
        error_message=f"{e}{error_details}",
        execution_selection=execution_selection,
    )


def _launch_on_prepared_worktree(
    task: TaskMetadata,
    branch_name: str,
    worktree_path: Path,
    dispatch_target: DispatchTarget,
    base_branch: str | None,
    execution_selection: ExecutionSelection | None = None,
) -> LaunchResult:
    """`prepare_task_worktree`が既に用意したworktreeへdispatch_targetを起動する。
    起動失敗時は、このworktreeを本呼び出しが用意したものとみなして補償削除する
    （#935: 作成自体は`prepare_task_worktree`側の責務に切り出し済み）。"""
    try:
        handle, dispatch_started_at = _provision_and_launch(
            dispatch_target,
            task,
            branch_name,
            worktree_path,
            execution_selection=execution_selection,
            base_branch=base_branch,
        )
        return LaunchResult(
            issue_number=task.issue_number,
            branch=branch_name,
            worktree_path=str(worktree_path),
            pid=handle.pid,
            launched=True,
            external_id=handle.external_id,
            external_url=handle.external_url,
            dispatch_started_at=dispatch_started_at,
            launch_attempt_id=handle.launch_attempt_id,
            execution_selection=execution_selection,
            target_name=handle.target_name,
            log_path=handle.log_path,
            log_offset=handle.log_offset,
        )
    except (subprocess.CalledProcessError, OSError, BranchReachabilityError) as e:
        return _handle_launch_error(
            e,
            task,
            branch_name,
            worktree_path,
            True,
            execution_selection,
        )


def _force_create_worktree(
    worktree_path: Path,
    worktree_root: Path,
    branch: str,
    base_branch: str | None,
    cwd: str | Path | None = None,
) -> tuple[str | None, bool]:
    """dirtyなら退避のうえ強制再作成する（既存セマンティクス）。
    戻り値は`(backup_error, branch_created)`。"""
    backup_error = _cleanup_existing_worktree(worktree_path, 0, cwd=cwd)
    if backup_error is not None:
        return backup_error, False
    branch_created = not _branch_exists(branch, cwd=cwd)
    _create_worktree(worktree_path, worktree_root, branch, base_branch, cwd=cwd)
    return None, branch_created


def _prepare_worktree_forced(
    worktree_path: Path,
    worktree_root: Path,
    branch: str,
    base_branch: str | None,
    cwd: str | Path | None = None,
) -> WorktreePreparation:
    """#935: 既存のforce cleanup経路（`create_worktree_and_launch`のdispatch専用
    互換パス）。所有権マーカーやbase_shaは記録しない: 既存テストの多くが
    `_create_worktree`自体をまるごとpatchして実worktreeを作らない前提のため、
    ここで追加のgit呼び出し（rev-parse等）を必須にするとそれらのテストダブルと
    衝突する。所有権追跡は`allow_force=False`の安全経路専用の機能とする。"""
    backup_error, branch_created = _force_create_worktree(
        worktree_path, worktree_root, branch, base_branch, cwd=cwd
    )
    if backup_error is not None:
        return WorktreePreparation(
            worktree_path=worktree_path,
            branch=branch,
            accepted=False,
            rejection_reason=backup_error,
        )
    # #935レビュー対応(P1): このpathに以前の安全経路が残した所有権マーカーが
    # 残っていると、強制再作成後もその旧claim_idが「一致する」と誤認され、
    # 元の所有者がresume/rollbackした際にdispatchが今使っているworktreeを
    # 削除してしまいかねない。forceで実際に再作成した場合は必ず無効化する。
    _remove_claim_marker(worktree_path)
    return WorktreePreparation(
        worktree_path=worktree_path,
        branch=branch,
        accepted=True,
        created=True,
        branch_created=branch_created,
    )


def _worktree_checked_out_branch(worktree_path: Path) -> str | None:
    """worktree_pathが実際にチェックアウトしているブランチ名を返す。
    gitワークツリーとして無効な場合はNoneを返す。"""
    return _common_worktree_checked_out_branch(worktree_path, git_runner=run_git)


def _git_common_dir(path: Path) -> str | None:
    return _common_git_common_dir(path, git_runner=run_git)


def _git_toplevel(path: Path) -> str | None:
    return _common_git_toplevel(path, git_runner=run_git)


def _verify_worktree_identity(
    worktree_path: Path, branch: str, repository_root: str | Path | None = None
) -> bool:
    """#935レビュー対応(P2, round2/3): `symbolic-ref`とbranch名だけでは、
    (1) 同名branchを持つ無関係な別リポジトリがstale markerと同じpathへ
    偶然存在するケースや、(2) このリポジトリの別checkoutの単なるサブ
    ディレクトリ（それ自身は独立したworktreeとして登録されていない）を
    誤って受理してしまうケースを見分けられない。共有git-dirが現在の
    リポジトリと一致すること（別リポジトリでない）に加え、`worktree_path`
    自身がそのcheckoutのtoplevel（＝それ自体が独立したworktree registration
    の根）であること（単なるサブディレクトリでない）も確認する。"""
    return _common_verify_worktree_identity(
        worktree_path, branch, repository_root=repository_root, git_runner=run_git
    )


def prepare_task_worktree(
    branch: str,
    worktree_root: str | Path,
    base_branch: str | None,
    claim_id: str,
    *,
    allow_force: bool = False,
    cwd: str | Path | None = None,
    trust_unclaimed_branch: bool = False,
) -> WorktreePreparation:
    """Preserve the dispatcher entry point and delegate safe preparation."""
    worktree_root_path = Path(worktree_root)
    worktree_path = _resolve_worktree_path(worktree_root_path, branch)
    if allow_force:
        with file_lock(_claim_lock_path(worktree_path)):
            return _prepare_worktree_forced(
                worktree_path, worktree_root_path, branch, base_branch, cwd=cwd
            )
    return _prepare_shared_task_worktree(
        branch,
        worktree_root_path,
        base_branch,
        claim_id,
        cwd=cwd,
        trust_unclaimed_branch=trust_unclaimed_branch,
    )


def _worktree_is_verified_clean(worktree_path: Path) -> bool:
    """#935レビュー対応(P1, round5): `dispatch_gc.worktree_has_uncommitted_changes`
    はGC用途上、status確認自体が失敗した場合にクオータ解放を優先してclean側に
    倒す設計（意図的な既存挙動）。rollbackの破壊的削除の可否判定としては
    安全方向が逆（確認できなければdirty扱いでblockすべき）なため、専用に
    fail-closedな確認を行う。"""
    try:
        result = run_git(["status", "--porcelain"], cwd=worktree_path, check=True)
    except (subprocess.CalledProcessError, OSError):
        return False
    return not result.stdout.strip()


def _rollback_blocking_reason_for_missing_worktree(
    preparation: WorktreePreparation,
) -> str | None:
    """#935レビュー対応(P1, round3): worktree本体は既に削除済み（branch削除
    だけが失敗して再試行されたケース）でも、branch自体が前回の確認以降に
    base_shaから進んでいないことは改めて確認する。worktree実体が無いことを
    理由に確認自体を省略すると、再試行のあいだにbranchが進んだ場合、その
    新しいコミットごと`git branch -D`で失ってしまう。"""
    try:
        # #935レビュー対応(P1, round4): 未修飾のrevisionはbranchと同名のtagが
        # あると曖昧になり、Gitはwarningを出すだけで成功してしまう。実際に
        # `git branch -D`が削除する参照（refs/heads/配下）を明示的に指定する。
        result = run_git(
            ["rev-parse", "--verify", f"refs/heads/{preparation.branch}"],
            cwd=None,
            check=False,
        )
    except OSError as e:
        return f"branch_sha_check_failed: {e}"
    if result.returncode != 0:
        return None  # branchが既に無ければ保護対象も無いので進めてよい
    if result.stdout.strip() != preparation.base_sha:
        return "advanced_beyond_base_sha"
    return None


def _rollback_blocking_reason(
    preparation: WorktreePreparation, claim_id: str
) -> str | None:
    """ロールバック実行可否を判定する。実行してよい場合のみNoneを返す。"""
    marker = _read_claim_marker(preparation.worktree_path)
    if marker is None or marker.get("claim_id") != claim_id:
        return "ownership_marker_missing_or_reassigned"
    if not preparation.worktree_path.exists():
        return _rollback_blocking_reason_for_missing_worktree(preparation)
    # #935レビュー対応(P2, round3): markerとdirty/SHAが一致していても、その
    # pathが実際にこのリポジトリの`branch`をチェックアウトした、それ自体が
    # 独立したworktreeであるとは限らない（手動削除後に別checkoutのクローンや
    # サブディレクトリが置かれた場合等）。削除前に必ず身元を確認する。
    if not _verify_worktree_identity(preparation.worktree_path, preparation.branch):
        return "stale_marker_unverified_worktree"
    if not _worktree_is_verified_clean(preparation.worktree_path):
        return "worktree_dirty"
    try:
        head_sha = _resolve_worktree_head_sha(preparation.worktree_path)
    except (subprocess.CalledProcessError, OSError) as e:
        return f"head_sha_check_failed: {e}"
    if head_sha != preparation.base_sha:
        return "advanced_beyond_base_sha"
    return None


def rollback_task_worktree(
    preparation: WorktreePreparation, claim_id: str
) -> str | None:
    """#935: `prepare_task_worktree`が新規作成したworktree/branchの後始末。

    このclaim_idが証明できなくなった（他の所有者に渡った）・dirty・base_shaから
    進んでいる、のいずれかに該当する場合は一切削除せず、診断文字列を返して
    復旧のため状態を保持する。すべて満たす場合のみNoneを返し実際に削除する。
    """
    if not preparation.accepted:
        return None
    if not preparation.created:
        return "reused_existing_worktree_not_rolled_back"

    # #935レビュー対応(P1): prepare_task_worktreeのforce奪取と同一のロックを
    # 取ることで、奪取直後の中間状態（新worktree作成済み・旧マーカー未削除）を
    # rollbackが観測して誤って削除することを防ぐ。
    with file_lock(_claim_lock_path(preparation.worktree_path)):
        blocking_reason = _rollback_blocking_reason(preparation, claim_id)
        if blocking_reason is not None:
            return blocking_reason

        # #935レビュー対応(P2): `_cleanup_failed_worktree`は削除失敗を握り潰す
        # fire-and-forgetのため、実際に消えたかどうかを自前で検証する。branchの
        # 削除も`check=False`の戻り値を無視せず確認する。いずれかが未完了なら
        # マーカーを残し、以後も所有権を保持したまま診断できるようにする
        # （中途半端に削除してmarkerだけ消すと、残ったpath/branchが以後
        # 「所有者不明」として拒否対象になり、復旧できなくなるため）。
        _cleanup_failed_worktree(preparation.worktree_path)
        if preparation.worktree_path.exists():
            return "worktree_removal_failed"
        if preparation.branch_created:
            branch_delete = run_git(
                ["branch", "-D", preparation.branch], cwd=None, check=False
            )
            # #935レビュー対応(P2, round5): branchが既に削除済み（前回試行が
            # worktree削除とbranch削除の両方を終えた後、marker削除の前に
            # クラッシュしたケース等）の場合、`git branch -D`は対象が無く
            # 失敗する。削除後もbranchが実在するかで真の失敗と区別しないと、
            # markerが永久に残り、以後このpathを誰も所有できなくなる。
            if branch_delete.returncode != 0 and _branch_exists(preparation.branch):
                return "branch_deletion_failed"
        _remove_claim_marker(preparation.worktree_path)
        return None


def _handle_backup_error(
    worktree_path: Path, issue_number: int, branch_name: str, backup_error: str | None
) -> LaunchResult:
    error_message = (
        f"Uncommitted changes in {worktree_path} could not be "
        f"backed up before recreation: {backup_error}"
    )
    print(
        f"Error: Failed to back up uncommitted changes for issue "
        f"#{issue_number} before recreation: {backup_error}",
        file=sys.stderr,
    )
    return LaunchResult(
        issue_number=issue_number,
        branch=branch_name,
        worktree_path=str(worktree_path),
        pid=None,
        launched=False,
        error_message=error_message,
    )


def _resolve_worktree_or_early_result(
    task: TaskMetadata,
    branch_name: str,
    worktree_root: str | Path,
    apply: bool,
    execution_selection: ExecutionSelection | None,
) -> tuple[Path | None, LaunchResult | None]:
    """ブランチ名検証とdry-run早期returnをまとめる。続行時は`(path, None)`、
    早期returnすべき場合は`(None, result)`を返す。"""
    try:
        worktree_path = _resolve_worktree_path(worktree_root, branch_name)
    except ValueError as e:
        print(
            f"Error: Invalid branch name {branch_name!r} for issue #{task.issue_number}: {e}",
            file=sys.stderr,
        )
        return None, LaunchResult(
            issue_number=task.issue_number,
            branch=branch_name,
            worktree_path="",
            pid=None,
            launched=False,
            error_message=str(e),
            validation_error=True,
            execution_selection=execution_selection,
        )

    if not apply:
        return None, LaunchResult(
            issue_number=task.issue_number,
            branch=branch_name,
            worktree_path=str(worktree_path),
            pid=None,
            launched=False,
            execution_selection=execution_selection,
        )

    return worktree_path, None


def _acquire_dispatch_worktree(
    task: TaskMetadata,
    branch_name: str,
    worktree_root: str | Path,
    worktree_path: Path,
    base_branch: str | None,
    execution_selection: ExecutionSelection | None,
) -> tuple[WorktreePreparation | None, LaunchResult | None]:
    """#935: `prepare_task_worktree`のforce互換経路を呼び出し、成功時は
    `(preparation, None)`、失敗時は`(None, result)`を返す。"""
    try:
        preparation = prepare_task_worktree(
            branch_name,
            worktree_root,
            base_branch,
            claim_id=f"dispatch:{task.issue_number}",
            allow_force=True,
        )
    except (subprocess.CalledProcessError, OSError) as e:
        return None, _handle_launch_error(
            e,
            task,
            branch_name,
            worktree_path,
            worktree_path.exists(),
            execution_selection,
        )

    if not preparation.accepted:
        return None, _handle_backup_error(
            worktree_path, task.issue_number, branch_name, preparation.rejection_reason
        )
    return preparation, None


def create_worktree_and_launch(
    task: TaskMetadata,
    branch_name: str,
    worktree_root: str | Path,
    dispatch_target: DispatchTarget,
    apply: bool,
    base_branch: str | None = None,
    execution_selection: ExecutionSelection | None = None,
) -> LaunchResult:
    worktree_path, early_result = _resolve_worktree_or_early_result(
        task, branch_name, worktree_root, apply, execution_selection
    )
    if early_result is not None:
        return early_result
    assert worktree_path is not None

    preparation, prep_error = _acquire_dispatch_worktree(
        task,
        branch_name,
        worktree_root,
        worktree_path,
        base_branch,
        execution_selection,
    )
    if prep_error is not None:
        return prep_error
    assert preparation is not None

    return _launch_on_prepared_worktree(
        task,
        branch_name,
        preparation.worktree_path,
        dispatch_target,
        base_branch,
        execution_selection=execution_selection,
    )

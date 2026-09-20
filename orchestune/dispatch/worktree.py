from __future__ import annotations

import inspect
import json
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
from orchestune.validation import validate_ref_name

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


@dataclass(frozen=True)
class WorktreePreparation:
    """`prepare_task_worktree`の結果。後段のロールバック可否判定に使う。"""

    worktree_path: Path
    branch: str
    accepted: bool
    created: bool = False
    branch_created: bool = False
    base_sha: str | None = None
    rejection_reason: str | None = None


def _branch_exists(branch_name: str) -> bool:
    """指定されたブランチがローカルまたはリモート追跡ブランチとして存在するか確認する。

    #830: 本関数はテストから`patch("orchestune.dispatch.worktree._branch_exists")`
    で直接差し替えられることを許容された注入境界である（呼び出し側の分岐選択を
    検証するための正当な手段。`CONTRIBUTING.md`/`CONTRIBUTING.ja.md`の
    `autospec=True`解説サンプルとしても掲載済み）。local/remote判定ロジック
    自体の検証は、本関数が実際に経由する`run_git`境界のpatchのみで完結する
    `tests/test_dispatch_worktree.py::TestBranchExists`に一本化している。
    """
    res_local = run_git(
        ["show-ref", "--verify", f"refs/heads/{branch_name}"], cwd=None, check=False
    )
    if res_local.returncode == 0:
        return True

    res_remote = run_git(
        ["show-ref", "--verify", f"refs/remotes/origin/{branch_name}"],
        cwd=None,
        check=False,
    )
    if res_remote.returncode == 0:
        return True

    return False


def _resolve_worktree_path(worktree_root: str | Path, branch_name: str) -> Path:
    """ブランチ名のバリデーションを行い、worktreeの対象パスを計算する。"""
    validate_ref_name(branch_name)
    slug = branch_name.replace("/", "-")
    return Path(worktree_root) / slug


def _cleanup_existing_worktree(worktree_path: Path, issue_number: int) -> str | None:
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
            cwd=None,
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
) -> None:
    """無効なworktreeを整理し、指定のブランチ/ベースブランチでworktreeを作成する。"""
    run_git(["worktree", "prune"], cwd=None, check=False)
    worktree_root.mkdir(parents=True, exist_ok=True)

    if _branch_exists(branch_name):
        cmd = ["worktree", "add", str(worktree_path), branch_name]
    else:
        cmd = ["worktree", "add", "-b", branch_name, str(worktree_path)]
        if base_branch:
            resolved_base = resolve_local_or_remote_branch(
                ".",
                base_branch,
                prefer_remote=base_branch.startswith("parent/"),
            )
            cmd.append(resolved_base)
    run_git(cmd, cwd=None, check=True)


def _claim_marker_path(worktree_path: Path) -> Path:
    """#935: worktree本体の外側（sibling）に置く所有権マーカーのパス。

    worktree内部に置くと`git status --porcelain`にuntrackedとして現れ、
    所有権確認用のファイル自体がdirty判定を汚染してしまうため、常に
    worktree_path.parent側に置く。"""
    return worktree_path.parent / f"{worktree_path.name}.claim.json"


def _read_claim_marker(worktree_path: Path) -> dict[str, Any] | None:
    try:
        raw = _claim_marker_path(worktree_path).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        marker: dict[str, Any] = json.loads(raw)
    except ValueError:
        return None
    return marker


def _write_claim_marker(
    worktree_path: Path,
    *,
    claim_id: str,
    branch: str,
    base_sha: str | None,
    branch_created: bool,
) -> None:
    marker_path = _claim_marker_path(worktree_path)
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        json.dumps(
            {
                "claim_id": claim_id,
                "branch": branch,
                "base_sha": base_sha,
                "branch_created": branch_created,
            }
        ),
        encoding="utf-8",
    )


def _remove_claim_marker(worktree_path: Path) -> None:
    _claim_marker_path(worktree_path).unlink(missing_ok=True)


def _claim_lock_path(worktree_path: Path) -> Path:
    """#935レビュー対応(P1): 同一branch/worktreeに対する`prepare_task_worktree`と
    `rollback_task_worktree`を相互排他にするロックファイル。forceによる奪取
    （worktree再作成→旧マーカー無効化）の途中状態を、並行するrollbackが
    「旧claim_idがまだ有効」として観測し、奪取直後のworktreeを削除してしまう
    TOCTOUを防ぐ。"""
    return _claim_marker_path(worktree_path).with_suffix(".lock")


def _resolve_worktree_head_sha(worktree_path: Path) -> str:
    return run_git(["rev-parse", "HEAD"], cwd=worktree_path, check=True).stdout.strip()


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
) -> tuple[str | None, bool]:
    """dirtyなら退避のうえ強制再作成する（既存セマンティクス）。
    戻り値は`(backup_error, branch_created)`。"""
    backup_error = _cleanup_existing_worktree(worktree_path, 0)
    if backup_error is not None:
        return backup_error, False
    branch_created = not _branch_exists(branch)
    _create_worktree(worktree_path, worktree_root, branch, base_branch)
    return None, branch_created


def _prepare_worktree_forced(
    worktree_path: Path,
    worktree_root: Path,
    branch: str,
    base_branch: str | None,
) -> WorktreePreparation:
    """#935: 既存のforce cleanup経路（`create_worktree_and_launch`のdispatch専用
    互換パス）。所有権マーカーやbase_shaは記録しない: 既存テストの多くが
    `_create_worktree`自体をまるごとpatchして実worktreeを作らない前提のため、
    ここで追加のgit呼び出し（rev-parse等）を必須にするとそれらのテストダブルと
    衝突する。所有権追跡は`allow_force=False`の安全経路専用の機能とする。"""
    backup_error, branch_created = _force_create_worktree(
        worktree_path, worktree_root, branch, base_branch
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


def _create_and_claim_worktree(
    worktree_path: Path,
    worktree_root: Path,
    branch: str,
    base_branch: str | None,
    claim_id: str,
) -> WorktreePreparation:
    """#935: 安全経路（`allow_force=False`）専用。新規worktreeを作成し、
    所有権マーカーとbase_shaを発行する。"""
    branch_created = not _branch_exists(branch)
    _create_worktree(worktree_path, worktree_root, branch, base_branch)
    base_sha = _resolve_worktree_head_sha(worktree_path)
    _write_claim_marker(
        worktree_path,
        claim_id=claim_id,
        branch=branch,
        base_sha=base_sha,
        branch_created=branch_created,
    )
    return WorktreePreparation(
        worktree_path=worktree_path,
        branch=branch,
        accepted=True,
        created=True,
        branch_created=branch_created,
        base_sha=base_sha,
    )


def _worktree_checked_out_branch(worktree_path: Path) -> str | None:
    """worktree_pathが実際にチェックアウトしているブランチ名を返す。
    gitワークツリーとして無効な場合はNoneを返す。"""
    try:
        result = run_git(
            ["symbolic-ref", "--short", "HEAD"], cwd=worktree_path, check=False
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _git_common_dir(path: Path) -> str | None:
    try:
        result = run_git(["rev-parse", "--git-common-dir"], cwd=path, check=False)
    except OSError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return str((path / result.stdout.strip()).resolve())


def _git_toplevel(path: Path) -> str | None:
    try:
        result = run_git(["rev-parse", "--show-toplevel"], cwd=path, check=False)
    except OSError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return str(Path(result.stdout.strip()).resolve())


def _verify_worktree_identity(worktree_path: Path, branch: str) -> bool:
    """#935レビュー対応(P2, round2/3): `symbolic-ref`とbranch名だけでは、
    (1) 同名branchを持つ無関係な別リポジトリがstale markerと同じpathへ
    偶然存在するケースや、(2) このリポジトリの別checkoutの単なるサブ
    ディレクトリ（それ自身は独立したworktreeとして登録されていない）を
    誤って受理してしまうケースを見分けられない。共有git-dirが現在の
    リポジトリと一致すること（別リポジトリでない）に加え、`worktree_path`
    自身がそのcheckoutのtoplevel（＝それ自体が独立したworktree registration
    の根）であること（単なるサブディレクトリでない）も確認する。"""
    if _worktree_checked_out_branch(worktree_path) != branch:
        return False
    if _git_toplevel(worktree_path) != str(worktree_path.resolve()):
        return False
    this_repo_git_dir = _git_common_dir(Path("."))
    return this_repo_git_dir is not None and this_repo_git_dir == _git_common_dir(
        worktree_path
    )


def _prepare_worktree_from_marker(
    worktree_path: Path,
    worktree_root: Path,
    branch: str,
    base_branch: str | None,
    marker: dict[str, Any],
) -> WorktreePreparation:
    """#935: claim_idが一致するマーカーが既にある場合の安全な再開経路。
    既存パスは無条件では削除も強制もしない。"""
    claim_id = marker["claim_id"]
    base_sha = marker.get("base_sha")
    if worktree_path.exists():
        # #935レビュー対応(P2): マーカーが一致していても、そのパスが実際に
        # このリポジトリの`branch`をチェックアウトした有効なgit worktreeで
        # ある保証はない（手動削除後に無関係/破損したディレクトリや、同名
        # branchを持つ別リポジトリが同じpathへ作られた場合等）。所有権が
        # 確認できないstaleなマーカーを信用して受理しない。
        if not _verify_worktree_identity(worktree_path, branch):
            return WorktreePreparation(
                worktree_path=worktree_path,
                branch=branch,
                accepted=False,
                rejection_reason="stale_marker_unverified_worktree",
            )
        return WorktreePreparation(
            worktree_path=worktree_path,
            branch=branch,
            accepted=True,
            created=False,
            base_sha=base_sha,
        )
    if not _branch_exists(branch):
        return _create_and_claim_worktree(
            worktree_path, worktree_root, branch, base_branch, claim_id
        )
    run_git(["worktree", "prune"], cwd=None, check=False)
    worktree_root.mkdir(parents=True, exist_ok=True)
    run_git(["worktree", "add", str(worktree_path), branch], cwd=None, check=True)
    _write_claim_marker(
        worktree_path,
        claim_id=claim_id,
        branch=branch,
        base_sha=base_sha,
        branch_created=False,
    )
    return WorktreePreparation(
        worktree_path=worktree_path,
        branch=branch,
        accepted=True,
        created=True,
        branch_created=False,
        base_sha=base_sha,
    )


def _prepare_worktree_unclaimed(
    worktree_path: Path,
    branch: str,
    worktree_root: Path,
    base_branch: str | None,
    claim_id: str,
) -> WorktreePreparation:
    """#935: 所有権マーカーが存在しない場合の経路。既存パス／既存ブランチのいずれかが
    残っていれば、所有権を証明できないため削除もforceも行わず拒否する。"""
    if worktree_path.exists():
        return WorktreePreparation(
            worktree_path=worktree_path,
            branch=branch,
            accepted=False,
            rejection_reason="unclaimed_existing_worktree",
        )
    if _branch_exists(branch):
        return WorktreePreparation(
            worktree_path=worktree_path,
            branch=branch,
            accepted=False,
            rejection_reason="unclaimed_existing_branch",
        )
    return _create_and_claim_worktree(
        worktree_path, worktree_root, branch, base_branch, claim_id
    )


def prepare_task_worktree(
    branch: str,
    worktree_root: str | Path,
    base_branch: str | None,
    claim_id: str,
    *,
    allow_force: bool = False,
) -> WorktreePreparation:
    """#935: 所有権を確認したうえで安全にworktreeを準備する。

    `allow_force=True`は既存dispatch経路専用の後方互換パスであり、所有権を
    問わず既存の force cleanup セマンティクスをそのまま実行する。
    `allow_force=False`（既定）では、claim_idが一致するマーカーがない既存の
    パス／ブランチを一切削除・強制せず拒否する。"""
    worktree_root_path = Path(worktree_root)
    worktree_path = _resolve_worktree_path(worktree_root_path, branch)

    # #935レビュー対応(P1): force奪取の「作成→旧マーカー無効化」と、安全経路の
    # 「マーカー確認→作成/再利用」を同一branchに対して相互排他にし、並行する
    # rollback_task_worktreeが奪取の中間状態を観測できないようにする。
    with file_lock(_claim_lock_path(worktree_path)):
        if allow_force:
            return _prepare_worktree_forced(
                worktree_path, worktree_root_path, branch, base_branch
            )

        marker = _read_claim_marker(worktree_path)
        if marker is not None:
            if marker.get("claim_id") != claim_id or marker.get("branch") != branch:
                return WorktreePreparation(
                    worktree_path=worktree_path,
                    branch=branch,
                    accepted=False,
                    rejection_reason="claim_id_mismatch",
                )
            return _prepare_worktree_from_marker(
                worktree_path, worktree_root_path, branch, base_branch, marker
            )

        return _prepare_worktree_unclaimed(
            worktree_path, branch, worktree_root_path, base_branch, claim_id
        )


def _rollback_blocking_reason_for_missing_worktree(
    preparation: WorktreePreparation,
) -> str | None:
    """#935レビュー対応(P1, round3): worktree本体は既に削除済み（branch削除
    だけが失敗して再試行されたケース）でも、branch自体が前回の確認以降に
    base_shaから進んでいないことは改めて確認する。worktree実体が無いことを
    理由に確認自体を省略すると、再試行のあいだにbranchが進んだ場合、その
    新しいコミットごと`git branch -D`で失ってしまう。"""
    try:
        result = run_git(
            ["rev-parse", "--verify", preparation.branch], cwd=None, check=False
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
    if dispatch_gc.worktree_has_uncommitted_changes(preparation.worktree_path):
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
            if branch_delete.returncode != 0:
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

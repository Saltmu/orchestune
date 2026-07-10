import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestune.dispatch_state import ActiveWorktree, RunState
from orchestune.dispatcher import DispatcherConfig, recover_run_state
from orchestune.github import IssueRecord, PrRecord


def test_recover_run_state_no_missing():
    # 欠損がない場合は modified が False であること
    run_state = RunState(active_worktrees={})
    in_progress = []
    config = DispatcherConfig(run_state_path="dummy.json", worktree_root="worktrees")

    modified = recover_run_state(run_state, in_progress, config)
    assert not modified
    assert len(run_state.active_worktrees) == 0


@patch("orchestune.github.list_open_prs")
@patch("subprocess.run")
def test_recover_run_state_with_missing_no_pr(mock_subproc, mock_list_prs):
    # status:in-progress の Issue があるが、run_state にない場合 (PRなし)
    run_state = RunState(active_worktrees={})

    # yaml footprint を含む Issue
    issue_body = """
## Footprint
```yaml
subtask_id: task-a
footprint:
  - src/foo.py
```
"""
    issue = IssueRecord(
        number=101,
        title="Test task a",
        body=issue_body,
        labels=("status:in-progress",),
        created_at="2026-07-10T12:00:00Z",
    )
    in_progress = [issue]
    config = DispatcherConfig(run_state_path="dummy.json", worktree_root="worktrees")

    # PRは存在しない
    mock_list_prs.return_value = []

    # git worktree list のモック (空)
    mock_res = MagicMock()
    mock_res.stdout = ""
    mock_subproc.return_value = mock_res

    modified = recover_run_state(run_state, in_progress, config)

    assert modified
    assert "101" in run_state.active_worktrees
    active = run_state.active_worktrees["101"]
    assert active.issue_number == 101
    assert active.branch == "claude/issue-101-task-a"
    assert active.worktree_path == "worktrees/claude-issue-101-task-a"
    assert active.declared_footprint == ("src/foo.py",)
    assert active.pid is None
    assert active.external_id is None


@patch("orchestune.github.list_open_prs")
@patch("subprocess.run")
def test_recover_run_state_with_missing_and_pr(mock_subproc, mock_list_prs):
    # status:in-progress の Issue があり、紐づく PR がある場合
    run_state = RunState(active_worktrees={})

    issue_body = """
## Footprint
```yaml
subtask_id: task-b
footprint: []
```
"""
    issue = IssueRecord(
        number=102,
        title="Test task b",
        body=issue_body,
        labels=("status:in-progress",),
        created_at="2026-07-10T12:00:00Z",
    )
    in_progress = [issue]
    config = DispatcherConfig(run_state_path="dummy.json", worktree_root="worktrees")

    # PRのモック
    mock_pr = PrRecord(
        number=50,
        head_ref="feature/my-branch",
        changed_files=(),
        closes_issue_numbers=(102,),
    )
    mock_list_prs.return_value = [mock_pr]

    # git worktree list (worktrees/feature-my-branch が物理的に存在すると仮定)
    # これにより、物理チェックをパスさせる
    mock_res = MagicMock()
    # 絶対パスをシミュレート
    mock_res.stdout = "worktree " + str(Path("worktrees/feature-my-branch").resolve())
    mock_subproc.return_value = mock_res

    modified = recover_run_state(run_state, in_progress, config)

    assert modified
    assert "102" in run_state.active_worktrees
    active = run_state.active_worktrees["102"]
    assert active.issue_number == 102
    assert active.branch == "feature/my-branch"
    assert active.external_id == "50"
    assert active.external_url == "PR#50"
    assert Path(active.worktree_path).name == "feature-my-branch"


@patch("subprocess.run")
def test_recover_run_state_physical_worktree_mismatch(mock_subproc):
    # run_state にはあるが、物理 worktree がなく、PRもない場合は削除されること
    config = DispatcherConfig(run_state_path="dummy.json", worktree_root="worktrees")

    # PRもPIDもないアクティブ worktree
    active_no_pr = ActiveWorktree(
        issue_number=103,
        branch="claude/issue-103-task-c",
        worktree_path=str(Path("worktrees/claude-issue-103-task-c")),
        pid=None,
        started_at=time.time(),
        declared_footprint=(),
        external_id=None,  # PRなし
    )

    # PRがあるアクティブ worktree (物理worktreeがなくても削除されないはず)
    active_with_pr = ActiveWorktree(
        issue_number=104,
        branch="claude/issue-104-task-d",
        worktree_path=str(Path("worktrees/claude-issue-104-task-d")),
        pid=None,
        started_at=time.time(),
        declared_footprint=(),
        external_id="55",  # PRあり
    )

    run_state = RunState(
        active_worktrees={
            "103": active_no_pr,
            "104": active_with_pr,
        }
    )

    # git worktree list (物理的には何もない)
    mock_res = MagicMock()
    mock_res.stdout = ""
    mock_subproc.return_value = mock_res

    # in-progress_issues には載っているとする (修復処理が走る)
    in_progress = []

    modified = recover_run_state(run_state, in_progress, config)

    assert not modified
    # 物理worktreeがなくても削除されずに残ること
    assert "103" in run_state.active_worktrees
    assert "104" in run_state.active_worktrees

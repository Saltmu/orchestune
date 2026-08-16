"""`PrepareTasksStep`: どの`status:done`タスクを統合対象に選ぶか。

依存関係によるトポロジカル順序付け、CLOSED済みタスクの除外、`subtask_id`を
抽出できなかったタスクの通知、依存失敗による後続タスクのブロックを扱う。
"""

from __future__ import annotations

from orchestune.integrator import Integrator, IntegratorConfig
from tests.conftest import IntegratorEnv, make_done_issue

_EMPTY_FOOTPRINT_BODY = "```yaml\n```\n"


class TestDoneTaskSelection:
    def test_closed_done_task_is_included_via_state_all_lookup(
        self, integrator_env: IntegratorEnv
    ):
        issue_a = make_done_issue(1, subtask_id="task-1")
        issue_b = make_done_issue(2, subtask_id="task-2", depends_on=("task-1",))

        def list_side_effect(label, state="open"):
            if label != "status:done":
                return []
            return [issue_a, issue_b] if state == "all" else [issue_b]

        integrator_env.list_issues_by_label.side_effect = list_side_effect

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "success"
        assert res["merged"] == ["task-1", "task-2"]

    def test_excludes_tasks_whose_issue_or_parent_is_closed(
        self, integrator_env: IntegratorEnv
    ):
        issue_closed = make_done_issue(1, subtask_id="task-1", state="CLOSED")
        issue_parent_closed = make_done_issue(
            2, subtask_id="task-2", parent={"number": 100, "state": "CLOSED"}
        )
        issue_active = make_done_issue(3, subtask_id="task-3")
        integrator_env.set_done_issues(issue_closed, issue_parent_closed, issue_active)
        integrator_env.create_pull_request.return_value = 888

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "success"
        assert res["merged"] == ["task-3"]
        assert res["integration_pr_number"] == 888

        merge_calls = integrator_env.calls_with("merge")
        assert len(merge_calls) == 1
        assert any("claude/issue-3-task-3" in arg for arg in merge_calls[0].args[0])


class TestBlockedHumanReviewExclusion:
    """#437: 親branch陳腐化の連続によりstatus:blocked-human-reviewへ
    エスカレーション済みのタスクは、人間の確認が入るまで統合対象から
    除外され、同じ陳腐化への再試行ループが止まる。"""

    def test_excludes_task_labeled_blocked_human_review(
        self, integrator_env: IntegratorEnv
    ):
        blocked = make_done_issue(
            1,
            subtask_id="task-1",
            labels=("status:done", "status:blocked-human-review"),
        )
        active = make_done_issue(2, subtask_id="task-2")
        integrator_env.set_done_issues(blocked, active)

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "success"
        assert res["merged"] == ["task-2"]

    def test_all_tasks_blocked_yields_no_done_tasks(
        self, integrator_env: IntegratorEnv
    ):
        blocked = make_done_issue(
            1,
            subtask_id="task-1",
            labels=("status:done", "status:blocked-human-review"),
        )
        integrator_env.set_done_issues(blocked)

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "no_done_tasks"


class TestUnparsableDoneTask:
    """#54: Footprint YAMLから`subtask_id`を抽出できなかった`status:done`タスクが、
    警告もなく黙って処理対象から消えていた不具合の回帰テスト。"""

    def test_flagged_not_silently_dropped(self, integrator_env: IntegratorEnv):
        issue = make_done_issue(7, body=_EMPTY_FOOTPRINT_BODY)
        integrator_env.set_done_issues(issue)

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "no_done_tasks"
        assert res["unparsable_done_issues"] == [7]
        integrator_env.add_comment.assert_called_once()
        assert integrator_env.add_comment.call_args[0][0] == 7

    def test_flagged_alongside_valid_merged_task(self, integrator_env: IntegratorEnv):
        # subtask_idの取れるタスクが他に存在する場合は、そちらは通常通り統合しつつ、
        # 抽出できなかったタスクの存在も結果に残す。
        issue_a = make_done_issue(1, subtask_id="task-1")
        issue_unparsable = make_done_issue(7, body=_EMPTY_FOOTPRINT_BODY)
        integrator_env.set_done_issues(issue_a, issue_unparsable)
        integrator_env.create_pull_request.return_value = 42

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "success"
        assert res["merged"] == ["task-1"]
        assert res["unparsable_done_issues"] == [7]
        # issue #7宛のコメントで警告済み（issue #1は統合成功のため対象外）。
        assert any(
            call.args[0] == 7 for call in integrator_env.add_comment.call_args_list
        )


class TestDependencyFailureBlocking:
    """#50: 依存タスクの失敗後も後続タスクをmerge・CIしてしまい、無関係な後続タスクを
    誤って差し戻す不具合の回帰テスト。"""

    def test_dependent_task_is_blocked_not_merged(self, integrator_env: IntegratorEnv):
        # task-2はtask-1に依存、task-3は独立。task-1がmerge conflictで失敗した場合、
        # task-2はfetch/mergeを一切試みずblocked扱いにすべきで、task-3は影響を受けない。
        integrator_env.set_done_issues(
            make_done_issue(1, subtask_id="task-1"),
            make_done_issue(2, subtask_id="task-2", depends_on=("task-1",)),
            make_done_issue(3, subtask_id="task-3"),
        )
        integrator_env.fail_git(
            lambda args: "merge" in args
            and any("claude/issue-1-task-1" in a for a in args),
            stderr=b"CONFLICT (content): Merge conflict",
        )

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "partial_success"
        assert res["failed"] == ["task-1"]
        assert res["blocked"] == ["task-2"]
        assert res["merged"] == ["task-3"]

        # task-2用のブランチに対するfetch/mergeは一切試みられない。
        assert [
            call
            for call in integrator_env.run.call_args_list
            if any("claude/issue-2-task-2" in a for a in call.args[0])
        ] == []

        # 実際に失敗したtask-1（issue 1）のみラベル差し戻し・コメントが行われ、
        # blockedなだけのtask-2（issue 2）のstatus:doneラベルは維持される。
        integrator_env.remove_label.assert_called_once_with(1, "status:done")
        integrator_env.add_label.assert_called_once_with(1, "status:queued")
        integrator_env.add_comment.assert_called_once()
        assert integrator_env.add_comment.call_args[0][0] == 1

    def test_transitive_dependents_are_blocked(self, integrator_env: IntegratorEnv):
        # task-3 depends_on task-2 depends_on task-1。task-1がCI失敗すると、
        # task-2・task-3の両方がblockedになるべき。
        integrator_env.set_done_issues(
            make_done_issue(1, subtask_id="task-1"),
            make_done_issue(2, subtask_id="task-2", depends_on=("task-1",)),
            make_done_issue(3, subtask_id="task-3", depends_on=("task-2",)),
        )
        integrator_env.fail_git(lambda args: any("local-ci." in arg for arg in args))

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "failure"
        assert res["failed"] == ["task-1"]
        assert res["blocked"] == ["task-2", "task-3"]
        assert res["merged"] == []

        for branch in ("claude/issue-2-task-2", "claude/issue-3-task-3"):
            assert [
                call
                for call in integrator_env.run.call_args_list
                if any(branch in a for a in call.args[0])
            ] == []

        # blockedな2件についてはラベル操作が行われない。
        integrator_env.remove_label.assert_called_once_with(1, "status:done")
        integrator_env.add_label.assert_called_once_with(1, "status:queued")

    def test_report_distinguishes_own_failure_from_blocked(
        self, integrator_env: IntegratorEnv
    ):
        integrator_env.set_done_issues(
            make_done_issue(1, subtask_id="task-1"),
            make_done_issue(2, subtask_id="task-2", depends_on=("task-1",)),
        )
        integrator_env.fail_git(
            lambda args: "merge" in args
            and any("claude/issue-1-task-1" in a for a in args),
            stderr=b"CONFLICT",
        )

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert "task-1" in res["failed_reasons"]
        assert "task-2" not in res["failed_reasons"]
        assert "task-2" in res["blocked_reasons"]
        assert "task-1" not in res["blocked_reasons"]
        assert "task-1" in res["blocked_reasons"]["task-2"]

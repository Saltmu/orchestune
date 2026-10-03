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

        res = Integrator(IntegratorConfig(parent_issue_number=100, apply=True)).run()

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

        res = Integrator(IntegratorConfig(parent_issue_number=100, apply=True)).run()

        assert res["status"] == "success"
        assert res["merged"] == ["task-3"]
        assert res["integration_pr_number"] == 888

        merge_calls = integrator_env.calls_with("merge")
        assert len(merge_calls) == 1
        assert any("claude/issue-3-task-3" in arg for arg in merge_calls[0].args[0])


class TestBlockedHumanReviewExclusion:
    """#437: 親branch陳腐化の連続によりstatus:blocked-human-reviewへ
    エスカレーション済みのタスクは、人間の確認が入るまで統合（マージ）
    対象から外れる。#437レビュー対応: ただし`PrepareTasksStep`の時点で
    `active_done_tasks`から完全に除外してはいけない。除外すると、この
    タスクに依存する後続タスク（特にスタッキングにより既にこのタスクの
    未マージコミットを含んだブランチを持つ後続タスク）を検知してブロック
    する既存の推移的依存判定（`IntegrationMerger.merge_and_test_tasks`）が
    このタスクの存在自体を認識できず素通りしてしまい、エスカレーションで
    意図した人間の確認をブロックされた変更が後続タスク経由でparent branchへ
    迂回して入ってしまう。そのため、実際にマージをスキップする判定は
    `merge_and_test_tasks`側で行う（`TestDependencyFailureBlocking`と同じ
    箇所）。"""

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

        res = Integrator(IntegratorConfig(parent_issue_number=100, apply=True)).run()

        assert res["status"] == "success"
        assert res["merged"] == ["task-2"]
        assert res["blocked"] == ["task-1"]

    def test_all_tasks_blocked_yields_failure_not_silently_dropped(
        self, integrator_env: IntegratorEnv
    ):
        # #437レビュー対応: 唯一の対象タスクがエスカレーション済みの場合、
        # 以前は`PrepareTasksStep`が完全に除外して`no_done_tasks`（＝何も
        # 対象が無かった）を返していたが、これは「ブロックされたタスクが
        # 存在する」ことを覆い隠してしまう。実際にはタスクは存在し統合を
        # 試みてブロックされたため、`failure`として明示的に報告する。
        blocked = make_done_issue(
            1,
            subtask_id="task-1",
            labels=("status:done", "status:blocked-human-review"),
        )
        integrator_env.set_done_issues(blocked)

        res = Integrator(IntegratorConfig(parent_issue_number=100, apply=True)).run()

        assert res["status"] == "failure"
        assert res["blocked"] == ["task-1"]

    def test_stacked_dependent_of_blocked_task_is_also_blocked(
        self, integrator_env: IntegratorEnv
    ):
        # #437レビュー対応（Codexの指摘の回帰テスト）: task-1がescalation
        # 済みでも、task-1に依存するtask-2（スタッキングによりtask-1の
        # 未マージコミットを既に含んだブランチを持ちうる）が独立にマージ
        # されてしまうと、ブロックしたはずのtask-1の変更が実質的に
        # parent branchへ入ってしまう。task-2も推移的にブロックされ、
        # 一切fetch/mergeを試みてはならない。
        blocked = make_done_issue(
            1,
            subtask_id="task-1",
            labels=("status:done", "status:blocked-human-review"),
        )
        dependent = make_done_issue(2, subtask_id="task-2", depends_on=("task-1",))
        integrator_env.set_done_issues(blocked, dependent, done=[dependent, blocked])

        res = Integrator(IntegratorConfig(parent_issue_number=100, apply=True)).run()

        assert res["status"] == "failure"
        assert res.get("merged", []) == []
        assert sorted(res["blocked"]) == ["task-1", "task-2"]
        assert not [
            call
            for call in integrator_env.run.call_args_list
            if any("claude/issue-2-task-2" in a for a in call.args[0])
            and "merge" in call.args[0]
        ]


class TestUnparsableDoneTask:
    """#54: Footprint YAMLから`subtask_id`を抽出できなかった`status:done`タスクが、
    警告もなく黙って処理対象から消えていた不具合の回帰テスト。"""

    def test_flagged_not_silently_dropped(self, integrator_env: IntegratorEnv):
        issue = make_done_issue(7, body=_EMPTY_FOOTPRINT_BODY)
        integrator_env.set_done_issues(issue)

        res = Integrator(IntegratorConfig(parent_issue_number=100, apply=True)).run()

        assert res["status"] == "no_done_tasks"
        assert res["unparsable_done_issues"] == [7]
        integrator_env.add_comment.assert_called_once()
        assert integrator_env.add_comment.call_args[0][0] == 7
        integrator_env.list_open_prs.assert_not_called()

    def test_a_timed_out_flag_comment_is_an_unknown_write_that_holds(
        self, integrator_env: IntegratorEnv, tmp_path
    ):
        # #820: the comment may have been accepted before gh timed out.
        from orchestune.infra.execution_deadline import ExecutionCommandTimeout

        integrator_env.set_done_issues(make_done_issue(7, body=_EMPTY_FOOTPRINT_BODY))
        integrator_env.add_comment.side_effect = ExecutionCommandTimeout(
            "gh issue", 60, "normal"
        )

        res = Integrator(
            IntegratorConfig(
                parent_issue_number=100, apply=True, repository_root=tmp_path
            )
        ).run()

        assert res["status"] == "execution_indeterminate"
        failure = res["execution_failures"][0]
        assert failure["stage"] == "PrepareTasksStep"
        assert failure["side_effect_state"] == "unknown"
        assert (tmp_path / "worktrees" / ".holds").exists()

    def test_the_hold_of_a_timed_out_flag_comment_uses_the_current_generation(
        self, integrator_env: IntegratorEnv, fake_forge, tmp_path
    ):
        # After an earlier successful integration the parent is in generation 2; a hold
        # stamped with generation 1 would be ignored and the comment posted again.
        import json

        from orchestune.infra.execution_deadline import ExecutionCommandTimeout
        from orchestune.integrator.timeout_retry import (
            EVENT_FINISHED,
            EVENT_RESERVED,
            ExecutionEvent,
        )

        def event(kind: str, **fields: object) -> None:
            body = ExecutionEvent(
                parent_issue_number=100,
                generation=1,
                attempt_id="old",
                event=kind,
                executed_at="2026-01-01T00:00:00Z",
                **fields,  # type: ignore[arg-type]
            ).render()
            fake_forge.create_issue_comment(100, body)

        event(EVENT_RESERVED, stage="ci")
        event(EVENT_FINISHED, outcome="success", stop_confirmed=True)
        integrator_env.set_done_issues(make_done_issue(7, body=_EMPTY_FOOTPRINT_BODY))
        integrator_env.add_comment.side_effect = ExecutionCommandTimeout(
            "gh issue", 60, "normal"
        )
        config = IntegratorConfig(
            parent_issue_number=100, apply=True, repository_root=tmp_path
        )

        Integrator(config).run()

        (hold_file,) = (tmp_path / "worktrees" / ".holds").glob("*.json")
        assert json.loads(hold_file.read_text())["generation"] == 2

    def test_flagged_alongside_valid_merged_task(self, integrator_env: IntegratorEnv):
        # subtask_idの取れるタスクが他に存在する場合は、そちらは通常通り統合しつつ、
        # 抽出できなかったタスクの存在も結果に残す。
        issue_a = make_done_issue(1, subtask_id="task-1")
        issue_unparsable = make_done_issue(7, body=_EMPTY_FOOTPRINT_BODY)
        integrator_env.set_done_issues(issue_a, issue_unparsable)
        integrator_env.create_pull_request.return_value = 42

        res = Integrator(IntegratorConfig(parent_issue_number=100, apply=True)).run()

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

        res = Integrator(IntegratorConfig(parent_issue_number=100, apply=True)).run()

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
        integrator_env.remove_label.assert_any_call(1, "status:done")
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

        res = Integrator(IntegratorConfig(parent_issue_number=100, apply=True)).run()

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
        integrator_env.remove_label.assert_any_call(1, "status:done")
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

        res = Integrator(IntegratorConfig(parent_issue_number=100, apply=True)).run()

        assert "task-1" in res["failed_reasons"]
        assert "task-2" not in res["failed_reasons"]
        assert "task-2" in res["blocked_reasons"]
        assert "task-1" not in res["blocked_reasons"]
        assert "task-1" in res["blocked_reasons"]["task-2"]

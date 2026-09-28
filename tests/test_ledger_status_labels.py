from __future__ import annotations

from unittest.mock import MagicMock

from orchestune.ledger.status_labels import transition_status_label


class TestTransitionStatusLabel:
    def test_adds_new_label_before_removing_old_ones(self):
        # #381: 途中で例外が起きてもIssueが必ずいずれかのラベルを持ち続ける
        # ことを保証するため、addが必ずremoveより先に呼ばれなければならない。
        forge = MagicMock()
        call_order: list[str] = []
        forge.add_label.side_effect = lambda *a, **k: call_order.append("add")
        forge.remove_label.side_effect = lambda *a, **k: call_order.append("remove")

        transition_status_label(forge, 1, "status:done", ("status:in-progress",))

        assert call_order == ["add", "remove"]
        forge.add_label.assert_called_once_with(1, "status:done")
        forge.remove_label.assert_called_once_with(1, "status:in-progress")

    def test_removes_every_old_label_provided(self):
        forge = MagicMock()

        transition_status_label(
            forge, 1, "status:in-progress", ("status:queued", "status:blocked")
        )

        forge.remove_label.assert_any_call(1, "status:queued")
        forge.remove_label.assert_any_call(1, "status:blocked")
        assert forge.remove_label.call_count == 2

    def test_does_not_remove_old_label_matching_the_new_label(self):
        # 起動失敗の再試行等で、旧ラベルと新ラベルが同名になりうる
        # （例: 既にstatus:blockedだったタスクが再び起動失敗しstatus:blocked
        # を付与し直す）。この場合、addで付与した直後に自分自身を消して
        # しまってはならない。
        forge = MagicMock()

        transition_status_label(
            forge, 1, "status:blocked", ("status:queued", "status:blocked")
        )

        forge.add_label.assert_called_once_with(1, "status:blocked")
        forge.remove_label.assert_called_once_with(1, "status:queued")

    def test_empty_old_labels_only_adds(self):
        forge = MagicMock()

        transition_status_label(forge, 1, "status:in-progress", ())

        forge.add_label.assert_called_once_with(1, "status:in-progress")
        forge.remove_label.assert_not_called()


class TestCompletionTransition:
    def run(
        self,
        labels,
        *,
        add_error=False,
        remove_error=False,
        lost=False,
        generation=lambda: True,
        reads=None,
    ):
        from orchestune.complete.status_labels import transition_completion_status_label

        live = set(labels)
        forge = MagicMock()
        operations = []

        def add(number, label):
            operations.append(("add", label))
            if not add_error or lost:
                live.add(label)
            if add_error:
                raise RuntimeError("lost add")

        def remove(number, label):
            operations.append(("remove", label))
            if not remove_error or lost:
                live.discard(label)
            if remove_error:
                raise RuntimeError("lost remove")

        forge.add_label.side_effect = add
        forge.remove_label.side_effect = remove
        forge.get_issue_labels.side_effect = reads or (lambda n: tuple(sorted(live)))
        result = transition_completion_status_label(
            forge, 1, "status:done", generation_matches=generation
        )
        return result, operations, live

    def test_confirmed_order_and_preservation(self):
        result, ops, live = self.run(
            [
                "status:in-progress",
                "status:queued",
                "status:blocked",
                "status:force-serial",
                "feature",
            ]
        )
        assert result.confirmed
        assert ops[0] == ("add", "status:done")
        assert live == {"status:done", "status:force-serial", "feature"}

    def test_existing_target_is_not_added_or_removed(self):
        result, ops, _ = self.run(["status:done", "status:queued"])
        assert result.confirmed
        assert ops == [("remove", "status:queued")]
        assert self.run(["status:done"])[1] == []

    def test_add_failure_does_not_remove(self):
        result, ops, _ = self.run(["status:queued"], add_error=True)
        assert result.status.value == "add_failed"
        assert result.failed_operation == "add:status:done"
        assert ops == [("add", "status:done")]

    def test_partial_cleanup_and_replay(self):
        result, ops, live = self.run(["status:queued"], remove_error=True)
        assert result.status.value == "cleanup_incomplete"
        assert result.failed_operation == "remove:status:queued"
        replay, ops, _ = self.run(live)
        assert replay.confirmed
        assert ops == [("remove", "status:queued")]

    def test_response_loss_recovers_from_live_state(self):
        for add, remove in [(True, False), (False, True), (True, True)]:
            assert self.run(
                ["status:queued"], add_error=add, remove_error=remove, lost=True
            )[0].confirmed

    def test_protected_unknown_and_other_terminal_labels_conflict(self):
        for label in [
            "status:blocked-human-review",
            "status:manual-merge-required",
            "status:external-lock",
            "status:blocked-recompute",
            "status:not-needed",
            "status:future",
        ]:
            result, ops, live = self.run([label, "status:queued"])
            assert result.status.value == "conflict"
            assert ops == []
            assert label in live

    def test_generation_mismatch_and_failure(self):
        result, ops, _ = self.run(["status:queued"], generation=lambda: False)
        assert result.status.value == "conflict"
        assert ops == []

    def test_final_get_failure_is_unknown(self):
        result, _, _ = self.run(
            ["status:done"], reads=[("status:done",), RuntimeError("get")]
        )
        assert result.status.value == "unknown"

    def test_protection_appearing_after_add_prevents_cleanup(self):
        result, ops, _ = self.run(
            ["status:queued"],
            reads=[
                ("status:queued",),
                ("status:done", "status:queued", "status:external-lock"),
            ],
        )
        assert result.status.value == "conflict"
        assert ops == [("add", "status:done")]

    def test_initial_get_failure_is_unknown_without_writes(self):
        result, ops, _ = self.run([], reads=[RuntimeError("unavailable")])
        assert result.status.value == "unknown"
        assert result.failed_operation == "get"
        assert ops == []

    def test_generation_callback_failure_is_unknown(self):
        def unavailable():
            raise RuntimeError("cannot reconcile reservation")

        result, ops, _ = self.run(["status:queued"], generation=unavailable)
        assert result.status.value == "unknown"
        assert ops == []

    def test_generation_changes_after_add(self):
        generations = iter([True, False])
        result, ops, _ = self.run(
            ["status:queued"], generation=lambda: next(generations)
        )
        assert result.status.value == "conflict"
        assert ops == [("add", "status:done")]

    def test_missing_target_before_cleanup_does_not_remove(self):
        result, ops, _ = self.run(
            ["status:queued"],
            reads=[("status:queued", "status:done"), ("status:queued",)],
        )
        assert result.status.value == "unknown"
        assert ops == []

    def test_final_observation_detects_new_old_label(self):
        result, ops, _ = self.run(
            ["status:done"], reads=[("status:done",), ("status:done", "status:queued")]
        )
        assert result.status.value == "cleanup_incomplete"
        assert ops == []

    def test_each_completion_target(self):
        from orchestune.complete.status_labels import transition_completion_status_label

        for target in ("status:done", "status:blocked", "status:not-needed"):
            live = {target, "status:queued"}
            forge = MagicMock()
            forge.get_issue_labels.side_effect = lambda n, live=live: tuple(live)
            forge.remove_label.side_effect = lambda n, label, live=live: live.discard(
                label
            )
            result = transition_completion_status_label(
                forge, 1, target, generation_matches=lambda: True
            )
            assert result.confirmed
            assert live == {target}
            forge.add_label.assert_not_called()

    def test_invalid_target_does_not_touch_forge(self):
        import pytest

        from orchestune.complete.status_labels import transition_completion_status_label

        forge = MagicMock()
        with pytest.raises(ValueError):
            transition_completion_status_label(
                forge, 1, "status:queued", generation_matches=lambda: True
            )
        assert forge.mock_calls == []

    def test_legacy_callback_runs_before_cleanup(self):
        forge = MagicMock()
        operations = []
        forge.add_label.side_effect = lambda *args: operations.append("add")
        forge.remove_label.side_effect = lambda *args: operations.append("remove")
        transition_status_label(
            forge,
            1,
            "status:done",
            ["status:queued"],
            on_label_added=lambda: operations.append("callback"),
        )
        assert operations == ["add", "callback", "remove"]

from __future__ import annotations

from unittest.mock import MagicMock

from orchestune.dispatch_labels import transition_status_label


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

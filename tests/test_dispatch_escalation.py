from unittest.mock import patch

from orchestune.dispatch_escalation import apply_human_review_escalation


class TestApplyHumanReviewEscalation:
    """空コミット完了・重複起動検知・CHANGES_REQUESTEDエスカレーションの
    3箇所で共有される、status:blocked-human-reviewへの遷移処理。"""

    def test_removes_in_progress_and_adds_human_review_label(self):
        with (
            patch("orchestune.dispatch_escalation.github.remove_label") as mock_remove,
            patch("orchestune.dispatch_escalation.github.add_label") as mock_add,
            patch("orchestune.dispatch_escalation.github.add_comment") as mock_comment,
        ):
            apply_human_review_escalation(1, ("status:in-progress",), "理由")

        mock_remove.assert_called_once_with(1, "status:in-progress")
        mock_add.assert_called_once_with(1, "status:blocked-human-review")
        mock_comment.assert_called_once_with(1, "理由")

    def test_removes_both_queued_and_blocked_when_both_present(self):
        with (
            patch("orchestune.dispatch_escalation.github.remove_label") as mock_remove,
            patch("orchestune.dispatch_escalation.github.add_label"),
            patch("orchestune.dispatch_escalation.github.add_comment"),
        ):
            apply_human_review_escalation(
                2, ("status:queued", "status:blocked"), "理由"
            )

        mock_remove.assert_any_call(2, "status:queued")
        mock_remove.assert_any_call(2, "status:blocked")
        assert mock_remove.call_count == 2

    def test_ignores_unrelated_labels(self):
        with (
            patch("orchestune.dispatch_escalation.github.remove_label") as mock_remove,
            patch("orchestune.dispatch_escalation.github.add_label"),
            patch("orchestune.dispatch_escalation.github.add_comment"),
        ):
            apply_human_review_escalation(
                3, ("status:in-progress", "priority:high"), "理由"
            )

        mock_remove.assert_called_once_with(3, "status:in-progress")

    def test_no_removable_labels_still_adds_human_review_and_comment(self):
        with (
            patch("orchestune.dispatch_escalation.github.remove_label") as mock_remove,
            patch("orchestune.dispatch_escalation.github.add_label") as mock_add,
            patch("orchestune.dispatch_escalation.github.add_comment") as mock_comment,
        ):
            apply_human_review_escalation(4, (), "理由")

        mock_remove.assert_not_called()
        mock_add.assert_called_once_with(4, "status:blocked-human-review")
        mock_comment.assert_called_once_with(4, "理由")

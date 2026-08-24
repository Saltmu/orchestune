"""dispatch_report.py のレポート整形に関する境界値・エラーハンドリングテスト (#338)。

`write_github_step_summary`は既存の`tests/test_dispatch_locks.py`にも数件あるが、
`post_cycle_results`ブロック、各セクションの「変更なし」表示、`to_unlock`側の
ロック解除表示、およびファイル書き込み失敗時のフォールバックは未検証だったため、
本ファイルで単体テストとして完結させる。
"""

from orchestune.dispatch.cycle import CycleReport
from orchestune.dispatch.report import write_github_step_summary
from orchestune.dispatch.result import PhaseResult, PhaseStatus
from orchestune.dispatch.scoring import Task


def _task(**overrides):
    defaults = dict(
        issue_number=1,
        subtask_id="task-a",
        footprint=(),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=(),
        created_at="2026-01-01T00:00:00+00:00",
    )
    defaults.update(overrides)
    return Task(**defaults)


def _cycle_report(**overrides):
    defaults = dict(
        selected=[],
        quota_slots_available=1,
        lock_changes={"to_lock": [], "to_unlock": []},
        deviation_events=[],
        completion_events=[],
        promotion_events=[],
        applied=True,
    )
    defaults.update(overrides)
    return CycleReport(**defaults)


class TestPostCycleResultsSection:
    def test_renders_all_status_kinds_with_retry_and_detail(self, tmp_path):
        summary_file = tmp_path / "step_summary.md"
        post_cycle_results = [
            PhaseResult(
                phase_name="phase-success",
                status=PhaseStatus.SUCCESS,
                retryable=False,
                error_message=None,
            ),
            PhaseResult(
                phase_name="phase-warning",
                status=PhaseStatus.WARNING,
                retryable=False,
                error_message="警告メッセージ",
            ),
            PhaseResult(
                phase_name="phase-retryable",
                status=PhaseStatus.RETRYABLE_FAILURE,
                retryable=True,
                error_message="一時的な失敗",
            ),
            PhaseResult(
                phase_name="phase-fatal",
                status=PhaseStatus.FATAL_FAILURE,
                retryable=False,
                error_message="致命的な失敗",
            ),
        ]

        write_github_step_summary(
            cycle_report=None,
            integrator_report=None,
            summary_path=str(summary_file),
            post_cycle_results=post_cycle_results,
        )

        content = summary_file.read_text(encoding="utf-8")
        assert "### 🔄 後処理フェーズ結果（Post-Cycle Phases）" in content
        assert "| `phase-success` | ✅ 成功 | - | - |" in content
        assert "| `phase-warning` | ⚠️ 警告 | - | 警告メッセージ |" in content
        assert (
            "| `phase-retryable` | ❌ 失敗（再試行可能） | 🔄 必要 | 一時的な失敗 |"
            in content
        )
        assert "| `phase-fatal` | 🚨 致命的失敗 | - | 致命的な失敗 |" in content

    def test_omitted_when_no_post_cycle_results(self, tmp_path):
        summary_file = tmp_path / "step_summary.md"

        write_github_step_summary(
            cycle_report=None,
            integrator_report=None,
            summary_path=str(summary_file),
            post_cycle_results=None,
        )

        content = summary_file.read_text(encoding="utf-8")
        assert "後処理フェーズ結果" not in content


class TestIntegratorReportSection:
    def test_omitted_when_integrator_report_is_none(self, tmp_path):
        summary_file = tmp_path / "step_summary.md"

        write_github_step_summary(
            cycle_report=None,
            integrator_report=None,
            summary_path=str(summary_file),
        )

        content = summary_file.read_text(encoding="utf-8")
        assert "仮マージ検証" not in content

    def test_omitted_when_integrator_report_is_empty_dict(self, tmp_path):
        summary_file = tmp_path / "step_summary.md"

        write_github_step_summary(
            cycle_report=None,
            integrator_report={},
            summary_path=str(summary_file),
        )

        content = summary_file.read_text(encoding="utf-8")
        assert "仮マージ検証" not in content

    def test_shows_no_target_message_when_merged_and_failed_are_empty(self, tmp_path):
        summary_file = tmp_path / "step_summary.md"

        write_github_step_summary(
            cycle_report=None,
            integrator_report={"status": "success", "merged": [], "failed": []},
            summary_path=str(summary_file),
        )

        content = summary_file.read_text(encoding="utf-8")
        assert "検証対象の完了タスク（`status:done`）はありませんでした。" in content


class TestCycleReportSection:
    def test_shows_no_launch_message_when_selected_is_empty(self, tmp_path):
        summary_file = tmp_path / "step_summary.md"
        cycle_report = _cycle_report(selected=[])

        write_github_step_summary(
            cycle_report=cycle_report,
            integrator_report=None,
            summary_path=str(summary_file),
        )

        content = summary_file.read_text(encoding="utf-8")
        assert "今回新たに起動されたタスクはありません。" in content

    def test_shows_no_lock_change_message_when_both_empty(self, tmp_path):
        summary_file = tmp_path / "step_summary.md"
        cycle_report = _cycle_report(
            lock_changes={"to_lock": [], "to_unlock": []},
        )

        write_github_step_summary(
            cycle_report=cycle_report,
            integrator_report=None,
            summary_path=str(summary_file),
        )

        content = summary_file.read_text(encoding="utf-8")
        assert "外部ロックの変更はありませんでした。" in content

    def test_renders_unlock_rows(self, tmp_path):
        summary_file = tmp_path / "step_summary.md"
        unlock_task = _task(issue_number=42, subtask_id="task-unlock-42")
        cycle_report = _cycle_report(
            lock_changes={"to_lock": [], "to_unlock": [unlock_task]},
        )

        write_github_step_summary(
            cycle_report=cycle_report,
            integrator_report=None,
            summary_path=str(summary_file),
        )

        content = summary_file.read_text(encoding="utf-8")
        assert "| `task-unlock-42` | #42 | 🔓 ロック解除 |" in content


class TestWriteFailureFallback:
    def test_warns_on_stderr_when_summary_path_is_unwritable(self, tmp_path, capsys):
        unwritable_path = tmp_path / "no-such-dir" / "step_summary.md"

        write_github_step_summary(
            cycle_report=None,
            integrator_report=None,
            summary_path=str(unwritable_path),
        )

        captured = capsys.readouterr()
        assert "Warning: Failed to write to GITHUB_STEP_SUMMARY" in captured.err
        assert not unwritable_path.exists()

"""#787: サイクル終了時の「未選定タスクとその理由」要約の整形。"""

from __future__ import annotations

from orchestune.dispatch.scoring import (
    REASON_QUOTA_EXHAUSTED,
    REASON_SELECTED,
    SchedulingDecision,
    ScoreComponents,
)
from orchestune.dispatch.summary import (
    REASON_DEPENDENCY,
    REASON_EXTERNAL_LOCK,
    SUMMARY_PREFIX,
    SkipRecord,
    merge_skips,
    render_forge_warnings_markdown,
    render_forge_warnings_text,
    render_skipped_markdown,
    render_skipped_text,
    skip_record_to_dict,
)


def _decision(issue_number, reason, selected=False):
    return SchedulingDecision(
        issue_number=issue_number,
        subtask_id=f"task-{issue_number}",
        mode="critical-path",
        score=0.0,
        components=ScoreComponents(),
        selected=selected,
        reason=reason,
    )


def _skip(issue_number, reason=REASON_EXTERNAL_LOCK, detail=""):
    return SkipRecord(
        issue_number=issue_number,
        subtask_id=f"task-{issue_number}",
        reason=reason,
        detail=detail,
    )


class TestMergeSkips:
    def test_adds_unselected_scheduling_decisions(self):
        merged = merge_skips([], [_decision(1, REASON_QUOTA_EXHAUSTED)])
        assert merged == [_skip(1, REASON_QUOTA_EXHAUSTED)]

    def test_omits_selected_decisions(self):
        assert merge_skips([], [_decision(1, REASON_SELECTED, selected=True)]) == []

    def test_orders_by_issue_number(self):
        merged = merge_skips([_skip(9)], [_decision(2, REASON_QUOTA_EXHAUSTED)])
        assert [record.issue_number for record in merged] == [2, 9]

    def test_prefilter_skip_wins_over_decision_for_same_issue(self):
        """事前フィルタで落ちた理由の方が具体的なので、そちらを残す。"""
        merged = merge_skips(
            [_skip(1, REASON_EXTERNAL_LOCK, "feat/x [a.py]")],
            [_decision(1, REASON_QUOTA_EXHAUSTED)],
        )
        assert merged == [_skip(1, REASON_EXTERNAL_LOCK, "feat/x [a.py]")]


class TestRenderSkippedText:
    def test_returns_nothing_when_no_task_was_skipped(self):
        assert render_skipped_text([]) == []

    def test_lists_each_skipped_task_with_reason_and_detail(self):
        lines = render_skipped_text(
            [_skip(695, REASON_EXTERNAL_LOCK, "fix/issue-777 [tests/conftest.py]")]
        )
        assert all(line.startswith(SUMMARY_PREFIX) for line in lines)
        body = "\n".join(lines)
        assert "#695" in body
        assert REASON_EXTERNAL_LOCK in body
        assert "fix/issue-777 [tests/conftest.py]" in body

    def test_aggregates_dependency_waits(self):
        """依存待ちは規模の大きい親Issueで数十件になりうるため集約する。"""
        records = [
            _skip(n, REASON_DEPENDENCY, f"waiting: #{n - 1}") for n in range(2, 8)
        ]
        body = "\n".join(render_skipped_text(records))
        assert "6" in body
        assert "#2" in body
        assert "#7" not in body

    def test_is_ascii_only(self):
        """stderr経路はWindows(cp932)でも出力されるため非ASCIIを混ぜない。"""
        records = [
            _skip(1, REASON_EXTERNAL_LOCK, "feat/x [a.py]"),
            _skip(2, REASON_DEPENDENCY, "waiting: #1"),
            _skip(3, REASON_QUOTA_EXHAUSTED),
        ]
        "\n".join(render_skipped_text(records)).encode("ascii")


class TestRenderSkippedMarkdown:
    def test_returns_nothing_when_no_task_was_skipped(self):
        assert render_skipped_markdown([]) == []

    def test_renders_a_table_row_per_skipped_task(self):
        body = "\n".join(
            render_skipped_markdown(
                [_skip(695, REASON_EXTERNAL_LOCK, "fix/issue-777 [tests/conftest.py]")]
            )
        )
        assert "未選定タスク" in body
        assert "#695" in body
        assert "外部ロック" in body
        assert "fix/issue-777 [tests/conftest.py]" in body

    def test_aggregates_dependency_waits(self):
        records = [
            _skip(n, REASON_DEPENDENCY, f"waiting: #{n - 1}") for n in range(2, 8)
        ]
        body = "\n".join(render_skipped_markdown(records))
        assert "6" in body
        assert "#7" not in body

    def test_falls_back_to_the_raw_reason_code_when_unmapped(self):
        body = "\n".join(render_skipped_markdown([_skip(1, "brand-new-reason")]))
        assert "brand-new-reason" in body


class TestRenderForgeWarnings:
    def test_returns_nothing_without_warnings(self):
        assert render_forge_warnings_text([]) == []
        assert render_forge_warnings_markdown([]) == []

    def test_reports_operation_and_error(self):
        warnings = [
            {"issue_number": 702, "operation": "list_prs", "error": "HTTPError: 504"}
        ]
        text = "\n".join(render_forge_warnings_text(warnings))
        assert "#702" in text
        assert "list_prs" in text
        assert "504" in text
        assert "Forge" in "\n".join(render_forge_warnings_markdown(warnings))

    def test_text_is_ascii_only(self):
        warnings = [
            {"issue_number": 702, "operation": "list_prs", "error": "HTTPError: 504"}
        ]
        "\n".join(render_forge_warnings_text(warnings)).encode("ascii")


class TestSkipRecordToDict:
    def test_is_json_serializable(self):
        assert skip_record_to_dict(_skip(1, REASON_EXTERNAL_LOCK, "feat/x")) == {
            "issue_number": 1,
            "subtask_id": "task-1",
            "reason": REASON_EXTERNAL_LOCK,
            "detail": "feat/x",
        }


class TestMergeSkipsPrecedence:
    """PR#789レビュー(Codex P2): 同一Issueに複数の記録が来たときの優先順。"""

    def test_keeps_the_first_record_for_an_issue(self):
        """並びの先頭ほど具体的な理由（外部ロックの衝突詳細など）が来る。
        後勝ちにすると、詳細を持つ記録が詳細なしの記録で潰される。"""
        detailed = _skip(1, REASON_EXTERNAL_LOCK, "feat/x [a.py]")
        vague = SkipRecord(
            issue_number=1, subtask_id="task-1", reason="actor-unverified"
        )
        assert merge_skips([detailed, vague]) == [detailed]


class TestAsciiSafety:
    """PR#789レビュー(Codex P2): テキスト経路のASCII契約を、補間される外部由来の
    文字列（例外メッセージ・ブランチ名・ファイルパス）にも適用する。

    cp932のコンソールへ表現できない文字を書くと`UnicodeEncodeError`になり、
    「保守的に保留する」はずの判定がサイクルの失敗に化ける。"""

    def test_skip_detail_with_non_ascii_paths_is_escaped(self):
        record = _skip(1, REASON_EXTERNAL_LOCK, "feat/日本語 [tests/テスト.py]")
        rendered = "\n".join(render_skipped_text([record]))
        rendered.encode("ascii")
        assert "\\u" in rendered

    def test_forge_warning_with_non_ascii_error_is_escaped(self):
        warnings = [
            {
                "issue_number": 1,
                "operation": "list_prs",
                "error": "RuntimeError: 接続に失敗しました",
            }
        ]
        "\n".join(render_forge_warnings_text(warnings)).encode("ascii")

    def test_markdown_keeps_the_original_text(self):
        """Markdown経路はUTF-8で書かれるため、原文を保つ。"""
        record = _skip(1, REASON_EXTERNAL_LOCK, "feat/日本語 [tests/テスト.py]")
        assert "feat/日本語" in "\n".join(render_skipped_markdown([record]))

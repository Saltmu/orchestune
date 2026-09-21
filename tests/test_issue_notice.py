"""#787: Issueへ理由を書き残す汎用通知レイヤの冪等性・fail closed挙動。"""

from __future__ import annotations

import pytest

from orchestune.consistency.models import (
    ConsistencyFinding,
    ConsistencyReport,
    ConsistencyScope,
    Evidence,
    FindingSeverity,
    Repairability,
)
from orchestune.consistency.supervisor import (
    ConsistencyCycleReport,
    ConsistencyMode,
    ConsistencyRepairOutcome,
    ConsistencyScanResult,
    RepairDisposition,
    ScanKind,
)
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_report import CycleReport
from orchestune.dispatch.postcycle import _post_finding_notices, post_finding_notices
from orchestune.dispatch.result import PhaseStatus
from orchestune.forge import ForgeAuthError
from orchestune.issue_notice import (
    NoticeOutcome,
    latest_notice_body,
    notice_marker,
    post_notice_if_changed,
    render_notice,
)
from tests.conftest import FakeForge


class _RaisingListComments(FakeForge):
    def list_comments(self, issue_number):
        raise RuntimeError("504 Gateway Timeout")


class _RaisingAddComment(FakeForge):
    def add_comment(self, issue_number, body, author="bot"):
        raise RuntimeError("secondary rate limit")


class TestNoticeMarker:
    def test_marker_is_namespaced_per_kind(self):
        assert (
            notice_marker("external-lock") == "<!-- orchestune:notice:external-lock -->"
        )

    def test_render_prefixes_body_with_marker(self):
        rendered = render_notice("external-lock", "衝突しています")
        assert rendered.startswith(notice_marker("external-lock"))
        assert rendered.endswith("衝突しています")


class TestLatestNoticeBody:
    def test_returns_none_without_matching_marker(self):
        comments = [{"body": "無関係なコメント"}]
        assert latest_notice_body(comments, "external-lock") is None

    def test_returns_body_of_most_recent_matching_comment(self):
        comments = [
            {"body": render_notice("external-lock", "古い理由")},
            {"body": "無関係なコメント"},
            {"body": render_notice("external-lock", "新しい理由")},
        ]
        assert latest_notice_body(comments, "external-lock") == "新しい理由"

    def test_ignores_other_kinds(self):
        comments = [{"body": render_notice("other-kind", "別の通知")}]
        assert latest_notice_body(comments, "external-lock") is None

    def test_normalizes_crlf_stored_by_github(self):
        """GitHubはコメント本文をCRLFで返すため、LFへ揃えないと毎サイクル
        「本文が変わった」と誤判定して連投になる。"""
        stored = render_notice("external-lock", "1行目\n2行目").replace("\n", "\r\n")
        assert latest_notice_body([{"body": stored}], "external-lock") == "1行目\n2行目"


class TestPostNoticeIfChanged:
    def test_posts_when_no_prior_notice(self):
        forge = FakeForge()
        assert (
            post_notice_if_changed(forge, 1, "external-lock", "理由A")
            is NoticeOutcome.POSTED
        )
        assert latest_notice_body(forge.list_comments(1), "external-lock") == "理由A"

    def test_skips_when_body_is_unchanged(self):
        forge = FakeForge()
        post_notice_if_changed(forge, 1, "external-lock", "理由A")
        assert (
            post_notice_if_changed(forge, 1, "external-lock", "理由A")
            is NoticeOutcome.UNCHANGED
        )
        assert len(forge.list_comments(1)) == 1

    def test_reposts_when_body_changed(self):
        """継続ロック中に衝突相手が変わった場合、Issue上の理由が古いままに
        ならないよう再投稿する。"""
        forge = FakeForge()
        post_notice_if_changed(forge, 1, "external-lock", "理由A")
        assert (
            post_notice_if_changed(forge, 1, "external-lock", "理由B")
            is NoticeOutcome.POSTED
        )
        assert len(forge.list_comments(1)) == 2
        assert latest_notice_body(forge.list_comments(1), "external-lock") == "理由B"

    def test_kinds_are_deduplicated_independently(self):
        forge = FakeForge()
        post_notice_if_changed(forge, 1, "external-lock", "同じ本文")
        assert (
            post_notice_if_changed(forge, 1, "other-kind", "同じ本文")
            is NoticeOutcome.POSTED
        )
        assert len(forge.list_comments(1)) == 2

    def test_fails_closed_when_comments_cannot_be_read(self, capsys):
        """既存コメントを読めないまま投稿すると多重投稿になるため見送る。"""
        forge = _RaisingListComments()
        assert (
            post_notice_if_changed(forge, 1, "external-lock", "理由A")
            is NoticeOutcome.FAILED
        )
        assert forge.comments == {}
        assert "[orchestune:warn]" in capsys.readouterr().err

    def test_reports_failure_to_post(self, capsys):
        forge = _RaisingAddComment()
        assert (
            post_notice_if_changed(forge, 1, "external-lock", "理由A")
            is NoticeOutcome.FAILED
        )
        assert "[orchestune:warn]" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "forge_factory", [_RaisingListComments, _RaisingAddComment]
    )
    def test_warning_lines_are_ascii_only(self, forge_factory, capsys):
        """stderrは`local-ci.ps1`を使うWindows(cp932)でも出力される経路のため、
        非ASCII文字（絵文字を含む）を混ぜない。"""
        post_notice_if_changed(forge_factory(), 1, "external-lock", "理由A")
        captured = capsys.readouterr().err
        assert captured
        captured.encode("ascii")


class TestUpdateOnlyNotices:
    def test_skips_issue_without_a_prior_notice(self):
        forge = FakeForge()
        assert (
            post_notice_if_changed(
                forge, 1, "external-lock", "解除しました", update_only=True
            )
            is NoticeOutcome.NO_PRIOR_NOTICE
        )
        assert forge.comments == {}

    def test_updates_issue_that_has_a_prior_notice(self):
        forge = FakeForge()
        post_notice_if_changed(forge, 1, "external-lock", "ロック中です")
        assert (
            post_notice_if_changed(
                forge, 1, "external-lock", "解除しました", update_only=True
            )
            is NoticeOutcome.POSTED
        )
        assert (
            latest_notice_body(forge.list_comments(1), "external-lock")
            == "解除しました"
        )


class TestNoticeOutcome:
    """PR#789レビュー(Codex P2): 「書く必要が無かった」と「書けなかった」を区別する。"""

    def test_only_failures_are_unsettled(self):
        assert NoticeOutcome.POSTED.settled
        assert NoticeOutcome.UNCHANGED.settled
        assert NoticeOutcome.NO_PRIOR_NOTICE.settled
        assert not NoticeOutcome.FAILED.settled


def _make_finding(
    code: str = "execution.branch-ownership-conflict",
    scope: ConsistencyScope = ConsistencyScope.TASK,
    subject_id: str | None = "101",
    severity: FindingSeverity = FindingSeverity.WARNING,
    summary: str = "Branch conflict",
    details: tuple[str, ...] = ("detail 1",),
) -> ConsistencyFinding:
    return ConsistencyFinding(
        code=code,
        scope=scope,
        severity=severity,
        expected=Evidence(summary="no conflict"),
        observed=Evidence(summary=summary, details=details),
        repairability=Repairability.MANUAL,
        subject_id=subject_id,
    )


def _make_cycle_report(
    findings: tuple[ConsistencyFinding, ...] = (),
    outcomes: tuple[ConsistencyRepairOutcome, ...] = (),
) -> ConsistencyCycleReport:
    scan = ConsistencyScanResult(
        boundary="test",
        kind=ScanKind.FULL,
        report=ConsistencyReport(repository_id="repo", findings=findings),
    )
    return ConsistencyCycleReport(
        mode=ConsistencyMode.REPAIR,
        scans=(scan,),
        repair_outcomes=outcomes,
    )


class TestPostFindingNotices:
    def test_posts_task_scope_finding_to_subject_issue(self):
        forge = FakeForge()
        finding = _make_finding(
            code="execution.branch-ownership-conflict",
            scope=ConsistencyScope.TASK,
            subject_id="101",
            severity=FindingSeverity.WARNING,
            summary="Branch ownership conflict detected",
            details=("branch fix/101 conflicts with fix/102",),
        )
        report = _make_cycle_report(findings=(finding,))

        outcomes = post_finding_notices(forge, report)

        assert len(outcomes) == 1
        assert outcomes[0] is NoticeOutcome.POSTED
        body = latest_notice_body(
            forge.list_comments(101), "finding:execution.branch-ownership-conflict"
        )
        assert body is not None
        assert "execution.branch-ownership-conflict" in body
        assert "Branch ownership conflict detected" in body
        assert "branch fix/101 conflicts with fix/102" in body

    def test_posts_repository_and_parent_scope_findings_to_parent_issue(self):
        forge = FakeForge()
        repo_finding = _make_finding(
            code="repository.clean",
            scope=ConsistencyScope.REPOSITORY,
            subject_id=None,
            severity=FindingSeverity.WARNING,
        )
        parent_finding = _make_finding(
            code="parent.subtask-consistency",
            scope=ConsistencyScope.PARENT,
            subject_id="100",
            severity=FindingSeverity.ERROR,
        )
        report = _make_cycle_report(findings=(repo_finding, parent_finding))

        outcomes = post_finding_notices(forge, report, parent_issue_number=100)

        assert len(outcomes) == 2
        assert all(o is NoticeOutcome.POSTED for o in outcomes)
        assert (
            latest_notice_body(forge.list_comments(100), "finding:repository.clean")
            is not None
        )
        assert (
            latest_notice_body(
                forge.list_comments(100), "finding:parent.subtask-consistency"
            )
            is not None
        )

    def test_skips_repository_or_parent_scope_findings_when_parent_issue_unspecified(
        self,
    ):
        forge = FakeForge()
        finding = _make_finding(
            code="repository.clean",
            scope=ConsistencyScope.REPOSITORY,
            subject_id=None,
        )
        report = _make_cycle_report(findings=(finding,))

        outcomes = post_finding_notices(forge, report, parent_issue_number=None)

        assert outcomes == ()
        assert forge.comments == {}

    def test_skips_finding_with_severity_below_warning(self):
        forge = FakeForge()
        info_finding = _make_finding(
            code="execution.info-notice",
            severity=FindingSeverity.INFO,
        )
        report = _make_cycle_report(findings=(info_finding,))

        outcomes = post_finding_notices(forge, report)

        assert outcomes == ()
        assert forge.comments == {}

    def test_skips_unknown_fact_and_forge_observation_findings(self):
        forge = FakeForge()
        obs_finding = _make_finding(code="observation.git-branch-state")
        unknown_finding = _make_finding(code="execution.forge-observation-unknown")
        report = _make_cycle_report(findings=(obs_finding, unknown_finding))

        outcomes = post_finding_notices(forge, report)

        assert outcomes == ()
        assert forge.comments == {}

    def test_posts_resolution_notice_with_update_only_when_finding_is_resolved(self):
        forge = FakeForge()
        finding = _make_finding(
            code="execution.branch-ownership-conflict",
            subject_id="101",
        )
        # 1st cycle: finding is unresolved -> posted
        report1 = _make_cycle_report(findings=(finding,))
        post_finding_notices(forge, report1)
        assert len(forge.list_comments(101)) == 1

        # 2nd cycle: finding is resolved
        outcome = ConsistencyRepairOutcome(
            finding_code="execution.branch-ownership-conflict",
            scope=ConsistencyScope.TASK,
            subject_id="101",
            disposition=RepairDisposition.RESOLVED,
        )
        scan = ConsistencyScanResult(
            boundary="test",
            kind=ScanKind.FULL,
            report=ConsistencyReport(repository_id="repo", findings=()),
        )
        report2 = ConsistencyCycleReport(
            mode=ConsistencyMode.REPAIR,
            scans=(scan,),
            repair_outcomes=(outcome,),
        )
        outcomes2 = post_finding_notices(forge, report2)

        assert len(outcomes2) == 1
        assert outcomes2[0] is NoticeOutcome.POSTED
        assert len(forge.list_comments(101)) == 2
        body = latest_notice_body(
            forge.list_comments(101), "finding:execution.branch-ownership-conflict"
        )
        assert body is not None
        assert "解消されました" in body

    def test_resolution_notice_skipped_if_issue_had_no_prior_notice(self):
        forge = FakeForge()
        outcome = ConsistencyRepairOutcome(
            finding_code="execution.branch-ownership-conflict",
            scope=ConsistencyScope.TASK,
            subject_id="101",
            disposition=RepairDisposition.RESOLVED,
        )
        scan = ConsistencyScanResult(
            boundary="test",
            kind=ScanKind.FULL,
            report=ConsistencyReport(repository_id="repo", findings=()),
        )
        report = ConsistencyCycleReport(
            mode=ConsistencyMode.REPAIR,
            scans=(scan,),
            repair_outcomes=(outcome,),
        )
        outcomes = post_finding_notices(forge, report)

        assert len(outcomes) == 1
        assert outcomes[0] is NoticeOutcome.NO_PRIOR_NOTICE
        assert forge.comments == {}

    def test_does_not_repost_when_finding_content_is_unchanged(self):
        forge = FakeForge()
        finding = _make_finding(
            code="execution.branch-ownership-conflict",
            subject_id="101",
        )
        report = _make_cycle_report(findings=(finding,))

        outcomes1 = post_finding_notices(forge, report)
        assert outcomes1[0] is NoticeOutcome.POSTED
        assert len(forge.list_comments(101)) == 1

        outcomes2 = post_finding_notices(forge, report)
        assert outcomes2[0] is NoticeOutcome.UNCHANGED
        assert len(forge.list_comments(101)) == 1


class TestPostFindingNoticesPhase:
    def _dummy_cycle_report(
        self, consistency: ConsistencyCycleReport | None = None
    ) -> CycleReport:
        return CycleReport(
            selected=[],
            quota_slots_available=2,
            lock_changes={},
            deviation_events=[],
            completion_events=[],
            promotion_events=[],
            applied=True,
            consistency=consistency
            if consistency is not None
            else ConsistencyCycleReport(mode=ConsistencyMode.OFF),
        )

    def test_phase_success_with_evaluated_findings(self, tmp_path):
        forge = FakeForge()
        config = DispatcherConfig(
            parent_issue_number=100,
            apply=True,
            forge=forge,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
        )
        finding = _make_finding(
            code="execution.branch-ownership-conflict",
            subject_id="101",
        )
        consistency_report = _make_cycle_report(findings=(finding,))
        report = self._dummy_cycle_report(consistency=consistency_report)

        result = _post_finding_notices(config, report)

        assert result.status is PhaseStatus.SUCCESS
        assert result.retryable is False
        assert result.report == {
            "total_evaluated": 1,
            "outcomes": ["posted"],
        }
        assert len(forge.list_comments(101)) == 1

    def test_phase_handles_auth_error(self, tmp_path):
        forge = FakeForge()
        config = DispatcherConfig(
            parent_issue_number=100,
            apply=True,
            forge=forge,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
        )
        report = self._dummy_cycle_report()

        result = _post_finding_notices(
            config, report, auth_error=ForgeAuthError("unauthorized")
        )

        assert result.status is PhaseStatus.FATAL_FAILURE
        assert result.retryable is False
        assert "unauthorized" in (result.error_message or "")

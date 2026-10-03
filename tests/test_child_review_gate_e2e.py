"""子レビュー証跡ゲートのE2E (#1030): 子PRレビュー → `complete` → done Outcome → Integrator。

単体テストは各部品（証跡検証・Outcome・ゲート判定）を個別に押さえている。
ここでは `complete_task` が実際に書いたOutcome Recordをそのまま子Issueのコメントとして
Integratorへ渡し、親ブランチ更新または親Issueの`status:blocked-human-review`
エスカレーションまでの一連を通しで検証する。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from complete_lifecycle_test_support import PublicationForge, lifecycle_environment

from orchestune.complete.contracts import CompleteFailureReason
from orchestune.complete.service import complete_task
from orchestune.integrator import IntegrationStatus, Integrator, IntegratorConfig
from orchestune.integrator.types import IntegrationReport
from orchestune.outcome_record import (
    OutcomeRecord,
    ReviewSummary,
    find_child_outcome_record,
)
from orchestune.review.markers import (
    review_head_marker,
    review_round_marker,
    review_selection_marker,
    review_trigger_marker,
)
from tests.conftest import (
    IntegratorEnv,
    _completed,
    _default_git_completed,
    make_done_issue,
)

CHILD = 1110
PR = 42
PARENT = 100
HEAD = "a" * 40
LATER = "b" * 40
PARENT_PUSH = f"HEAD:refs/heads/parent/issue-{PARENT}"


class ChildPr:
    """子PR上のレビュー状態（トリガー・botの指摘・ワーカーの判断表）を保持する。"""

    def __init__(self, tmp_path: Path, head: str = HEAD) -> None:
        self.head = head
        self.reviews: list[dict[str, Any]] = []
        self.inlines: list[dict[str, Any]] = []
        self.comments: list[dict[str, Any]] = []
        self.reply = tmp_path / "review-reply.md"
        self._next_id = 1
        self.request_review(round_number=1, head=head, at="2026-10-03T00:00:00Z")

    def _id(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    def request_review(self, *, round_number: int, head: str, at: str) -> None:
        self.comments.append(
            dict(
                id=self._id(),
                body="\n".join(
                    (
                        review_trigger_marker("claude"),
                        review_round_marker(round_number),
                        review_head_marker(head),
                    )
                ),
                created_at=at,
                user={"login": "worker"},
            )
        )

    def bot_reply(self, *, at: str, body: str = "No required findings.") -> str:
        item_id = self._id()
        self.comments.append(
            dict(
                id=item_id,
                body=body,
                created_at=at,
                user={"login": "claude[bot]"},
            )
        )
        return f"issue_comment:{item_id}"

    def judge(self, *, round_number: int, rows: list[dict[str, str]]) -> None:
        table = {"round": round_number, "findings": rows}
        self.reply.write_text(
            "```orchestune-review-judgments\n" + yaml.safe_dump(table) + "```\n"
        )

    def judge_clean(self, *, round_number: int, source: str) -> None:
        self.judge(
            round_number=round_number,
            rows=[
                dict(
                    source=source,
                    location="summary",
                    judgment="already_addressed",
                    status="resolved",
                    basis="No required findings in the acquired summary",
                    evidence="current source",
                )
            ],
        )


def _bind_local_head(monkeypatch: pytest.MonkeyPatch, head: str) -> None:
    """ローカルHEADと、そのHEADで実行したローカルCI証跡を一致させる。

    `lifecycle_environment`はCI証跡のheadを固定値にするため、リベース後のHEADでCIを
    再実行した状況を再現するには、HEADとCI証跡を同時に差し替える必要がある。
    """
    ci = SimpleNamespace(to_dict=lambda: {"head_sha": head, "definition": "fixed"})
    monkeypatch.setattr("orchestune.complete.service._head_sha", lambda _: head)
    monkeypatch.setattr(
        "orchestune.complete.service.run_local_ci_if_needed", lambda _: ci
    )
    monkeypatch.setattr(
        "orchestune.complete.service.validate_ci_evidence", lambda _: ci
    )


def _publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pr: ChildPr, *, local_head: str
) -> tuple[Any, PublicationForge]:
    """`complete_task`が使う実Forge相当（台帳は実物、GitHubはfake）を組み立てる。"""
    request, forge, _ = lifecycle_environment(tmp_path, monkeypatch, "done")
    request = replace(
        request,
        payload=replace(request.payload, reviewer="claude", review_reply=pr.reply),
    )
    forge.pr = replace(forge.pr, head_sha=pr.head)
    original = forge.list_all_issue_comments
    forge.list_all_issue_comments = lambda number: (
        pr.comments if number == PR else original(number)
    )
    forge.list_pull_request_reviews = lambda number: pr.reviews
    forge.list_pull_request_review_comments = lambda number: pr.inlines
    _bind_local_head(monkeypatch, local_head)
    return request, forge


def complete_reviewed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pr: ChildPr, *, local_head: str
) -> tuple[Any, list[str]]:
    """レビュー済みの子で`complete`を実行し、結果と子Issueに投稿されたコメントを返す。"""
    request, forge = _publish(tmp_path, monkeypatch, pr, local_head=local_head)
    result = complete_task(request, forge=forge)
    return result, [comment["body"] for comment in forge.comments]


def complete_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, head: str = HEAD
) -> list[str]:
    request, forge, _ = lifecycle_environment(tmp_path, monkeypatch, "done")
    forge.pr = replace(forge.pr, head_sha=head)
    _bind_local_head(monkeypatch, head)
    original = forge.list_all_issue_comments
    forge.list_all_issue_comments = lambda number: (
        [{"body": review_selection_marker("skip", head)}]
        if number == PR
        else original(number)
    )
    result = complete_task(request, forge=forge)
    assert result.success, result.failure
    return [comment["body"] for comment in forge.comments]


def integrate(
    env: IntegratorEnv,
    fake_forge: Any,
    child_comments: list[str],
    *,
    merge_sha: str = HEAD,
    gate: str = "required",
    parent_comments: list[str] | None = None,
    parent_labels: tuple[str, ...] = (),
) -> IntegrationReport:
    """子Issueのコメントを与えてIntegratorを実行する（統合対象は子1件）。"""
    env.set_done_issues(make_done_issue(CHILD, subtask_id=f"task-{CHILD}"))
    bodies = {CHILD: child_comments, PARENT: parent_comments or []}
    fake_forge.list_comments.side_effect = lambda number: [
        {"body": body, "created_at": "2026-10-03T01:00:00Z"}
        for body in bodies.get(number, [])
    ]
    fake_forge.get_issue_labels.return_value = parent_labels

    def git(args: list[str], **kwargs: Any) -> Any:
        if args[:3] == ["git", "rev-parse", "--verify"] and str(args[3]).startswith(
            "origin/"
        ):
            return _completed(args, stdout=merge_sha + "\n")
        return _default_git_completed(args)

    env.run.side_effect = git
    config = IntegratorConfig(
        apply=True,
        parent_issue_number=PARENT,
        integration_run_id="e2e",
        child_review_gate=gate,
    )
    return Integrator(config).run()


def parent_updated(env: IntegratorEnv) -> bool:
    return any(PARENT_PUSH in call.args[0] for call in env.calls_with("push"))


def assert_escalated(env: IntegratorEnv, res: IntegrationReport, reason: str) -> str:
    """親ブランチを更新せず、親Issueが人間レビューへ送られ、理由が証跡に残る。"""
    assert res["status"] == IntegrationStatus.REVIEW_GATE_BLOCKED
    assert not parent_updated(env)
    env.close_issue.assert_not_called()
    env.delete_branch.assert_not_called()
    env.add_label.assert_called_once_with(PARENT, "status:blocked-human-review")
    env.add_comment.assert_called_once()
    assert env.add_comment.call_args.args[0] == PARENT
    comment = str(env.add_comment.call_args.args[1])
    assert f"`{reason}`" in comment
    assert f"#{CHILD}" in comment
    return comment


@pytest.fixture
def reviewed_pr(tmp_path: Path) -> ChildPr:
    pr = ChildPr(tmp_path)
    source = pr.bot_reply(at="2026-10-03T00:01:00Z")
    pr.judge_clean(round_number=1, source=source)
    return pr


class TestPassingEvidenceUpdatesParent:
    def test_complete_pass_evidence_lets_integrator_update_parent(
        self, tmp_path, monkeypatch, reviewed_pr, integrator_env, fake_forge
    ):
        result, posted = complete_reviewed(
            tmp_path, monkeypatch, reviewed_pr, local_head=HEAD
        )
        assert result.success, result.failure
        assert result.outcome_record.review.verdict == "pass"
        assert len(posted) == 1

        res = integrate(integrator_env, fake_forge, posted)

        assert res["status"] == "success"
        assert parent_updated(integrator_env)
        assert res["closed_issues"] == [CHILD]
        integrator_env.add_label.assert_any_call(CHILD, "integration:included")
        assert not any(
            call.args[1] == "status:blocked-human-review"
            for call in integrator_env.add_label.call_args_list
        )


class TestNonPassingEvidenceBlocksParent:
    def test_skip_evidence_stops_integration(
        self, tmp_path, monkeypatch, integrator_env, fake_forge
    ):
        posted = complete_skipped(tmp_path, monkeypatch)
        record = find_child_outcome_record([{"body": body} for body in posted], CHILD)
        assert record is not None
        assert record.review.verdict == "skipped"

        res = integrate(integrator_env, fake_forge, posted)

        comment = assert_escalated(integrator_env, res, "skipped")
        assert "`--child-review-gate off`" in comment

    def test_legacy_outcome_without_review_stops_integration(
        self, integrator_env, fake_forge
    ):
        legacy = OutcomeRecord(
            issue=CHILD, head_sha=HEAD, result="done", review=ReviewSummary()
        )

        res = integrate(integrator_env, fake_forge, [legacy.render()])

        assert_escalated(integrator_env, res, "legacy")

    def test_head_changed_after_complete_stops_integration(
        self, tmp_path, monkeypatch, reviewed_pr, integrator_env, fake_forge
    ):
        _, posted = complete_reviewed(
            tmp_path, monkeypatch, reviewed_pr, local_head=HEAD
        )

        # complete後に子ブランチが進み、統合対象のSHAが証跡のSHAと一致しなくなる。
        res = integrate(integrator_env, fake_forge, posted, merge_sha=LATER)

        assert_escalated(integrator_env, res, "sha_mismatch")

    def test_missing_evidence_stops_integration(self, integrator_env, fake_forge):
        res = integrate(integrator_env, fake_forge, [])

        assert_escalated(integrator_env, res, "absent")

    @pytest.mark.parametrize(
        "record",
        [
            OutcomeRecord(
                issue=CHILD,
                head_sha=HEAD,
                result="done",
                review=ReviewSummary(
                    bot="claude", verdict="fail", reviewed_head_sha=HEAD
                ),
            ),
            OutcomeRecord(
                issue=CHILD,
                head_sha=HEAD,
                result="blocked",
                reason="needs-human",
                review=ReviewSummary(
                    bot="claude", verdict="pass", reviewed_head_sha=HEAD
                ),
            ),
        ],
        ids=["verdict-fail", "result-blocked"],
    )
    def test_records_other_than_a_passing_done_stop_integration(
        self, record, integrator_env, fake_forge
    ):
        res = integrate(integrator_env, fake_forge, [record.render()])

        assert_escalated(integrator_env, res, "not_pass")


class TestGateOff:
    def test_explicit_off_proceeds_with_warning_even_without_evidence(
        self, integrator_env, fake_forge, capsys
    ):
        res = integrate(integrator_env, fake_forge, [], gate="off")

        assert res["status"] == "success"
        assert parent_updated(integrator_env)
        assert "Warning: child_review_gate is off" in capsys.readouterr().err


class TestRebaseAfterReviewRequiresReReview:
    @pytest.mark.parametrize("pushed", [False, True], ids=["local-only", "pushed"])
    def test_rebase_rejects_complete_then_re_review_integrates(
        self, pushed, tmp_path, monkeypatch, reviewed_pr, integrator_env, fake_forge
    ):
        # 1. 1回目のレビュー(HEAD)後にリベースしてHEADがLATERへ変わる。
        #    ローカルのみ: PR上のheadは旧HEADのまま / push済み: PR上のheadもLATER。
        #    いずれもレビュー対象(HEAD)と現在のHEADが一致しないためcompleteは拒否される。
        if pushed:
            reviewed_pr.head = LATER
        rejected, posted = complete_reviewed(
            tmp_path, monkeypatch, reviewed_pr, local_head=LATER
        )
        assert not rejected.success
        assert rejected.failure.reason == CompleteFailureReason.REVIEW_HEAD_MISMATCH
        assert posted == []  # 拒否されたcompleteはOutcomeを残さない

        # 証跡が無いまま統合へ進んでも親ブランチは更新されない。
        res = integrate(integrator_env, fake_forge, posted, merge_sha=LATER)
        assert_escalated(integrator_env, res, "absent")

        # 2. 新しいHEADを対象に再レビューし、判断表を作り直してcompleteを再実行する。
        reviewed_pr.head = LATER
        reviewed_pr.request_review(
            round_number=2, head=LATER, at="2026-10-03T00:10:00Z"
        )
        source = reviewed_pr.bot_reply(at="2026-10-03T00:11:00Z")
        reviewed_pr.judge_clean(round_number=2, source=source)
        retry_dir = tmp_path / "retry"
        retry_dir.mkdir()
        accepted, posted = complete_reviewed(
            retry_dir, monkeypatch, reviewed_pr, local_head=LATER
        )
        assert accepted.success, accepted.failure
        assert accepted.outcome_record.review.reviewed_head_sha == LATER
        assert accepted.outcome_record.review.rounds == 2

        # 3. 再レビュー後の証跡で統合が成功する。
        integrator_env.add_label.reset_mock()
        integrator_env.add_comment.reset_mock()
        integrator_env.run.reset_mock()
        res = integrate(
            integrator_env,
            fake_forge,
            posted,
            merge_sha=LATER,
            parent_labels=("status:blocked-human-review",),
        )
        assert res["status"] == "success"
        assert parent_updated(integrator_env)
        assert res["closed_issues"] == [CHILD]


class TestIncompleteJudgmentsAreRejectedByComplete:
    @pytest.mark.parametrize(
        "mutation",
        ["omitted_source", "unresolved", "needs_information", "deferred_adopt"],
    )
    def test_complete_refuses_and_integration_stays_blocked(
        self,
        mutation,
        tmp_path,
        monkeypatch,
        reviewed_pr,
        integrator_env,
        fake_forge,
    ):
        source = f"issue_comment:{reviewed_pr.comments[-1]['id']}"
        row = dict(
            source=source,
            location="summary",
            judgment="adopt",
            status="resolved",
            basis="Finding is valid",
            evidence="fixed in the current source",
        )
        if mutation == "omitted_source":
            rows: list[dict[str, str]] = []
        elif mutation == "unresolved":
            rows = [{**row, "status": "unresolved"}]
        elif mutation == "needs_information":
            rows = [{**row, "judgment": "needs_information"}]
        else:
            rows = [{**row, "status": "deferred"}]
        reviewed_pr.judge(round_number=1, rows=rows)

        result, posted = complete_reviewed(
            tmp_path, monkeypatch, reviewed_pr, local_head=HEAD
        )

        assert not result.success
        assert result.failure.reason == CompleteFailureReason.REVIEW_EVIDENCE_INVALID
        assert posted == []
        res = integrate(integrator_env, fake_forge, posted)
        assert_escalated(integrator_env, res, "absent")


class TestResumeAfterEscalation:
    def test_same_failure_is_not_reposted_and_complete_evidence_resumes(
        self, tmp_path, monkeypatch, reviewed_pr, integrator_env, fake_forge
    ):
        # 初回: 証跡が無く、親Issueへエスカレーションコメントが1件付く。
        first = integrate(integrator_env, fake_forge, [])
        comment = assert_escalated(integrator_env, first, "absent")

        # 再実行（状況不変）: 同一digestのコメントは再投稿されない。
        integrator_env.add_label.reset_mock()
        integrator_env.add_comment.reset_mock()
        second = integrate(
            integrator_env,
            fake_forge,
            [],
            parent_comments=[comment],
            parent_labels=("status:blocked-human-review",),
        )
        assert second["status"] == IntegrationStatus.REVIEW_GATE_BLOCKED
        integrator_env.add_comment.assert_not_called()

        # 再開: 子PRで証跡を揃えてcompleteを再実行 → 同じ統合が成功する。
        _, posted = complete_reviewed(
            tmp_path, monkeypatch, reviewed_pr, local_head=HEAD
        )
        resumed = integrate(
            integrator_env,
            fake_forge,
            posted,
            parent_comments=[comment],
            parent_labels=("status:blocked-human-review",),
        )
        assert resumed["status"] == "success"
        assert parent_updated(integrator_env)


class TestHandedOffEvidenceCannotBeReplaced:
    """引き渡し済みのdoneは、同じclaimのcomplete再実行では証跡を差し替えられない。

    docs/{ja,en}/usage.md の再開手順（引き渡し済みで証跡不足の子は明示的な
    `--child-review-gate off`）の根拠。completeが拒否された場合（何も投稿されない）とは
    異なり、skipなどで引き渡し済みの子は再レビュー後も同じリクエストの再生しかできない。
    """

    def test_rerun_with_new_review_args_is_rejected_and_gate_still_stops(
        self, tmp_path, monkeypatch, integrator_env, fake_forge
    ):
        request, forge, _ = lifecycle_environment(tmp_path, monkeypatch, "done")
        _bind_local_head(monkeypatch, HEAD)
        first = complete_task(request, forge=forge)
        assert first.success, first.failure
        assert find_child_outcome_record(forge.comments, CHILD).review.verdict == (
            "skipped"
        )

        # 再レビューして判断表を揃え、claude指定で同じIssueのcompleteを再実行する。
        pr = ChildPr(tmp_path)
        pr.judge_clean(round_number=1, source=pr.bot_reply(at="2026-10-03T00:01:00Z"))
        rerun = replace(
            request,
            payload=replace(request.payload, reviewer="claude", review_reply=pr.reply),
        )
        original = forge.list_all_issue_comments
        forge.list_all_issue_comments = lambda number: (
            pr.comments if number == PR else original(number)
        )
        forge.list_pull_request_reviews = lambda number: pr.reviews
        forge.list_pull_request_review_comments = lambda number: pr.inlines

        second = complete_task(rerun, forge=forge)
        assert not second.success
        assert (
            second.failure.reason == CompleteFailureReason.REQUEST_FINGERPRINT_MISMATCH
        )
        replayed = complete_task(request, forge=forge)
        assert replayed.success
        assert len(forge.comments) == 1  # 新しいOutcomeは投稿されない

        res = integrate(
            integrator_env, fake_forge, [comment["body"] for comment in forge.comments]
        )
        assert_escalated(integrator_env, res, "skipped")

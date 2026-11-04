"""Explicit replacement publication: interrupted POST/PATCH must not double-send."""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from orchestune.review.rounds import build_restorable_trigger_body, restore_triggers
from orchestune.review.snapshot import EvidenceContractError
from scripts.review_trigger_replacement import publish_replacement

OLD = "a" * 40
NEW = "b" * 40


def comment(id_, body, minute):
    return {
        "id": id_,
        "body": body,
        "created_at": f"2026-10-04T03:{minute:02}:00Z",
        "user": {"login": "worker"},
    }


def setup_publication(tmp_path, *, disconnect=False, patch_failure=False):
    old = comment(
        1, build_restorable_trigger_body("judgment table", "codex", 1, OLD), 1
    )
    new = comment(
        2,
        build_restorable_trigger_body("judgment table", "codex", 1, NEW, replaces=1),
        2,
    )
    comments = [old]

    def request(args):
        if args == ["api", "user"]:
            return json.dumps({"login": "worker"})
        if "PATCH" in args:
            if patch_failure:
                raise RuntimeError("PATCH unavailable")
            old["body"] = args[-1].removeprefix("body=")
            return json.dumps(old)
        if args[1].endswith("/comments"):
            comments.append(new)
            if disconnect:
                raise RuntimeError("connection lost after POST")
            return json.dumps(new)
        return json.dumps(new if args[1].endswith("/2") else old)

    gh = Mock(side_effect=request)
    kwargs = dict(
        pr_number=7,
        previous=old,
        bot_name="codex",
        head_sha=NEW,
        reply_body="judgment table",
        fetch_comments=lambda: list(comments),
        fetch_head=lambda: NEW,
        run_gh=gh,
        journal_dir=tmp_path,
    )
    return old, new, comments, gh, kwargs


@pytest.mark.parametrize("disconnect", [False, True])
def test_verified_post_then_supersede_preserves_judgments(tmp_path, disconnect):
    old, new, comments, gh, kwargs = setup_publication(tmp_path, disconnect=disconnect)
    assert publish_replacement(**kwargs)["id"] == 2
    assert "judgment table" in old["body"]
    assert "orchestune:superseded-trigger" in old["body"]
    assert [t.id for t in restore_triggers(comments)] == [2]
    assert publish_replacement(**kwargs)["id"] == 2
    posts = [c for c in gh.call_args_list if c.args[0][1].endswith("/comments")]
    assert len(posts) == 1


def test_patch_failure_restart_reuses_verified_replacement(tmp_path):
    old, new, comments, gh, kwargs = setup_publication(tmp_path, patch_failure=True)
    with pytest.raises(EvidenceContractError, match="2"):
        publish_replacement(**kwargs)
    assert [t.id for t in restore_triggers(comments)] == [2]
    with pytest.raises(EvidenceContractError, match="2"):
        publish_replacement(**kwargs)
    assert (
        len([c for c in gh.call_args_list if c.args[0][1].endswith("/comments")]) == 1
    )


def test_unknown_post_result_without_confirmation_is_never_retried(tmp_path):
    _, _, comments, gh, kwargs = setup_publication(tmp_path)
    gh.side_effect = RuntimeError("unknown POST result")
    # Identity must be confirmed first.
    gh.side_effect = [json.dumps({"login": "worker"}), RuntimeError("lost POST")]
    with pytest.raises(EvidenceContractError, match="unconfirmed"):
        publish_replacement(**kwargs)
    gh.reset_mock(side_effect=True)
    gh.return_value = json.dumps({"login": "worker"})
    with pytest.raises(EvidenceContractError, match="unconfirmed"):
        publish_replacement(**kwargs)
    assert all(not c.args[0][1].endswith("/comments") for c in gh.call_args_list)


def test_retiring_marker_preserves_quoted_examples_and_exact_reply_text():
    from scripts.review_trigger_replacement import _supersede_body

    marker = "<!-- orchestune:review-trigger bot=codex -->"
    body = f"reply\nInline example: {marker}\n```\n{marker}\n```\n> {marker}\n\n{marker}\nend\n"
    expected = (
        body.rsplit(marker, 1)[0]
        + "<!-- orchestune:superseded-trigger bot=codex -->\nend\n"
    )
    assert _supersede_body(body, "codex") == expected


def test_round_trigger_request_fields():
    from scripts.review_trigger_replacement import (
        RoundTriggerRequest,
        TriggerOps,
        resolve_round_trigger,
    )

    req = RoundTriggerRequest(
        pr_number=42,
        bot_name="codex",
        current_round=2,
        max_rounds=5,
        body="sample body",
        body_file=None,
        repair_trigger_markers=False,
    )
    assert req.pr_number == 42
    assert req.bot_name == "codex"
    assert req.current_round == 2
    assert req.max_rounds == 5
    assert req.body == "sample body"
    assert req.body_file is None
    assert req.repair_trigger_markers is False

    ops = TriggerOps(
        fetch_head=lambda: "d" * 40,
        fetch_comments=lambda: [],
        run_gh=lambda args: "{}",
        publish=lambda **kwargs: {"id": 1, "created_at": "2026-01-01T00:00:00Z"},
        post_trigger=lambda pr, **kwargs: {
            "id": 10,
            "created_at": "2026-01-01T00:00:00Z",
            "body": f"<!-- orchestune:review-head {'d' * 40} -->",
        },
        retire=lambda comments, id_, run_gh: None,
    )
    t_time, t_id, head = resolve_round_trigger(
        req,
        initial_data={},
        initial_snapshot={},
        excluded_ids=set(),
        ops=ops,
    )
    assert t_id == 10
    assert head == "d" * 40


class TestParameterObjectsScripts:
    """#1667: verify parameter objects for scripts functions."""

    def test_review_wait_request_defaults(self):
        from scripts.wait_for_review import ReviewWaitRequest

        req = ReviewWaitRequest(pr_number=10)
        assert req.pr_number == 10
        assert req.timeout == 1800
        assert req.interval == 5
        assert req.bot_name == "claude"
        assert req.post_trigger is True
        assert req.body is None
        assert req.body_file is None
        assert req.max_rounds == 5
        assert req.max_retries == 1
        assert req.round_num is None
        assert req.stall_grace_seconds == 600
        assert req.jev_threshold is None
        assert req.switch_reviewer is False
        assert req.completed_grace_seconds == 30.0
        assert req.repair_trigger_markers is False

    def test_review_extract_and_record_context_fields(self):
        from scripts.wait_for_review import (
            ReviewExtractContext,
            ReviewRecordContext,
            _extract_review_result,
        )

        extract_ctx = ReviewExtractContext(
            latest_trigger_time="2026-01-01T00:00:00Z",
            round_num=2,
            jev_threshold=0.5,
            pr_number=123,
            trigger_id=456,
            requested_head_sha="a" * 40,
            repository="Saltmu/orchestune",
        )
        assert extract_ctx.latest_trigger_time == "2026-01-01T00:00:00Z"
        assert extract_ctx.round_num == 2
        assert extract_ctx.jev_threshold == 0.5
        assert extract_ctx.pr_number == 123
        assert extract_ctx.trigger_id == 456
        assert extract_ctx.requested_head_sha == "a" * 40
        assert extract_ctx.repository == "Saltmu/orchestune"

        rec_ctx = ReviewRecordContext(
            replacement_proven=True,
            bot_name="codex",
            pr_number=123,
            round_num=2,
            trigger_id=456,
            triggered_at="2026-01-01T00:00:00Z",
            requested_head_sha="a" * 40,
            repository="Saltmu/orchestune",
            jev_threshold=0.5,
        )
        assert rec_ctx.replacement_proven is True
        assert rec_ctx.bot_name == "codex"
        assert rec_ctx.pr_number == 123
        assert rec_ctx.round_num == 2

        # Verify _extract_review_result accepts context
        result = _extract_review_result(
            {"issue_comments": []},
            "claude",
            context=extract_ctx,
        )
        assert result is None

    def test_wait_for_review_accepts_request_object(self):
        from unittest.mock import patch

        from scripts.wait_for_review import ReviewWaitRequest, wait_for_review

        req = ReviewWaitRequest(pr_number=99, bot_name="skip", post_trigger=False)
        with patch("scripts.wait_for_review.execute_review_wait") as mock_execute:
            mock_execute.return_value = {"status": "ok"}
            result = wait_for_review(99, request=req)
            assert result == {"status": "ok"}
            mock_execute.assert_called_once()
            _, passed_req = mock_execute.call_args[0]
            assert passed_req.pr_number == 99
            assert passed_req.bot_name == "skip"

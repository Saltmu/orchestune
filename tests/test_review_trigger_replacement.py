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
    body = f"reply\n```\n{marker}\n```\n> {marker}\n\n{marker}\nend\n"
    expected = (
        body.rsplit(marker, 1)[0]
        + "<!-- orchestune:superseded-trigger bot=codex -->\nend\n"
    )
    assert _supersede_body(body, "codex") == expected

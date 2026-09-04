"""#787: Issueへ理由を書き残す汎用通知レイヤの冪等性・fail closed挙動。"""

from __future__ import annotations

import pytest

from orchestune.issue_notice import (
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
        assert post_notice_if_changed(forge, 1, "external-lock", "理由A") is True
        assert latest_notice_body(forge.list_comments(1), "external-lock") == "理由A"

    def test_skips_when_body_is_unchanged(self):
        forge = FakeForge()
        post_notice_if_changed(forge, 1, "external-lock", "理由A")
        assert post_notice_if_changed(forge, 1, "external-lock", "理由A") is False
        assert len(forge.list_comments(1)) == 1

    def test_reposts_when_body_changed(self):
        """継続ロック中に衝突相手が変わった場合、Issue上の理由が古いままに
        ならないよう再投稿する。"""
        forge = FakeForge()
        post_notice_if_changed(forge, 1, "external-lock", "理由A")
        assert post_notice_if_changed(forge, 1, "external-lock", "理由B") is True
        assert len(forge.list_comments(1)) == 2
        assert latest_notice_body(forge.list_comments(1), "external-lock") == "理由B"

    def test_kinds_are_deduplicated_independently(self):
        forge = FakeForge()
        post_notice_if_changed(forge, 1, "external-lock", "同じ本文")
        assert post_notice_if_changed(forge, 1, "other-kind", "同じ本文") is True
        assert len(forge.list_comments(1)) == 2

    def test_fails_closed_when_comments_cannot_be_read(self, capsys):
        """既存コメントを読めないまま投稿すると多重投稿になるため見送る。"""
        forge = _RaisingListComments()
        assert post_notice_if_changed(forge, 1, "external-lock", "理由A") is False
        assert forge.comments == {}
        assert "[orchestune:warn]" in capsys.readouterr().err

    def test_reports_failure_to_post(self, capsys):
        forge = _RaisingAddComment()
        assert post_notice_if_changed(forge, 1, "external-lock", "理由A") is False
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
            is False
        )
        assert forge.comments == {}

    def test_updates_issue_that_has_a_prior_notice(self):
        forge = FakeForge()
        post_notice_if_changed(forge, 1, "external-lock", "ロック中です")
        assert (
            post_notice_if_changed(
                forge, 1, "external-lock", "解除しました", update_only=True
            )
            is True
        )
        assert (
            latest_notice_body(forge.list_comments(1), "external-lock")
            == "解除しました"
        )

"""`orchestune.validation`: gh CLIへ渡す前の引数サニタイズ。

Forgeの実装よりも下のレイヤなので、`GitHubForge`の契約テストからは切り離す。
"""

from __future__ import annotations

import pytest

from orchestune.validation import (
    validate_issue_number,
    validate_label,
    validate_ref_name,
    validate_username,
)


class TestValidateIssueNumber:
    def test_accepts_positive_int(self):
        assert validate_issue_number(184) == 184

    def test_accepts_numeric_string(self):
        assert validate_issue_number("184") == 184

    def test_rejects_non_numeric(self):
        with pytest.raises(ValueError, match="issue番号"):
            validate_issue_number("184; rm -rf /")

    def test_rejects_negative(self):
        with pytest.raises(ValueError, match="issue番号"):
            validate_issue_number(-1)

    def test_rejects_zero(self):
        with pytest.raises(ValueError, match="issue番号"):
            validate_issue_number(0)


class TestValidateLabel:
    def test_accepts_known_label_pattern(self):
        assert validate_label("status:queued") == "status:queued"
        assert validate_label("priority:high") == "priority:high"
        assert validate_label("risk:flagged") == "risk:flagged"

    def test_rejects_shell_metacharacters(self):
        with pytest.raises(ValueError, match="ラベル"):
            validate_label("status:queued; rm -rf /")

    def test_rejects_spaces(self):
        with pytest.raises(ValueError, match="ラベル"):
            validate_label("status queued")

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="ラベル"):
            validate_label("")


class TestValidateRefName:
    def test_accepts_normal_branch_name(self):
        assert validate_ref_name("claude/issue-184-dispatcher") == (
            "claude/issue-184-dispatcher"
        )

    def test_rejects_shell_metacharacters(self):
        with pytest.raises(ValueError, match="ブランチ名"):
            validate_ref_name("foo`rm -rf /`")

    def test_rejects_leading_dash(self):
        with pytest.raises(ValueError, match="ブランチ名"):
            validate_ref_name("--force")

    def test_rejects_double_dot(self):
        with pytest.raises(ValueError, match="ブランチ名"):
            validate_ref_name("foo..bar")

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="ブランチ名"):
            validate_ref_name("")


class TestValidateUsername:
    def test_accepts_normal_username(self):
        assert validate_username("Saltmu") == "Saltmu"

    def test_accepts_bot_username(self):
        assert validate_username("dependabot[bot]") == "dependabot[bot]"

    def test_rejects_shell_metacharacters(self):
        with pytest.raises(ValueError, match="ユーザー名"):
            validate_username("foo; rm -rf /")

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="ユーザー名"):
            validate_username("")

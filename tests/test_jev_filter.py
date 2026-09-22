"""Tests for Jev review finding filter."""

from __future__ import annotations

import json
import urllib.error
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from scripts.jev_filter import (
    DEFAULT_JEV_API_URL,
    JevFindingEvaluation,
    evaluate_finding_with_jev,
    filter_review_findings,
    is_finding_accepted,
)


class TestIsFindingAccepted:
    def test_high_impact_above_threshold_accepted(self) -> None:
        assert is_finding_accepted(validity=0.8, impact="HIGH", threshold=0.7) is True

    def test_medium_impact_at_threshold_accepted(self) -> None:
        assert is_finding_accepted(validity=0.7, impact="MEDIUM", threshold=0.7) is True

    def test_below_threshold_rejected(self) -> None:
        assert is_finding_accepted(validity=0.69, impact="HIGH", threshold=0.7) is False

    def test_low_impact_always_rejected(self) -> None:
        # Even with high validity, LOW impact must be rejected
        assert is_finding_accepted(validity=0.95, impact="LOW", threshold=0.7) is False
        assert is_finding_accepted(validity=1.0, impact="low", threshold=0.7) is False

    def test_case_insensitive_impact(self) -> None:
        assert is_finding_accepted(validity=0.8, impact="high", threshold=0.7) is True
        assert is_finding_accepted(validity=0.8, impact="Medium", threshold=0.7) is True


class TestEvaluateFindingWithJev:
    def test_no_api_key_bypasses_evaluation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("JEV_API_KEY", raising=False)
        result = evaluate_finding_with_jev(
            comment="Fix memory leak",
            path="foo.py",
            line=10,
        )
        # When no API key is set, it should safely bypass (validity 1.0, impact HIGH)
        assert result.validity == 1.0
        assert result.impact == "HIGH"
        assert result.bypassed is True

    def test_successful_api_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JEV_API_KEY", "secret-test-key")
        fake_response_data = {
            "validity": 0.85,
            "impact": "MEDIUM",
            "reason": "Probable issue",
        }

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(fake_response_data).encode("utf-8")
        mock_response.__enter__.return_value = mock_response

        with patch(
            "urllib.request.urlopen", return_value=mock_response
        ) as mock_urlopen:
            result = evaluate_finding_with_jev(
                comment="Potential null pointer",
                path="bar.py",
                line=42,
            )

            assert result.validity == 0.85
            assert result.impact == "MEDIUM"
            assert result.bypassed is False

            # Verify the request
            assert mock_urlopen.call_count == 1
            req = mock_urlopen.call_args[0][0]
            assert req.full_url == DEFAULT_JEV_API_URL
            assert req.headers["Authorization"] == "Bearer secret-test-key"
            assert req.headers["Content-type"] == "application/json"
            body = json.loads(req.data.decode("utf-8"))
            assert body["comment"] == "Potential null pointer"
            assert body["path"] == "bar.py"
            assert body["line"] == 42

    def test_api_error_falls_back_safely(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JEV_API_KEY", "secret-test-key")
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("Network unreachable"),
        ):
            # Should not crash; safe fallback
            result = evaluate_finding_with_jev(
                comment="Some comment",
                path="baz.py",
                line=1,
            )
            assert result.validity == 1.0
            assert result.impact == "HIGH"
            assert result.bypassed is True

    def test_api_key_not_leaked_in_exceptions_or_attributes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secret_key = "super-secret-jev-api-token-12345"
        monkeypatch.setenv("JEV_API_KEY", secret_key)

        with patch("urllib.request.urlopen", side_effect=ValueError("bad request")):
            result = evaluate_finding_with_jev("check", "a.py", 1)
            # Verify string representation of result does not contain the secret key
            assert secret_key not in str(result)
            assert secret_key not in repr(result)

    def test_evaluate_finding_retries_on_transient_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JEV_API_KEY", "secret-test-key")
        fake_response_data = {"validity": 0.88, "impact": "HIGH"}

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(fake_response_data).encode("utf-8")
        mock_response.__enter__.return_value = mock_response

        # Fail twice with 503 HTTPError, succeed on 3rd attempt
        error_503 = urllib.error.HTTPError(
            url=DEFAULT_JEV_API_URL,
            code=503,
            msg="Service Unavailable",
            hdrs={},  # type: ignore[arg-type]
            fp=None,
        )

        with (
            patch(
                "urllib.request.urlopen",
                side_effect=[error_503, error_503, mock_response],
            ) as mock_urlopen,
            patch("time.sleep") as mock_sleep,
        ):
            result = evaluate_finding_with_jev(
                comment="test",
                path="foo.py",
                line=1,
            )
            assert result.validity == 0.88
            assert result.impact == "HIGH"
            assert result.bypassed is False
            assert mock_urlopen.call_count == 3
            assert mock_sleep.call_count == 2

    def test_evaluate_finding_chunks_oversized_comment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JEV_API_KEY", "secret-test-key")
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {"validity": 0.9, "impact": "HIGH"}
        ).encode("utf-8")
        mock_response.__enter__.return_value = mock_response

        huge_comment = "A" * 10000
        with patch(
            "urllib.request.urlopen", return_value=mock_response
        ) as mock_urlopen:
            evaluate_finding_with_jev(huge_comment, path="large.py", line=1)
            req = mock_urlopen.call_args[0][0]
            body = json.loads(req.data.decode("utf-8"))
            # Comment sent to API must be truncated/chunked
            assert len(body["comment"]) < 5000
            assert "[truncated" in body["comment"]


class TestFilterReviewFindings:
    def test_filter_excludes_low_impact_and_low_validity(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("JEV_API_KEY", "test-key")

        inlines = [
            {"path": "a.py", "line": 10, "body": "Real bug"},
            {"path": "b.py", "line": 20, "body": "Nitpick style"},
            {"path": "c.py", "line": 30, "body": "Unlikely edge case"},
        ]

        # Mock evaluate_finding_with_jev to return different evaluations
        evaluations = {
            "Real bug": JevFindingEvaluation(
                validity=0.9, impact="HIGH", bypassed=False
            ),
            "Nitpick style": JevFindingEvaluation(
                validity=0.95, impact="LOW", bypassed=False
            ),
            "Unlikely edge case": JevFindingEvaluation(
                validity=0.4, impact="MEDIUM", bypassed=False
            ),
        }

        def mock_eval(
            comment: str, path: str = "", line: Any = "", **kwargs: Any
        ) -> JevFindingEvaluation:
            return evaluations[comment]

        with patch(
            "scripts.jev_filter.evaluate_finding_with_jev", side_effect=mock_eval
        ):
            filtered = filter_review_findings(inlines, bot_name="claude", threshold=0.7)

            # Only "Real bug" should survive
            assert len(filtered) == 1
            assert filtered[0]["body"] == "Real bug"

            # Check stderr for structured logs
            captured = capsys.readouterr()
            log_lines = [
                json.loads(line)
                for line in captured.err.splitlines()
                if line.strip().startswith("{")
            ]
            assert len(log_lines) == 3

            # Verify each logged event
            real_bug_log = next(
                log for log in log_lines if log["comment"] == "Real bug"
            )
            assert real_bug_log["accepted"] is True
            assert real_bug_log["impact"] == "HIGH"
            assert real_bug_log["validity"] == 0.9

            nitpick_log = next(
                log for log in log_lines if log["comment"] == "Nitpick style"
            )
            assert nitpick_log["accepted"] is False
            assert nitpick_log["impact"] == "LOW"

            edge_log = next(
                log for log in log_lines if log["comment"] == "Unlikely edge case"
            )
            assert edge_log["accepted"] is False
            assert edge_log["validity"] == 0.4

    def test_filter_bypasses_when_no_api_key(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("JEV_API_KEY", raising=False)
        inlines = [
            {"path": "a.py", "line": 10, "body": "Real bug"},
            {"path": "b.py", "line": 20, "body": "Nitpick style"},
        ]
        filtered = filter_review_findings(inlines, bot_name="claude")
        assert len(filtered) == 2
        assert filtered == inlines


class TestJevFilterIntegrationWithWaitForReview:
    @patch("scripts.wait_for_review._get_pr_data", autospec=True)
    def test_wait_for_review_filters_out_low_impact_to_converge(
        self, mock_get_data: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scripts.review_verdict import EXIT_NO_FINDINGS
        from scripts.wait_for_review import wait_for_review

        monkeypatch.setenv("JEV_API_KEY", "test-key")

        trigger_comment = {
            "id": 1,
            "user": {"login": "human"},
            "created_at": "2026-09-22T07:59:00Z",
            "body": "@claude review",
        }
        review_summary = {
            "id": 10,
            "user": {"login": "claude[bot]"},
            "created_at": "2026-09-22T08:00:00Z",
            "body": "LGTM, all checks passed without blocking issues.",
        }
        inline_comment = {
            "id": 11,
            "user": {"login": "claude[bot]"},
            "created_at": "2026-09-22T08:00:00Z",
            "path": "sample.py",
            "line": 5,
            "body": "Consider adding a comment here (style/nitpick)",
        }

        mock_get_data.return_value = {
            "issue_comments": [trigger_comment],
            "reviews": [review_summary],
            "inline_comments": [inline_comment],
        }

        # Mock Jev to mark this inline comment as LOW impact
        with patch(
            "scripts.jev_filter.evaluate_finding_with_jev",
            return_value=JevFindingEvaluation(
                validity=0.9, impact="LOW", bypassed=False
            ),
        ):
            result = wait_for_review(
                pr_number=1011,
                post_trigger=False,
                bot_name="claude",
                timeout=0,
                interval=0,
            )

            # Finding was filtered out; inline_comments became empty
            assert result["inline_comments"] == []
            # Because body is clean and inlines are now empty, verdict converges to 0 (EXIT_NO_FINDINGS)
            assert result["verdict"] == EXIT_NO_FINDINGS

    @patch("scripts.wait_for_review._get_pr_data", autospec=True)
    def test_wait_for_review_keeps_high_impact_finding(
        self, mock_get_data: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scripts.review_verdict import EXIT_FINDINGS_PRESENT
        from scripts.wait_for_review import wait_for_review

        monkeypatch.setenv("JEV_API_KEY", "test-key")

        trigger_comment = {
            "id": 1,
            "user": {"login": "human"},
            "created_at": "2026-09-22T07:59:00Z",
            "body": "@claude review",
        }
        review_summary = {
            "id": 10,
            "user": {"login": "claude[bot]"},
            "created_at": "2026-09-22T08:00:00Z",
            "body": "LGTM otherwise, but please check inline.",
        }
        inline_comment = {
            "id": 11,
            "user": {"login": "claude[bot]"},
            "created_at": "2026-09-22T08:00:00Z",
            "path": "sample.py",
            "line": 5,
            "body": "Critical security bug",
        }

        mock_get_data.return_value = {
            "issue_comments": [trigger_comment],
            "reviews": [review_summary],
            "inline_comments": [inline_comment],
        }

        # Mock Jev to mark this inline comment as HIGH impact
        with patch(
            "scripts.jev_filter.evaluate_finding_with_jev",
            return_value=JevFindingEvaluation(
                validity=0.95, impact="HIGH", bypassed=False
            ),
        ):
            result = wait_for_review(
                pr_number=1011,
                post_trigger=False,
                bot_name="claude",
                timeout=0,
                interval=0,
            )

            assert len(result["inline_comments"]) == 1
            assert result["verdict"] == EXIT_FINDINGS_PRESENT

    @patch("scripts.wait_for_review._get_pr_data", autospec=True)
    def test_wait_for_review_filters_out_findings_even_when_bot_body_says_fail(
        self, mock_get_data: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scripts.review_verdict import EXIT_NO_FINDINGS
        from scripts.wait_for_review import wait_for_review

        monkeypatch.setenv("JEV_API_KEY", "test-key")

        trigger_comment = {
            "id": 1,
            "user": {"login": "human"},
            "created_at": "2026-09-22T07:59:00Z",
            "body": "@claude review",
        }
        review_summary = {
            "id": 10,
            "user": {"login": "claude[bot]"},
            "created_at": "2026-09-22T08:00:00Z",
            "body": "### Findings\n🔴 blocking bug: potential race condition\n\nMarking this **FAIL**.",
        }
        inline_comment = {
            "id": 11,
            "user": {"login": "claude[bot]"},
            "created_at": "2026-09-22T08:00:00Z",
            "path": "sample.py",
            "line": 5,
            "body": "Defensive check suggested for race condition",
        }

        mock_get_data.return_value = {
            "issue_comments": [trigger_comment],
            "reviews": [review_summary],
            "inline_comments": [inline_comment],
        }

        # Mock Jev to mark this inline comment as LOW impact (speculative)
        with patch(
            "scripts.jev_filter.evaluate_finding_with_jev",
            return_value=JevFindingEvaluation(
                validity=0.5, impact="LOW", bypassed=False
            ),
        ):
            result = wait_for_review(
                pr_number=1011,
                post_trigger=False,
                bot_name="claude",
                timeout=0,
                interval=0,
            )

            # Even though review_body said FAIL and had 🔴, all findings were rejected by Jev,
            # so verdict must converge to 0 (EXIT_NO_FINDINGS)
            assert result["inline_comments"] == []
            assert result["verdict"] == EXIT_NO_FINDINGS

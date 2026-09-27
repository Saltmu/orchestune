"""Tests for Jev review finding filter."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from scripts.jev_filter import (
    DEFAULT_JEV_API_URL,
    DEFAULT_JEV_BASE_URL,
    DEFAULT_JEV_LOG_PATH,
    JevFindingEvaluation,
    evaluate_finding_with_jev,
    evaluate_review_findings,
    filter_review_findings,
    is_finding_accepted,
)


def _api_response(validity: float, impact: str) -> dict[str, Any]:
    return {
        "model": "jev-1.13.0",
        "answers": {
            "validity": {"type": "noul", "noul": validity},
            "impact": {
                "type": "choice",
                "choice": impact,
                "probabilities": {
                    key: float(key == impact) for key in ("LOW", "MEDIUM", "HIGH")
                },
                "confidence": 1.0,
            },
        },
        "usage": {"input_tokens": 300, "output_tokens": 30},
    }


@pytest.fixture(autouse=True)
def _no_network_sha_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    """The `wait_for_review()` integration tests below don't exercise SHA/repo
    metadata; keep them hermetic instead of hitting `gh` for real."""
    monkeypatch.setattr(
        "scripts.wait_for_review._fetch_pr_head_sha", lambda pr_number: None
    )
    monkeypatch.setattr("scripts.wait_for_review._fetch_repository_slug", lambda: None)


class TestJevUrls:
    def test_default_jev_base_and_api_urls(self) -> None:
        assert DEFAULT_JEV_BASE_URL == "https://api.typesafe.ai/v1"
        assert DEFAULT_JEV_API_URL == "https://api.typesafe.ai/v1/systemone"

    def test_base_url_argument_appends_systemone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JEV_API_KEY", "test-key")
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(_api_response(0.9, "HIGH")).encode(
            "utf-8"
        )
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            evaluate_finding_with_jev(
                comment="test",
                base_url="https://api.typesafe.ai/v1",
            )
            req = mock_urlopen.call_args[0][0]
            assert req.full_url == "https://api.typesafe.ai/v1/systemone"

    def test_base_url_argument_with_trailing_slash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JEV_API_KEY", "test-key")
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(_api_response(0.9, "HIGH")).encode(
            "utf-8"
        )
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            evaluate_finding_with_jev(
                comment="test",
                base_url="https://api.typesafe.ai/v1/",
            )
            req = mock_urlopen.call_args[0][0]
            assert req.full_url == "https://api.typesafe.ai/v1/systemone"

    def test_base_url_argument_already_ends_with_systemone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JEV_API_KEY", "test-key")
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(_api_response(0.9, "HIGH")).encode(
            "utf-8"
        )
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            evaluate_finding_with_jev(
                comment="test",
                base_url="https://custom.host/v1/systemone",
            )
            req = mock_urlopen.call_args[0][0]
            assert req.full_url == "https://custom.host/v1/systemone"

    def test_jev_base_url_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JEV_API_KEY", "test-key")
        monkeypatch.setenv("JEV_BASE_URL", "https://env.example.com/v1")
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(_api_response(0.9, "HIGH")).encode(
            "utf-8"
        )
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            evaluate_finding_with_jev(comment="test")
            req = mock_urlopen.call_args[0][0]
            assert req.full_url == "https://env.example.com/v1/systemone"

    def test_jev_api_url_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JEV_API_KEY", "test-key")
        monkeypatch.setenv("JEV_API_URL", "https://env.example.com/v1/systemone")
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(_api_response(0.9, "HIGH")).encode(
            "utf-8"
        )
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            evaluate_finding_with_jev(comment="test")
            req = mock_urlopen.call_args[0][0]
            assert req.full_url == "https://env.example.com/v1/systemone"

    def test_jev_api_url_env_var_preserved_exact(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JEV_API_KEY", "test-key")
        monkeypatch.setenv("JEV_API_URL", "https://proxy.example/jev")
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(_api_response(0.9, "HIGH")).encode(
            "utf-8"
        )
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            evaluate_finding_with_jev(comment="test")
            req = mock_urlopen.call_args[0][0]
            assert req.full_url == "https://proxy.example/jev"

    def test_base_url_argument_precedence_over_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JEV_API_KEY", "test-key")
        monkeypatch.setenv("JEV_BASE_URL", "https://ignored.com/v1")
        monkeypatch.setenv("JEV_API_URL", "https://ignored.com/v1/systemone")
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(_api_response(0.9, "HIGH")).encode(
            "utf-8"
        )
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            evaluate_finding_with_jev(
                comment="test",
                base_url="https://explicit.com/v1",
            )
            req = mock_urlopen.call_args[0][0]
            assert req.full_url == "https://explicit.com/v1/systemone"


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
        fake_response_data = _api_response(0.85, "MEDIUM")

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

            assert result.raw_response == fake_response_data

            # Verify the request
            assert mock_urlopen.call_count == 1
            req = mock_urlopen.call_args[0][0]
            assert req.full_url == DEFAULT_JEV_API_URL
            assert req.headers["Authorization"] == "Bearer secret-test-key"
            assert req.headers["Content-type"] == "application/json"
            body = json.loads(req.data.decode("utf-8"))
            assert body["state"]["comment"] == "Potential null pointer"
            assert body["state"]["path"] == "bar.py"
            assert body["state"]["line"] == 42
            assert req.method == "POST"
            assert body["model"] == "jev-latest"
            assert set(body) == {"model", "state", "questions"}
            assert set(body["questions"]) == {"validity", "impact", "applicability"}
            assert body["questions"]["validity"]["type"] == "noul"
            assert body["questions"]["impact"]["type"] == "choice"
            assert set(body["questions"]["impact"]["criteria"]) == {
                "LOW",
                "MEDIUM",
                "HIGH",
            }
            for question in body["questions"].values():
                assert "`comment`" in question["instructions"]

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

    @pytest.mark.parametrize("status", [429, 503, 529])
    def test_evaluate_finding_retries_on_transient_error(
        self, monkeypatch: pytest.MonkeyPatch, status: int
    ) -> None:
        monkeypatch.setenv("JEV_API_KEY", "secret-test-key")
        fake_response_data = _api_response(0.88, "HIGH")

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(fake_response_data).encode("utf-8")
        mock_response.__enter__.return_value = mock_response

        # Fail twice with a transient HTTPError, succeed on 3rd attempt
        error_503 = urllib.error.HTTPError(
            url=DEFAULT_JEV_API_URL,
            code=status,
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
        mock_response.read.return_value = json.dumps(_api_response(0.9, "HIGH")).encode(
            "utf-8"
        )
        mock_response.__enter__.return_value = mock_response

        huge_comment = "A" * 10000
        with patch(
            "urllib.request.urlopen", return_value=mock_response
        ) as mock_urlopen:
            evaluate_finding_with_jev(huge_comment, path="large.py", line=1)
            req = mock_urlopen.call_args[0][0]
            body = json.loads(req.data.decode("utf-8"))
            # Comment sent to API must be truncated/chunked
            assert len(body["state"]["comment"]) < 5000
            assert "[truncated" in body["state"]["comment"]


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


class TestEvaluateReviewFindings:
    """Structured report: findings are annotated, never dropped from the report."""

    def test_no_api_key_marks_every_finding_not_evaluated_and_keeps_all(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("JEV_API_KEY", raising=False)
        inlines = [
            {"id": 1, "path": "a.py", "line": 10, "body": "Real bug"},
            {"id": 2, "path": "b.py", "line": 20, "body": "Nitpick"},
        ]

        result = evaluate_review_findings(inlines, bot_name="claude")

        assert result["kept"] == inlines
        assert [e["decision"] for e in result["jev_evaluations"]] == [
            "not_evaluated",
            "not_evaluated",
        ]
        assert [e["finding_id"] for e in result["jev_evaluations"]] == [1, 2]
        assert all(
            e["decision_reason"] == "no_api_key" for e in result["jev_evaluations"]
        )

    def test_mixed_decisions_annotate_every_finding_even_when_filtered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JEV_API_KEY", "test-key")
        inlines = [
            {"id": 1, "path": "a.py", "line": 10, "body": "Real bug"},
            {"id": 2, "path": "b.py", "line": 20, "body": "Nitpick style"},
        ]
        evaluations = {
            "Real bug": JevFindingEvaluation(validity=0.9, impact="HIGH"),
            "Nitpick style": JevFindingEvaluation(validity=0.95, impact="LOW"),
        }

        def mock_eval(
            comment: str, path: str = "", line: Any = "", **kwargs: Any
        ) -> JevFindingEvaluation:
            return evaluations[comment]

        with patch(
            "scripts.jev_filter.evaluate_finding_with_jev", side_effect=mock_eval
        ):
            result = evaluate_review_findings(inlines, bot_name="claude", threshold=0.7)

        # kept mirrors the legacy filtered-list contract.
        assert [item["id"] for item in result["kept"]] == [1]
        # But the report still carries an entry for the filtered finding too —
        # its body is not lost, only annotated as filtered.
        assert len(result["jev_evaluations"]) == 2
        by_id = {e["finding_id"]: e for e in result["jev_evaluations"]}
        assert by_id[1]["decision"] == "kept"
        assert by_id[1]["decision_reason"] == "accepted"
        assert by_id[2]["decision"] == "filtered"
        assert by_id[2]["decision_reason"] == "low_impact"

    def test_api_failure_marks_bypassed_and_keeps_finding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JEV_API_KEY", "test-key")
        inlines = [{"id": 7, "path": "a.py", "line": 1, "body": "Something"}]

        with patch(
            "scripts.jev_filter.evaluate_finding_with_jev",
            return_value=JevFindingEvaluation(
                validity=1.0, impact="HIGH", bypassed=True
            ),
        ):
            result = evaluate_review_findings(inlines, bot_name="claude", threshold=0.7)

        assert [item["id"] for item in result["kept"]] == [7]
        assert result["jev_evaluations"][0]["decision"] == "bypassed"

    def test_finding_without_id_falls_back_to_index(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("JEV_API_KEY", raising=False)
        inlines = [{"path": "a.py", "line": 1, "body": "no id here"}]

        result = evaluate_review_findings(inlines, bot_name="claude")

        assert result["jev_evaluations"][0]["finding_id"] == "index:0"

    def test_explicit_null_id_is_also_treated_as_id_less(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`dict.get("id", index)` only falls back on a *missing* key, so an
        offline/MCP snapshot serializing id-less findings as `"id": null`
        would otherwise collide every one onto the same `None` finding_id
        (Codex PR #1114 round 5 finding)."""
        monkeypatch.delenv("JEV_API_KEY", raising=False)
        inlines = [
            {"id": None, "path": "a.py", "line": 1, "body": "first"},
            {"id": None, "path": "b.py", "line": 2, "body": "second"},
        ]

        result = evaluate_review_findings(inlines, bot_name="claude")

        finding_ids = [e["finding_id"] for e in result["jev_evaluations"]]
        assert finding_ids == ["index:0", "index:1"]

    def test_fallback_id_never_collides_with_a_coincidentally_equal_supplied_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fallback id for an id-less finding must not collide with a
        real supplied id equal to that positional index, e.g. an id-less
        finding at position 0 alongside another finding explicitly numbered
        `0` (Codex PR #1114 round 6 finding)."""
        monkeypatch.delenv("JEV_API_KEY", raising=False)
        inlines = [
            {"path": "a.py", "line": 1, "body": "id-less, position 0"},
            {"id": 0, "path": "b.py", "line": 2, "body": "explicitly id 0"},
        ]

        result = evaluate_review_findings(inlines, bot_name="claude")

        finding_ids = [e["finding_id"] for e in result["jev_evaluations"]]
        assert len(set(finding_ids)) == 2
        assert 0 in finding_ids
        assert "index:0" in finding_ids

    def test_filter_review_findings_is_a_thin_wrapper_over_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("JEV_API_KEY", raising=False)
        inlines = [{"id": 1, "path": "a.py", "line": 1, "body": "x"}]
        assert (
            filter_review_findings(inlines, bot_name="claude")
            == evaluate_review_findings(inlines, bot_name="claude")["kept"]
        )


class TestJevFilterIntegrationWithWaitForReview:
    @patch("scripts.wait_for_review._get_pr_data", autospec=True)
    def test_wait_for_review_annotates_low_impact_as_filtered_without_dropping_it(
        self, mock_get_data: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Jev filtering must never remove a finding from `inline_comments` — it
        only annotates `jev_evaluations`; the LLM decides what to do with it."""
        from scripts.review_verdict import ACQUISITION_ACQUIRED
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

            # The finding stays in inline_comments — it is the source of truth —
            # but the report marks it filtered rather than dropping it.
            assert result["acquisition_status"] == ACQUISITION_ACQUIRED
            assert len(result["inline_comments"]) == 1
            assert result["inline_comments"][0]["id"] == 11
            assert len(result["jev_evaluations"]) == 1
            assert result["jev_evaluations"][0]["finding_id"] == 11
            assert result["jev_evaluations"][0]["decision"] == "filtered"
            assert result["jev_evaluations"][0]["decision_reason"] == "low_impact"

    @patch("scripts.wait_for_review._get_pr_data", autospec=True)
    def test_wait_for_review_keeps_high_impact_finding(
        self, mock_get_data: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
            assert result["jev_evaluations"][0]["decision"] == "kept"
            assert result["jev_evaluations"][0]["decision_reason"] == "accepted"

    @patch("scripts.wait_for_review._get_pr_data", autospec=True)
    def test_wait_for_review_annotates_findings_as_filtered_even_when_bot_body_says_fail(
        self, mock_get_data: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

            # Even though review_body said FAIL and had 🔴, Jev marks the
            # finding filtered — but it still appears in inline_comments and
            # review_body for the calling LLM to weigh for itself.
            assert len(result["inline_comments"]) == 1
            assert result["jev_evaluations"][0]["decision"] == "filtered"
            assert "FAIL" in result["review_body"]


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"validity": 0.8, "impact": "LOW"},
        {"answers": {"validity": {"type": "noul", "noul": 0.8}}},
        _api_response(-0.1, "LOW"),
        _api_response(1.1, "LOW"),
        _api_response(float("nan"), "LOW"),
        _api_response(0.8, "UNKNOWN"),
        _api_response(True, "LOW"),
        _api_response("0.8", "LOW"),  # type: ignore[arg-type]
    ],
)
def test_invalid_api_answers_bypass_filter(data: dict[str, Any]) -> None:
    response = MagicMock()
    response.read.return_value = json.dumps(data).encode()
    response.__enter__.return_value = response
    with patch("urllib.request.urlopen", return_value=response):
        result = evaluate_finding_with_jev("finding", api_key="test-key")
    assert result.bypassed is True
    assert result.validity == 1.0
    assert result.impact == "HIGH"


@pytest.mark.parametrize(
    "validity,impact", [(0.0, "LOW"), (1.0, "HIGH"), (0.3, "MEDIUM")]
)
def test_system_one_answers_drive_filter(validity: float, impact: str) -> None:
    response = MagicMock()
    response.read.return_value = json.dumps(_api_response(validity, impact)).encode()
    response.__enter__.return_value = response
    finding = {"body": "finding", "path": "a.py", "line": 3}
    with patch("urllib.request.urlopen", return_value=response):
        result = filter_review_findings([finding], api_key="test-key")
    assert result == ([finding] if validity >= 0.7 and impact != "LOW" else [])


class TestJevLogPersistence:
    def test_default_jev_log_path(self) -> None:
        assert DEFAULT_JEV_LOG_PATH == ".orchestune/jev/evaluations.jsonl"

    def test_filter_falls_back_to_default_log_path_when_env_var_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("JEV_LOG_PATH", raising=False)
        default_target = tmp_path / "default_evals.jsonl"
        monkeypatch.setattr(
            "scripts.jev_filter.DEFAULT_JEV_LOG_PATH", str(default_target)
        )

        response = MagicMock()
        response.read.return_value = json.dumps(_api_response(0.9, "HIGH")).encode()
        response.__enter__.return_value = response

        finding = {"body": "Defect", "path": "a.py", "line": 1}
        with patch("urllib.request.urlopen", return_value=response):
            filter_review_findings([finding], api_key="test-key")

        assert default_target.exists()
        lines = default_target.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1

    def test_filter_persists_evaluation_to_specified_log_path(
        self, tmp_path: Path
    ) -> None:
        log_file = tmp_path / "sub" / "evaluations.jsonl"
        response = MagicMock()
        response.read.return_value = json.dumps(_api_response(0.9, "HIGH")).encode()
        response.__enter__.return_value = response

        finding = {"body": "Defect in logic", "path": "core.py", "line": 100}
        with patch("urllib.request.urlopen", return_value=response):
            result = filter_review_findings(
                [finding],
                bot_name="codex",
                api_key="test-key",
                log_path=log_file,
                pr=1082,
            )

        assert len(result) == 1
        assert log_file.exists()
        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["reviewer"] == "codex"
        assert record["pr"] == 1082
        assert record["path"] == "core.py"
        assert record["line"] == 100
        assert record["comment"] == "Defect in logic"
        assert record["validity"] == 0.9
        assert record["impact"] == "HIGH"
        assert record["accepted"] is True
        assert record["bypassed"] is False
        assert "timestamp" in record

    def test_filter_persists_to_env_var_log_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_log = tmp_path / "env_evals.jsonl"
        monkeypatch.setenv("JEV_LOG_PATH", str(env_log))

        response = MagicMock()
        response.read.return_value = json.dumps(_api_response(0.5, "LOW")).encode()
        response.__enter__.return_value = response

        finding = {"body": "Minor typo", "path": "README.md", "line": 1}
        with patch("urllib.request.urlopen", return_value=response):
            result = filter_review_findings(
                [finding],
                bot_name="claude",
                api_key="test-key",
            )

        assert len(result) == 0
        assert env_log.exists()
        lines = env_log.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["reviewer"] == "claude"
        assert record["pr"] is None
        assert record["validity"] == 0.5
        assert record["impact"] == "LOW"
        assert record["accepted"] is False
        assert record["bypassed"] is False

    def test_filter_appends_multiple_evaluations(self, tmp_path: Path) -> None:
        log_file = tmp_path / "evals.jsonl"
        response1 = MagicMock()
        response1.read.return_value = json.dumps(_api_response(0.85, "HIGH")).encode()
        response1.__enter__.return_value = response1

        response2 = MagicMock()
        response2.read.return_value = json.dumps(_api_response(0.4, "LOW")).encode()
        response2.__enter__.return_value = response2

        findings = [
            {"body": "Issue 1", "path": "a.py", "line": 10},
            {"body": "Issue 2", "path": "b.py", "line": 20},
        ]
        with patch("urllib.request.urlopen", side_effect=[response1, response2]):
            filter_review_findings(
                findings,
                api_key="test-key",
                log_path=log_file,
            )

        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2

        # Second call to ensure append, not overwrite
        response3 = MagicMock()
        response3.read.return_value = json.dumps(_api_response(0.95, "HIGH")).encode()
        response3.__enter__.return_value = response3
        with patch("urllib.request.urlopen", return_value=response3):
            filter_review_findings(
                [{"body": "Issue 3", "path": "c.py", "line": 30}],
                api_key="test-key",
                log_path=log_file,
            )

        lines_after = log_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines_after) == 3

    def test_filter_handles_write_error_gracefully(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        log_file = tmp_path / "restricted.jsonl"
        response = MagicMock()
        response.read.return_value = json.dumps(_api_response(0.9, "HIGH")).encode()
        response.__enter__.return_value = response

        finding = {"body": "Defect", "path": "core.py", "line": 10}
        with patch("builtins.open", side_effect=OSError("Disk full")):
            with patch("urllib.request.urlopen", return_value=response):
                result = filter_review_findings(
                    [finding],
                    api_key="test-key",
                    log_path=log_file,
                )

        # Filtering should still succeed despite log write failure
        assert len(result) == 1
        captured = capsys.readouterr()
        assert "Warning: Failed to write Jev evaluation log" in captured.err

    def test_filter_logs_when_bypassed_due_to_api_error(self, tmp_path: Path) -> None:
        log_file = tmp_path / "bypassed.jsonl"
        finding = {"body": "Potential issue", "path": "x.py", "line": 5}
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            result = filter_review_findings(
                [finding],
                api_key="test-key",
                log_path=log_file,
            )

        assert len(result) == 1  # Bypassed findings are preserved
        assert log_file.exists()
        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["bypassed"] is True
        assert record["accepted"] is True


def test_speculative_requires_evidence_and_bypass_wins() -> None:
    context = {
        "schema_version": 2,
        "pr": {},
        "code": {
            "status": "available",
            "text": "def run(): pass",
            "source": "git_blob",
            "commit_sha": "a" * 40,
            "side": "RIGHT",
            "start_line": 1,
        },
        "execution": {
            "evidence": [
                {
                    "source": "module_description",
                    "commit_sha": "a" * 40,
                    "text": "Only documented local inputs are used.",
                }
            ]
        },
        "repository_rules": {
            "status": "available",
            "text": "Validate external inputs.",
            "source": ".agents/AGENTS.md",
            "commit_sha": "b" * 40,
        },
        "missing": [],
        "truncated": [],
    }
    kwargs: dict[str, Any] = {
        "applicability": "SPECULATIVE",
        "applicability_confidence": 0.95,
    }
    assert not is_finding_accepted(0.99, "HIGH", context=context, **kwargs)
    assert is_finding_accepted(0.99, "HIGH", **kwargs)
    assert is_finding_accepted(
        0.99, "HIGH", context={**context, "missing": ["rules"]}, **kwargs
    )
    assert is_finding_accepted(0.99, "HIGH", context=context, applicability="UNKNOWN")
    assert is_finding_accepted(0, "LOW", 2, bypassed=True)


def test_new_axis_payload_and_legacy_response() -> None:
    from scripts.jev_filter import _build_payload, _parse_evaluation

    payload = json.loads(_build_payload("finding", "scripts/a.py", 4, context={}))
    assert payload["state"]["context"]["schema_version"] == 2
    assert payload["questions"]["applicability"]["type"] == "choice"
    assert _parse_evaluation(_api_response(0.9, "HIGH")).applicability == "UNKNOWN"


@pytest.mark.parametrize(
    "answer",
    [
        None,
        {},
        {"type": "choice", "choice": "BAD", "confidence": 1},
        {"type": "choice", "choice": "SPECULATIVE", "confidence": float("nan")},
        {"type": "choice", "choice": "SPECULATIVE", "confidence": True},
        {"type": "choice", "choice": "SPECULATIVE", "confidence": 1.1},
        {"type": "choice", "choice": "SPECULATIVE", "confidence": -0.1},
        {"type": "choice", "choice": "SPECULATIVE", "confidence": float("inf")},
        {
            "type": "choice",
            "choice": "SPECULATIVE",
            "confidence": 0.95,
            "probabilities": {"APPLICABLE": 0.1, "SPECULATIVE": 0.95, "UNKNOWN": 0.1},
        },
        {
            "type": "choice",
            "choice": "SPECULATIVE",
            "confidence": 0.95,
            "probabilities": {"APPLICABLE": 0.96, "SPECULATIVE": 0.03, "UNKNOWN": 0.01},
        },
    ],
)
def test_invalid_new_answer_keeps_legacy_evaluation(answer: Any) -> None:
    from scripts.jev_filter import _parse_evaluation

    response = _api_response(0.99, "HIGH")
    response["answers"]["applicability"] = answer
    evaluation = _parse_evaluation(response)
    assert evaluation.applicability == "UNKNOWN"
    assert evaluation.applicability_confidence is None
    assert not evaluation.bypassed
    assert is_finding_accepted(evaluation.validity, evaluation.impact)


@pytest.mark.parametrize(
    ("choice", "confidence", "probabilities"),
    [
        (
            "SPECULATIVE",
            0.95,
            {"APPLICABLE": 0.03, "SPECULATIVE": 0.95, "UNKNOWN": 0.02},
        ),
        (
            "APPLICABLE",
            0.21,
            {"APPLICABLE": 0.48, "SPECULATIVE": 0.11, "UNKNOWN": 0.41},
        ),
        (
            "SPECULATIVE",
            0.95,
            {"APPLICABLE": 0.08, "SPECULATIVE": 0.9, "UNKNOWN": 0.02},
        ),
    ],
)
def test_valid_new_answer_parses_choice_confidence(
    choice: str, confidence: float, probabilities: dict[str, float]
) -> None:
    from scripts.jev_filter import _parse_evaluation

    response = _api_response(0.99, "HIGH")
    response["answers"]["applicability"] = {
        "type": "choice",
        "choice": choice,
        "confidence": confidence,
        "probabilities": probabilities,
    }
    evaluation = _parse_evaluation(response)
    assert evaluation.applicability == choice
    assert evaluation.applicability_confidence == confidence


def test_bypass_retains_finding_above_one_threshold(tmp_path: Path) -> None:
    finding = {"body": "bug", "path": "a.py", "line": 2}
    with patch(
        "scripts.jev_filter.evaluate_finding_with_jev",
        return_value=JevFindingEvaluation(1, "HIGH", bypassed=True),
    ):
        assert filter_review_findings(
            [finding], api_key="test-key", threshold=2, log_path=tmp_path / "log"
        ) == [finding]
    record = json.loads((tmp_path / "log").read_text())
    assert record["decision_reason"] == "bypass"
    assert record["schema_version"] == 2
    assert "text" not in record["context"]["code"]


def test_utf8_total_payload_is_bounded_and_marks_omissions() -> None:
    from scripts.jev_filter import _build_payload

    context = {
        "pr": {"title": "語" * 300, "body": "語" * 3000},
        "code": {
            "status": "available",
            "text": "語" * 6000,
            "module_description": "語" * 2000,
        },
        "execution": {
            "evidence": [{"source": "module_description", "text": "語" * 2000}]
        },
        "repository_rules": {"status": "available", "text": "語" * 4000},
    }
    payload = _build_payload("語" * 4000, "scripts/a.py", 1, context=context)
    assert len(payload) <= 32 * 1024
    sent = json.loads(payload)
    assert sent["state"]["context"]["truncated"]
    assert is_finding_accepted(
        0.99,
        "HIGH",
        context=sent["state"]["context"],
        applicability="SPECULATIVE",
        applicability_confidence=0.95,
    )

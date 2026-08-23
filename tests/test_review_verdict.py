from __future__ import annotations

import json
import sys

import pytest

from scripts.review_verdict import (
    EXIT_FINDINGS_PRESENT,
    EXIT_NO_FINDINGS,
    EXIT_UNDETERMINED,
    evaluate_review_state,
    evaluate_review_verdict,
    normalize_review_state,
)


def test_codex_boilerplate_with_inline_finding_is_not_a_clean_pass():
    state = {
        "reviews": [
            {
                "id": 1,
                "user": {"login": "chatgpt-codex-connector[bot]"},
                "submitted_at": "2026-08-20T10:00:00Z",
                "body": "Here are some automated review suggestions.",
            }
        ],
        "inline_comments": [
            {
                "id": 2,
                "user": {"login": "chatgpt-codex-connector[bot]"},
                "created_at": "2026-08-20T10:00:00Z",
                "path": "app.py",
                "line": 42,
                "body": "Validate this input before using it.",
            }
        ],
    }

    result = evaluate_review_state(state, bot_name="codex")

    assert result is not None
    assert result["verdict"] == EXIT_FINDINGS_PRESENT
    assert result["inline_comments"] == [
        {
            "path": "app.py",
            "line": 42,
            "body": "Validate this input before using it.",
        }
    ]


def test_codex_clean_pass_without_inlines_returns_clean_exit_code():
    assert (
        evaluate_review_verdict(
            "Didn't find any major issues. Nice work!", bot_name="codex"
        )
        == EXIT_NO_FINDINGS
    )


def test_codex_boilerplate_without_inline_findings_is_a_clean_pass():
    assert (
        evaluate_review_verdict(
            "Here are some automated review suggestions.", bot_name="codex"
        )
        == EXIT_NO_FINDINGS
    )


def test_claude_without_blocking_issues_remains_a_clean_pass():
    assert (
        evaluate_review_verdict("Reviewed without any blocking issues.")
        == EXIT_NO_FINDINGS
    )


def test_external_state_uses_the_same_verdict_contract_as_direct_evaluation():
    state = {
        "issue_comments": [],
        "reviews": [
            {
                "id": 1,
                "user": {"login": "codex[bot]"},
                "submitted_at": "2026-08-20T10:00:00Z",
                "body": "Didn't find any major issues.",
            }
        ],
        "inline_comments": [],
    }

    result = evaluate_review_state(state, bot_name="codex")

    assert result is not None
    assert result["verdict"] == evaluate_review_verdict(
        result["review_body"], result["inline_comments"], bot_name="codex"
    )
    assert result["verdict"] == EXIT_NO_FINDINGS


def test_external_state_uses_the_same_latest_item_tiebreak_as_gh_polling(capsys):
    from scripts.wait_for_review import _extract_review_result

    state = {
        "issue_comments": [
            {
                "id": 1,
                "user": {"login": "claude[bot]"},
                "created_at": "2026-08-20T10:00:00Z",
                "body": "LGTM",
            }
        ],
        "reviews": [
            {
                "id": 2,
                "user": {"login": "claude[bot]"},
                "submitted_at": "2026-08-20T10:00:00Z",
                "body": "### Findings\n- [ ] Bug: validate the input.",
            }
        ],
        "inline_comments": [],
    }

    result = evaluate_review_state(state)
    gh_result = _extract_review_result(state, "claude")

    assert gh_result is not None
    assert result["review_body"] == gh_result["review_body"]
    assert result["verdict"] == EXIT_FINDINGS_PRESENT
    capsys.readouterr()


def test_empty_external_state_is_undetermined():
    assert evaluate_review_state({}, bot_name="codex") == {
        "review_body": "",
        "inline_comments": [],
        "timestamp": "",
        "verdict": EXIT_UNDETERMINED,
    }


def test_normalize_review_state_rejects_non_list_sections():
    with pytest.raises(ValueError, match="reviews"):
        normalize_review_state({"reviews": {"id": 1}})


def test_cli_evaluates_external_state_without_a_pr_or_gh(tmp_path, capsys, monkeypatch):
    from scripts.wait_for_review import main

    state_file = tmp_path / "review-state.json"
    state_file.write_text(
        json.dumps(
            {
                "reviews": [
                    {
                        "id": 1,
                        "user": {"login": "codex[bot]"},
                        "submitted_at": "2026-08-20T10:00:00Z",
                        "body": "Here are some automated review suggestions.",
                    }
                ],
                "inline_comments": [
                    {
                        "id": 2,
                        "user": {"login": "codex[bot]"},
                        "created_at": "2026-08-20T10:00:00Z",
                        "path": "app.py",
                        "line": 42,
                        "body": "Validate this input before using it.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "wait_for_review.py",
            "--bot-name",
            "codex",
            "--review-state-file",
            str(state_file),
        ],
    )

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_FINDINGS_PRESENT
    output = capsys.readouterr().out
    assert "[ACTION REQUIRED] Inline Findings: 1 item(s)" in output
    assert "--- Inline Finding 1: app.py:42 ---" in output
    assert "Validate this input before using it." in output

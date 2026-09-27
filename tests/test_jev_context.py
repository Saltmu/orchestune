"""Revision and evidence contract tests for Jev context."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from scripts.jev_context import (
    JevReviewContext,
    collect_finding_context,
    normalize_context,
)

HEAD = "a" * 40
BASE = "b" * 40
OLD = "c" * 40


def review_context() -> JevReviewContext:
    return JevReviewContext(
        left_sha=BASE,
        pr={
            "number": 1,
            "title": "Title",
            "body": "internal tool",
            "head_sha": HEAD,
            "base_sha": BASE,
        },
    )


def test_right_and_left_revision_selection() -> None:
    for side, sha in (("RIGHT", HEAD), ("LEFT", BASE)):
        with patch(
            "scripts.jev_context._read_blob",
            return_value='"""Documented local command inputs."""\n' + "line\n" * 30,
        ) as read:
            context = collect_finding_context(
                {"path": "scripts/a.py", "line": 15, "side": side, "commit_id": HEAD},
                review_context(),
            )
        assert context["code"]["commit_sha"] == sha
        assert context["code"]["source"] == "git_blob"
        assert read.call_args_list[0].args[:2] == (sha, "scripts/a.py")
        assert context["execution"]["input_trust"] == "unknown"


def test_uncertain_and_old_left_use_original_diff() -> None:
    for item in (
        {"line": 5, "side": "RIGHT", "commit_id": OLD},
        {"line": "N/A", "original_line": 5, "side": "LEFT", "original_commit_id": OLD},
    ):
        with patch("scripts.jev_context._read_blob", return_value="rules"):
            context = collect_finding_context(
                {"path": "a.py", "diff_hunk": "@@ -1 +1 @@\n-old\n+new", **item},
                review_context(),
            )
        assert context["code"]["source"] == "review_diff"
        assert context["code"]["commit_sha"] == OLD


def test_outdated_right_uses_original_revision() -> None:
    with patch("scripts.jev_context._read_blob", return_value="one\ntwo\nthree"):
        context = collect_finding_context(
            {
                "path": "a.py",
                "line": "N/A",
                "original_line": 2,
                "side": "RIGHT",
                "original_commit_id": OLD,
            },
            review_context(),
        )
    assert context["code"]["commit_sha"] == OLD


def test_no_blob_fallback_and_no_path_based_evidence() -> None:
    with patch("scripts.jev_context._read_blob", return_value=None):
        context = collect_finding_context(
            {
                "path": "scripts/a.py",
                "line": 3,
                "side": "RIGHT",
                "commit_id": HEAD,
                "diff_hunk": "+pass",
            },
            review_context(),
        )
    assert context["code"]["source"] == "review_diff"
    assert context["execution"]["evidence"] == []
    assert context["repository_rules"]["status"] == "missing"


def test_normalize_context_bounds_and_invalid_input() -> None:
    raw: dict[str, Any] = {
        "pr": {"title": "語" * 400, "body": "語" * 4000},
        "code": {"status": "available", "text": "語" * 7000},
        "repository_rules": {"status": "available", "text": "語" * 5000},
        "execution": {
            "evidence": [{"source": "module_description", "text": "語" * 3000}]
        },
    }
    normalized = normalize_context(raw)
    assert len(normalized["code"]["text"]) <= 6000
    assert "code.text" in normalized["truncated"]
    assert normalize_context([])["missing"]
    assert json.dumps(normalized, ensure_ascii=False).encode().decode() is not None


def test_blob_validation_never_runs_invalid_arguments() -> None:
    from scripts.jev_context import _read_blob

    with patch("scripts.jev_context.subprocess.run") as run:
        for path in (
            "/etc/passwd",
            "../secret",
            "a/../b",
            "a\\b",
            "a:b",
            "a\x00b",
            "",
            "./a",
        ):
            assert _read_blob(HEAD, path) is None
        assert _read_blob("HEAD", "a.py") is None
        run.assert_not_called()


def test_blob_reads_git_revision_with_limits(tmp_path, monkeypatch) -> None:
    import subprocess

    from scripts.jev_context import MAX_SOURCE_BYTES, _read_blob

    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "config", "user.name", "Test"], check=True)
    (tmp_path / "a.py").write_text("original\n")
    (tmp_path / "binary").write_bytes(b"\x00binary")
    (tmp_path / "large").write_bytes(b"x" * (MAX_SOURCE_BYTES + 1))
    subprocess.run(["git", "add", "."], check=True)
    subprocess.run(["git", "commit", "-qm", "test"], check=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    (tmp_path / "a.py").write_text("wrong working tree\n")
    assert _read_blob(sha, "a.py") == "original\n"
    assert _read_blob(sha, "binary") is None
    assert _read_blob(sha, "large") is None
    assert _read_blob(sha, "deleted.py") is None
    assert _read_blob(HEAD, "a.py") is None


def test_pr_metadata_failure_and_blob_cache() -> None:
    import subprocess

    from scripts.jev_context import collect_review_context

    with patch(
        "scripts.jev_context.subprocess.run",
        side_effect=subprocess.TimeoutExpired("gh", 30),
    ):
        assert collect_review_context(5).missing == ["pr"]
    review = review_context()
    finding = {"path": "a.py", "line": 1, "side": "RIGHT", "commit_id": HEAD}
    with patch("scripts.jev_context._read_blob", return_value="text") as read:
        collect_finding_context(finding, review)
        collect_finding_context(finding, review)
    assert read.call_count == 2  # code + base rules, each exactly once


def test_missing_and_truncated_context_never_satisfies_evidence() -> None:
    from scripts.jev_context import has_speculative_evidence

    context = {
        "pr": {},
        "code": {
            "status": "available",
            "text": "code",
            "source": "git_blob",
            "commit_sha": HEAD,
            "side": "RIGHT",
            "start_line": 1,
        },
        "repository_rules": {
            "status": "available",
            "text": "rules",
            "source": ".agents/AGENTS.md",
            "commit_sha": BASE,
        },
        "execution": {
            "evidence": [
                {
                    "source": "module_description",
                    "text": "Documented inputs",
                    "commit_sha": HEAD,
                }
            ]
        },
    }
    assert has_speculative_evidence(context)
    for marker in (
        "code.text",
        "repository_rules.text",
        "execution.evidence",
        "comment",
    ):
        assert not has_speculative_evidence({**context, "truncated": [marker]})
    assert not has_speculative_evidence({**context, "missing": "invalid"})
    assert not has_speculative_evidence({**context, "schema_version": 1})
    assert not has_speculative_evidence(
        {
            **context,
            "execution": {
                "component_hint": "internal_tool_candidate",
                "input_trust": "trusted",
                "evidence": [{"source": "pr_body", "text": "internal"}],
            },
        }
    )


# These are mocked policy contrasts, not predictions of live Jev accuracy.
CONTRAST_EXAMPLES = [
    (
        "Documented trusted local config only; allegation needs a future public API",
        "SPECULATIVE",
        False,
    ),
    ("A public endpoint supplies untrusted request data", "APPLICABLE", True),
    ("An internal command deletes a real user directory", "APPLICABLE", True),
    (
        "The internal script violates an existing input validation rule",
        "APPLICABLE",
        True,
    ),
    ("Caller and input provenance are unknown", "UNKNOWN", True),
    (
        "#1086 path traversal is disputed; existing rules and real input need evaluation",
        "UNKNOWN",
        True,
    ),
]


def test_contrast_policy_and_log_privacy(tmp_path) -> None:
    from scripts.jev_filter import JevFindingEvaluation, filter_review_findings

    context = {
        "pr": {"body": "PRIVATE_PR_TEXT", "base_sha": BASE},
        "code": {
            "source": "git_blob",
            "commit_sha": HEAD,
            "side": "RIGHT",
            "start_line": 1,
            "status": "available",
            "text": "PRIVATE_CODE_TEXT",
        },
        "repository_rules": {
            "source": ".agents/AGENTS.md",
            "commit_sha": BASE,
            "status": "available",
            "text": "PRIVATE_RULE_TEXT",
        },
        "execution": {
            "evidence": [
                {
                    "source": "code",
                    "commit_sha": HEAD,
                    "text": "Documented execution conditions",
                }
            ]
        },
    }
    for index, (description, applicability, kept) in enumerate(CONTRAST_EXAMPLES):
        finding = {"body": description, "path": "scripts/a.py", "line": 1}
        evaluation = JevFindingEvaluation(
            0.99, "HIGH", applicability=applicability, applicability_confidence=0.95
        )
        path = tmp_path / f"{index}.jsonl"
        with patch(
            "scripts.jev_filter.evaluate_finding_with_jev", return_value=evaluation
        ):
            result = filter_review_findings(
                [finding], api_key="test-key", context=context, log_path=path
            )
        assert bool(result) == kept
        content = path.read_text()
        assert all(
            secret not in content
            for secret in (
                "PRIVATE_PR_TEXT",
                "PRIVATE_CODE_TEXT",
                "PRIVATE_RULE_TEXT",
                "test-key",
            )
        )
        record = json.loads(content)
        assert record["decision_reason"] == ("accepted" if kept else "speculative")


def test_contradictory_source_provenance_prevents_exclusion() -> None:
    from scripts.jev_context import has_speculative_evidence

    context = {
        "pr": {"base_sha": BASE},
        "code": {
            "source": "git_blob",
            "commit_sha": HEAD,
            "side": "RIGHT",
            "start_line": 1,
            "status": "available",
            "text": "code",
        },
        "repository_rules": {
            "source": ".agents/AGENTS.md",
            "commit_sha": BASE,
            "status": "available",
            "text": "rules",
        },
        "execution": {
            "evidence": [
                {"source": "code", "commit_sha": OLD, "text": "wrong revision"}
            ]
        },
    }
    assert not has_speculative_evidence(context)
    assert "execution.provenance" in normalize_context(context)["missing"]


def test_left_uses_diff_merge_base_in_divergent_history(tmp_path, monkeypatch) -> None:
    import subprocess

    monkeypatch.chdir(tmp_path)

    def git(*args):
        return subprocess.check_output(["git", *args], text=True).strip()

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (tmp_path / "a.py").write_text("original left\n")
    git("add", "a.py")
    git("commit", "-qm", "root")
    original = git("rev-parse", "HEAD")
    git("checkout", "-qb", "task")
    (tmp_path / "a.py").write_text("task head\n")
    git("commit", "-qam", "head")
    head = git("rev-parse", "HEAD")
    git("checkout", "-qb", "base", original)
    (tmp_path / "a.py").write_text("base tip unrelated to original left\n")
    git("commit", "-qam", "base advancement")
    base = git("rev-parse", "HEAD")
    review = JevReviewContext(pr={"head_sha": head, "base_sha": base})
    context = collect_finding_context(
        {"path": "a.py", "line": 1, "side": "LEFT", "commit_id": head}, review
    )
    assert context["code"]["commit_sha"] == original
    assert context["code"]["text"] == "original left"


def test_ambiguous_left_revision_is_cached_as_unknown() -> None:
    from scripts.jev_context import _left_revision

    review = review_context()
    review.left_sha = None
    with patch("scripts.jev_context.subprocess.run") as run:
        run.return_value.stdout = HEAD + "\n" + BASE
        assert _left_revision(review) == ""
        assert _left_revision(review) == ""
    assert run.call_count == 1

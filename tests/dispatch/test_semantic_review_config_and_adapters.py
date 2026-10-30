"""Configuration and I/O adapter contracts for Layer-2 semantic review."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from orchestune.config_contracts import ConfigError
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.config_loader import build_arg_parser, load_and_resolve_config
from orchestune.dispatch.config_schema import validate_toml_config
from orchestune.dispatch.semantic_review_adapters import (
    SemanticOperationResult,
    dispatch_semantic_fix_agent,
    escalate_semantic_review_to_human,
    poll_semantic_operation,
    trigger_semantic_review_round,
    validate_semantic_review_preflight,
)
from orchestune.review.markers import build_trigger_body
from orchestune.review.semantic_gate import (
    SemanticFixResultV1,
    SemanticOperationStatus,
    format_semantic_fix_result_marker,
    semantic_fix_output_branch,
)
from orchestune.targets.contracts import DispatchHandle

HEAD = "a" * 40
FIXED = "b" * 40
ATTEMPT = "123456789abc" + "d" * 52
OPERATION = f"{ATTEMPT[:12]}-fix-1"


def _paths(root: Path) -> tuple[Path, Path]:
    return root / "run_state.json", root / "worktrees"


def _load(tmp_path: Path, cli: list[str], toml: dict[str, object]) -> DispatcherConfig:
    target = SimpleNamespace(target_name="codex-cli")
    return load_and_resolve_config(
        ["-p", "1033", *cli],
        cwd=tmp_path,
        load_config_fn=lambda _: toml,
        build_target_fn=lambda _: target,
        resolve_paths_fn=lambda _args, _cwd: _paths(tmp_path),
        forge=MagicMock(),
    )


class CommentForge:
    def __init__(self, comments: list[dict[str, object]] | None = None) -> None:
        self.comments = list(comments or [])
        self.created: list[str] = []

    def list_comments(self, _number: int) -> list[dict[str, object]]:
        return list(self.comments)

    def create_issue_comment(self, _number: int, body: str) -> dict[str, object]:
        self.created.append(body)
        return {"id": 91, "body": body, "author": "orchestune[bot]"}

    def get_authenticated_user(self) -> str:
        return "orchestune[bot]"

    def list_notice_trusted_authors(self) -> frozenset[str]:
        return frozenset({"claude[bot]", "orchestune[bot]"})


class TestSemanticReviewConfiguration:
    def test_defaults_and_limits_projection(self, tmp_path: Path) -> None:
        config = DispatcherConfig(
            parent_issue_number=1033,
            forge=MagicMock(),
            events_log_path=tmp_path / "events.jsonl",
        )

        assert config.semantic_review_mode == "required"
        assert config.force_integration_review is False
        assert config.semantic_review_provider == "auto"
        assert config.semantic_fix_provider == "auto"
        assert config.semantic_review_trigger == "pr-comment"
        assert config.semantic_review_limits.max_rounds == 3
        assert config.semantic_review_limits.max_fix_retries == 2
        assert config.semantic_review_limits.review_timeout_seconds == 1800
        assert config.semantic_review_limits.fix_timeout_seconds == 1800
        assert config.semantic_review_limits.ci_timeout_seconds == 1800
        assert config.semantic_review_limits.attempt_timeout_seconds == 7200

    def test_ack_and_retry_defaults_keep_the_current_behavior(
        self, tmp_path: Path
    ) -> None:
        config = DispatcherConfig(
            parent_issue_number=1033,
            forge=MagicMock(),
            events_log_path=tmp_path / "events.jsonl",
        )

        assert config.semantic_review_ack_timeout_seconds == 600
        assert config.semantic_review_auto_retry is False
        assert config.semantic_review_retry_backoff_seconds == 300

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"semantic_review_ack_timeout_seconds": 0}, "ack_timeout"),
            ({"semantic_review_retry_backoff_seconds": -1}, "retry_backoff"),
            ({"semantic_review_auto_retry": "yes"}, "auto_retry"),
            ({"semantic_review_ack_timeout_seconds": True}, "must be an integer"),
        ],
    )
    def test_invalid_ack_and_retry_settings_are_rejected(
        self, tmp_path: Path, overrides: dict[str, object], message: str
    ) -> None:
        with pytest.raises(ValueError, match=message):
            DispatcherConfig(
                parent_issue_number=1033,
                forge=MagicMock(),
                events_log_path=tmp_path / "events.jsonl",
                **overrides,  # type: ignore[arg-type]
            )

    @pytest.mark.parametrize(
        ("review", "ack", "deadline"), [(1800, 600, 600), (300, 600, 300)]
    )
    def test_ack_deadline_never_exceeds_the_review_timeout(
        self, tmp_path: Path, review: int, ack: int, deadline: int
    ) -> None:
        config = DispatcherConfig(
            parent_issue_number=1033,
            forge=MagicMock(),
            events_log_path=tmp_path / "events.jsonl",
            semantic_review_timeout_seconds=review,
            semantic_review_ack_timeout_seconds=ack,
        )
        assert config.semantic_review_ack_deadline_seconds == deadline

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("semantic-review-mode", "sometimes"),
            ("semantic-review-provider", "other"),
            ("semantic-fix-provider", "other"),
            ("semantic-review-trigger", "webhook"),
            ("max-semantic-review-rounds", 6),
            ("semantic-review-timeout-seconds", 0),
            ("max-semantic-fix-retries", -1),
            ("force-integration-review", "yes"),
            ("semantic-review-ack-timeout-seconds", 0),
            ("semantic-review-auto-retry", "yes"),
            ("semantic-review-retry-backoff-seconds", -1),
        ],
    )
    def test_toml_rejects_invalid_semantic_settings(
        self, key: str, value: object
    ) -> None:
        with pytest.raises(ConfigError):
            validate_toml_config({key: value})

    def test_toml_accepts_every_semantic_setting(self) -> None:
        values = {
            "semantic-review-mode": "advisory",
            "force-integration-review": True,
            "semantic-review-provider": "claude",
            "semantic-fix-provider": "agy-cli",
            "semantic-review-trigger": "pr-comment",
            "max-semantic-review-rounds": 5,
            "max-semantic-fix-retries": 0,
            "semantic-review-timeout-seconds": 1,
            "semantic-fix-timeout-seconds": 2,
            "semantic-ci-timeout-seconds": 3,
            "semantic-attempt-timeout-seconds": 4,
            "semantic-review-ack-timeout-seconds": 5,
            "semantic-review-auto-retry": True,
            "semantic-review-retry-backoff-seconds": 0,
        }
        validated = validate_toml_config(values)

        assert validated["semantic_review_ack_timeout_seconds"] == 5
        assert validated["semantic_review_auto_retry"] is True
        assert validated["semantic_review_retry_backoff_seconds"] == 0

        assert validated["semantic_review_mode"] == "advisory"
        assert validated["force_integration_review"] is True
        assert validated["max_semantic_review_rounds"] == 5

    def test_cli_options_default_to_none(self) -> None:
        args = build_arg_parser().parse_args(["-p", "1033"])

        assert args.semantic_review_mode is None
        assert args.force_integration_review is None

    def test_mode_precedence_and_force_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("ORCHESTUNE_SEMANTIC_REVIEW_MODE", "off")
        monkeypatch.setenv("ORCHESTUNE_SEMANTIC_REVIEW", "0")

        config = _load(
            tmp_path,
            ["--semantic-review-mode", "required", "--force-integration-review"],
            {"semantic_review_mode": "advisory", "force_integration_review": False},
        )

        assert config.semantic_review_mode == "required"
        assert config.force_integration_review is True

    def test_new_environment_mode_beats_legacy_flag(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("ORCHESTUNE_SEMANTIC_REVIEW_MODE", "advisory")
        monkeypatch.setenv("ORCHESTUNE_SEMANTIC_REVIEW", "0")
        assert _load(tmp_path, [], {}).semantic_review_mode == "advisory"

    def test_legacy_zero_disables_only_semantic_mode(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("ORCHESTUNE_SEMANTIC_REVIEW", "0")
        config = _load(tmp_path, [], {})
        assert config.semantic_review_mode == "off"
        assert config.child_review_gate == "required"

    def test_invalid_environment_value_is_rejected_when_shadowed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("ORCHESTUNE_SEMANTIC_REVIEW_MODE", "secret-invalid")
        with pytest.raises(ConfigError, match="ORCHESTUNE_SEMANTIC_REVIEW_MODE"):
            _load(tmp_path, ["--semantic-review-mode", "required"], {})


class TestSemanticReviewPreflight:
    def _config(self, tmp_path: Path, **overrides: object) -> DispatcherConfig:
        values: dict[str, object] = {
            "parent_issue_number": 1033,
            "dispatch_target": SimpleNamespace(target_name="codex-cli"),
            "semantic_review_provider": "claude",
            "semantic_fix_provider": "codex-cli",
            "forge": MagicMock(),
            "events_log_path": tmp_path / "events.jsonl",
        }
        values.update(overrides)
        return DispatcherConfig(**values)  # type: ignore[arg-type]

    def test_off_mode_skips_provider_and_claim_checks(self, tmp_path: Path) -> None:
        config = self._config(tmp_path, semantic_review_mode="off")
        result = validate_semantic_review_preflight(
            config, repository_root=tmp_path, which_fn=lambda _: None
        )
        assert result.ok is True
        assert result.resolved_reviewer is None

    def test_unresolved_reviewer_returns_deduplicated_error(
        self, tmp_path: Path
    ) -> None:
        config = self._config(
            tmp_path,
            dispatch_target=SimpleNamespace(target_name="local"),
            semantic_review_provider="auto",
            semantic_fix_provider="local",
        )
        result = validate_semantic_review_preflight(config, repository_root=tmp_path)

        assert result.error_code == "reviewer_unresolved"
        expected = hashlib.sha256(b"reviewer_unresolved:None:pr-comment").hexdigest()[
            :16
        ]
        assert result.notice_key == expected

    def test_dispatch_target_trigger_is_explicitly_unsupported(
        self, tmp_path: Path
    ) -> None:
        result = validate_semantic_review_preflight(
            self._config(tmp_path, semantic_review_trigger="dispatch-target"),
            repository_root=tmp_path,
            which_fn=lambda _: "/bin/tool",
        )
        assert result.error_code == "trigger_unsupported"

    def test_missing_fix_cli_is_redacted(self, tmp_path: Path) -> None:
        result = validate_semantic_review_preflight(
            self._config(tmp_path), repository_root=tmp_path, which_fn=lambda _: None
        )
        assert result.error_code == "fix_provider_unavailable"
        assert "token" not in (result.error_message or "").lower()

    def test_claim_backend_failure_is_structured(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "orchestune.dispatch.semantic_review_adapters.probe_claim_backend",
            lambda _: "claim_backend_unsupported",
        )
        result = validate_semantic_review_preflight(
            self._config(tmp_path),
            repository_root=tmp_path,
            which_fn=lambda _: "/bin/codex",
        )
        assert result.error_code == "claim_backend_unsupported"
        assert result.ok is False

    def test_success_resolves_reviewer_and_auto_fix_target(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "orchestune.dispatch.semantic_review_adapters.probe_claim_backend",
            lambda _: None,
        )
        config = self._config(
            tmp_path, semantic_review_provider="auto", semantic_fix_provider="auto"
        )
        result = validate_semantic_review_preflight(
            config, repository_root=tmp_path, which_fn=lambda _: "/bin/codex"
        )
        assert result.ok is True
        assert result.resolved_reviewer == "claude"
        assert result.resolved_trigger == "pr-comment"


class TestSemanticReviewOperations:
    def test_review_trigger_reuses_trusted_round_and_head(self) -> None:
        body = build_trigger_body("prior judgments", "claude", 2, HEAD)
        forge = CommentForge([{"id": 41, "author": "claude[bot]", "body": body}])

        result = trigger_semantic_review_round(
            forge,  # type: ignore[arg-type]
            pr_number=7,
            attempt_id=ATTEMPT,
            round_number=2,
            head_sha=HEAD,
            reviewer="claude",
            reply_body="prior judgments",
        )

        assert result.trigger_comment_id == 41
        assert result.status is SemanticOperationStatus.DISPATCHING
        assert forge.created == []

    def test_review_trigger_posts_one_combined_comment(self) -> None:
        forge = CommentForge()
        result = trigger_semantic_review_round(
            forge,  # type: ignore[arg-type]
            pr_number=7,
            attempt_id=ATTEMPT,
            round_number=2,
            head_sha=HEAD,
            reviewer="claude",
            reply_body="judgments and commit deadbeef",
        )

        assert result.trigger_comment_id == 91
        assert len(forge.created) == 1
        assert "judgments and commit deadbeef" in forge.created[0]
        assert forge.created[0].count("@claude review") == 1
        assert f"orchestune:review-head {HEAD}" in forge.created[0]

    def test_fix_dispatch_prompt_uses_output_branch_and_result_marker(self) -> None:
        class Target:
            def __init__(self) -> None:
                self.prompt = ""

            def fire_text_once(self, prompt: str) -> DispatchHandle:
                self.prompt = prompt
                return DispatchHandle(external_id="session-17")

        target = Target()
        result = dispatch_semantic_fix_agent(
            target,
            pr_number=7,
            attempt_id=ATTEMPT,
            operation_id=OPERATION,
            temp_branch="integration/temp-parent-1033",
            expected_head_sha=HEAD,
            round_number=1,
        )

        assert result.provider_handle == "session-17"
        assert result.status is SemanticOperationStatus.DISPATCHING
        assert semantic_fix_output_branch(OPERATION) in target.prompt
        assert "orchestune-review-judgments" in target.prompt
        assert "SemanticFixResultV1" in target.prompt
        assert "Do not push to `integration/temp-parent-1033`" in target.prompt

    def test_poll_requires_trusted_result_marker(self, tmp_path: Path) -> None:
        marker = format_semantic_fix_result_marker(
            SemanticFixResultV1(ATTEMPT, OPERATION, HEAD, None, 42, "completed")
        )
        forge = CommentForge([{"author": "stranger", "body": marker}])
        result = poll_semantic_operation(
            forge,  # type: ignore[arg-type]
            tmp_path,
            pr_number=7,
            attempt_id=ATTEMPT,
            operation_id=OPERATION,
        )
        assert result.completed is False
        assert result.status is SemanticOperationStatus.RUNNING

    def test_poll_completes_no_change_result(self, tmp_path: Path) -> None:
        marker = format_semantic_fix_result_marker(
            SemanticFixResultV1(ATTEMPT, OPERATION, HEAD, None, 42, "completed")
        )
        forge = CommentForge([{"author": "orchestune[bot]", "body": marker}])
        result = poll_semantic_operation(
            forge,  # type: ignore[arg-type]
            tmp_path,
            pr_number=7,
            attempt_id=ATTEMPT,
            operation_id=OPERATION,
        )
        assert result.completed is True
        assert result.output_sha is None

    def test_poll_verifies_output_branch_tip_and_ancestry(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        marker = format_semantic_fix_result_marker(
            SemanticFixResultV1(ATTEMPT, OPERATION, HEAD, FIXED, 42, "completed")
        )
        forge = CommentForge([{"author": "orchestune[bot]", "body": marker}])
        monkeypatch.setattr(
            "orchestune.dispatch.semantic_review_adapters.fetch_remote_branch",
            lambda _root, branch: f"origin/{branch}",
        )
        monkeypatch.setattr(
            "orchestune.dispatch.semantic_review_adapters.resolve_commit_sha",
            lambda _root, _ref: FIXED,
        )
        monkeypatch.setattr(
            "orchestune.dispatch.semantic_review_adapters.is_ancestor_commit",
            lambda _root, ancestor, ref: ancestor == HEAD and ref.endswith(OPERATION),
        )

        result = poll_semantic_operation(
            forge,  # type: ignore[arg-type]
            tmp_path,
            pr_number=7,
            attempt_id=ATTEMPT,
            operation_id=OPERATION,
        )
        assert result.completed is True
        assert result.output_sha == FIXED

    def test_human_escalation_posts_deduplicated_summaries_and_transitions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        forge = MagicMock()
        forge.get_issue_labels.return_value = ("status:in-progress", "priority:high")
        notices: list[tuple[int, str, str]] = []
        transitions: list[tuple[int, tuple[str, ...], str]] = []
        monkeypatch.setattr(
            "orchestune.dispatch.semantic_review_adapters.post_notice_if_changed",
            lambda _forge, number, kind, body: notices.append((number, kind, body)),
        )
        monkeypatch.setattr(
            "orchestune.dispatch.semantic_review_adapters.apply_human_review_escalation",
            lambda number, labels, comment, forge=None: transitions.append(
                (number, labels, comment)
            ),
        )

        escalate_semantic_review_to_human(
            forge,
            parent_issue_number=1033,
            pr_number=77,
            attempt_id=ATTEMPT,
            reason="review_timeout",
            details="token=very-secret",
        )

        assert [item[0] for item in notices] == [77, 1033]
        assert all(ATTEMPT in item[1] for item in notices)
        assert all("very-secret" not in item[2] for item in notices)
        assert transitions[0][0] == 1033
        assert transitions[0][1] == ("status:in-progress", "priority:high")

    def test_operation_result_is_frozen(self) -> None:
        result = SemanticOperationResult(
            operation_id=OPERATION,
            status=SemanticOperationStatus.RUNNING,
            input_sha=HEAD,
        )
        with pytest.raises(AttributeError):
            result.completed = True  # type: ignore[misc]

    def test_poll_returns_indeterminate_on_deadline_exceeded(
        self, tmp_path: Path
    ) -> None:
        from datetime import UTC, datetime, timedelta

        forge = CommentForge([])
        started = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
        overdue = started + timedelta(seconds=1801)
        res = poll_semantic_operation(
            forge,  # type: ignore[arg-type]
            tmp_path,
            pr_number=7,
            attempt_id=ATTEMPT,
            operation_id=OPERATION,
            started_at=started,
            timeout_seconds=1800,
            now=overdue,
        )
        assert res.status == SemanticOperationStatus.INDETERMINATE
        assert res.error_code == "operation_timed_out"

    def test_redact_handles_bearer_tokens_and_long_env_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from orchestune.dispatch.semantic_review_adapters import _redact

        monkeypatch.setenv("SHORT_TOKEN", "1")
        monkeypatch.setenv("MY_SECRET_TOKEN", "secret-token-12345")
        text = "Authorization: Bearer secret-token-12345 and count is 1"
        redacted = _redact(text)
        assert "secret-token-12345" not in redacted
        assert "count is 1" in redacted
        assert "Authorization=<redacted>" in redacted

    def test_trigger_prefers_present_over_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from orchestune.pr_link_notice_trust import NoticePresence

        marker = build_trigger_body("summary", "claude", 1, HEAD)
        forge = CommentForge(
            [
                {"id": 101, "author": "trusted[bot]", "body": marker},
                {"id": 102, "author": "unknown-user", "body": marker},
            ]
        )
        evaluations = [NoticePresence.UNKNOWN, NoticePresence.PRESENT]
        monkeypatch.setattr(
            "orchestune.dispatch.semantic_review_adapters.TrustContext.evaluate_candidate_markers",
            lambda _self, _comments: (evaluations.pop(0), "evaluated"),
        )
        res = trigger_semantic_review_round(
            forge,  # type: ignore[arg-type]
            pr_number=1,
            attempt_id=ATTEMPT,
            round_number=1,
            head_sha=HEAD,
            reviewer="claude",
            reply_body="summary",
        )
        assert res.status == SemanticOperationStatus.DISPATCHING
        assert res.trigger_comment_id == 101

    def test_config_rejects_bool_for_integer_timeouts(self) -> None:
        with pytest.raises(ValueError, match="must be an integer"):
            DispatcherConfig(
                parent_issue_number=1033,
                semantic_review_timeout_seconds=True,  # type: ignore[arg-type]
            )

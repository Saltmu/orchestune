"""#820: the seven integration execution settings and how they reach the Integrator."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from orchestune.dag.models import ConfigError
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.config_loader import validate_toml_config
from orchestune.dispatch.config_values import (
    RUNTIME_TUNING_KEYS,
    runtime_tuning_overrides,
)
from orchestune.dispatch.postcycle import (
    _build_integrator_config,
    _run_semantic_integrator,
)
from orchestune.dispatch.report import _format_integrator_summary
from orchestune.dispatch.result import PhaseStatus
from orchestune.integrator import IntegrationStatus, IntegratorConfig
from orchestune.integrator.timeout_policy import (
    DEFAULT_INTEGRATION_CI_TIMEOUT_SECONDS,
    DEFAULT_INTEGRATION_CLEANUP_TIMEOUT_SECONDS,
    DEFAULT_INTEGRATION_COMMAND_TIMEOUT_SECONDS,
    DEFAULT_INTEGRATION_CYCLE_TIMEOUT_SECONDS,
    DEFAULT_INTEGRATION_DEPENDENCY_TIMEOUT_SECONDS,
    DEFAULT_INTEGRATION_TIMEOUT_BACKOFF_SECONDS,
    DEFAULT_MAX_INTEGRATION_TIMEOUT_RETRIES,
    INTEGRATION_EXECUTION_FIELDS,
    IntegrationExecutionPolicy,
)

_SECONDS_KEYS = (
    "integration-dependency-timeout-seconds",
    "integration-ci-timeout-seconds",
    "integration-cycle-timeout-seconds",
    "integration-cleanup-timeout-seconds",
    "integration-command-timeout-seconds",
    "integration-timeout-backoff-seconds",
)


class TestDefaults:
    def test_policy_defaults_match_the_issue(self) -> None:
        policy = IntegrationExecutionPolicy()

        assert policy.integration_dependency_timeout_seconds == 600
        assert policy.integration_ci_timeout_seconds == 1800
        assert policy.integration_cycle_timeout_seconds == 3600
        assert policy.integration_cleanup_timeout_seconds == 30
        assert policy.integration_command_timeout_seconds == 60
        assert policy.max_integration_timeout_retries == 2
        assert policy.integration_timeout_backoff_seconds == 60
        assert policy.max_attempts == 3

    def test_dispatcher_and_directly_built_integrator_agree(
        self, tmp_path: Path
    ) -> None:
        dispatcher = DispatcherConfig(
            parent_issue_number=100,
            run_state_path=tmp_path / "run_state.json",
            events_log_path=tmp_path / "events.jsonl",
            forge=MagicMock(),
        )
        integrator = IntegratorConfig(parent_issue_number=100, forge=MagicMock())

        assert dispatcher.integration_execution_policy == integrator.execution_policy
        assert integrator.execution_policy == IntegrationExecutionPolicy(
            DEFAULT_INTEGRATION_DEPENDENCY_TIMEOUT_SECONDS,
            DEFAULT_INTEGRATION_CI_TIMEOUT_SECONDS,
            DEFAULT_INTEGRATION_CYCLE_TIMEOUT_SECONDS,
            DEFAULT_INTEGRATION_CLEANUP_TIMEOUT_SECONDS,
            DEFAULT_INTEGRATION_COMMAND_TIMEOUT_SECONDS,
            DEFAULT_MAX_INTEGRATION_TIMEOUT_RETRIES,
            DEFAULT_INTEGRATION_TIMEOUT_BACKOFF_SECONDS,
        )


class TestValidation:
    @pytest.mark.parametrize("name", INTEGRATION_EXECUTION_FIELDS)
    @pytest.mark.parametrize("value", [True, 1.5, "10", None])
    def test_policy_rejects_non_integers_and_booleans(
        self, name: str, value: object
    ) -> None:
        with pytest.raises(ValueError, match=name):
            IntegrationExecutionPolicy(**{name: value})  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "name",
        [
            n
            for n in INTEGRATION_EXECUTION_FIELDS
            if n != "max_integration_timeout_retries"
        ],
    )
    @pytest.mark.parametrize("value", [0, -1])
    def test_seconds_must_be_positive_so_zero_never_means_unlimited(
        self, name: str, value: int
    ) -> None:
        with pytest.raises(ValueError, match=name):
            IntegrationExecutionPolicy(**{name: value})

    def test_retries_may_be_zero_for_a_terminal_first_timeout(self) -> None:
        policy = IntegrationExecutionPolicy(max_integration_timeout_retries=0)

        assert policy.max_attempts == 1

    def test_negative_retries_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_integration_timeout_retries"):
            IntegrationExecutionPolicy(max_integration_timeout_retries=-1)

    def test_integrator_config_validates_at_construction(self) -> None:
        with pytest.raises(ValueError, match="integration_ci_timeout_seconds"):
            IntegratorConfig(
                parent_issue_number=100,
                forge=MagicMock(),
                integration_ci_timeout_seconds=0,
            )

    def test_dispatcher_config_validates_at_construction(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="integration_cycle_timeout_seconds"):
            DispatcherConfig(
                parent_issue_number=100,
                run_state_path=tmp_path / "run_state.json",
                events_log_path=tmp_path / "events.jsonl",
                forge=MagicMock(),
                integration_cycle_timeout_seconds=0,
            )


class TestTomlLoading:
    @pytest.mark.parametrize("key", _SECONDS_KEYS)
    def test_hyphen_keys_are_accepted_and_normalized(self, key: str) -> None:
        validated = validate_toml_config({key: 5})

        assert validated[key.replace("-", "_")] == 5

    @pytest.mark.parametrize("key", _SECONDS_KEYS)
    @pytest.mark.parametrize("value", [0, -3, True, 1.5, "7"])
    def test_seconds_reject_zero_negative_boolean_and_non_integers(
        self, key: str, value: object
    ) -> None:
        with pytest.raises(ConfigError):
            validate_toml_config({key: value})

    def test_retries_accept_zero_but_not_negative_or_boolean(self) -> None:
        assert (
            validate_toml_config({"max-integration-timeout-retries": 0})[
                "max_integration_timeout_retries"
            ]
            == 0
        )
        for bad in (-1, True, "2"):
            with pytest.raises(ConfigError):
                validate_toml_config({"max-integration-timeout-retries": bad})

    def test_only_explicit_values_are_forwarded(self) -> None:
        overrides = runtime_tuning_overrides(
            validate_toml_config({"integration-ci-timeout-seconds": 90})
        )

        assert overrides["integration_ci_timeout_seconds"] == 90
        assert "integration_cycle_timeout_seconds" not in overrides

    def test_every_setting_is_forwarded_by_the_runtime_tuning_allowlist(self) -> None:
        assert set(INTEGRATION_EXECUTION_FIELDS) <= set(RUNTIME_TUNING_KEYS)


class TestDispatcherForwarding:
    def test_postcycle_forwards_all_seven_settings_to_the_integrator(
        self, tmp_path: Path
    ) -> None:
        values: dict[str, Any] = {
            "integration_dependency_timeout_seconds": 11,
            "integration_ci_timeout_seconds": 12,
            "integration_cycle_timeout_seconds": 13,
            "integration_cleanup_timeout_seconds": 14,
            "integration_command_timeout_seconds": 15,
            "max_integration_timeout_retries": 4,
            "integration_timeout_backoff_seconds": 16,
        }
        config = DispatcherConfig(
            parent_issue_number=100,
            run_state_path=tmp_path / "run_state.json",
            events_log_path=tmp_path / "events.jsonl",
            forge=MagicMock(),
            **values,
        )

        integrator_config = _build_integrator_config(config, False)

        for name, value in values.items():
            assert getattr(integrator_config, name) == value, name


def _phase(report: dict[str, Any]):  # type: ignore[no-untyped-def]
    config = MagicMock()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "orchestune.dispatch.postcycle._build_integrator_config",
            lambda *_: MagicMock(),
        )
        integrator = MagicMock()
        integrator.return_value.run.return_value = report
        patch.setattr("orchestune.dispatch.postcycle.Integrator", integrator)
        return _run_semantic_integrator(config, False)


class TestPostcycleClassification:
    def test_confirmed_timeout_with_budget_left_is_retryable(self) -> None:
        result = _phase({"status": IntegrationStatus.EXECUTION_TIMED_OUT})

        assert result.status is PhaseStatus.RETRYABLE_FAILURE
        assert result.retryable is True

    @pytest.mark.parametrize(
        "status",
        [
            IntegrationStatus.EXECUTION_CLEANUP_FAILED,
            IntegrationStatus.EXECUTION_RETRY_EXHAUSTED,
            IntegrationStatus.EXECUTION_INDETERMINATE,
        ],
    )
    def test_unconfirmed_exhausted_or_indeterminate_needs_a_human(
        self, status: IntegrationStatus
    ) -> None:
        result = _phase({"status": status})

        assert result.status is PhaseStatus.WARNING
        assert result.retryable is False

    def test_final_report_shows_cause_limits_and_confirmations(self) -> None:
        lines = _format_integrator_summary(
            {
                "status": "execution_timed_out",
                "execution_failures": [
                    {
                        "cause": "ci_timeout",
                        "stage": "ci",
                        "issue_number": 7,
                        "subtask_id": "task-7",
                        "configured_limit_seconds": 1800.0,
                        "effective_limit_seconds": 120.0,
                        "attempt": 1,
                        "max_attempts": 3,
                        "next_retry_at": "2026-01-01T00:01:00Z",
                        "stop_confirmed": True,
                        "rollback_confirmed": True,
                        "side_effect_state": "none",
                    }
                ],
            }
        )

        text = "\n".join(lines)
        assert "ci_timeout" in text
        assert "#7" in text and "task-7" in text
        assert "1800.0 / 120.0" in text
        assert "1/3" in text
        assert "stop=True rollback=True write=none" in text

    def test_final_report_includes_failures_of_composite_details(self) -> None:
        lines = _format_integrator_summary(
            {
                "status": "composite_failure",
                "details": {
                    "issue_100": {
                        "execution_failures": [
                            {"cause": "cleanup_failed", "stage": "merge"}
                        ]
                    }
                },
            }
        )

        assert "cleanup_failed" in "\n".join(lines)

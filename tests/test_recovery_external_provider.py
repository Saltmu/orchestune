"""Read-only recovery provider reads reuse config/auth without dispatch side effects."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from orchestune.dispatch import runtime_reader as external_execution
from orchestune.dispatch.attempt_record import LaunchAttempt
from orchestune.dispatch.config import DispatcherConfig
from tests.dispatch_test_support import replace_flat

pytest_plugins = ["tests.test_local_claim_identity"]


@pytest.fixture
def provider(local_claim, monkeypatch, fake_forge):
    workspace, active, _ = local_claim
    active = replace_flat(
        active, external_id="run", launch_attempt_id="attempt", started_at=10.0
    )
    (workspace.repository_root / "orchestune.toml").write_text(
        'dispatch_target = "codex-cloud"\ncodex_cloud_env = "env"\n'
    )
    target = SimpleNamespace(
        target_name="codex-cloud",
        launch_capabilities=SimpleNamespace(durable_attempt=True),
        execution_status=Mock(return_value="stopped"),
    )
    build = Mock(return_value=target)
    monkeypatch.setattr(external_execution, "build_dispatch_target", build)
    monkeypatch.setattr(
        external_execution,
        "DispatcherConfig",
        lambda **kw: DispatcherConfig(forge=fake_forge, **kw),
    )
    attempt = LaunchAttempt(
        "attempt", "launched", "codex-cloud", active.core.branch, "main", 10.0, "run"
    )
    reader = Mock(return_value=attempt)
    monkeypatch.setattr("orchestune.dispatch.external_execution.read_attempt", reader)
    return workspace, active, target, reader, build


@pytest.mark.parametrize("status", ["running", "stopped", "unknown", "done"])
def test_runtime_normalization_and_auth_build(provider, status):
    workspace, active, target, _, build = provider
    target.execution_status.return_value = status
    runtime, reason = external_execution.read_runtime(active, workspace)
    assert runtime == (status if status in {"running", "stopped"} else "unknown")
    assert build.call_args.args[0].codex_cloud_env == "env"
    assert target.execution_status.call_count == 1


def test_api_exception_does_not_expose_provider_secrets(provider):
    workspace, active, target, _, _ = provider
    target.execution_status.side_effect = RuntimeError("private-token")
    assert external_execution.read_runtime(active, workspace) == (
        "unknown",
        "provider_unavailable",
    )


@pytest.mark.parametrize(
    "change", ["provider", "attempt", "external", "branch", "started", "legacy"]
)
def test_unproven_origin_never_queries_another_provider(provider, change):
    from dataclasses import replace

    workspace, active, target, reader, _ = provider
    if change == "legacy":
        active = replace_flat(active, launch_attempt_id=None)
    else:
        changes = {
            "provider": {"target": "cloud-routine"},
            "attempt": {"attempt_id": "other"},
            "external": {"external_id": "other"},
            "branch": {"branch": "other"},
            "started": {"started_at": 30.0},
        }
        reader.return_value = replace(reader.return_value, **changes[change])
    assert external_execution.read_runtime(active, workspace)[0] == "unknown"
    target.execution_status.assert_not_called()


@pytest.mark.parametrize(
    "text", ["invalid = [", "dispatch_target = 5", 'routine_token = "secret"']
)
def test_invalid_configuration_is_a_safe_refusal(local_claim, text):
    workspace, active, _ = local_claim
    (workspace.repository_root / "orchestune.toml").write_text(text)
    with pytest.raises(ValueError, match="^provider_config_invalid$"):
        external_execution.read_runtime(active, workspace)


def test_missing_auth_is_unknown_and_never_builds(local_claim, monkeypatch):
    workspace, active, _ = local_claim
    (workspace.repository_root / "orchestune.toml").write_text(
        'dispatch_target = "cloud-routine"\nroutine_id = "routine"\n'
    )
    monkeypatch.delenv("ORCHESTUNE_CLAUDE_ROUTINE_TOKEN", raising=False)
    monkeypatch.setattr(
        external_execution, "_resolve_cloud_settings", lambda _: ("routine", None, None)
    )
    build = Mock()
    monkeypatch.setattr(external_execution, "build_dispatch_target", build)
    assert external_execution.read_runtime(active, workspace)[0] == "unknown"
    build.assert_not_called()

"""Shared targets remain usable independently of the dispatch orchestrator."""

import importlib
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "first",
    [
        "orchestune.integrator.coordinator",
        "orchestune.dispatch.targets",
        "orchestune.targets.cloud_routine",
    ],
)
def test_shared_routine_contracts_survive_import_order(first: str) -> None:
    code = f"""
import importlib
import sys
importlib.import_module({first!r})
from orchestune.dispatch import targets, execution_profiles, reviewer
from orchestune.targets import cloud_routine, contracts
assert targets.ClaudeCodeCloudRoutineDispatchTarget is cloud_routine.ClaudeCodeCloudRoutineDispatchTarget
for name in ('DispatchHandle', 'DispatchTarget', 'LaunchCapabilities'):
    assert getattr(targets, name) is getattr(contracts, name)
assert execution_profiles.ExecutionSelection is contracts.ExecutionSelection
assert reviewer.ReviewerBot is contracts.ReviewerBot
assert reviewer.ReviewerBotSetting is contracts.ReviewerBotSetting
assert issubclass(cloud_routine.ClaudeCodeCloudRoutineDispatchTarget, contracts.DispatchTarget)
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_shared_routine_import_does_not_load_dispatch_or_integrator() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import orchestune.targets.cloud_routine; assert not any(name == 'orchestune.dispatch' or name.startswith('orchestune.dispatch.') or name == 'orchestune.integrator' or name.startswith('orchestune.integrator.') for name in sys.modules)",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_legacy_routine_environment_names_are_shared() -> None:
    legacy = importlib.import_module("orchestune.dispatch.targets")
    shared = importlib.import_module("orchestune.targets.cloud_routine")
    assert (
        shared.ROUTINE_ID_ENV_VAR
        == legacy.ROUTINE_ID_ENV_VAR
        == "ORCHESTUNE_ROUTINE_ID"
    )
    assert (
        shared.ROUTINE_TOKEN_ENV_VAR
        == legacy.ROUTINE_TOKEN_ENV_VAR
        == "ORCHESTUNE_ROUTINE_TOKEN"
    )

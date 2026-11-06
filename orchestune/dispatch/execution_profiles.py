"""Compatibility exports for shared repository configuration."""

from typing import Any

from orchestune.execution_profiles import (
    DEFAULT_EXECUTION_PROFILE as DEFAULT_EXECUTION_PROFILE,
)
from orchestune.execution_profiles import (
    ExecutionProfileConfig as ExecutionProfileConfig,
)
from orchestune.execution_profiles import (
    ExecutionSelection as ExecutionSelection,
)
from orchestune.execution_profiles import (
    TargetExecutionConfig as TargetExecutionConfig,
)
from orchestune.execution_profiles import (
    TargetProfileConfig as TargetProfileConfig,
)
from orchestune.execution_profiles import (
    extract_execution_profile_config as extract_execution_profile_config,
)
from orchestune.execution_profiles import (
    extract_target_name as extract_target_name,
)
from orchestune.execution_profiles import (
    load_execution_profile_config as load_execution_profile_config,
)
from orchestune.execution_profiles import (
    logger as logger,
)
from orchestune.execution_profiles import (
    resolve_execution_profile as resolve_execution_profile,
)
from orchestune.execution_profiles import (
    validate_model_name as validate_model_name,
)
from orchestune.execution_profiles import (
    validate_profile_for_target as validate_profile_for_target,
)
from orchestune.execution_profiles import (
    validate_profile_name as validate_profile_name,
)
from orchestune.execution_profiles import (
    validate_reasoning_effort as validate_reasoning_effort,
)
from orchestune.execution_profiles import (
    validate_target_name as validate_target_name,
)

_extract_target_name = extract_target_name


def resolve_task_execution_selection(
    task: Any,
    config: Any,
) -> ExecutionSelection:
    """Deterministically resolve an ExecutionSelection for a task, applying DispatcherConfig overrides if present."""
    cli_profile = getattr(config, "profile", None)
    selected_profile = (
        cli_profile
        if cli_profile is not None
        else getattr(task, "execution_profile", None)
    )
    selection = resolve_execution_profile(
        selected_profile,
        getattr(config, "dispatch_target", None),
        getattr(config, "execution_profile_config", None),
    )
    if cli_profile is None:
        return selection
    return ExecutionSelection(
        profile=selection.profile,
        model=selection.model,
        reasoning_effort=selection.reasoning_effort,
        reason=f"{selection.reason} (CLI profile override)",
    )

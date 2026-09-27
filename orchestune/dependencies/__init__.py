"""Shared dependency resolution, assessment, and policy subsystem."""

from __future__ import annotations

from orchestune.dependencies.assessment import (
    AssessedDependency,
    DependencyAssessment,
    DependencyState,
    DependencyStateView,
    assess_dependencies,
)
from orchestune.dependencies.policy import (
    DependencyPolicyView,
    StackDecision,
    StackTarget,
    decide_stack_target,
    has_pending_dependencies,
)
from orchestune.dependencies.resolution import (
    EMPTY_DEPENDENCIES,
    REASON_AMBIGUOUS,
    REASON_MISSING,
    REASON_UNKNOWN_PARENT,
    DependencyDeclarations,
    TaskDependencies,
    UnresolvedDependency,
    build_legacy_dag_inputs,
    describe_unresolved_dependency,
    legacy_merged_depends_on,
    resolve_all_dependencies,
    resolve_stackable_dependency_issue,
    resolve_task_dependencies,
)

__all__ = [
    "AssessedDependency",
    "DependencyAssessment",
    "DependencyDeclarations",
    "DependencyPolicyView",
    "DependencyState",
    "DependencyStateView",
    "EMPTY_DEPENDENCIES",
    "REASON_AMBIGUOUS",
    "REASON_MISSING",
    "REASON_UNKNOWN_PARENT",
    "StackDecision",
    "StackTarget",
    "TaskDependencies",
    "UnresolvedDependency",
    "assess_dependencies",
    "build_legacy_dag_inputs",
    "decide_stack_target",
    "describe_unresolved_dependency",
    "has_pending_dependencies",
    "legacy_merged_depends_on",
    "resolve_all_dependencies",
    "resolve_stackable_dependency_issue",
    "resolve_task_dependencies",
]

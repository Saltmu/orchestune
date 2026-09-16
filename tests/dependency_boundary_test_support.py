"""AST guard support for the dispatcher dependency/Context boundary."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

RAW_DEPENDENCY_ATTRIBUTES = frozenset({"depends_on", "native_depends_on"})
REMOVED_CONTEXT_ATTRIBUTES = frozenset(
    {
        "branch_by_issue_number",
        "changes_requested_issue_numbers",
        "ci_passed_pr_issue_numbers",
        "dependency_resolution",
        "done_issue_numbers",
        "issue_number_by_subtask_id",
        "pr_by_branch",
        "prior_parent_merge_completed_issue_numbers",
        "prior_parent_merge_hold_issue_numbers",
        "prs",
        "run_state",
        "tasks_by_issue",
    }
)
RAW_TASK_MODULES = frozenset({"orchestune.models", "orchestune.dispatch.scoring"})


@dataclass(frozen=True, slots=True)
class BoundaryException:
    module: str
    function: str
    attribute: str
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("boundary exception reason must not be empty")


@dataclass(frozen=True, slots=True)
class BoundaryViolation:
    module: str
    function: str
    attribute: str
    kind: str
    line: int


def _exception(
    module: str, function: str, attribute: str, reason: str
) -> BoundaryException:
    return BoundaryException(module, function, attribute, reason)


_IDENTITY = "Designated raw Task identity/declaration conversion boundary."
_LOW_LEVEL = "Private low-level action context below the CycleContext port."
_PRIVATE_VIEW = "Private typed adapter/snapshot, not the public CycleContext."
_COMPAT = "Explicit legacy raw-Task compatibility boundary."

PRODUCTION_EXCEPTIONS = frozenset(
    {
        _exception("orchestune.task_metadata", "<module>", "Task", _COMPAT),
        _exception("orchestune.dispatch.conflicts", "<module>", "Task", _COMPAT),
        _exception("orchestune.dispatch.critical_path", "<module>", "Task", _COMPAT),
        _exception(
            "orchestune.dispatch.cycle_context_state", "<module>", "Task", _IDENTITY
        ),
        _exception(
            "orchestune.dispatch.dependency_resolution", "<module>", "Task", _IDENTITY
        ),
        _exception("orchestune.dispatch.locks", "<module>", "Task", _COMPAT),
        _exception("orchestune.dispatch.recovery", "<module>", "Task", _LOW_LEVEL),
        _exception("orchestune.dispatch.rules", "<module>", "Task", _IDENTITY),
        _exception("orchestune.dispatch.scoring", "<module>", "Task", _COMPAT),
        _exception("orchestune.dispatch.status_repair", "<module>", "Task", _LOW_LEVEL),
        _exception(
            "orchestune.dispatch.status_repair_dependencies",
            "<module>",
            "Task",
            _LOW_LEVEL,
        ),
        _exception(
            "orchestune.dispatch.critical_path",
            "_successor_map_from_subtasks",
            "depends_on",
            "SubTask contains the derived semantic DAG edge.",
        ),
        _exception(
            "orchestune.dispatch.cycle_context_state",
            "_owned_task",
            "depends_on",
            _IDENTITY,
        ),
        _exception(
            "orchestune.dispatch.cycle_context_state",
            "_owned_task",
            "native_depends_on",
            _IDENTITY,
        ),
        *{
            _exception(
                "orchestune.dispatch.dependency_resolution",
                function,
                attribute,
                _IDENTITY,
            )
            for function, attribute in {
                ("_resolve_native", "native_depends_on"),
                ("_own_native_subtask_ids", "native_depends_on"),
                ("_resolve_body", "depends_on"),
                ("from_task", "depends_on"),
                ("from_task", "native_depends_on"),
            }
        },
        _exception(
            "orchestune.dispatch.cycle", "execute", "tasks_by_issue", _PRIVATE_VIEW
        ),
        _exception(
            "orchestune.dispatch.cycle",
            "_promotion_events",
            "tasks_by_issue",
            _PRIVATE_VIEW,
        ),
        _exception(
            "orchestune.dispatch.cycle_actions",
            "_run_active_worktree_rules",
            "run_state",
            _LOW_LEVEL,
        ),
        *{
            _exception(
                "orchestune.dispatch.phase_gc", function, attribute, _PRIVATE_VIEW
            )
            for function, attribute in {
                ("observe", "run_state"),
                ("observe", "tasks_by_issue"),
                ("derive", "tasks_by_issue"),
            }
        },
        *{
            _exception(
                "orchestune.dispatch.recovery",
                function,
                "tasks_by_issue",
                _PRIVATE_VIEW,
            )
            for function in {
                "tasks_by_issue",
                "observe",
                "derive",
                "execute_recovery_requeue_command",
            }
        },
        _exception(
            "orchestune.dispatch.escalation",
            "_rule_changes_requested",
            "run_state",
            _LOW_LEVEL,
        ),
        _exception(
            "orchestune.dispatch.launch",
            "_launch_selected_tasks",
            "run_state",
            _LOW_LEVEL,
        ),
        *{
            _exception("orchestune.dispatch.rebase", function, attribute, _LOW_LEVEL)
            for function, attribute in {
                ("_prepare_wip_backup_for_rebase", "run_state"),
                ("_handle_rebase_failure", "run_state"),
                ("_rule_auto_rebase", "run_state"),
                ("_rule_footprint_deviation", "tasks_by_issue"),
                ("_rule_footprint_deviation", "issue_number_by_subtask_id"),
            }
        },
        *{
            _exception(
                "orchestune.dispatch.gc.completion", function, "run_state", _LOW_LEVEL
            )
            for function in {"_handle_special_retry"}
        },
        *{
            _exception("orchestune.dispatch.gc", function, attribute, _LOW_LEVEL)
            for function, attribute in {
                ("_rule_not_needed", "run_state"),
                ("_persist_run_state_best_effort", "run_state"),
                ("_persist_run_state_best_effort", "prs"),
                ("_update_hold_record", "run_state"),
                ("_persist_and_confirm_completion", "run_state"),
                ("_persist_and_confirm_completion", "prs"),
                ("_release_entry", "run_state"),
                ("_release_entry", "prs"),
                ("_record_completed_worktree", "run_state"),
                ("_reserve_reclaim", "run_state"),
                ("_reserve_reclaim", "prs"),
                ("_abandoned_worktree_outcome", "run_state"),
                ("_handle_completed_event_outcome", "run_state"),
                ("_settle_early_death_requeue", "run_state"),
                ("_settle_early_death_requeue", "prs"),
                ("_settle_review_timeout_requeue", "run_state"),
                ("_settle_review_timeout_requeue", "prs"),
                ("_rule_completed", "run_state"),
                ("_rule_completed", "prs"),
            }
        },
    }
)


class _BoundaryVisitor(ast.NodeVisitor):
    def __init__(self, module: str) -> None:
        self.module = module
        self.functions = ["<module>"]
        self.violations: list[BoundaryViolation] = []

    def _record(self, attribute: str, kind: str, line: int) -> None:
        self.violations.append(
            BoundaryViolation(self.module, self.functions[-1], attribute, kind, line)
        )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in RAW_DEPENDENCY_ATTRIBUTES:
            self._record(node.attr, "raw-attribute", node.lineno)
        elif node.attr in REMOVED_CONTEXT_ATTRIBUTES:
            self._record(node.attr, "removed-context-attribute", node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        attribute = _literal_getattr_attribute(node)
        if attribute is not None:
            if attribute in RAW_DEPENDENCY_ATTRIBUTES | REMOVED_CONTEXT_ATTRIBUTES:
                self._record(attribute, "literal-getattr", node.lineno)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        if any(alias.name in RAW_TASK_MODULES for alias in node.names):
            self._record("Task", "raw-task-import", node.lineno)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        imports_task = node.module in RAW_TASK_MODULES and any(
            alias.name == "Task" for alias in node.names
        )
        imports_raw_module = node.module is not None and any(
            f"{node.module}.{alias.name}" in RAW_TASK_MODULES for alias in node.names
        )
        if imports_task or imports_raw_module:
            self._record("Task", "raw-task-import", node.lineno)


def _literal_getattr_attribute(node: ast.Call) -> str | None:
    if not (
        isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
    ):
        return None
    literal = node.args[1]
    if not isinstance(literal, ast.Constant) or not isinstance(literal.value, str):
        return None
    return literal.value


def boundary_violations(
    source: str,
    *,
    module: str,
    exceptions: frozenset[BoundaryException] = frozenset(),
) -> tuple[BoundaryViolation, ...]:
    visitor = _BoundaryVisitor(module)
    visitor.visit(ast.parse(source))
    allowed = {(item.module, item.function, item.attribute) for item in exceptions}
    return tuple(
        item
        for item in visitor.violations
        if item.kind == "literal-getattr"
        or (item.module, item.function, item.attribute) not in allowed
    )


def _module_name(package_root: Path, path: Path) -> str:
    parts = list(path.relative_to(package_root.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _production_observations(repo_root: Path) -> tuple[BoundaryViolation, ...]:
    package_root = repo_root / "orchestune"
    paths = [package_root / "task_metadata.py"]
    paths.extend(sorted((package_root / "dispatch").rglob("*.py")))
    observations = [
        violation
        for path in paths
        for violation in boundary_violations(
            path.read_text(encoding="utf-8"),
            module=_module_name(package_root, path),
        )
    ]
    return tuple(
        sorted(observations, key=lambda item: (item.module, item.line, item.kind))
    )


def production_boundary_violations(repo_root: Path) -> tuple[BoundaryViolation, ...]:
    allowed = {
        (item.module, item.function, item.attribute) for item in PRODUCTION_EXCEPTIONS
    }
    return tuple(
        item
        for item in _production_observations(repo_root)
        if item.kind == "literal-getattr"
        or (item.module, item.function, item.attribute) not in allowed
    )


def unused_production_boundary_exceptions(
    repo_root: Path,
) -> tuple[BoundaryException, ...]:
    observed = {
        (item.module, item.function, item.attribute)
        for item in _production_observations(repo_root)
    }
    return tuple(
        sorted(
            (
                item
                for item in PRODUCTION_EXCEPTIONS
                if (item.module, item.function, item.attribute) not in observed
            ),
            key=lambda item: (item.module, item.function, item.attribute),
        )
    )

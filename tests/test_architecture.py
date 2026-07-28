"""Architecture invariants kept visible while the package is refactored."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[1] / "orchestune"
PACKAGE_NAME = "orchestune"
L4_MODULES = frozenset({"cli", "dispatcher", "dag", "monitor", "bootstrap"})
KNOWN_CYCLE_MEMBERS = frozenset(
    {
        "dispatch_actor_verification",
        "dispatch_config",
        "dispatch_cycle",
        "dispatch_escalation",
        "dispatch_gc",
        "dispatch_launch",
        "dispatch_rebase",
        "dispatch_recovery",
        "dispatch_report",
        "dispatch_rules",
        "dispatch_targets",
        "dispatch_worktree",
        "dispatcher",
        "integration_coordinator",
        "integrator",
        "integrator_git_ops",
        "integrator_pr",
        "integrator_tasks",
    }
)
KNOWN_L4_DEPENDENTS = {
    "bootstrap": frozenset({"cli"}),
    "dag": frozenset({"cli", "dispatch_cycle", "dispatch_rebase", "integrator_tasks"}),
    "dispatcher": frozenset(
        {
            "cli",
            "dispatch_actor_verification",
            "dispatch_recovery",
            "dispatch_targets",
            "integrator",
            "integrator_git_ops",
            "integrator_pr",
            "integrator_tasks",
        }
    ),
    "monitor": frozenset({"cli"}),
}
KNOWN_SUBPROCESS_COMMAND_MODULES = frozenset(
    {
        "bootstrap",
        "dispatch_gc",
        "dispatch_launch",
        "dispatch_locks",
        "dispatch_rebase",
        "dispatch_recovery",
        "dispatch_targets",
        "dispatch_worktree",
        "forge",
        "github",
        "integrator",
        "integrator_git_ops",
        "integrator_worktree",
    }
)
_SUBPROCESS_CALLS = frozenset({"run", "Popen", "check_call", "check_output"})
_COMMANDS = frozenset({"git", "gh"})


def _module_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
    parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
    return ".".join((PACKAGE_NAME, *parts))


def _package_modules() -> dict[str, Path]:
    return {
        _module_name(path): path
        for path in PACKAGE_ROOT.rglob("*.py")
        if path.name != "__init__.py" or path.parent == PACKAGE_ROOT
    }


def _relative_import_name(current_module: str, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module

    package_parts = current_module.rsplit(".", 1)[0].split(".")
    parent_parts = package_parts[: len(package_parts) - node.level + 1]
    if node.module:
        parent_parts.extend(node.module.split("."))
    return ".".join(parent_parts) or None


def _internal_imports(
    current_module: str, tree: ast.AST, known_modules: set[str]
) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in known_modules:
                    imports.add(alias.name)
            continue

        if not isinstance(node, ast.ImportFrom):
            continue

        module_name = _relative_import_name(current_module, node)
        if module_name in known_modules:
            imports.add(module_name)
        if module_name == PACKAGE_NAME:
            imports.update(
                f"{PACKAGE_NAME}.{alias.name}"
                for alias in node.names
                if f"{PACKAGE_NAME}.{alias.name}" in known_modules
            )
    imports.discard(current_module)
    return imports


def _import_graph() -> dict[str, set[str]]:
    modules = _package_modules()
    known_modules = set(modules)
    return {
        module.removeprefix(f"{PACKAGE_NAME}."): {
            dependency.removeprefix(f"{PACKAGE_NAME}.")
            for dependency in _internal_imports(
                module, ast.parse(path.read_text(encoding="utf-8")), known_modules
            )
        }
        for module, path in modules.items()
    }


def _cycle_members(graph: dict[str, set[str]]) -> set[str]:
    active: list[str] = []
    visited: set[str] = set()
    cycles: set[str] = set()

    def visit(module: str) -> None:
        if module in active:
            cycles.update(active[active.index(module) :])
            return
        if module in visited:
            return

        active.append(module)
        for dependency in graph[module]:
            visit(dependency)
        active.pop()
        visited.add(module)

    for module in graph:
        visit(module)
    return cycles


def _l4_dependents(graph: dict[str, set[str]]) -> dict[str, set[str]]:
    dependents: dict[str, set[str]] = defaultdict(set)
    for module, dependencies in graph.items():
        for dependency in dependencies & L4_MODULES:
            dependents[dependency].add(module)
    return dict(dependents)


def _subprocess_command_modules() -> set[str]:
    command_modules: set[str] = set()
    for module, path in _package_modules().items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        subprocess_names = {"subprocess"}
        call_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                subprocess_names.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "subprocess"
                )
            elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
                call_names.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name in _SUBPROCESS_CALLS
                )

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            is_subprocess_call = (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in subprocess_names
                and node.func.attr in _SUBPROCESS_CALLS
            ) or (isinstance(node.func, ast.Name) and node.func.id in call_names)
            command = node.args[0]
            if (
                is_subprocess_call
                and isinstance(command, ast.List | ast.Tuple)
                and command.elts
                and isinstance(command.elts[0], ast.Constant)
                and command.elts[0].value in _COMMANDS
            ):
                command_modules.add(module.removeprefix(f"{PACKAGE_NAME}."))
    return command_modules


def test_package_import_graph_does_not_gain_cycles() -> None:
    assert _cycle_members(_import_graph()) <= KNOWN_CYCLE_MEMBERS


def test_l4_modules_do_not_gain_new_dependents() -> None:
    unexpected = {
        module: dependents - KNOWN_L4_DEPENDENTS.get(module, frozenset())
        for module, dependents in _l4_dependents(_import_graph()).items()
        if dependents - KNOWN_L4_DEPENDENTS.get(module, frozenset())
    }
    assert unexpected == {}


def test_git_and_gh_subprocess_modules_do_not_expand() -> None:
    assert _subprocess_command_modules() <= KNOWN_SUBPROCESS_COMMAND_MODULES

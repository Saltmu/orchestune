"""Architecture invariants kept visible while the package is refactored."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[1] / "orchestune"
TESTS_ROOT = Path(__file__).parent
PACKAGE_NAME = "orchestune"
L4_MODULES = frozenset({"cli", "dispatcher", "dag", "monitor", "bootstrap"})
ALLOWED_L4_DEPENDENTS = {
    "bootstrap": frozenset({"cli"}),
    "dag": frozenset({"cli"}),
    "dispatcher": frozenset({"cli"}),
    "monitor": frozenset({"cli"}),
}
EXPECTED_SUBPROCESS_COMMAND_MODULES = {
    "gh": {"forge"},
    "git": {"git_cli"},
}
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
    """非自明な強連結成分（要素数2以上、または自己ループ）に属するモジュール名を返す。

    Tarjan's SCCアルゴリズムを使う。単純な「探索中スタック+visited集合」による
    DFSは、あるノードが別の経路から先に`visited`化されてしまうと、そのノード
    経由でしか辿り着けない別の循環を再探索せず見逃す（探索順序に依存して
    検出結果が変わる）欠陥がある。Tarjan's SCCはノードの近傍を辿る順序に
    依らず正しい強連結成分を求められるため、この欠陥がない。
    """
    index_counter = [0]
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []
    cycles: set[str] = set()

    def strongconnect(module: str) -> None:
        index[module] = index_counter[0]
        lowlink[module] = index_counter[0]
        index_counter[0] += 1
        stack.append(module)
        on_stack[module] = True

        for dependency in graph[module]:
            if dependency not in index:
                strongconnect(dependency)
                lowlink[module] = min(lowlink[module], lowlink[dependency])
            elif on_stack.get(dependency):
                lowlink[module] = min(lowlink[module], index[dependency])

        if lowlink[module] == index[module]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack[member] = False
                component.append(member)
                if member == module:
                    break
            if len(component) > 1 or module in graph[module]:
                cycles.update(component)

    for module in graph:
        if module not in index:
            strongconnect(module)
    return cycles


def _l4_dependents(graph: dict[str, set[str]]) -> dict[str, set[str]]:
    dependents: dict[str, set[str]] = defaultdict(set)
    for module, dependencies in graph.items():
        for dependency in dependencies & L4_MODULES:
            dependents[dependency].add(module)
    return dict(dependents)


def _subprocess_command_modules() -> dict[str, set[str]]:
    command_modules: dict[str, set[str]] = defaultdict(set)
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
                command_modules[str(command.elts[0].value)].add(
                    module.removeprefix(f"{PACKAGE_NAME}.")
                )
    return dict(command_modules)


def _stale_github_patch_targets() -> list[str]:
    stale_targets: list[str] = []
    for path in TESTS_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            is_patch_call = (
                isinstance(node.func, ast.Name) and node.func.id == "patch"
            ) or (isinstance(node.func, ast.Attribute) and node.func.attr == "patch")
            if not is_patch_call:
                continue
            target = node.args[0]
            if (
                isinstance(target, ast.Constant)
                and isinstance(target.value, str)
                and target.value.startswith("orchestune.github")
            ):
                stale_targets.append(f"{path.name}:{node.lineno}:{target.value}")
    return stale_targets


def test_package_import_graph_does_not_gain_cycles() -> None:
    assert _cycle_members(_import_graph()) == set()


def test_l4_modules_do_not_gain_new_dependents() -> None:
    unexpected = {
        module: dependents - ALLOWED_L4_DEPENDENTS.get(module, frozenset())
        for module, dependents in _l4_dependents(_import_graph()).items()
        if dependents - ALLOWED_L4_DEPENDENTS.get(module, frozenset())
    }
    assert unexpected == {}


def test_internal_imports_are_not_hidden_inside_functions() -> None:
    hidden_imports: list[str] = []
    for module, path in _package_modules().items():
        short_name = module.removeprefix(f"{PACKAGE_NAME}.")
        if short_name == "cli":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        ):
            for node in ast.walk(function):
                if isinstance(node, ast.Import):
                    names = [
                        alias.name
                        for alias in node.names
                        if alias.name == PACKAGE_NAME
                        or alias.name.startswith(f"{PACKAGE_NAME}.")
                    ]
                elif isinstance(node, ast.ImportFrom):
                    imported = _relative_import_name(module, node)
                    names = (
                        [imported]
                        if imported == PACKAGE_NAME
                        or (
                            imported is not None
                            and imported.startswith(f"{PACKAGE_NAME}.")
                        )
                        else []
                    )
                else:
                    names = []
                hidden_imports.extend(
                    f"{short_name}:{getattr(node, 'lineno', '?')}:{name}"
                    for name in names
                )
    assert hidden_imports == []


def test_git_and_gh_subprocess_modules_are_strictly_partitioned() -> None:
    assert _subprocess_command_modules() == EXPECTED_SUBPROCESS_COMMAND_MODULES


def test_github_compatibility_module_is_removed() -> None:
    assert not (PACKAGE_ROOT / "github.py").exists()


def test_tests_do_not_patch_removed_github_module() -> None:
    assert _stale_github_patch_targets() == []

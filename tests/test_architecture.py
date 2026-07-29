"""Architecture invariants kept visible while the package is refactored."""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[1] / "orchestune"
TESTS_ROOT = Path(__file__).parent
DOCS_ROOT = Path(__file__).parents[1] / "docs"
DOC_LANGUAGES = ("en", "ja")
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
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_LAYER_ROW = re.compile(r"^\s*\|\s*\*\*L(\d)\*\*")
_COMMAND_ROW = re.compile(r"^\s*\|\s*`(git|gh)`\s*\|")
_BACKTICKED = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")


def _module_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
    parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
    return ".".join((PACKAGE_NAME, *parts))


def _package_modules() -> dict[str, Path]:
    """Every `.py` under `orchestune/`, subpackage initialisers included.

    A nested `__init__.py` can carry imports and package wiring of its own, so
    leaving it out would hide cycles and upward dependencies introduced there —
    and would quietly weaken the "the layer table covers every file" promise.
    """
    return {_module_name(path): path for path in PACKAGE_ROOT.rglob("*.py")}


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


def _leading_command(node: ast.expr | None) -> str | None:
    """`["git", ...]` / `("gh", ...)` の先頭要素が対象コマンドならその名前を返す。"""
    if not isinstance(node, ast.List | ast.Tuple) or not node.elts:
        return None
    first = node.elts[0]
    if isinstance(first, ast.Constant) and first.value in _COMMANDS:
        return str(first.value)
    return None


def _assigned_names(node: ast.AST) -> tuple[list[ast.expr], ast.expr | None]:
    if isinstance(node, ast.Assign):
        return list(node.targets), node.value
    if isinstance(node, ast.AnnAssign | ast.AugAssign):
        return [node.target], node.value
    return [], None


def _nodes_in_scope(scope: ast.AST) -> list[ast.AST]:
    """`scope` 直下のノードを、ネストしたスコープの中身を除いて位置順に返す。

    ネストしたスコープを定義するノード自体は返す（呼び出し側がそこで再帰する）。
    """
    collected: list[ast.AST] = []

    def visit(node: ast.AST, *, is_root: bool) -> None:
        if not is_root:
            collected.append(node)
            if isinstance(node, _SCOPE_NODES):
                return
        for child in ast.iter_child_nodes(node):
            visit(child, is_root=False)

    visit(scope, is_root=True)
    return sorted(
        collected, key=lambda n: (getattr(n, "lineno", 0), getattr(n, "col_offset", 0))
    )


def _subprocess_first_argument(
    node: ast.Call, subprocess_names: set[str], call_names: set[str]
) -> ast.expr | None:
    """`subprocess.run(...)` 系の呼び出しなら、そのargvにあたる式を返す。

    argvは第1位置引数だけでなく `subprocess.run(args=[...])` のキーワードでも
    渡せるため、両方を見る。
    """
    is_subprocess_call = (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in subprocess_names
        and node.func.attr in _SUBPROCESS_CALLS
    ) or (isinstance(node.func, ast.Name) and node.func.id in call_names)
    if not is_subprocess_call:
        return None
    if node.args:
        return node.args[0]
    for keyword in node.keywords:
        if keyword.arg == "args":
            return keyword.value
    return None


def _scan_scope(
    scope: ast.AST,
    bindings: dict[str, str],
    subprocess_names: set[str],
    call_names: set[str],
    found: set[str],
) -> None:
    """1つのスコープを位置順に走査し、実行されたコマンド名を `found` へ集める。

    名前の束縛はスコープごとに持ち、そのスコープ内で**その呼び出しより前に**
    現れた代入だけを参照する。モジュール全体で1つのマップを共有すると、
    `args` のようなありふれた名前を別々の関数が別のコマンドに使い回した際に、
    最後の代入が全呼び出しへ適用されて誤判定になるため。
    """
    for node in _nodes_in_scope(scope):
        if isinstance(node, _SCOPE_NODES):
            _scan_scope(node, dict(bindings), subprocess_names, call_names, found)
            continue
        if isinstance(node, ast.Call):
            argument = _subprocess_first_argument(node, subprocess_names, call_names)
            command = _leading_command(argument)
            if command is None and isinstance(argument, ast.Name):
                command = bindings.get(argument.id)
            if command is not None:
                found.add(command)
            continue
        targets, value = _assigned_names(node)
        command = _leading_command(value)
        for target in targets:
            if isinstance(target, ast.Name):
                if command is None:
                    bindings.pop(target.id, None)
                else:
                    bindings[target.id] = command


def _subprocess_command_modules() -> dict[str, set[str]]:
    """{コマンド: そのコマンドをsubprocess実行しているモジュール名}を返す。

    検出できるのは、コマンドリストがリテラルとして書かれている呼び出し
    （直接渡す場合と、同じスコープでリテラルを代入した変数を渡す場合）です。
    実行時に組み立てたリストや、他モジュールから受け取ったコマンドまでは
    追跡しません。
    """
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

        found: set[str] = set()
        _scan_scope(tree, {}, subprocess_names, call_names, found)
        for command in found:
            command_modules[command].add(module.removeprefix(f"{PACKAGE_NAME}."))
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


def _architecture_doc(lang: str) -> list[str]:
    return (
        (DOCS_ROOT / lang / "architecture.md").read_text(encoding="utf-8").splitlines()
    )


def _row_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _documented_layers(lang: str) -> dict[int, list[str]]:
    """The `**L<n>**` rows of the module-layer table, as {layer: modules}.

    Each row stays a list rather than a set so that a module repeated inside one
    row survives to be caught by the exact-once assertion.
    """
    layers: dict[int, list[str]] = {}
    for line in _architecture_doc(lang):
        match = _LAYER_ROW.match(line)
        if match is None:
            continue
        assert int(match.group(1)) not in layers, f"duplicate layer row in {lang}"
        layers[int(match.group(1))] = _BACKTICKED.findall(_row_cells(line)[1])
    return layers


def _documented_subprocess_partition(lang: str) -> dict[str, set[str]]:
    """The command/module rows of the '`git`/`gh` stay in L1' table."""
    return {
        _row_cells(line)[0].strip("`"): set(_BACKTICKED.findall(_row_cells(line)[1]))
        for line in _architecture_doc(lang)
        if _COMMAND_ROW.match(line)
    }


def _module_layer(lang: str = "en") -> dict[str, int]:
    return {
        module: layer
        for layer, modules in _documented_layers(lang).items()
        for module in modules
    }


def test_git_and_gh_subprocess_modules_are_strictly_partitioned() -> None:
    assert _subprocess_command_modules() == EXPECTED_SUBPROCESS_COMMAND_MODULES


def test_github_compatibility_module_is_removed() -> None:
    assert not (PACKAGE_ROOT / "github.py").exists()


def test_tests_do_not_patch_removed_github_module() -> None:
    assert _stale_github_patch_targets() == []


def test_documented_layers_cover_every_module_exactly_once() -> None:
    package_modules = {
        module.removeprefix(f"{PACKAGE_NAME}.")
        for module in _package_modules()
        if module != PACKAGE_NAME
    }
    for lang in DOC_LANGUAGES:
        layers = _documented_layers(lang)
        listed = [module for modules in layers.values() for module in modules]
        assert sorted(listed) == sorted(set(listed)), f"{lang}: module listed twice"
        assert set(listed) == package_modules, f"{lang}: layer table is out of date"


def test_layer_tables_agree_across_languages() -> None:
    assert _documented_layers("en") == _documented_layers("ja")


def test_documented_layers_are_consistent_with_the_l4_constant() -> None:
    assert set(_documented_layers("en")[4]) == set(L4_MODULES)


def test_no_module_imports_a_strictly_higher_layer() -> None:
    layer = _module_layer()
    violations = [
        f"{module}(L{layer[module]}) -> {dependency}(L{layer[dependency]})"
        for module, dependencies in _import_graph().items()
        if module in layer
        for dependency in dependencies
        # The package root itself is the boundary declaration, not a layer member;
        # `from orchestune import <submodule>` records an edge to it.
        if dependency in layer and layer[dependency] > layer[module]
    ]
    assert sorted(violations) == []


def test_documented_subprocess_partition_matches_the_enforced_one() -> None:
    expected = {
        command: set(modules)
        for command, modules in EXPECTED_SUBPROCESS_COMMAND_MODULES.items()
    }
    for lang in DOC_LANGUAGES:
        assert _documented_subprocess_partition(lang) == expected, lang


def test_package_root_declares_a_public_api_without_entrypoints() -> None:
    import orchestune

    assert orchestune.__all__ == sorted(orchestune.__all__)
    assert len(orchestune.__all__) == len(set(orchestune.__all__))
    exported_modules = {
        getattr(getattr(orchestune, name), "__module__", "")
        for name in orchestune.__all__
    }
    reexported = {
        module.removeprefix(f"{PACKAGE_NAME}.")
        for module in exported_modules
        if module.startswith(f"{PACKAGE_NAME}.")
    }
    assert reexported & L4_MODULES == set()
    assert _import_graph()[PACKAGE_NAME] & L4_MODULES == set()

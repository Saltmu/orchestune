"""Shared AST and import-graph analysis for architecture tests."""

from __future__ import annotations

import ast
import functools
from collections import defaultdict
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[1] / "orchestune"
PACKAGE_NAME = "orchestune"
_SUBPROCESS_CALLS = frozenset({"run", "call", "Popen", "check_call", "check_output"})
_COMMANDS = frozenset({"git", "gh"})
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _module_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
    parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
    return ".".join((PACKAGE_NAME, *parts))


@functools.cache
def _package_modules() -> dict[str, Path]:
    """Every `.py` under `orchestune/`, subpackage initialisers included.

    A nested `__init__.py` can carry imports and package wiring of its own, so
    leaving it out would hide cycles and upward dependencies introduced there —
    and would quietly weaken the "the layer table covers every file" promise.

    Cached: callers only read the result, never mutate it, and this file's
    package tree doesn't change mid test-run, so repeated calls (12+ across
    this module's tests) can safely share one filesystem walk.
    """
    return {_module_name(path): path for path in PACKAGE_ROOT.rglob("*.py")}


def _relative_import_name(
    current_module: str, node: ast.ImportFrom, *, is_package: bool
) -> str | None:
    """`from . import x` / `from ..y import z` の解決先モジュール名を返す。

    相対importの基準は「そのモジュールが属するパッケージ」であり、`__init__.py`
    ではモジュール自身がそのパッケージになる。`orchestune/sub/__init__.py` の
    `from .. import cli` は `orchestune.cli` を指すが、`orchestune/foo.py` の
    同じ記述は1つ上（存在しない親）を指す。この違いを `is_package` で分ける。
    """
    if node.level == 0:
        return node.module

    base = current_module if is_package else current_module.rsplit(".", 1)[0]
    package_parts = base.split(".")
    target_len = len(package_parts) - node.level + 1
    if target_len <= 0:
        return None
    parent_parts = package_parts[:target_len]
    if node.module:
        parent_parts.extend(node.module.split("."))
    return ".".join(parent_parts) or None


def _internal_imports(
    current_module: str,
    tree: ast.AST,
    known_modules: set[str],
    *,
    is_package: bool,
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

        module_name = _relative_import_name(current_module, node, is_package=is_package)
        if module_name in known_modules:
            imports.add(module_name)
        if module_name is not None:
            # `from orchestune import dispatch_gc` / `from . import worker` は、
            # パッケージ名そのものではなく個々のサブモジュールへの依存でもある。
            imports.update(
                f"{module_name}.{alias.name}"
                for alias in node.names
                if f"{module_name}.{alias.name}" in known_modules
            )
    imports.discard(current_module)
    return imports


@functools.cache
def _import_graph() -> dict[str, set[str]]:
    modules = _package_modules()
    known_modules = set(modules)
    return {
        module.removeprefix(f"{PACKAGE_NAME}."): {
            dependency.removeprefix(f"{PACKAGE_NAME}.")
            for dependency in _internal_imports(
                module,
                ast.parse(path.read_text(encoding="utf-8")),
                known_modules,
                is_package=path.name == "__init__.py",
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

        for dependency in graph.get(module, ()):
            if dependency not in graph:
                continue
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
            if len(component) > 1 or module in graph.get(module, ()):
                cycles.update(component)

    for module in graph:
        if module not in index:
            strongconnect(module)
    return cycles


def _top_level_package(module: str) -> str:
    """モジュール名（orchestune.除去後）からトップレベルパッケージまたは単一モジュール名を返す。"""
    return module.split(".", 1)[0]


def _package_import_graph() -> dict[str, set[str]]:
    """モジュール依存グラフをトップレベルパッケージ単位に縮約したグラフを返す。

    同一パッケージ内のimportおよび公開APIを宣言するパッケージルート
    （orchestune/__init__.py）自身とのエッジは含めない。
    """
    module_graph = _import_graph()
    package_graph: dict[str, set[str]] = defaultdict(set)
    for module, dependencies in module_graph.items():
        if module == PACKAGE_NAME:
            continue
        src_pkg = _top_level_package(module)
        package_graph[src_pkg]
        for dep in dependencies:
            if dep == PACKAGE_NAME:
                continue
            dst_pkg = _top_level_package(dep)
            package_graph[dst_pkg]
            if src_pkg != dst_pkg:
                package_graph[src_pkg].add(dst_pkg)
    return dict(package_graph)


def _tarjan_scc(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan's SCCアルゴリズムによりグラフの強連結成分のリストを返す。"""
    index_counter = [0]
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []
    sccs: list[list[str]] = []

    def strongconnect(node: str) -> None:
        index[node] = index_counter[0]
        lowlink[node] = index_counter[0]
        index_counter[0] += 1
        stack.append(node)
        on_stack[node] = True

        for dep in graph.get(node, ()):
            if dep not in graph:
                continue
            if dep not in index:
                strongconnect(dep)
                lowlink[node] = min(lowlink[node], lowlink[dep])
            elif on_stack.get(dep):
                lowlink[node] = min(lowlink[node], index[dep])

        if lowlink[node] == index[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack[member] = False
                component.append(member)
                if member == node:
                    break
            sccs.append(component)

    for node in graph:
        if node not in index:
            strongconnect(node)
    return sccs


def _package_cycle_edges(graph: dict[str, set[str]]) -> set[tuple[str, str]]:
    """非自明な強連結成分（要素数2以上、または自己ループ）に属する循環エッジを返す。

    許容エッジを先に除外する方式では既存の許容エッジを経由する新規循環を
    見逃すため、元の縮約グラフのSCCから同一SCC内エッジを列挙する。
    """
    cycle_edges: set[tuple[str, str]] = set()
    for scc in _tarjan_scc(graph):
        scc_set = set(scc)
        if len(scc_set) > 1:
            for u in scc_set:
                for v in graph.get(u, ()):
                    if v in scc_set:
                        cycle_edges.add((u, v))
        elif len(scc_set) == 1:
            u = scc[0]
            if u in graph.get(u, ()):
                cycle_edges.add((u, u))
    return cycle_edges


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


def _scope_bindings(scope: ast.AST) -> tuple[set[str], dict[str, set[str]]]:
    """そのスコープが代入する名前と、名前ごとのコマンド候補を返す。

    候補は「そのスコープ内のあらゆる代入」の和集合であり、位置も分岐も条件も
    問わない。`if` の片方だけで再代入される、ループで書き換わる、ネストした
    関数から実行時に参照される — いずれも静的には実行経路が決まらないため、
    ありうる束縛はすべて候補として扱う（見逃すより過剰に報告する側へ倒す）。

    第1要素はコマンドリテラル以外を代入された名前も含む。Pythonではスコープ内で
    一度でも代入された名前はそのスコープのローカルになるため、外側の同名を
    引き継がないようにするのに必要。ただし`global` / `nonlocal`宣言された名前は
    代入してもローカルにならない（外側の名前そのものを書き換える）ので除外し、
    外側から引き継いだ候補が残るようにする。
    """
    assigned: set[str] = set()
    rebound_outer: set[str] = set()
    candidates: dict[str, set[str]] = defaultdict(set)
    for node in _nodes_in_scope(scope):
        if isinstance(node, _SCOPE_NODES):
            continue
        if isinstance(node, ast.Global | ast.Nonlocal):
            rebound_outer.update(node.names)
            continue
        targets, value = _assigned_names(node)
        command = _leading_command(value)
        for target in targets:
            if isinstance(target, ast.Name):
                assigned.add(target.id)
                if command is not None:
                    candidates[target.id].add(command)
    return assigned - rebound_outer, dict(candidates)


def _scan_scope(
    scope: ast.AST,
    inherited: dict[str, set[str]],
    subprocess_names: set[str],
    call_names: set[str],
    found: set[str],
) -> None:
    """1つのスコープを走査し、実行されたコマンド名を `found` へ集める。

    スコープ内で代入される名前はそのスコープの候補で解決し（外側の同名は
    Pythonの規則どおり見えないので引き継がない）、代入されない自由変数は
    外側から引き継いだ候補で解決する。`global` / `nonlocal`宣言された名前は
    ローカルを作らないため、外側の候補と自スコープの候補を合わせて扱う。
    """
    assigned, candidates = _scope_bindings(scope)
    bindings = {
        name: set(commands)
        for name, commands in inherited.items()
        if name not in assigned
    }
    for name, commands in candidates.items():
        bindings[name] = bindings.get(name, set()) | commands

    for node in _nodes_in_scope(scope):
        if isinstance(node, _SCOPE_NODES):
            # クラス本体の名前はメソッドからは見えない（メソッド内の裸の名前は
            # 外側の関数スコープ→モジュールグローバルへと解決され、クラス属性は
            # 参照されない）。そのためクラス配下のスコープへは、クラス本体が
            # 作った束縛ではなく、クラス自身が引き継いだ束縛をそのまま渡す。
            nested = inherited if isinstance(scope, ast.ClassDef) else bindings
            _scan_scope(node, nested, subprocess_names, call_names, found)
            continue
        if isinstance(node, ast.Call):
            argument = _subprocess_first_argument(node, subprocess_names, call_names)
            command = _leading_command(argument)
            if command is not None:
                found.add(command)
            elif isinstance(argument, ast.Name):
                found.update(bindings.get(argument.id, ()))


def _subprocess_command_modules() -> dict[str, set[str]]:
    """{コマンド: そのコマンドをsubprocess実行しているモジュール名}を返す。

    検出できるのは、コマンドリストがリテラルとして書かれている呼び出し
    （直接渡す場合と、リテラルを代入した変数を渡す場合）です。変数経由の場合は
    分岐やループを問わず、その名前が取りうる束縛をすべて候補とします。
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

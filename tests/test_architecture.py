"""Architecture invariants kept visible while the package is refactored."""

from __future__ import annotations

import ast
import functools
import inspect
import re
import tomllib
from collections import defaultdict
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[1] / "orchestune"
TESTS_ROOT = Path(__file__).parent
REPO_ROOT = Path(__file__).parents[1]
SKILLS_ROOT = REPO_ROOT / "skills"
DOCS_ROOT = Path(__file__).parents[1] / "docs"
DOC_LANGUAGES = ("en", "ja")
# `local-ci-developer` documents this repo's own dev workflow (TDD/local CI
# rules) and is deliberately never linked into other projects by
# `setup_skills` (see `SKILLS_EXCLUDED_FROM_SETUP`), so it has no reason to
# ship in the distributed package either.
PACKAGING_EXCLUDED_SKILLS = frozenset({"local-ci-developer"})
PACKAGE_NAME = "orchestune"
# The layer assignment is a design decision, so it lives here rather than being
# parsed back out of the architecture documents. `_module_layer()` reads this,
# which keeps `test_no_module_imports_a_strictly_higher_layer` independent of
# prose: moving a module to a lower layer in a document used to silence real
# violations instead of failing (#515). The documents are checked against this
# mapping by `test_documented_layers_match_the_expected_layers`.
EXPECTED_LAYERS: dict[int, frozenset[str]] = {
    4: frozenset(
        {
            "bootstrap",
            "cli",
            "dag.cli",
            "dispatch.dispatcher",
            "monitor",
            "provisioning",
        }
    ),
    3: frozenset(
        {
            "dispatch.cycle",
            "dispatch.cycle_context",
            "dispatch.cycle_report",
            "dispatch.phase_gc",
            "dispatch.phase_reconciliation",
            "dispatch.phase_rebase",
            "dispatch.phase_scheduling",
            "dispatch.postcycle",
            "dispatch.report",
            "integrator",
            "integrator.coordinator",
            "integrator.parent_completion",
            "integrator.steps",
            "integrator.types",
            "provisioning_flow",
        }
    ),
    2: frozenset(
        {
            "dag.contracts",
            "dag.graph",
            "dag.parsing",
            "dag.similarity",
            "dispatch.actor_verification",
            "dispatch.config",
            "dispatch.escalation",
            "dispatch.filters",
            "dispatch.gc",
            "dispatch.gc.completion",
            "dispatch.gc.git",
            "dispatch.gc.zombies",
            "dispatch.labels",
            "dispatch.launch",
            "dispatch.locks",
            "dispatch.rebase",
            "dispatch.reconciliation",
            "dispatch.recovery",
            "dispatch.rules",
            "dispatch.scoring",
            "dispatch.state",
            "dispatch.targets",
            "dispatch.worktree",
            "infra.not_needed_review_state",
            "integrator.git_ops",
            "integrator.pr",
            "integrator.tasks",
            "integrator.worktree",
            "issue_parsing",
            "status_snapshot",
            "symbol_verification",
            "provisioning_parent",
            "provisioning_plan",
            "provisioning_rendering",
            "provisioning_subtasks",
        }
    ),
    1: frozenset(
        {"forge", "forge.admin", "forge.issues", "forge.prs", "infra.git_cli"}
    ),
    0: frozenset(
        {
            "bounded_limit",
            "dag",
            "dag.models",
            "dispatch",
            "dispatch.result",
            "infra",
            "infra.json_state",
            "infra.process_utils",
            "models",
            "outcome_record",
            "plan_writer",
            "setup_skills",
            "validation",
            "version",
        }
    ),
}
L4_MODULES = EXPECTED_LAYERS[4]
ALLOWED_L4_DEPENDENTS = {
    "bootstrap": frozenset({"cli"}),
    "dag.cli": frozenset({"cli"}),
    "dispatch.dispatcher": frozenset({"cli"}),
    "monitor": frozenset({"cli"}),
    "provisioning": frozenset({"cli"}),
}
EXPECTED_SUBPROCESS_COMMAND_MODULES = {
    "gh": {"forge.admin"},
    "git": {"infra.git_cli"},
}
_SUBPROCESS_CALLS = frozenset({"run", "call", "Popen", "check_call", "check_output"})
_COMMANDS = frozenset({"git", "gh"})
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_LAYER_ROW = re.compile(r"^\s*\|\s*\*\*L(\d)\*\*")
_COMMAND_ROW = re.compile(r"^\s*\|\s*`(git|gh)`\s*\|")
# Dotted so a module under a subpackage can be documented as `sub.worker`,
# matching the name `_module_name()` produces for it.
_BACKTICKED = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)`")
_BOUNDED_RECOVERY_LIMIT_NAME = re.compile(
    r"^(?:max_.*(?:retries|reclaims)|.*_timeout_seconds)$"
)
# #566: This is intentionally an allowlist rather than a control-flow guesser.
# It catches new finite retry/recovery limits by name, then makes the terminal
# behaviour they promise explicit and mechanically checked.  A limit that is
# not part of a retry/recovery loop (for example a throughput window) must not
# be added here; its boundedness is verified by its owning feature's tests.
BOUNDED_RECOVERY_TERMINALS = {
    "max_recompute_retries": ("dispatch/rebase.py", "forced_serial"),
    "max_task_reclaims": ("dispatch/gc/zombies.py", "apply_human_review_escalation"),
    "not_needed_review_timeout_seconds": (
        "integrator/coordinator.py",
        "apply_human_review_escalation",
    ),
}


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
                    imported = _relative_import_name(
                        module, node, is_package=path.name == "__init__.py"
                    )
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


def _determinism_section(lang: str) -> list[str]:
    """`### 0.1`見出しから、次の同位以上の見出しまたは水平線の直前までの行を返す。

    見出し番号だけで探すのは、節タイトルが言語ごとに異なるため。`####`以下の
    小見出しは節の内容として取り込む（打ち切るのはL1-L3の見出しのみ）。
    """
    collected: list[str] = []
    in_section = False
    for line in _architecture_doc(lang):
        if line.startswith("### 0.1"):
            in_section = True
            collected.append(line)
            continue
        if in_section:
            if re.match(r"#{1,3} ", line) or line.startswith("---"):
                break
            collected.append(line)
    return collected


def _row_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _documented_layers(lang: str) -> dict[int, list[str]]:
    """The `**L<n>**` rows of the module-layer table, as {layer: modules}.

    Modules are read from the row's *last* cell, so the table may carry any
    number of leading descriptive columns (layer, role, ...) without this
    breaking. Should a table ever end with something other than the module
    list, the extraction goes wrong loudly: the result no longer matches
    `EXPECTED_LAYERS` and the sync test fails naming the mismatch, rather than
    silently weakening an architectural check (#515).

    Each row stays a list rather than a set so that a module repeated inside one
    row survives to be caught by the exact-once assertion.
    """
    layers: dict[int, list[str]] = {}
    for line in _architecture_doc(lang):
        match = _LAYER_ROW.match(line)
        if match is None:
            continue
        assert int(match.group(1)) not in layers, f"duplicate layer row in {lang}"
        layers[int(match.group(1))] = _BACKTICKED.findall(_row_cells(line)[-1])
    return layers


def _documented_subprocess_partition(lang: str) -> dict[str, set[str]]:
    """The command/module rows of the '`git`/`gh` stay in L1' table."""
    return {
        _row_cells(line)[0].strip("`"): set(_BACKTICKED.findall(_row_cells(line)[-1]))
        for line in _architecture_doc(lang)
        if _COMMAND_ROW.match(line)
    }


def _module_layer() -> dict[str, int]:
    """Every layered module's layer, from `EXPECTED_LAYERS` — never from a doc."""
    return {
        module: layer
        for layer, modules in EXPECTED_LAYERS.items()
        for module in modules
    }


def test_git_and_gh_subprocess_modules_are_strictly_partitioned() -> None:
    assert _subprocess_command_modules() == EXPECTED_SUBPROCESS_COMMAND_MODULES


def test_expected_layers_cover_every_module_exactly_once() -> None:
    """`EXPECTED_LAYERS` is the layer map, so it is what must stay exhaustive.

    `orchestune/__init__.py` is the one file with no layer: it declares the
    boundary rather than living inside it. The exemption is stated in both
    architecture documents, and what the package root may import is asserted
    by `test_package_root_declares_a_public_api_without_entrypoints`.
    """
    package_modules = {
        module.removeprefix(f"{PACKAGE_NAME}.")
        for module in _package_modules()
        if module != PACKAGE_NAME
    }
    listed = [module for modules in EXPECTED_LAYERS.values() for module in modules]
    assert sorted(listed) == sorted(set(listed)), "module assigned to two layers"
    assert set(listed) == package_modules, "EXPECTED_LAYERS is out of date"


def test_documented_layers_match_the_expected_layers() -> None:
    """Both documents must reproduce `EXPECTED_LAYERS` — the one doc-sync check.

    This is the only place the layer tables are read. A document that drifts
    fails here by name; it can no longer change what the layering test enforces.
    """
    expected = {layer: set(modules) for layer, modules in EXPECTED_LAYERS.items()}
    for lang in DOC_LANGUAGES:
        documented = _documented_layers(lang)
        for layer, modules in documented.items():
            assert sorted(modules) == sorted(
                set(modules)
            ), f"{lang}: module listed twice in the L{layer} row"
        assert {
            layer: set(modules) for layer, modules in documented.items()
        } == expected, f"{lang}: layer table does not match EXPECTED_LAYERS"


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


def _pyproject_included_skill_paths() -> set[str]:
    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    return {
        entry["path"]
        for entry in pyproject["tool"]["poetry"]["include"]
        if entry["path"].startswith("skills/")
    }


def _distributable_skill_dirs() -> set[str]:
    return {
        f"skills/{d.name}"
        for d in SKILLS_ROOT.iterdir()
        if d.is_dir()
        and (d / "SKILL.md").is_file()
        and d.name not in PACKAGING_EXCLUDED_SKILLS
    }


def test_pyproject_include_lists_every_distributable_skill() -> None:
    """#408: a skill missing from `[tool.poetry].include` is silently absent
    from the built wheel/sdist, so `setup_skills(with_workflow_skill=True)`
    finds no source to copy under a pipx-style install even though the repo
    checkout has it under `skills/`."""
    assert _pyproject_included_skill_paths() == _distributable_skill_dirs()


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


def test_architecture_docs_mention_dispatch_cycle_report() -> None:
    """#411: #396のディスパッチサイクルレポート（親Issueへのコメント投稿）
    機能がarchitecture.mdのトレーサビリティに関する記述に反映されていること。
    見出し文字列は実装（`_format_event_log_comment`）のソースから直接抽出し、
    ハードコード複製によるドリフトを防ぐ。"""
    from orchestune.dispatch.postcycle import _format_event_log_comment

    source = inspect.getsource(_format_event_log_comment)
    match = re.search(r'lines = \["(## [^"]+)\\n"\]', source)
    assert match, (
        "_format_event_log_commentのソースからレポート見出し文字列を"
        "抽出できませんでした"
    )
    header = match.group(1)

    for lang in DOC_LANGUAGES:
        doc_text = "\n".join(_architecture_doc(lang))
        assert header in doc_text, f"{lang}のarchitecture.mdに'{header}'がありません"


def test_architecture_docs_document_the_determinism_principle() -> None:
    """#509: クオーター効率（第0章）を達成する手段としての決定論——LLMは判断のみを
    担い、状態遷移は決定論的なPython側が行う——が、ja/en両方のarchitecture.mdに
    設計原則として記載されていること。

    節の存在だけでなく、自動化が収束できなかった場合の終端状態（人間への
    エスカレーション）に言及していることも確認する。ラベル名は`_LABEL_PRIORITY`
    から抽出し、ハードコード複製によるドリフトを防ぐ。
    """
    from orchestune.status_snapshot import _LABEL_PRIORITY, MonitorState

    escalation_label = next(
        label
        for label, state in _LABEL_PRIORITY
        if state is MonitorState.BLOCKED_HUMAN_REVIEW
    )

    for lang in DOC_LANGUAGES:
        section = _determinism_section(lang)
        assert (
            section
        ), f"{lang}のarchitecture.mdに'### 0.1'で始まる決定論の節がありません"
        body = "\n".join(section)
        assert escalation_label in body, (
            f"{lang}のarchitecture.mdの0.1節に、終端状態'{escalation_label}'への"
            "言及がありません"
        )


def _bounded_recovery_limits(config_source: str) -> set[str]:
    """有限なリトライ／回収設定だけを`DispatcherConfig`から抽出する。

    `0` と `None` は既定で無効なタイムアウトを表すため対象外にする。対象の
    命名規約を持つ正の整数の設定が追加された場合、下のregistry完全性テストに
    より対応する終端動作なしではCIを通せない。
    """
    tree = ast.parse(config_source)
    config = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "DispatcherConfig"
    )
    return {
        node.target.id
        for node in config.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and _BOUNDED_RECOVERY_LIMIT_NAME.fullmatch(node.target.id)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, int)
        and node.value.value > 0
    }


def _source_has_terminal_marker(source: str, marker: str) -> bool:
    """AST上で終端動作を表す関数名または文字列リテラルを検出する。"""
    tree = ast.parse(source)
    return any(
        (isinstance(node, ast.Name) and node.id == marker)
        or (isinstance(node, ast.Constant) and node.value == marker)
        for node in ast.walk(tree)
    )


def test_bounded_recovery_limit_registry_covers_every_finite_config_setting() -> None:
    """#566: 新たな有限リトライ／回収上限には終端動作を必ず宣言させる。"""
    config_source = (PACKAGE_ROOT / "dispatch" / "config.py").read_text(
        encoding="utf-8"
    )
    assert _bounded_recovery_limits(config_source) == set(BOUNDED_RECOVERY_TERMINALS)


def test_bounded_recovery_limit_registry_points_at_terminal_behaviour() -> None:
    """#566: 登録済み上限が収束または人間エスカレーションへ実際に到達する。"""
    for limit, (module, marker) in BOUNDED_RECOVERY_TERMINALS.items():
        source = (PACKAGE_ROOT / module).read_text(encoding="utf-8")
        assert _source_has_terminal_marker(
            source, marker
        ), f"{limit} has no terminal behaviour marker {marker!r} in {module}"


def test_bounded_recovery_limit_detection_rejects_an_unregistered_limit() -> None:
    """設定だけを追加してregistryを更新しない変更をRedにする検出器の例。"""
    source = """
class DispatcherConfig:
    max_recompute_retries: int = 2
    max_missing_retries: int = 1
    task_timeout_seconds: int = 0
"""
    limits = _bounded_recovery_limits(source)
    registry = {"max_recompute_retries"}
    assert limits == {
        "max_recompute_retries",
        "max_missing_retries",
    }
    assert limits - registry == {"max_missing_retries"}


def test_bounded_recovery_limit_detection_rejects_a_missing_terminal_marker() -> None:
    """登録だけしても終端処理を消せばRedになる。"""
    assert not _source_has_terminal_marker("def retry(): pass", "forced_serial")


def _collect_dict_assignments(tree: ast.AST) -> dict[str, list[ast.Dict]]:
    dict_assignments: dict[str, list[ast.Dict]] = defaultdict(list)
    for node in ast.walk(tree):
        targets, value = _assigned_names(node)
        if isinstance(value, ast.Dict):
            for target in targets:
                if isinstance(target, ast.Name):
                    dict_assignments[target.id].append(value)
    return dict_assignments


def _is_subprocess_call(node: ast.Call) -> bool:
    if isinstance(node.func, ast.Attribute) and node.func.attr in _SUBPROCESS_CALLS:
        return (
            isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"
        )
    if isinstance(node.func, ast.Name):
        return node.func.id in _SUBPROCESS_CALLS
    return False


def _call_text_and_encoding_flags(
    node: ast.Call, dict_assignments: dict[str, list[ast.Dict]]
) -> tuple[bool, bool]:
    keyword_names = {kw.arg for kw in node.keywords if kw.arg is not None}
    has_text = "text" in keyword_names or "universal_newlines" in keyword_names
    has_encoding = "encoding" in keyword_names

    for kw in node.keywords:
        if kw.arg is None and isinstance(kw.value, ast.Name):
            for d in dict_assignments.get(kw.value.id, []):
                d_keys = [
                    k.value
                    for k in d.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                ]
                if "text" in d_keys or "universal_newlines" in d_keys:
                    has_text = True
                if "encoding" in d_keys:
                    has_encoding = True

    return has_text, has_encoding


def _unencoded_text_subprocess_calls() -> list[str]:
    """subprocess呼び出しでtext/universal_newlinesが有効なのにencodingが未指定の箇所を返す。

    #531: 非UTF-8ロケール（Windows cp932等）でsubprocess.run(text=True)を
    呼ぶと、encoding未指定時にlocale.getpreferredencoding()が使われ、
    非ASCIIの入出力でデコード/エンコードエラーが発生する。これを防ぐため、
    textモードを有効にする呼び出しは必ず明示的なencoding（"utf-8"等）を
    伴わなければならない。
    """
    violations: list[str] = []
    for module, path in _package_modules().items():
        short_name = module.removeprefix(f"{PACKAGE_NAME}.")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        dict_assignments = _collect_dict_assignments(tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_subprocess_call(node):
                continue

            has_text, has_encoding = _call_text_and_encoding_flags(
                node, dict_assignments
            )
            if has_text and not has_encoding:
                violations.append(
                    f"{short_name}:{node.lineno}:subprocess call with text=True missing encoding"
                )

    return sorted(violations)


def _unencoded_write_text_calls() -> list[str]:
    """Path.write_text呼び出しでencodingが未指定の箇所を返す。

    #531: write_textのencoding未指定はロケール依存のエンコーディングを使用するため、
    必ずencoding="utf-8"を明示しなければならない。
    """
    violations: list[str] = []
    for module, path in _package_modules().items():
        short_name = module.removeprefix(f"{PACKAGE_NAME}.")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute) and node.func.attr == "write_text":
                keyword_names = {kw.arg for kw in node.keywords if kw.arg is not None}
                has_encoding = len(node.args) >= 2 or "encoding" in keyword_names
                if not has_encoding:
                    violations.append(
                        f"{short_name}:{node.lineno}:write_text missing encoding"
                    )
    return sorted(violations)


def test_subprocess_text_mode_requires_explicit_encoding() -> None:
    assert _unencoded_text_subprocess_calls() == []


def test_path_write_text_requires_explicit_encoding() -> None:
    assert _unencoded_write_text_calls() == []


def test_collect_dict_assignments_captures_annotated_assignments() -> None:
    """#531 review: git_cli.pyのkwargs: dict[str, Any] = {...} のような
    型注釈付き代入（ast.AnnAssign）も_collect_dict_assignmentsが正しく捕捉すること。"""
    code = """
kwargs: dict[str, Any] = {"text": True, "encoding": "utf-8"}
unannotated = {"text": True}
"""
    tree = ast.parse(code)
    assignments = _collect_dict_assignments(tree)
    assert "kwargs" in assignments
    assert "unannotated" in assignments


def test_module_name_resolves_subpackage_paths() -> None:
    """#614: _module_nameが直下・サブパッケージ・ネスト構造を正確にモジュール名へ解決すること。"""
    assert _module_name(PACKAGE_ROOT / "cli.py") == "orchestune.cli"
    assert _module_name(PACKAGE_ROOT / "__init__.py") == "orchestune"
    assert (
        _module_name(PACKAGE_ROOT / "forge" / "issues.py") == "orchestune.forge.issues"
    )
    assert _module_name(PACKAGE_ROOT / "forge" / "__init__.py") == "orchestune.forge"
    assert (
        _module_name(PACKAGE_ROOT / "dispatch" / "phase" / "gc.py")
        == "orchestune.dispatch.phase.gc"
    )
    assert (
        _module_name(PACKAGE_ROOT / "dispatch" / "phase" / "__init__.py")
        == "orchestune.dispatch.phase"
    )


def test_relative_import_resolution_for_subpackages() -> None:
    """#614: _relative_import_nameがサブパッケージ内外の相対インポートを正しく解決すること。"""
    # 1. サブモジュールから同一サブパッケージ内への相対インポート
    tree1 = ast.parse("from . import context")
    import_from1 = tree1.body[0]
    assert isinstance(import_from1, ast.ImportFrom)
    assert (
        _relative_import_name(
            "orchestune.dispatch.cycle", import_from1, is_package=False
        )
        == "orchestune.dispatch"
    )

    tree2 = ast.parse("from .context import Context")
    import_from2 = tree2.body[0]
    assert isinstance(import_from2, ast.ImportFrom)
    assert (
        _relative_import_name(
            "orchestune.dispatch.cycle", import_from2, is_package=False
        )
        == "orchestune.dispatch.context"
    )

    # 2. サブモジュールから親パッケージ／別サブパッケージへの相対インポート
    tree3 = ast.parse("from ..forge import issues")
    import_from3 = tree3.body[0]
    assert isinstance(import_from3, ast.ImportFrom)
    assert (
        _relative_import_name(
            "orchestune.dispatch.cycle", import_from3, is_package=False
        )
        == "orchestune.forge"
    )

    tree4 = ast.parse("from .. import git_cli")
    import_from4 = tree4.body[0]
    assert isinstance(import_from4, ast.ImportFrom)
    assert (
        _relative_import_name(
            "orchestune.dispatch.cycle", import_from4, is_package=False
        )
        == "orchestune"
    )

    # 3. サブパッケージの __init__.py からの相対インポート
    tree5 = ast.parse("from . import cycle")
    import_from5 = tree5.body[0]
    assert isinstance(import_from5, ast.ImportFrom)
    assert (
        _relative_import_name("orchestune.dispatch", import_from5, is_package=True)
        == "orchestune.dispatch"
    )

    tree6 = ast.parse("from .cycle import run")
    import_from6 = tree6.body[0]
    assert isinstance(import_from6, ast.ImportFrom)
    assert (
        _relative_import_name("orchestune.dispatch", import_from6, is_package=True)
        == "orchestune.dispatch.cycle"
    )

    # 4. パッケージ境界を超える相対インポート
    tree7 = ast.parse("from ... import outside")
    import_from7 = tree7.body[0]
    assert isinstance(import_from7, ast.ImportFrom)
    assert (
        _relative_import_name("orchestune.cli", import_from7, is_package=False) is None
    )

    # 深い階層からパッケージ境界を大幅に超えるケース（負のスライスインデックスによる誤解決の防止）
    tree8 = ast.parse("from ..... import way_outside")
    import_from8 = tree8.body[0]
    assert isinstance(import_from8, ast.ImportFrom)
    assert (
        _relative_import_name(
            "orchestune.dispatch.phase.gc", import_from8, is_package=False
        )
        is None
    )


def test_internal_imports_capture_subpackage_dependencies() -> None:
    """#614: _internal_importsがサブパッケージ間の依存関係を正しく網羅すること。"""
    known_modules = {
        "orchestune",
        "orchestune.dispatch",
        "orchestune.dispatch.cycle",
        "orchestune.dispatch.context",
        "orchestune.forge",
        "orchestune.forge.issues",
        "orchestune.git_cli",
    }
    source = """
from . import context
from .context import Context
from ..forge import issues
from ..forge.issues import IssueRecord
from ..git_cli import run_git
import orchestune.forge
import os
"""
    tree = ast.parse(source)
    imports = _internal_imports(
        "orchestune.dispatch.cycle", tree, known_modules, is_package=False
    )
    assert imports == {
        "orchestune.dispatch",
        "orchestune.dispatch.context",
        "orchestune.forge",
        "orchestune.forge.issues",
        "orchestune.git_cli",
    }


def test_import_graph_detects_subpackage_cycles() -> None:
    """#614: _cycle_membersがサブパッケージをまたぐ循環依存を検出できること。"""
    graph_with_cycle = {
        "dispatch.cycle": {"forge.issues"},
        "forge.issues": {"dispatch.cycle"},
        "dag_graph": {"models"},
    }
    assert _cycle_members(graph_with_cycle) == {"dispatch.cycle", "forge.issues"}

    graph_without_cycle = {
        "dispatch.cycle": {"forge.issues"},
        "forge.issues": {"models"},
        "models": set(),
    }
    assert _cycle_members(graph_without_cycle) == set()


def test_relative_import_resolution_multilevel() -> None:
    """#614: 深い階層のサブパッケージからの多階層相対インポート解決。"""
    tree = ast.parse("from ...forge.issues import IssueRecord")
    import_from = tree.body[0]
    assert isinstance(import_from, ast.ImportFrom)
    assert (
        _relative_import_name(
            "orchestune.dispatch.phase.gc", import_from, is_package=False
        )
        == "orchestune.forge.issues"
    )

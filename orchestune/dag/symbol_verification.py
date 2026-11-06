"""#359: `Footprint.symbols`が現在のコードベースに見つかるかを検証する。

リファクタ（ファイル分割・関数移動・リネーム）を経たdecomposition planでは、
`symbols`に記載された対象が既に存在しないコードスナップショットを指して
いることがある。ただし`symbols`は「このsubtaskが定義または変更する
シンボル」でもあるため、未検出＝陳腐化と断定はできない（既存ファイルへの
新規追加の可能性がある）。Issue生成時に未検出のシンボルを検出し、中立な
注記として本文へ残せるようにする（`provisioning.py`から呼ばれる）。
"""

from __future__ import annotations

import ast
import functools
import logging
from pathlib import Path
from typing import Any

from orchestune.dag.models import SubTask

try:
    import tree_sitter
    import tree_sitter_go
    import tree_sitter_javascript
    import tree_sitter_rust
    import tree_sitter_typescript

    _TREE_SITTER_AVAILABLE = True
except ImportError:  # pragma: no cover
    _TREE_SITTER_AVAILABLE = False


logger = logging.getLogger(__name__)


def _flatten_scope_statements(statements: list[ast.stmt]) -> list[ast.stmt]:
    """`statements`を、関数・クラスの境界では止まりつつ`if`/`try`/`with`/
    `for`/`while`/`match`の内側までは平坦化して返す。

    このstatement列自身が属するスコープ（モジュール直下、またはクラス
    直下）がどちらであっても使える: `if`/`try`/`with`/ループ/`match`は、
    モジュールスコープでもクラススコープでも、その中で書かれた代入・def・
    classを、それを囲むブロックと同じスコープへそのまま束縛する
    （`try: import X as Y except: import Z as Y`のような条件付き定義や、
    `class Parser: if FEATURE: def parse(self): ...`のような条件付き
    メソッド定義が典型例）。単純に`statements`の直接の子だけを見ると、
    こうした複合文の中身を見落とす（レビュー指摘 #372）。関数・クラス定義は
    それ自体で新しいスコープを作るため、その中へは再帰しない。
    """
    flattened: list[ast.stmt] = []
    for stmt in statements:
        flattened.append(stmt)
        if isinstance(stmt, ast.If):
            flattened.extend(_flatten_scope_statements(stmt.body))
            flattened.extend(_flatten_scope_statements(stmt.orelse))
        elif isinstance(stmt, ast.Try | ast.TryStar):
            flattened.extend(_flatten_scope_statements(stmt.body))
            for handler in stmt.handlers:
                flattened.extend(_flatten_scope_statements(handler.body))
            flattened.extend(_flatten_scope_statements(stmt.orelse))
            flattened.extend(_flatten_scope_statements(stmt.finalbody))
        elif isinstance(stmt, ast.With | ast.AsyncWith):
            flattened.extend(_flatten_scope_statements(stmt.body))
        elif isinstance(stmt, ast.For | ast.AsyncFor | ast.While):
            flattened.extend(_flatten_scope_statements(stmt.body))
            flattened.extend(_flatten_scope_statements(stmt.orelse))
        elif isinstance(stmt, ast.Match):
            for case in stmt.cases:
                flattened.extend(_flatten_scope_statements(case.body))
    return flattened


def _collect_all_names(tree: ast.Module) -> set[str]:
    """`symbol`との完全一致判定に使う識別子集合を返す（クラス名・
    `ClassName.method`限定名 + トップレベル識別子全て）。

    ネストした関数定義（クロージャのヘルパ等）も対象に含めるため、
    クラス・メソッドの収集には`ast.walk`で全ノードを走査する。ただし
    トップレベル代入は`_collect_top_level_names`が返すモジュールスコープ
    限定の集合をそのまま取り込む — 関数・メソッド内のローカル変数を
    「定義済み識別子」として拾ってしまうと、実在しないbareシンボルが
    無関係なローカル変数と一致して見逃されてしまう（レビュー指摘 #372）。
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            names.add(node.name)
            for child in _flatten_scope_statements(node.body):
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    names.add(child.name)
                    names.add(f"{node.name}.{child.name}")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
    names |= _collect_top_level_names(tree)
    return names


class _ModuleAllReferenceVisitor(ast.NodeVisitor):
    """Find module-scope reads/bindings of ``__all__`` without entering scopes."""

    def __init__(self) -> None:
        self.found = False

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == "__all__":
            self.found = True

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            bound_name = alias.asname or alias.name.partition(".")[0]
            if bound_name == "__all__":
                self.found = True

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name != "*" and (alias.asname or alias.name) == "__all__":
                self.found = True

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function_header(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function_header(node)

    def _visit_function_header(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        if node.name == "__all__":
            self.found = True
        for decorator in node.decorator_list:
            self.visit(decorator)
        self.visit(node.args)
        if node.returns is not None:
            self.visit(node.returns)
        for type_param in getattr(node, "type_params", ()):
            self.visit(type_param)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node.name == "__all__":
            self.found = True
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword)
        for type_param in getattr(node, "type_params", ()):
            self.visit(type_param)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        # Lambda defaults are evaluated in the enclosing scope; the body is not.
        self.visit(node.args)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name == "__all__":
            self.found = True
        if node.type is not None:
            self.visit(node.type)
        for statement in node.body:
            self.visit(statement)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name == "__all__":
            self.found = True
        if node.pattern is not None:
            self.visit(node.pattern)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name == "__all__":
            self.found = True

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest == "__all__":
            self.found = True
        self.generic_visit(node)


def _is_standalone_all_assignment(node: ast.stmt) -> bool:
    if isinstance(node, ast.Assign):
        return (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "__all__"
        )
    return (
        isinstance(node, ast.AnnAssign)
        and node.value is not None
        and isinstance(node.target, ast.Name)
        and node.target.id == "__all__"
    )


def _collect_static_all_names(tree: ast.Module) -> set[str] | None:
    """Return a literal module ``__all__`` or ``None`` when it is ambiguous.

    Only a single direct assignment to ``__all__`` is accepted. Any other
    module-scope reference or binding makes the declaration ambiguous; nested
    function and class bodies are separate scopes and are not inspected.
    """
    assignments = [node for node in tree.body if _is_standalone_all_assignment(node)]
    if len(assignments) != 1:
        return None

    declaration = assignments[0]
    references = _ModuleAllReferenceVisitor()
    for node in _flatten_scope_statements(tree.body):
        if node is not declaration:
            references.visit(node)
            if references.found:
                return None

    if isinstance(declaration, ast.Assign):
        value = declaration.value
    elif isinstance(declaration, ast.AnnAssign) and declaration.value is not None:
        annotation_references = _ModuleAllReferenceVisitor()
        annotation_references.visit(declaration.annotation)
        if annotation_references.found:
            return None
        value = declaration.value
    else:
        return None
    if not isinstance(value, ast.List | ast.Tuple):
        return None
    names: set[str] = set()
    for element in value.elts:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            return None
        names.add(element.value)
    return names


def _collect_reexported_names(tree: ast.Module) -> set[str]:
    """Return module ``ImportFrom`` bindings explicitly named in static ``__all__``."""
    all_names = _collect_static_all_names(tree)
    if all_names is None or not all_names:
        return set()

    imported_names: set[str] = set()
    for node in _flatten_scope_statements(tree.body):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level == 0 and node.module == "__future__":
            continue
        imported_names.update(
            alias.asname or alias.name for alias in node.names if alias.name != "*"
        )
    return imported_names & all_names


def _collect_top_level_names(tree: ast.Module) -> set[str]:
    """`_symbol_matches`が「モジュール修飾記法」（`db.get_connection`等）を
    末尾セグメントだけで緩く照合する際の候補集合を返す。

    モジュールスコープの定義・代入に加え、静的`__all__`に明示された
    `ImportFrom`の再エクスポート名も含める。

    `ast.walk`は木全体をフラットに走査してしまいスコープ情報を失うため、
    これだけは`_flatten_scope_statements(tree.body)`（モジュールスコープの
    statementのみ、`if`/`try`/`with`/ループ/`match`の中身まで含む）を走査
    する: レビュー指摘 #372で複数件見つかった通り、`ast.walk`ベースの判定は
    クラスメソッドの裸名や関数・メソッドのローカル変数をモジュールレベルの
    定義と取り違える。
    """
    top_level_names: set[str] = set()
    for node in _flatten_scope_statements(tree.body):
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            top_level_names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    top_level_names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            top_level_names.add(node.target.id)
    top_level_names |= _collect_reexported_names(tree)
    return top_level_names


def _collect_defined_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """モジュール内で定義されている識別子集合を`(全識別子, トップレベル識別子)`で返す。"""
    return _collect_all_names(tree), _collect_top_level_names(tree)


def _looks_like_class_qualifier(segment: str) -> bool:
    """`segment`がクラス名らしい命名規則（PEP8のCapWords）に従っているかを返す。

    Pythonの命名規則ではクラス名は`CapWords`、モジュール/パッケージ名は
    `lower_snake_case`が慣習（PEP8）。2セグメント以上の修飾シンボルが
    `Class.method`（クラス修飾）と`pkg.function`/`pkg.subpkg.function`
    （モジュールパス）のどちらの意図かを区別する材料が他に無いため、この
    慣習をヒューリスティックとして利用する。絶対的な保証ではないが、
    "無関係なクラスの同名メソッドに誤って一致する"リスクと"多段モジュール
    パスのトップレベル関数を見逃す"リスクの両方を抑える妥協点として採用
    している。`_PrivateClass`のようなPEP8の非公開クラス命名（先頭
    アンダースコア）もクラス名として認識できるよう、大文字判定の前に
    先頭のアンダースコアを取り除く。
    """
    return segment.lstrip("_")[:1].isupper()


def _symbol_matches(
    symbol: str, defined_names: set[str], top_level_names: set[str]
) -> bool:
    """`symbol`が`defined_names`のいずれかと一致するかを判定する。

    `docs/en/usage.md`・`skills/orchestune/SKILL.md`はいずれも`db.get_connection`
    や`foo.Foo`のような「(モジュール/サブシステム名).symbol」記法を例示して
    いる。この接頭辞はPythonの実際のimportパスとは限らない自由記述の
    ラベルであり、AST側では追跡していないため、以下の順で緩く照合する:

    1. 完全一致（`symbol in defined_names`）。
    2. 末尾2セグメント（`Class.method`部分）が`defined_names`にあるか。
       `pkg.Parser.parse`のように、モジュール/サブシステム名を頭に付けた
       うえで`Class.method`まで書く3セグメント以上の表記を許容するため。
    3. 末尾1セグメントが`top_level_names`（モジュール直下の関数・クラス・
       代入の名前。メソッドやネストしたローカル変数は含まない）にあるか。
       ただし、直前セグメント（`Class.method`の`Class`に相当する位置。
       2セグメントの`NewParser.parse`でも3セグメント以上の
       `pkg.NewParser.parse`でも同じ位置）がクラス名らしい命名
       （`_looks_like_class_qualifier`）の場合はこの段階を行わない —
       段階2の`Class.method`照合が外れた時点で「別のクラスの同名メソッド」
       という解釈しか残らず、ここで裸のleafへ緩めると無関係な同名
       トップレベル関数に誤って一致してしまうため。一方、
       `src.db.get_connection`のような多段モジュールパスでは直前セグメント
       `db`がクラス名らしくないため、この段階でトップレベル関数
       `get_connection`と正しく照合できる。
    """
    if symbol in defined_names:
        return True
    parts = symbol.split(".")
    if len(parts) >= 2 and ".".join(parts[-2:]) in defined_names:
        return True
    if len(parts) >= 2 and _looks_like_class_qualifier(parts[-2]):
        return False
    return len(parts) >= 2 and parts[-1] in top_level_names


_EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".rs": "rust",
    ".go": "go",
}

_UNSUPPORTED_SOURCE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".h",
        ".hpp",
        ".hxx",
        ".java",
        ".kt",
        ".kts",
        ".scala",
        ".cs",
        ".swift",
        ".rb",
        ".php",
        ".clj",
        ".cljs",
        ".dart",
        ".lua",
        ".zig",
        ".ex",
        ".exs",
        ".erl",
        ".hs",
    }
)


def _node_text(node: Any, content: bytes) -> str:
    return content[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


@functools.cache
def _get_tree_sitter_parser(language_name: str) -> Any | None:
    if not _TREE_SITTER_AVAILABLE:
        return None
    if language_name == "typescript":
        return tree_sitter.Parser(
            tree_sitter.Language(tree_sitter_typescript.language_typescript())
        )
    if language_name == "tsx":
        return tree_sitter.Parser(
            tree_sitter.Language(tree_sitter_typescript.language_tsx())
        )
    if language_name == "javascript":
        return tree_sitter.Parser(
            tree_sitter.Language(tree_sitter_javascript.language())
        )
    if language_name == "rust":
        return tree_sitter.Parser(tree_sitter.Language(tree_sitter_rust.language()))
    if language_name == "go":
        return tree_sitter.Parser(tree_sitter.Language(tree_sitter_go.language()))
    return None


def _extract_ts_js_export_specifiers(
    node: Any, content: bytes, defined: set[str], top: set[str]
) -> None:
    for child in node.children:
        if child.type == "export_clause":
            for spec in child.children:
                if spec.type == "export_specifier":
                    alias_node = spec.child_by_field_name(
                        "alias"
                    ) or spec.child_by_field_name("name")
                    if alias_node:
                        name = _node_text(alias_node, content)
                        defined.add(name)
                        top.add(name)


def _extract_ts_class(
    decl: Any, content: bytes, defined: set[str], top: set[str]
) -> None:
    name_node = decl.child_by_field_name("name")
    if not name_node:
        return
    class_name = _node_text(name_node, content)
    defined.add(class_name)
    top.add(class_name)
    body = decl.child_by_field_name("body")
    if not body:
        return
    for member in body.children:
        if member.type in (
            "method_definition",
            "field_definition",
            "public_field_definition",
            "abstract_method_signature",
        ):
            mname_node = member.child_by_field_name("name")
            if mname_node:
                mname = _node_text(mname_node, content)
                defined.add(mname)
                defined.add(f"{class_name}.{mname}")


def _extract_ts_type_or_enum(
    decl: Any, content: bytes, defined: set[str], top: set[str]
) -> None:
    name_node = decl.child_by_field_name("name")
    if not name_node:
        return
    tname = _node_text(name_node, content)
    defined.add(tname)
    top.add(tname)
    body = decl.child_by_field_name("body")
    if not body:
        return
    for member in body.children:
        if member.type in ("method_signature", "property_signature"):
            mname_node = member.child_by_field_name("name")
            if mname_node:
                mname = _node_text(mname_node, content)
                defined.add(mname)
                defined.add(f"{tname}.{mname}")
        elif member.type == "enum_assignment":
            mname_node = member.child_by_field_name("name")
            if mname_node:
                mname = _node_text(mname_node, content)
                defined.add(mname)
                defined.add(f"{tname}.{mname}")
        elif member.type == "property_identifier":
            mname = _node_text(member, content)
            defined.add(mname)
            defined.add(f"{tname}.{mname}")


def _extract_ts_variables(
    decl: Any, content: bytes, defined: set[str], top: set[str]
) -> None:
    for child in decl.children:
        if child.type == "variable_declarator":
            name_node = child.child_by_field_name("name")
            if name_node and name_node.type == "identifier":
                name = _node_text(name_node, content)
                defined.add(name)
                top.add(name)


def _extract_ts_js_declaration(
    decl: Any, content: bytes, defined: set[str], top: set[str]
) -> None:
    if decl.type == "export_statement":
        inner = decl.child_by_field_name("declaration")
        if inner:
            decl = inner
        else:
            _extract_ts_js_export_specifiers(decl, content, defined, top)
            return
    if decl.type in (
        "function_declaration",
        "generator_function_declaration",
        "function_signature",
    ):
        name_node = decl.child_by_field_name("name")
        if name_node:
            name = _node_text(name_node, content)
            defined.add(name)
            top.add(name)
    elif decl.type in ("class_declaration", "abstract_class_declaration"):
        _extract_ts_class(decl, content, defined, top)
    elif decl.type in (
        "interface_declaration",
        "type_alias_declaration",
        "enum_declaration",
    ):
        _extract_ts_type_or_enum(decl, content, defined, top)
    elif decl.type in ("lexical_declaration", "variable_declaration"):
        _extract_ts_variables(decl, content, defined, top)
    elif decl.type == "ambient_declaration":
        for child in decl.children:
            if child.type != "declare":
                _extract_ts_js_declaration(child, content, defined, top)
    elif decl.type in ("internal_module", "module"):
        _extract_ts_module(decl, content, defined, top)


def _extract_ts_module(
    decl: Any, content: bytes, defined: set[str], top: set[str]
) -> None:
    name_node = decl.child_by_field_name("name")
    if not name_node:
        return
    ns_name = _node_text(name_node, content)
    defined.add(ns_name)
    top.add(ns_name)
    body = decl.child_by_field_name("body")
    if not body:
        return
    for member in body.children:
        _extract_ts_js_declaration(member, content, defined, top)


def _collect_ts_js_names(tree: Any, content: bytes) -> tuple[set[str], set[str]]:
    defined_names: set[str] = set()
    top_level_names: set[str] = set()
    for child in tree.root_node.children:
        decl = child
        if child.type == "export_statement":
            inner = child.child_by_field_name("declaration")
            if inner:
                decl = inner
            else:
                _extract_ts_js_export_specifiers(
                    child, content, defined_names, top_level_names
                )
        elif (
            child.type == "expression_statement"
            and child.children
            and child.children[0].type == "internal_module"
        ):
            decl = child.children[0]
        _extract_ts_js_declaration(decl, content, defined_names, top_level_names)
    return defined_names, top_level_names


def _extract_rust_enum_or_trait(
    item: Any, content: bytes, defined: set[str], top: set[str]
) -> None:
    name_node = item.child_by_field_name("name")
    if not name_node:
        return
    tname = _node_text(name_node, content)
    defined.add(tname)
    top.add(tname)
    body = item.child_by_field_name("body")
    if not body:
        return
    for member in body.children:
        mname_node = member.child_by_field_name("name")
        if mname_node:
            mname = _node_text(mname_node, content)
            defined.add(mname)
            defined.add(f"{tname}.{mname}")


def _extract_rust_type_name(type_node: Any, content: bytes) -> str | None:
    if not type_node:
        return None
    if type_node.type == "generic_type":
        base = type_node.child_by_field_name("type")
        return _extract_rust_type_name(base, content)
    if type_node.type == "scoped_type_identifier":
        name = type_node.child_by_field_name("name")
        return _extract_rust_type_name(name, content)
    return _node_text(type_node, content)


def _extract_rust_impl(item: Any, content: bytes, defined: set[str]) -> None:
    type_node = item.child_by_field_name("type")
    type_name = _extract_rust_type_name(type_node, content)
    body = item.child_by_field_name("body")
    if not body:
        return
    for member in body.children:
        mname_node = member.child_by_field_name("name")
        if mname_node:
            mname = _node_text(mname_node, content)
            defined.add(mname)
            if type_name:
                defined.add(f"{type_name}.{mname}")


def _extract_rust_use_tree(
    node: Any, content: bytes, defined: set[str], top: set[str]
) -> None:
    if node.type == "identifier":
        name = _node_text(node, content)
        defined.add(name)
        top.add(name)
    elif node.type == "use_as_clause":
        alias = node.child_by_field_name("alias")
        if alias:
            name = _node_text(alias, content)
            defined.add(name)
            top.add(name)
    elif node.type == "scoped_identifier":
        name_node = node.child_by_field_name("name")
        if name_node:
            name = _node_text(name_node, content)
            defined.add(name)
            top.add(name)
    elif node.type in ("use_list", "scoped_use_list"):
        for child in node.children:
            if child.type not in ("::", "{", "}", ","):
                _extract_rust_use_tree(child, content, defined, top)


def _extract_rust_item(
    item: Any, content: bytes, defined: set[str], top: set[str]
) -> None:
    if item.type in (
        "function_item",
        "struct_item",
        "type_item",
        "const_item",
        "static_item",
        "mod_item",
    ):
        name_node = item.child_by_field_name("name")
        if name_node:
            name = _node_text(name_node, content)
            defined.add(name)
            top.add(name)
    elif item.type in ("enum_item", "trait_item"):
        _extract_rust_enum_or_trait(item, content, defined, top)
    elif item.type == "impl_item":
        _extract_rust_impl(item, content, defined)
    elif item.type == "use_declaration":
        if any(c.type == "visibility_modifier" for c in item.children):
            for child in item.children:
                if child.type in (
                    "scoped_identifier",
                    "identifier",
                    "use_as_clause",
                    "use_list",
                    "scoped_use_list",
                ):
                    _extract_rust_use_tree(child, content, defined, top)


def _collect_rust_names(tree: Any, content: bytes) -> tuple[set[str], set[str]]:
    defined_names: set[str] = set()
    top_level_names: set[str] = set()
    for child in tree.root_node.children:
        _extract_rust_item(child, content, defined_names, top_level_names)
    return defined_names, top_level_names


def _extract_go_base_type(node: Any, content: bytes) -> str | None:
    if not node:
        return None
    if node.type == "generic_type":
        base = node.child_by_field_name("type")
        return _extract_go_base_type(base, content)
    if node.type == "type_identifier":
        return _node_text(node, content)
    if node.type == "pointer_type":
        for child in node.children:
            if child.type != "*":
                return _extract_go_base_type(child, content)
    return None


def _extract_go_receiver_type(receiver: Any, content: bytes) -> str | None:
    for child in receiver.children:
        if child.type == "parameter_declaration":
            for sub in child.children:
                if sub.type in ("type_identifier", "pointer_type", "generic_type"):
                    return _extract_go_base_type(sub, content)
    return None


def _extract_go_method(item: Any, content: bytes, defined: set[str]) -> None:
    name_node = item.child_by_field_name("name")
    if not name_node:
        return
    mname = _node_text(name_node, content)
    defined.add(mname)
    receiver = item.child_by_field_name("receiver")
    if receiver:
        rec_type = _extract_go_receiver_type(receiver, content)
        if rec_type:
            defined.add(f"{rec_type}.{mname}")


def _extract_go_type_members(
    type_body: Any, tname: str, content: bytes, defined: set[str]
) -> None:
    if type_body.type == "interface_type":
        for member in type_body.children:
            if member.type in ("method_elem", "method_spec"):
                mname_node = member.child_by_field_name("name")
                if mname_node:
                    mname = _node_text(mname_node, content)
                    defined.add(mname)
                    defined.add(f"{tname}.{mname}")
    elif type_body.type == "struct_type":
        fields = type_body.child_by_field_name("fields")
        if fields:
            for field in fields.children:
                if field.type == "field_declaration":
                    fname_node = field.child_by_field_name("name")
                    if fname_node:
                        fname = _node_text(fname_node, content)
                        defined.add(fname)
                        defined.add(f"{tname}.{fname}")


def _extract_go_type(
    item: Any, content: bytes, defined: set[str], top: set[str]
) -> None:
    for spec in item.children:
        if spec.type in ("type_spec", "type_alias"):
            tname_node = spec.child_by_field_name("name")
            if not tname_node:
                continue
            tname = _node_text(tname_node, content)
            defined.add(tname)
            top.add(tname)
            type_body = spec.child_by_field_name("type")
            if type_body:
                _extract_go_type_members(type_body, tname, content, defined)


def _extract_go_vars(
    item: Any, content: bytes, defined: set[str], top: set[str]
) -> None:
    specs: list[Any] = []
    for child in item.children:
        if child.type in ("const_spec", "var_spec"):
            specs.append(child)
        elif child.type in ("const_spec_list", "var_spec_list"):
            for sub in child.children:
                if sub.type in ("const_spec", "var_spec"):
                    specs.append(sub)
    for spec in specs:
        for sub in spec.children:
            if sub.type == "identifier":
                name = _node_text(sub, content)
                defined.add(name)
                top.add(name)


def _extract_go_item(
    item: Any, content: bytes, defined: set[str], top: set[str]
) -> None:
    if item.type == "function_declaration":
        name_node = item.child_by_field_name("name")
        if name_node:
            name = _node_text(name_node, content)
            defined.add(name)
            top.add(name)
    elif item.type == "method_declaration":
        _extract_go_method(item, content, defined)
    elif item.type == "type_declaration":
        _extract_go_type(item, content, defined, top)
    elif item.type in ("const_declaration", "var_declaration"):
        _extract_go_vars(item, content, defined, top)


def _collect_go_names(tree: Any, content: bytes) -> tuple[set[str], set[str]]:
    defined_names: set[str] = set()
    top_level_names: set[str] = set()
    for child in tree.root_node.children:
        _extract_go_item(child, content, defined_names, top_level_names)
    return defined_names, top_level_names


def _parse_with_tree_sitter(
    path: Path, language_name: str
) -> tuple[set[str], set[str]] | None:
    parser = _get_tree_sitter_parser(language_name)
    if parser is None:
        return None
    try:
        content = path.read_bytes()
        content.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    try:
        tree = parser.parse(content)
        if tree.root_node.has_error:
            return None
        if language_name in ("typescript", "tsx", "javascript"):
            return _collect_ts_js_names(tree, content)
        if language_name == "rust":
            return _collect_rust_names(tree, content)
        if language_name == "go":
            return _collect_go_names(tree, content)
    except Exception as error:  # noqa: BLE001 - boundary: unsupported parser failures make symbol evidence unknown
        logger.warning("swallowed broad exception: %s", error)
        return None
    return None


def find_missing_footprint_paths(
    subtask: SubTask, repo_root: str | Path
) -> tuple[str, ...]:
    """`subtask.footprint`のうち、`repo_root`上に実在しないパスを返す。

    `find_missing_symbols`と異なり、検証材料の有無で判定を保留すること
    はしない: パスの実在は常にファイルシステムへの問い合わせだけで判定
    できるため、footprintが全て未作成のパスであっても、それがそのまま
    検出結果になる（新規ファイル作成を意図した記載は正当なので、呼び出し
    側ではエラーではなく警告として扱うこと）。
    """
    repo_root = Path(repo_root)
    return tuple(path for path in subtask.footprint if not (repo_root / path).is_file())


def find_missing_symbols(subtask: SubTask, repo_root: str | Path) -> tuple[str, ...]:
    """`subtask.symbols`のうち、`subtask.footprint`のソースコードファイル群に
    実在しないものを返す。

    Python（`.py`）、TypeScript / JavaScript（`.ts`, `.tsx`, `.js`等）、
    Rust（`.rs`）、Go（`.go`）の構文解析をサポートする。

    footprintに実在する検査対象ファイルが1つも無い場合（新規作成予定の
    footprintのみのsubtask等）は検証材料が無いため空タプルを返す —
    「存在しない」と機械的に断定してfalse positiveを出すよりは、
    判定を保留する方が安全なため。footprint中の一部のファイルだけが
    パース不能（構文エラー等）だった場合、あるいは未対応言語のソースファイル
    が含まれる場合も同様に空タプルを返す: パースできたファイルだけを基準に
    判定すると、他のファイル側で定義されていたはずのシンボルまで
    「見つからない」と誤検出してしまう（レビュー指摘 #372, #1543）。

    **既存ファイルへの新規追加との区別はしない**: `docs/en/usage.md`が
    `symbols`を「このsubtaskが定義または変更するシンボル」と定義している
    通り、footprintファイルが既に存在していても、シンボル自体はこの
    subtaskで初めて追加されるだけかもしれない。その場合も「未検出」として
    同じ結果を返す。呼び出し側（`provisioning.py`）は、これを「リファクタ
    による陳腐化」と断定する注記ではなく「見つからなかったので着手前に
    確認してほしい」という中立な注記として提示する。
    """
    if not subtask.symbols:
        return ()

    repo_root = Path(repo_root)
    defined_names: set[str] = set()
    top_level_names: set[str] = set()
    any_file_checked = False
    any_file_unparseable = False
    any_file_unsupported = False

    for relative_path in subtask.footprint:
        path = repo_root / relative_path
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext in _EXTENSION_TO_LANGUAGE:
            lang = _EXTENSION_TO_LANGUAGE[ext]
            if lang == "python":
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8"))
                except (SyntaxError, UnicodeDecodeError):
                    any_file_unparseable = True
                    continue
                file_names, file_top_level_names = _collect_defined_names(tree)
            else:
                names = _parse_with_tree_sitter(path, lang)
                if names is None:
                    any_file_unparseable = True
                    continue
                file_names, file_top_level_names = names
            any_file_checked = True
            defined_names |= file_names
            top_level_names |= file_top_level_names
        elif ext in _UNSUPPORTED_SOURCE_EXTENSIONS:
            any_file_unsupported = True
        else:
            continue

    if not any_file_checked or any_file_unparseable or any_file_unsupported:
        return ()

    return tuple(
        symbol
        for symbol in subtask.symbols
        if not _symbol_matches(symbol, defined_names, top_level_names)
    )

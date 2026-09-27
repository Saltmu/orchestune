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
from pathlib import Path

from orchestune.dag.models import SubTask


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
    """`subtask.symbols`のうち、`subtask.footprint`のPythonファイル群に
    実在しないものを返す。

    footprintに実在する`.py`ファイルが1つも無い場合（新規作成予定の
    footprintのみのsubtask等）は検証材料が無いため空タプルを返す —
    「存在しない」と機械的に断定してfalse positiveを出すよりは、
    判定を保留する方が安全なため。footprint中の一部のファイルだけが
    パース不能（構文エラー等）だった場合も同様に空タプルを返す:
    パースできたファイルだけを基準に判定すると、パースできなかった
    ファイル側で定義されていたはずのシンボルまで「見つからない」と
    誤検出してしまう（レビュー指摘 #372）。

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

    for relative_path in subtask.footprint:
        if not relative_path.endswith(".py"):
            continue
        path = repo_root / relative_path
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            any_file_unparseable = True
            continue
        any_file_checked = True
        file_names, file_top_level_names = _collect_defined_names(tree)
        defined_names |= file_names
        top_level_names |= file_top_level_names

    if not any_file_checked or any_file_unparseable:
        return ()

    return tuple(
        symbol
        for symbol in subtask.symbols
        if not _symbol_matches(symbol, defined_names, top_level_names)
    )

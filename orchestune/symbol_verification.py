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

from orchestune.dag_models import SubTask


def _module_scope_statements(statements: list[ast.stmt]) -> list[ast.stmt]:
    """`statements`（モジュール直下のstatement列）を、関数・クラスの境界では
    止まりつつ`if`/`try`/`with`/`for`/`while`の内側までは平坦化して返す。

    Pythonでは`if`/`try`/`with`/ループの中で書かれた代入・def・classも
    モジュールスコープに束縛される（`try: import X as Y except: import Z as Y`
    のような条件付き定義は典型例）。単純に`tree.body`の直接の子だけを見ると、
    こうした複合文の中身を見落とす（レビュー指摘 #372）。関数・クラス定義は
    それ自体で新しいスコープを作るため、その中へは再帰しない。
    """
    flattened: list[ast.stmt] = []
    for stmt in statements:
        flattened.append(stmt)
        if isinstance(stmt, ast.If):
            flattened.extend(_module_scope_statements(stmt.body))
            flattened.extend(_module_scope_statements(stmt.orelse))
        elif isinstance(stmt, ast.Try):
            flattened.extend(_module_scope_statements(stmt.body))
            for handler in stmt.handlers:
                flattened.extend(_module_scope_statements(handler.body))
            flattened.extend(_module_scope_statements(stmt.orelse))
            flattened.extend(_module_scope_statements(stmt.finalbody))
        elif isinstance(stmt, ast.With | ast.AsyncWith):
            flattened.extend(_module_scope_statements(stmt.body))
        elif isinstance(stmt, ast.For | ast.AsyncFor | ast.While):
            flattened.extend(_module_scope_statements(stmt.body))
            flattened.extend(_module_scope_statements(stmt.orelse))
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
            for child in node.body:
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    names.add(child.name)
                    names.add(f"{node.name}.{child.name}")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
    names |= _collect_top_level_names(tree)
    return names


def _collect_top_level_names(tree: ast.Module) -> set[str]:
    """`_symbol_matches`が「モジュール修飾記法」（`db.get_connection`等）を
    末尾セグメントだけで緩く照合する際の候補集合を返す。

    `ast.walk`は木全体をフラットに走査してしまいスコープ情報を失うため、
    これだけは`_module_scope_statements(tree.body)`（モジュールスコープの
    statementのみ、`if`/`try`/`with`/ループの中身まで含む）を走査する:
    レビュー指摘 #372で複数件見つかった通り、`ast.walk`ベースの判定は
    クラスメソッドの裸名や関数・メソッドのローカル変数をモジュールレベルの
    定義と取り違える。
    """
    top_level_names: set[str] = set()
    for node in _module_scope_statements(tree.body):
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            top_level_names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    top_level_names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            top_level_names.add(node.target.id)
    return top_level_names


def _collect_defined_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """モジュール内で定義されている識別子集合を`(全識別子, トップレベル識別子)`で返す。"""
    return _collect_all_names(tree), _collect_top_level_names(tree)


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
       うえで`Class.method`まで書く3セグメント表記を許容するため。
    3. 末尾1セグメントが`top_level_names`（モジュール直下の関数・クラス・
       代入の名前。メソッドやネストしたローカル変数は含まない）にあるか。

    メソッド名はクラスをまたいで重複しうるため、末尾1セグメントだけの
    緩い照合を`top_level_names`（メソッドを含まない）に限定している。
    これにより、`NewParser.parse`のような修飾シンボルが、無関係な
    `OldParser.parse`の裸名`parse`だけで「見つかった」ことにはならない。
    """
    if symbol in defined_names:
        return True
    parts = symbol.split(".")
    if len(parts) >= 2 and ".".join(parts[-2:]) in defined_names:
        return True
    return parts[-1] in top_level_names


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

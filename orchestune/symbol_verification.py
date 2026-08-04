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


def _collect_all_names(tree: ast.Module) -> set[str]:
    """`symbol`との完全一致判定に使う識別子集合を返す（裸の関数名・クラス名・
    `ClassName.method`限定名・トップレベル代入名 + メソッドの裸名）。

    ネストした関数定義（クロージャのヘルパ等）も対象に含めるため、
    `ast.walk`で全ノードを走査する。
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
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _collect_top_level_names(tree: ast.Module) -> set[str]:
    """`_symbol_matches`が「モジュール修飾記法」（`db.get_connection`等）を
    末尾セグメントだけで緩く照合する際の候補集合を返す。

    `ast.walk`は木全体をフラットに走査してしまいスコープ情報を失うため、
    これだけは`tree.body`（モジュール直下のstatementのみ）を走査する:
    レビュー指摘 #372で2件見つかった通り、`ast.walk`ベースの判定は
    (1) クラスメソッドの裸名を関数と取り違える、(2) 関数・メソッドの
    ローカル変数をモジュールレベルの代入と取り違える、という2種類の
    スコープ混同を引き起こす。`tree.body`はモジュール直下のstatementのみを
    列挙するため、そのいずれのクラスの混同も起こらない。
    """
    top_level_names: set[str] = set()
    for node in tree.body:
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
    判定を保留する方が安全なため。

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

    for relative_path in subtask.footprint:
        if not relative_path.endswith(".py"):
            continue
        path = repo_root / relative_path
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        any_file_checked = True
        file_names, file_top_level_names = _collect_defined_names(tree)
        defined_names |= file_names
        top_level_names |= file_top_level_names

    if not any_file_checked:
        return ()

    return tuple(
        symbol
        for symbol in subtask.symbols
        if not _symbol_matches(symbol, defined_names, top_level_names)
    )

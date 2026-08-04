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


def _collect_defined_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    """モジュール内で定義されている識別子集合を`(全識別子, トップレベル識別子)`で返す。

    「全識別子」は`symbol`との完全一致判定に使う（裸の関数名・クラス名・
    `ClassName.method`限定名・トップレベル代入名 + メソッドの裸名）。
    ネストした関数定義（クロージャのヘルパ等）も対象に含めるため、
    `ast.walk`で全ノードを走査する。

    「トップレベル識別子」は、クラスのメソッドを除いた集合（関数・クラス・
    トップレベル代入の名前のみ）。`_symbol_matches`が「モジュール修飾記法」
    （`db.get_connection`等）を末尾セグメントだけで緩く照合する際の候補を
    これに限定する。メソッド名はクラスをまたいで重複しうるため、全識別子
    セットをそのまま候補にすると、無関係なクラスの同名メソッドの存在有無で
    判定が揺れてしまう（レビュー指摘 #372 二段階）:
    1回目の修正でメソッド名を「ブロックリスト」として使ったところ、今度は
    正しいトップレベル関数と同名のメソッドが別クラスにたまたま存在するだけで
    誤ってfalse positiveになる逆方向の不具合を生んだため、「メソッド名を除外
    する」のではなく「トップレベル識別子のみを候補にする」ポジティブリスト
    方式に変更した。
    """
    names: set[str] = set()
    top_level_names: set[str] = set()
    # `ast.walk` yields every node in the tree flat, including each method's
    # own FunctionDef — so a method node would independently satisfy the
    # bare `isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)` check
    # below unless excluded by identity first. `tree` keeps every node in
    # `method_node_ids` alive for the remainder of this call, so reusing
    # `id()` as a stand-in for object identity is safe here.
    method_node_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            names.add(node.name)
            top_level_names.add(node.name)
            for child in node.body:
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    names.add(child.name)
                    names.add(f"{node.name}.{child.name}")
                    method_node_ids.add(id(child))

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if id(node) in method_node_ids:
                continue
            names.add(node.name)
            top_level_names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                    top_level_names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
            top_level_names.add(node.target.id)
    return names, top_level_names


def _symbol_matches(
    symbol: str, defined_names: set[str], top_level_names: set[str]
) -> bool:
    """`symbol`が`defined_names`のいずれかと一致するかを判定する。

    `docs/en/usage.md`・`skills/orchestune/SKILL.md`はいずれも`db.get_connection`
    や`foo.Foo`のような「(モジュール/サブシステム名).symbol」記法を例示して
    いる。この接頭辞はPythonの実際のimportパスとは限らない自由記述の
    ラベルであり、AST側では追跡していないため、完全一致に加えて最後の
    ドット区切りセグメントを`top_level_names`（クラスのメソッドを除いた
    関数・クラス・トップレベル代入の名前）と照合する。

    メソッド名（クラス直下の関数）は`top_level_names`に含めていないため、
    `NewParser.parse`のような修飾シンボルが、無関係な`OldParser.parse`の
    裸名`parse`だけで「見つかった」ことにはならない — メソッドを指す
    修飾シンボルは完全一致（`symbol in defined_names`）でのみ検出される。
    """
    if symbol in defined_names:
        return True
    leaf = symbol.rsplit(".", 1)[-1]
    return leaf in top_level_names


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

"""#359: `Footprint.symbols`が実際のコードベースに実在するかを検証する。

リファクタ（ファイル分割・関数移動・リネーム）を経たdecomposition planでは、
`symbols`に記載された対象が既に存在しないコードスナップショットを指して
いることがある。Issue生成時にこれを検出し、本文へ注記を残せるようにする
（`provisioning.py`から呼ばれる）。
"""

from __future__ import annotations

import ast
from pathlib import Path

from orchestune.dag_models import SubTask


def _collect_defined_names(tree: ast.AST) -> set[str]:
    """モジュール内で定義されている識別子集合を返す（裸名 + `Class.method`）。

    ネストした関数定義（クロージャのヘルパ等）も対象に含めるため、
    `ast.walk`で全ノードを走査する。関数・クラスの深さそのものは区別せず、
    クラス直下のメソッドのみ`ClassName.method`の限定名も追加する
    （`symbols`が実例として両方の書式で書かれているため）。
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


def find_missing_symbols(subtask: SubTask, repo_root: str | Path) -> tuple[str, ...]:
    """`subtask.symbols`のうち、`subtask.footprint`のPythonファイル群に
    実在しないものを返す。

    footprintに実在する`.py`ファイルが1つも無い場合（新規作成予定の
    footprintのみのsubtask等）は検証材料が無いため空タプルを返す —
    「存在しない」と機械的に断定してfalse positiveを出すよりは、
    判定を保留する方が安全なため。
    """
    if not subtask.symbols:
        return ()

    repo_root = Path(repo_root)
    defined_names: set[str] = set()
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
        defined_names |= _collect_defined_names(tree)

    if not any_file_checked:
        return ()

    return tuple(symbol for symbol in subtask.symbols if symbol not in defined_names)

"""#359: Footprint.symbolsが実コードに実在するかの検証（symbol_verification.py）。"""

from pathlib import Path

from orchestune.dag_models import SubTask
from orchestune.symbol_verification import find_missing_symbols


def _subtask(footprint: tuple[str, ...] = (), symbols: tuple[str, ...] = ()) -> SubTask:
    return SubTask(
        id="task-a",
        description="",
        footprint=footprint,
        symbols=symbols,
        depends_on=(),
        risk=False,
        risk_reasons=(),
    )


def _write(repo_root: Path, relative_path: str, content: str) -> None:
    path = repo_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestFindMissingSymbols:
    def test_bare_function_name_found(self, tmp_path):
        _write(tmp_path, "pkg/mod.py", "def format_dispatch_report():\n    pass\n")
        subtask = _subtask(
            footprint=("pkg/mod.py",), symbols=("format_dispatch_report",)
        )

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_bare_class_name_found(self, tmp_path):
        _write(tmp_path, "pkg/mod.py", "class Foo:\n    pass\n")
        subtask = _subtask(footprint=("pkg/mod.py",), symbols=("Foo",))

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_class_method_qualified_name_found(self, tmp_path):
        _write(
            tmp_path,
            "pkg/mod.py",
            "class IntegrationMerger:\n"
            "    def merge_and_test_tasks(self):\n"
            "        pass\n",
        )
        subtask = _subtask(
            footprint=("pkg/mod.py",),
            symbols=("IntegrationMerger.merge_and_test_tasks",),
        )

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_module_qualified_name_matches_via_leaf_segment(self, tmp_path):
        """docs/en/usage.md・skills/orchestune/SKILL.mdは`db.get_connection`
        のような「(モジュール/サブシステム名).symbol」記法を例示している。
        この接頭辞はASTでは追跡しないPython外の自由記述ラベルのため、
        末尾セグメントでの一致も許容する（レビュー指摘 #372）。"""
        _write(tmp_path, "src/db/connection.py", "def get_connection():\n    pass\n")
        subtask = _subtask(
            footprint=("src/db/connection.py",), symbols=("db.get_connection",)
        )

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_renamed_class_qualified_symbol_is_not_matched_via_unrelated_leaf(
        self, tmp_path
    ):
        """レビュー指摘 #372: `NewParser.parse`のようなクラス修飾シンボルを、
        無関係な`OldParser.parse`が持つ裸のメソッド名`parse`だけで
        「見つかった」ことにしてはならない。メソッド名はクラスをまたいで
        重複しうるため、修飾シンボルは完全一致でのみ検出されるべき。"""
        _write(
            tmp_path,
            "pkg/mod.py",
            "class OldParser:\n    def parse(self):\n        pass\n",
        )
        subtask = _subtask(footprint=("pkg/mod.py",), symbols=("NewParser.parse",))

        assert find_missing_symbols(subtask, tmp_path) == ("NewParser.parse",)

    def test_module_qualified_symbol_not_poisoned_by_unrelated_same_named_method(
        self, tmp_path
    ):
        """レビュー指摘 #372（2巡目）: `db.get_connection`が正しいトップレベル
        関数`get_connection`を指していても、無関係な別クラスがたまたま同名の
        メソッド`get_connection`を持っているだけでfalse positiveになっては
        ならない（メソッド名をブロックリストとして使う実装だと壊れる）。"""
        _write(
            tmp_path,
            "src/db/connection.py",
            "def get_connection():\n    pass\n\n\n"
            "class ConnectionPool:\n"
            "    def get_connection(self):\n"
            "        pass\n",
        )
        subtask = _subtask(
            footprint=("src/db/connection.py",), symbols=("db.get_connection",)
        )

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_module_qualified_class_method_symbol_matches_via_trailing_pair(
        self, tmp_path
    ):
        """レビュー指摘 #372（3巡目）: `pkg.Parser.parse`のように、モジュール/
        サブシステム名を頭に付けたうえで`Class.method`まで書く3セグメント
        表記も、末尾2セグメント（`Parser.parse`）が実在すれば検出できる
        べき。裸のメソッド名`parse`だけの緩い一致に頼らないため、無関係な
        クラスの同名メソッドとは引き続き混同しない。"""
        _write(
            tmp_path,
            "pkg/mod.py",
            "class Parser:\n    def parse(self):\n        pass\n",
        )
        subtask = _subtask(footprint=("pkg/mod.py",), symbols=("pkg.Parser.parse",))

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_stale_class_method_symbol_not_matched_via_unrelated_top_level_function(
        self, tmp_path
    ):
        """レビュー指摘 #372（5巡目）: `pkg.NewParser.parse`のような3セグメント
        修飾シンボルは、末尾2セグメント（`NewParser.parse`）の照合で明確に
        `Class.method`の意図と分かるため、それが外れた時点で無関係な
        トップレベル関数`parse`への裸leaf一致にフォールバックしてはならない。"""
        _write(
            tmp_path,
            "pkg/mod.py",
            "def parse():\n    pass\n",
        )
        subtask = _subtask(footprint=("pkg/mod.py",), symbols=("pkg.NewParser.parse",))

        assert find_missing_symbols(subtask, tmp_path) == ("pkg.NewParser.parse",)

    def test_class_method_under_class_level_if_still_matches(self, tmp_path):
        """レビュー指摘 #372（5巡目）: `class Parser: if FEATURE: def parse():
        ...`のようにクラス直下の`if`配下で条件付きに定義されたメソッドも、
        `Class.method`限定名として検出できるべき。"""
        _write(
            tmp_path,
            "pkg/mod.py",
            "FEATURE = True\n\n\nclass Parser:\n    if FEATURE:\n        def parse(self):\n            pass\n",
        )
        subtask = _subtask(footprint=("pkg/mod.py",), symbols=("Parser.parse",))

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_module_scope_assignment_under_match_case_still_matches(self, tmp_path):
        """レビュー指摘 #372（5巡目）: モジュール直下の`match`文のcase内で
        定義された名前も、モジュールスコープの束縛として扱われるべき。"""
        _write(
            tmp_path,
            "pkg/mod.py",
            "import sys\n\nmatch sys.platform:\n    case _:\n        enabled = True\n",
        )
        subtask = _subtask(footprint=("pkg/mod.py",), symbols=("pkg.enabled",))

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_local_variable_inside_function_does_not_satisfy_module_qualified_symbol(
        self, tmp_path
    ):
        """レビュー指摘 #372（3巡目）: 関数・メソッド内のローカル代入
        （例: `def helper(): get_connection = ...`）は、モジュール直下の
        代入ではないため、`db.get_connection`のようなモジュール修飾記法の
        緩い一致の根拠にしてはならない。"""
        _write(
            tmp_path,
            "src/db/connection.py",
            "def helper():\n    get_connection = lambda: None\n    return get_connection\n",
        )
        subtask = _subtask(
            footprint=("src/db/connection.py",), symbols=("db.get_connection",)
        )

        assert find_missing_symbols(subtask, tmp_path) == ("db.get_connection",)

    def test_local_variable_inside_function_does_not_satisfy_bare_symbol(
        self, tmp_path
    ):
        """レビュー指摘 #372（4巡目）: バレ（ドット無し）のシンボル判定でも、
        関数・メソッド内のローカル代入を「定義済み」として拾ってはならない。"""
        _write(
            tmp_path,
            "src/db/connection.py",
            "def helper():\n    get_connection = lambda: None\n    return get_connection\n",
        )
        subtask = _subtask(
            footprint=("src/db/connection.py",), symbols=("get_connection",)
        )

        assert find_missing_symbols(subtask, tmp_path) == ("get_connection",)

    def test_module_scope_assignment_under_if_still_matches(self, tmp_path):
        """レビュー指摘 #372（4巡目）: `if`/`try`/`with`等の中で書かれた
        モジュールスコープの代入・def・classも、それ自体は新しいスコープを
        作らないため、緩い一致の候補に含める必要がある
        （例: `try: import X as impl except ImportError: import Y as impl`）。"""
        _write(
            tmp_path,
            "pkg/mod.py",
            "import sys\n\nif sys.version_info >= (3, 12):\n    enabled = True\nelse:\n    enabled = False\n",
        )
        subtask = _subtask(footprint=("pkg/mod.py",), symbols=("mod.enabled",))

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_module_scope_function_under_try_still_matches(self, tmp_path):
        """`try`ブロック内で条件付きに定義された関数も、モジュールスコープの
        定義として扱われるべき。"""
        _write(
            tmp_path,
            "pkg/mod.py",
            "try:\n    def load_config():\n        pass\nexcept ImportError:\n    def load_config():\n        pass\n",
        )
        subtask = _subtask(footprint=("pkg/mod.py",), symbols=("pkg.load_config",))

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_unparseable_sibling_footprint_file_defers_the_whole_subtask(
        self, tmp_path
    ):
        """レビュー指摘 #372（4巡目）: footprintの一部だけがパース不能な
        場合、パースできたファイルだけを基準に「見つからない」と判定して
        はならない — パースできなかったファイル側にそのシンボルが定義
        されている可能性を排除できないため、判定全体を保留する。"""
        _write(tmp_path, "pkg/good.py", "def unrelated():\n    pass\n")
        _write(tmp_path, "pkg/broken.py", "def this is not valid python(\n")
        subtask = _subtask(
            footprint=("pkg/good.py", "pkg/broken.py"),
            symbols=("defined_only_in_broken_py",),
        )

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_module_qualified_class_symbol_still_matches_via_leaf(self, tmp_path):
        """クラス名自体を指す修飾シンボル（`pkg.Foo`のような表記）は、
        クラス名がメソッド名のように重複しうる曖昧さを持たないため、
        引き続き末尾セグメントでの緩い一致を許容する。"""
        _write(tmp_path, "pkg/mod.py", "class Foo:\n    pass\n")
        subtask = _subtask(footprint=("pkg/mod.py",), symbols=("pkg.Foo",))

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_bare_method_name_found_without_class_qualification(self, tmp_path):
        _write(
            tmp_path,
            "pkg/mod.py",
            "class IntegrationMerger:\n"
            "    def merge_and_test_tasks(self):\n"
            "        pass\n",
        )
        subtask = _subtask(footprint=("pkg/mod.py",), symbols=("merge_and_test_tasks",))

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_top_level_constant_found(self, tmp_path):
        _write(tmp_path, "pkg/mod.py", "DEFAULT_TIMEOUT = 30\n")
        subtask = _subtask(footprint=("pkg/mod.py",), symbols=("DEFAULT_TIMEOUT",))

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_missing_symbol_is_reported(self, tmp_path):
        _write(tmp_path, "pkg/mod.py", "def write_github_step_summary():\n    pass\n")
        subtask = _subtask(
            footprint=("pkg/mod.py",), symbols=("format_dispatch_report",)
        )

        assert find_missing_symbols(subtask, tmp_path) == ("format_dispatch_report",)

    def test_symbols_aggregated_across_multiple_footprint_files(self, tmp_path):
        _write(tmp_path, "pkg/a.py", "def foo():\n    pass\n")
        _write(tmp_path, "pkg/b.py", "def bar():\n    pass\n")
        subtask = _subtask(
            footprint=("pkg/a.py", "pkg/b.py"), symbols=("foo", "bar", "missing")
        )

        assert find_missing_symbols(subtask, tmp_path) == ("missing",)

    def test_nonexistent_footprint_file_is_skipped_without_crashing(self, tmp_path):
        _write(tmp_path, "pkg/a.py", "def foo():\n    pass\n")
        subtask = _subtask(
            footprint=("pkg/a.py", "pkg/does_not_exist.py"), symbols=("foo",)
        )

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_all_footprint_files_missing_returns_empty_to_avoid_false_positive(
        self, tmp_path
    ):
        subtask = _subtask(
            footprint=("pkg/not_yet_created.py",), symbols=("brand_new_function",)
        )

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_no_footprint_returns_empty(self, tmp_path):
        subtask = _subtask(footprint=(), symbols=("anything",))

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_non_python_footprint_files_are_ignored(self, tmp_path):
        _write(tmp_path, "docs/readme.md", "# format_dispatch_report\n")
        subtask = _subtask(
            footprint=("docs/readme.md",), symbols=("format_dispatch_report",)
        )

        # マークダウン中に文字列として出現していても、Pythonの識別子として
        # 定義されているわけではないため検証材料にならず、falls back to empty.
        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_no_symbols_returns_empty(self, tmp_path):
        _write(tmp_path, "pkg/mod.py", "def foo():\n    pass\n")
        subtask = _subtask(footprint=("pkg/mod.py",), symbols=())

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_nested_function_definition_is_found(self, tmp_path):
        _write(
            tmp_path,
            "pkg/mod.py",
            "def outer():\n    def inner_helper():\n        pass\n    return inner_helper\n",
        )
        subtask = _subtask(footprint=("pkg/mod.py",), symbols=("inner_helper",))

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_unparseable_python_file_is_skipped_without_crashing(self, tmp_path):
        _write(tmp_path, "pkg/broken.py", "def this is not valid python(\n")
        subtask = _subtask(footprint=("pkg/broken.py",), symbols=("anything",))

        assert find_missing_symbols(subtask, tmp_path) == ()

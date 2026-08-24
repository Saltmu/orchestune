"""#272: Usageドキュメント（日英）と実装の乖離を機械的に検知するテスト。

`decomposition_plan.md` のスキーマ、`orchestune-dispatch` のCLIオプション、
統合パイプラインの説明が、実装からドリフトしていないことを検証する。
"""

import inspect
import pathlib
import re
import tomllib

import pytest

from orchestune.dag.cli import _build_parser as _build_dag_arg_parser
from orchestune.dag.contracts import _SHARED_CONTRACT_PATTERNS
from orchestune.dag.models import DAG_TOOL_CONFIG_KEYS
from orchestune.dag.parsing import _parse_subtask
from orchestune.dispatch.dispatcher import _build_arg_parser
from orchestune.provisioning.cli import _build_arg_parser as _build_provision_arg_parser

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
USAGE_DOCS = {
    "ja": REPO_ROOT / "docs" / "ja" / "usage.md",
    "en": REPO_ROOT / "docs" / "en" / "usage.md",
}
_REQUIRED_MARKERS = {"ja": "必須", "en": "required"}
_OPTIONAL_MARKERS = {"ja": "任意", "en": "optional"}

_OPTION_ROW_PATTERN = re.compile(
    r"^\|\s*`(--[a-z-]+)[^`]*`(?:\s*/\s*`--[a-z-]+`)?\s*\|\s*([^|]*?)\s*\|"
)
_SCHEMA_BULLET_PATTERN = re.compile(r"^\*\s+\*\*`([a-z_]+)`\*\*\s*\(([^)]*)\)")


def _read_usage(lang: str) -> str:
    return USAGE_DOCS[lang].read_text(encoding="utf-8")


def _section(lang: str, heading_number: int) -> str:
    """Usageの`## <heading_number>.`見出し節（次の`## `見出しの直前まで）を切り出す。"""
    text = _read_usage(lang)
    pattern = rf"^## {heading_number}\..*?(?=^## |\Z)"
    match = re.search(pattern, text, re.MULTILINE | re.DOTALL)
    assert match, f"{lang}のUsageに見出し（## {heading_number}.）が見つかりません"
    return match.group(0)


def _documented_options_in(section_text: str) -> dict[str, str | None]:
    """Usageのオプション表から {オプション名: 記載デフォルト値} を抽出する。

    デフォルト値がバッククォートで囲まれた単一トークンでない行（`-`や
    「自動選択」のような散文）は、機械比較の対象外として ``None`` を返す。
    """
    options: dict[str, str | None] = {}
    for line in section_text.splitlines():
        match = _OPTION_ROW_PATTERN.match(line)
        if not match:
            continue
        option, raw_default = match.group(1), match.group(2).strip()
        default_match = re.fullmatch(r"`([^`]+)`", raw_default)
        documented = default_match.group(1) if default_match else None
        if documented is not None and documented.startswith("--"):
            documented = None
        options[option] = documented
    return options


def _documented_options(lang: str) -> dict[str, str | None]:
    """`orchestune-dispatch`（## 4.）節のオプション表を抽出する。"""
    return _documented_options_in(_section(lang, 4))


def _documented_provision_options(lang: str) -> dict[str, str | None]:
    """`orchestune provision`（## 2.）節のオプション表を抽出する。"""
    return _documented_options_in(_section(lang, 2))


def _documented_dag_options(lang: str) -> dict[str, str | None]:
    """`orchestune-dag`（## 3.）節のオプション表を抽出する。"""
    return _documented_options_in(_section(lang, 3))


def _documented_schema_fields(lang: str) -> dict[str, str]:
    """Usageのスキーマ節から {フィールド名: 必須/任意の記載} を抽出する。"""
    fields: dict[str, str] = {}
    for line in _read_usage(lang).splitlines():
        match = _SCHEMA_BULLET_PATTERN.match(line.strip())
        if not match:
            continue
        qualifiers = [part.strip() for part in match.group(2).split(",")]
        if _REQUIRED_MARKERS[lang] in qualifiers:
            fields[match.group(1)] = "required"
        elif _OPTIONAL_MARKERS[lang] in qualifiers:
            fields[match.group(1)] = "optional"
    return fields


def _integration_section(lang: str) -> str:
    """Usageの「5. 統合」節（次の`## `見出しの直前まで）を切り出す。"""
    return _section(lang, 5)


def _schema_bullet_block(lang: str, field: str) -> str:
    """スキーマ節の1フィールド分の記述（箇条書き本体＋そのインデント配下）を返す。"""
    lines = _read_usage(lang).splitlines()
    start = next(
        index for index, line in enumerate(lines) if line.startswith(f"* **`{field}`**")
    )
    block = [lines[start]]
    for line in lines[start + 1 :]:
        if line.startswith("* ") or line.startswith("#"):
            break
        block.append(line)
    return "\n".join(block)


def _supported_plan_fields() -> set[str]:
    """`_parse_subtask` のソースから、実際に参照される計画フィールド名を抽出する。"""
    source = inspect.getsource(_parse_subtask)
    fields = set(re.findall(r"""raw\.get\(\s*["']([a-z_]+)["']""", source))
    fields |= set(re.findall(r"""raw\[\s*["']([a-z_]+)["']\s*\]""", source))
    return fields


class TestDocsCliConsistency:
    """#272: Usageに記載されたCLIオプションと実装の乖離を機械的に検知する。"""

    @pytest.mark.parametrize("lang", sorted(USAGE_DOCS))
    def test_documented_defaults_match_parser(self, lang):
        parser_defaults = vars(_build_arg_parser().parse_args([]))
        documented = _documented_options(lang)
        assert documented, f"{lang}のUsageからオプション表を抽出できませんでした"

        for option, documented_default in documented.items():
            dest = option.removeprefix("--").replace("-", "_")
            assert (
                dest in parser_defaults
            ), f"{lang}のUsageに記載された{option}は実装に存在しません"
            if documented_default is None:
                continue
            assert documented_default == str(parser_defaults[dest]), (
                f"{lang}のUsageの{option}のデフォルト値が実装と一致しません: "
                f"docs={documented_default} / implementation={parser_defaults[dest]}"
            )

    def test_ja_and_en_document_the_same_options(self):
        assert _documented_options("ja").keys() == _documented_options("en").keys()

    def test_ja_and_en_document_the_same_defaults(self):
        ja, en = _documented_options("ja"), _documented_options("en")
        assert {k: v for k, v in ja.items() if v is not None} == {
            k: v for k, v in en.items() if v is not None
        }


class TestDocsProvisionCliConsistency:
    """#306: Usageに記載された`orchestune provision`のオプションと実装の乖離を検知する。"""

    @pytest.mark.parametrize("lang", sorted(USAGE_DOCS))
    def test_documented_defaults_match_parser(self, lang):
        parser_defaults = vars(_build_provision_arg_parser().parse_args([]))
        documented = _documented_provision_options(lang)
        assert (
            documented
        ), f"{lang}のUsageからprovisionのオプション表を抽出できませんでした"

        for option, documented_default in documented.items():
            dest = option.removeprefix("--").replace("-", "_")
            assert (
                dest in parser_defaults
            ), f"{lang}のUsageに記載された{option}は実装に存在しません"
            if documented_default is None:
                continue
            assert documented_default == str(parser_defaults[dest]), (
                f"{lang}のUsageの{option}のデフォルト値が実装と一致しません: "
                f"docs={documented_default} / implementation={parser_defaults[dest]}"
            )

    def test_ja_and_en_document_the_same_options(self):
        assert (
            _documented_provision_options("ja").keys()
            == _documented_provision_options("en").keys()
        )

    def test_ja_and_en_document_the_same_defaults(self):
        ja, en = (
            _documented_provision_options("ja"),
            _documented_provision_options("en"),
        )
        assert {k: v for k, v in ja.items() if v is not None} == {
            k: v for k, v in en.items() if v is not None
        }


class TestDagDocsCliConsistency:
    """#411: Usageに記載された`orchestune-dag`（## 3.）のオプションと
    実装の乖離を機械的に検知する。"""

    @pytest.mark.parametrize("lang", sorted(USAGE_DOCS))
    def test_documented_defaults_match_parser(self, lang):
        parser_defaults = vars(_build_dag_arg_parser().parse_args([]))
        documented = _documented_dag_options(lang)
        assert documented, f"{lang}のUsageからdagのオプション表を抽出できませんでした"

        for option, documented_default in documented.items():
            dest = option.removeprefix("--").replace("-", "_")
            assert (
                dest in parser_defaults
            ), f"{lang}のUsageに記載された{option}は実装に存在しません"
            if documented_default is None:
                continue
            assert documented_default == str(parser_defaults[dest]), (
                f"{lang}のUsageの{option}のデフォルト値が実装と一致しません: "
                f"docs={documented_default} / implementation={parser_defaults[dest]}"
            )

    def test_ja_and_en_document_the_same_options(self):
        assert (
            _documented_dag_options("ja").keys() == _documented_dag_options("en").keys()
        )


_DAG_CONFIG_KEY_PATTERN = re.compile(r"`(dag[_-][a-z_-]+)`")


def _documented_dag_config_keys(lang: str) -> set[str]:
    """Usageの`orchestune-dag`（## 3.）節「Configuration File Options」節から
    バッククォート付きの`dag_.../dag-...`キー名トークンを抽出する。"""
    return set(_DAG_CONFIG_KEY_PATTERN.findall(_section(lang, 3)))


class TestDagConfigFileDocsConsistency:
    """#411: `dag_ignore_patterns`/`dag_similarity_threshold`のような
    CLIフラグを持たない設定ファイル専用キーが、Usageと実装（`dag_models.py`の
    `DAG_TOOL_CONFIG_KEYS`）の間でドリフトしないことを検証する。"""

    @pytest.mark.parametrize("lang", sorted(USAGE_DOCS))
    def test_config_keys_match_dag_tool_config_keys(self, lang):
        documented = _documented_dag_config_keys(lang)
        assert documented == set(DAG_TOOL_CONFIG_KEYS), (
            f"{lang}のUsageに記載された設定キーがDAG_TOOL_CONFIG_KEYSと"
            f"一致しません: docs={documented} / impl={set(DAG_TOOL_CONFIG_KEYS)}"
        )

    def test_ja_and_en_document_the_same_config_keys(self):
        assert _documented_dag_config_keys("ja") == _documented_dag_config_keys("en")

    _TOML_FENCE_PATTERN = re.compile(r"```toml\n(.*?)```", re.DOTALL)

    @pytest.mark.parametrize("lang", sorted(USAGE_DOCS))
    def test_toml_ignore_pattern_example_is_valid_and_matches_a_path(self, lang):
        section = _section(lang, 3)
        match = self._TOML_FENCE_PATTERN.search(section)
        assert match, f"{lang}のUsageに## 3.節のtomlフェンスブロックが見つかりません"
        parsed = tomllib.loads(match.group(1))
        patterns = parsed["dag_ignore_patterns"]
        assert patterns and re.compile(patterns[0]).search("src/package.json")

    def test_basic_string_and_literal_string_escaping_examples_are_equivalent(self):
        """#411: `[!WARNING]`注意書きが示すbasic string/literal stringの
        エスケープ対応関係が、実際に同じ正規表現へ解決することを直接証明する
        （散文の説明が正しいTOML/正規表現の知識であることの根拠）。"""
        basic = '"(^|/)package\\\\.json$"'
        literal = "'(^|/)package\\.json$'"
        assert (
            tomllib.loads(f"p = {basic}")["p"] == tomllib.loads(f"p = {literal}")["p"]
        )


class TestDocsSchemaConsistency:
    """#272: `_parse_subtask` がサポートするフィールドとUsageの乖離を検知する。"""

    @pytest.mark.parametrize("lang", sorted(USAGE_DOCS))
    def test_all_supported_fields_are_documented(self, lang):
        documented = _documented_schema_fields(lang)
        missing = _supported_plan_fields() - documented.keys()
        assert (
            not missing
        ), f"{lang}のUsageに未記載の計画フィールドがあります: {missing}"

    @pytest.mark.parametrize("lang", sorted(USAGE_DOCS))
    def test_only_id_is_documented_as_required(self, lang):
        documented = _documented_schema_fields(lang)
        required = {name for name, kind in documented.items() if kind == "required"}
        assert required == {"id"}

    def test_ja_and_en_schema_sections_agree(self):
        assert _documented_schema_fields("ja") == _documented_schema_fields("en")


class TestDocsIntegrationConsistency:
    """#272: 統合節が親ブランチ二層モデルを説明していることを検証する。"""

    @pytest.mark.parametrize("lang", sorted(USAGE_DOCS))
    def test_describes_parent_branch_auto_integration(self, lang):
        section = _integration_section(lang)
        assert "--parent-issue" in section
        assert "parent/issue-" in section
        assert "integration/temp-parent-issue-" in section

    @pytest.mark.parametrize("lang", sorted(USAGE_DOCS))
    def test_describes_human_final_merge_into_main(self, lang):
        section = _integration_section(lang)
        marker = "人間" if lang == "ja" else "human"
        assert marker in section
        assert "main" in section

    @pytest.mark.parametrize("lang", sorted(USAGE_DOCS))
    def test_does_not_claim_rebase_onto_main(self, lang):
        """自動リベース先は最新のmainではなく、CI通過済みの依存先ブランチ。"""
        section = _integration_section(lang)
        stale_claim = (
            "最新の main にリベース" if lang == "ja" else "rebases any downstream"
        )
        assert stale_claim not in section


class TestDocsSharedContractConsistency:
    """#279レビュー対応: `writes_shared_contract` の要否は、`orchestune-dag` が
    自動検出できるカテゴリに依存する。カテゴリ一覧が実装と文書で乖離しないよう検証する。"""

    @pytest.mark.parametrize("lang", sorted(USAGE_DOCS))
    def test_auto_detected_categories_are_documented(self, lang):
        text = _read_usage(lang)
        missing = [
            category
            for category, _ in _SHARED_CONTRACT_PATTERNS
            if category not in text
        ]
        assert (
            not missing
        ), f"{lang}のUsageに未記載の共有拡張ポイントカテゴリがあります: {missing}"

    @pytest.mark.parametrize("lang", sorted(USAGE_DOCS))
    def test_flag_is_documented_as_required_outside_the_heuristic(self, lang):
        """カテゴリに一致しないパスでは明示指定が必要である旨が記載されていること。"""
        block = _schema_bullet_block(lang, "writes_shared_contract")
        marker = "必要です" if lang == "ja" else "must set"
        assert marker in block

    @pytest.mark.parametrize("lang", sorted(USAGE_DOCS))
    def test_tag_alone_is_not_documented_as_sufficient(self, lang):
        """タグの一致だけでは警告されない（書き込み者同士のみ比較される）こと。"""
        block = _schema_bullet_block(lang, "shared_contract")
        marker = "書き込む" if lang == "ja" else "write"
        assert marker in block


class TestSkillDocumentsExistenceVerificationTriage:
    """#409: `orchestune-dag`の実在検証warning（#393/#400）の解釈手順が
    `skills/orchestune/SKILL.md`のStage 2に記載されていることを検証する。"""

    def _stage2_section(self) -> str:
        skill_text = (REPO_ROOT / "skills" / "orchestune" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        match = re.search(
            r"^### Stage 2: Validate DAG.*?(?=^### |\Z)",
            skill_text,
            re.MULTILINE | re.DOTALL,
        )
        assert match, "SKILL.mdに'### Stage 2: Validate DAG'節が見つかりません"
        return match.group(0)

    def test_stage2_describes_existence_verification_warning(self):
        """SKILL.md Stage 2 documents existence-verification warning triage for footprint and symbols."""
        section = self._stage2_section()
        assert "Existence-verification warning" in section
        assert "footprint" in section
        assert "symbols" in section

    def test_stage2_documents_that_symbol_verification_can_be_silently_skipped(self):
        """#414レビュー指摘: footprintが丸ごと新規ファイルのみの場合、
        `find_missing_symbols`は検証材料が無いため`symbols`の警告を一切
        出さずスキップする（`any_file_checked`ガード）。"警告が無い＝
        symbolsが確認済み"と誤読されないよう、この旨が明記されていること
        を検証する。"""
        section = self._stage2_section()
        assert "silently skipped" in section

    def test_stage2_documents_that_one_unparseable_file_skips_the_whole_subtask(self):
        """#414レビュー再指摘: footprintに既存の`.py`ファイルがあっても、
        そのうち1件でもparseに失敗（構文/エンコーディングエラー）すれば
        `any_file_unparseable`ガードによりsubtask全体でsymbols検証が
        スキップされる（他の実在パースできる`.py`ファイルがあっても関係
        ない）。この非自明な挙動が明記されていることを検証する。"""
        section = self._stage2_section()
        assert "unparseable" in section


class TestDocsExistenceVerificationConsistency:
    """#409: Usageの「DAG Validation」節（## 3.）に実在検証warningの説明が
    あることを検証する。"""

    @pytest.mark.parametrize("lang", sorted(USAGE_DOCS))
    def test_key_checks_section_mentions_existence_verification(self, lang):
        section = _section(lang, 3)
        marker = "実在" if lang == "ja" else "exist"
        assert marker in section

    @pytest.mark.parametrize("lang", sorted(USAGE_DOCS))
    def test_key_checks_section_documents_that_symbol_verification_can_be_skipped(
        self, lang
    ):
        """#414レビュー指摘: symbols検証が黙ってスキップされうることの明記。"""
        section = _section(lang, 3)
        marker = "スキップされ" if lang == "ja" else "silently skipped"
        assert marker in section

    @pytest.mark.parametrize("lang", sorted(USAGE_DOCS))
    def test_key_checks_section_documents_the_unparseable_file_case(self, lang):
        """#414レビュー再指摘: parse失敗ファイルが1件でもあれば
        subtask全体でsymbols検証がスキップされることの明記。"""
        section = _section(lang, 3)
        marker = "parse失敗" if lang == "ja" else "unparseable"
        assert marker in section

    @pytest.mark.parametrize("lang", sorted(USAGE_DOCS))
    def test_key_checks_section_notes_multiple_warning_types_can_coexist(self, lang):
        """複数種類のwarningが同一の`Warnings:`出力に混在しうることの明示。

        既存の"File/Symbol Conflict"文言に'multiple'/'複数'という語が偶然
        含まれるため、それらと衝突しないより具体的なマーカーで判定する。
        """
        section = _section(lang, 3)
        marker = "同時に" if lang == "ja" else "at once"
        assert marker in section

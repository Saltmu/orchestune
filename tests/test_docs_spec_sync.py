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


class TestDocsExecutionProfilesConsistency:
    """#670: Usageに記載されたExecution Profilesの設定例とスキーマの乖離を検証する。"""

    _TOML_FENCE_PATTERN = re.compile(r"```toml\n(.*?)```", re.DOTALL)

    @pytest.mark.parametrize("lang", sorted(USAGE_DOCS))
    def test_toml_execution_profile_examples_are_valid_and_extractable(self, lang):
        from orchestune.dispatch.execution_profiles import (
            extract_execution_profile_config,
        )

        section = _section(lang, 4)
        toml_blocks = self._TOML_FENCE_PATTERN.findall(section)
        assert (
            len(toml_blocks) >= 2
        ), f"{lang}のUsage ## 4.節にTOML設定例が見つかりません"

        # Check orchestune.toml example
        orchestune_toml = tomllib.loads(toml_blocks[0])
        config_orchestune = extract_execution_profile_config(orchestune_toml)
        assert config_orchestune.default_execution_profile == "balanced"
        assert "balanced" in config_orchestune.profiles
        assert "deep-reasoning" in config_orchestune.profiles

        # Check pyproject.toml example
        pyproject_toml = tomllib.loads(toml_blocks[1])
        tool_section = pyproject_toml.get("tool", {}).get("orchestune", {})
        config_pyproject = extract_execution_profile_config(tool_section)
        assert config_pyproject.default_execution_profile == "balanced"
        assert "balanced" in config_pyproject.profiles
        assert "deep-reasoning" in config_pyproject.profiles

    def test_ja_and_en_execution_profile_configs_match(self):
        from orchestune.dispatch.execution_profiles import (
            extract_execution_profile_config,
        )

        ja_blocks = self._TOML_FENCE_PATTERN.findall(_section("ja", 4))
        en_blocks = self._TOML_FENCE_PATTERN.findall(_section("en", 4))

        ja_cfg1 = extract_execution_profile_config(tomllib.loads(ja_blocks[0]))
        en_cfg1 = extract_execution_profile_config(tomllib.loads(en_blocks[0]))
        assert ja_cfg1 == en_cfg1

        ja_cfg2 = extract_execution_profile_config(
            tomllib.loads(ja_blocks[1])["tool"]["orchestune"]
        )
        en_cfg2 = extract_execution_profile_config(
            tomllib.loads(en_blocks[1])["tool"]["orchestune"]
        )
        assert ja_cfg2 == en_cfg2


class TestOrchestuneTomlExample:
    """orchestune.toml.example が存在し、構文・設定値・ドキュメント参照が正しいことを検証する。"""

    def test_orchestune_toml_example_exists_and_valid_config(self):
        from orchestune.dag.models import (
            extract_dag_ignore_patterns,
            extract_dag_similarity_threshold,
        )
        from orchestune.dispatch.dispatcher import _build_arg_parser, _config_defaults
        from orchestune.dispatch.execution_profiles import (
            extract_execution_profile_config,
        )

        example_path = REPO_ROOT / "orchestune.toml.example"
        assert (
            example_path.is_file()
        ), "orchestune.toml.example がリポジトリルートに存在しません"

        raw_toml = example_path.read_text(encoding="utf-8")
        data = tomllib.loads(raw_toml)
        assert isinstance(data, dict)

        # Dispatcherの引数パーサーと設定値バリデーションに通ることを確認
        parser = _build_arg_parser()
        defaults = _config_defaults(parser, data)
        assert isinstance(defaults, dict)

        # DAG設定が正常に抽出できることを確認
        patterns = extract_dag_ignore_patterns(data)
        assert isinstance(patterns, list)
        threshold = extract_dag_similarity_threshold(data)
        assert isinstance(threshold, float)
        assert 0.0 <= threshold <= 1.0

        # ExecutionProfile設定が正常に抽出できることを確認
        profile_config = extract_execution_profile_config(data)
        assert profile_config.default_execution_profile in profile_config.profiles
        assert "balanced" in profile_config.profiles
        assert "deep-reasoning" in profile_config.profiles
        assert "fast-code" in profile_config.profiles

        # model_tiers が正常に抽出できることを確認
        if profile_config.model_tiers:
            assert "strong" in profile_config.model_tiers
            assert "middle" in profile_config.model_tiers
            assert "weak" in profile_config.model_tiers

    def test_non_model_settings_keep_runtime_defaults(self):
        from orchestune.dag.models import (
            extract_dag_ignore_patterns,
            extract_dag_similarity_threshold,
        )
        from orchestune.dag.similarity import DEFAULT_SIMILARITY_THRESHOLD
        from orchestune.dispatch.dispatcher import _build_arg_parser, _config_defaults

        data = tomllib.loads(
            (REPO_ROOT / "orchestune.toml.example").read_text(encoding="utf-8")
        )
        parser = _build_arg_parser()
        runtime_defaults = vars(parser.parse_args([]))

        for key, configured_value in _config_defaults(parser, data).items():
            assert configured_value == runtime_defaults[key], (
                f"非モデル設定 {key!r} は推奨値ではなく実行時デフォルトを使用してください: "
                f"example={configured_value!r} / default={runtime_defaults[key]!r}"
            )

        assert extract_dag_ignore_patterns(data) == []
        assert extract_dag_similarity_threshold(data) == DEFAULT_SIMILARITY_THRESHOLD

    def test_every_dispatcher_setting_is_present_or_commented(self):
        raw_toml = (REPO_ROOT / "orchestune.toml.example").read_text(encoding="utf-8")
        parser = _build_arg_parser()

        missing = []
        for action in parser._actions:
            if action.dest == "help":
                continue
            key = action.dest.replace("_", "-")
            if re.search(rf"(?m)^#?\s*{re.escape(key)}\s*=", raw_toml) is None:
                missing.append(key)

        assert not missing, f"設定例に未記載のdispatcher設定があります: {missing}"

    @pytest.mark.parametrize(
        ("profile", "tier", "target", "expected_model", "expected_effort"),
        [
            ("balanced", "middle", "codex-cli", "gpt-5.6-terra", "medium"),
            ("fast-code", "weak", "codex-cli", "gpt-5.6-luna", "medium"),
            ("deep-reasoning", "strong", "codex-cli", "gpt-5.6-sol", "high"),
            ("balanced", "middle", "codex-cloud", "gpt-5.6-terra", "medium"),
            ("fast-code", "weak", "codex-cloud", "gpt-5.6-luna", "medium"),
            ("deep-reasoning", "strong", "codex-cloud", "gpt-5.6-sol", "high"),
            ("balanced", "middle", "claude-cli", "sonnet", "medium"),
            ("fast-code", "weak", "claude-cli", "haiku", None),
            ("deep-reasoning", "strong", "claude-cli", "opus", "high"),
            ("balanced", "middle", "cloud-routine", "claude-sonnet-5", None),
            (
                "fast-code",
                "weak",
                "cloud-routine",
                "claude-haiku-4-5-20251001",
                None,
            ),
            ("deep-reasoning", "strong", "cloud-routine", "claude-opus-5", None),
        ],
    )
    def test_recommended_profiles_resolve_for_supported_targets(
        self, profile, tier, target, expected_model, expected_effort
    ):
        from orchestune.dispatch.execution_profiles import (
            extract_execution_profile_config,
            resolve_execution_profile,
        )

        data = tomllib.loads(
            (REPO_ROOT / "orchestune.toml.example").read_text(encoding="utf-8")
        )
        config = extract_execution_profile_config(data)
        selection = resolve_execution_profile(profile, target, config, model_tier=tier)

        assert selection.model == expected_model
        assert selection.reasoning_effort == expected_effort

    def test_private_orchestune_toml_is_ignored(self):
        ignore_rules = (
            (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        )
        assert "orchestune.toml" in ignore_rules

    @pytest.mark.parametrize(
        "doc_rel_path",
        [
            "docs/ja/setup.md",
            "docs/ja/usage.md",
            "docs/en/setup.md",
            "docs/en/usage.md",
        ],
    )
    def test_docs_reference_orchestune_toml_example(self, doc_rel_path):
        doc_path = REPO_ROOT / doc_rel_path
        content = doc_path.read_text(encoding="utf-8")
        assert (
            "orchestune.toml.example" in content
        ), f"{doc_rel_path} に orchestune.toml.example への言及がありません"

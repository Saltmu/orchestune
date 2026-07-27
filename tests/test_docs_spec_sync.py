"""#272: Usageドキュメント（日英）と実装の乖離を機械的に検知するテスト。

`decomposition_plan.md` のスキーマ、`orchestune-dispatch` のCLIオプション、
統合パイプラインの説明が、実装からドリフトしていないことを検証する。
"""

import inspect
import pathlib
import re

import pytest

from orchestune.dag_parsing import _parse_subtask
from orchestune.dispatcher import _build_arg_parser

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


def _documented_options(lang: str) -> dict[str, str | None]:
    """Usageのオプション表から {オプション名: 記載デフォルト値} を抽出する。

    デフォルト値がバッククォートで囲まれた単一トークンでない行（`-`や
    「自動選択」のような散文）は、機械比較の対象外として ``None`` を返す。
    """
    options: dict[str, str | None] = {}
    for line in _read_usage(lang).splitlines():
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
    """Usageの「4. 統合」節（次の`## `見出しの直前まで）を切り出す。"""
    text = _read_usage(lang)
    match = re.search(r"^## 4\..*?(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    assert match, f"{lang}のUsageに統合節（## 4.）が見つかりません"
    return match.group(0)


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

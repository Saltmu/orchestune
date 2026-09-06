"""#822: 実装前の影響範囲調査に使うコード解析ツール(Serena MCP)の設定を機械的に検証する。

導入PRが満たすべき受け入れ条件のうち、機械検証できるものをここで固定する。

* 再現可能なバージョン指定（厳密ピン）
* 環境固有の絶対パスを設定へ書かない
* worktree ごとに索引が分離される起動方法
* 索引・キャッシュ・機密情報をコミットしない
* 接続失敗時のフォールバックが文書化されている
* 実装着手前の影響範囲確定が開発ワークフローに組み込まれている
"""

import json
import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MCP_CONFIG = REPO_ROOT / ".mcp.json"
GITIGNORE = REPO_ROOT / ".gitignore"
AGENT_RULES = REPO_ROOT / ".agents" / "AGENTS.md"
SKILL_DIR = REPO_ROOT / "skills" / "local-ci-developer"
IMPACT_SCOPE_REFERENCE = SKILL_DIR / "references" / "impact-scope.md"
CONTRIBUTING_DOCS = (REPO_ROOT / "CONTRIBUTING.md", REPO_ROOT / "CONTRIBUTING.ja.md")

_PINNED_REQUIREMENT_PATTERN = re.compile(r"^serena-agent==\d+\.\d+\.\d+$")


def _mcp_servers() -> dict:
    assert MCP_CONFIG.is_file(), "プロジェクトスコープの .mcp.json が存在しません"
    config = json.loads(MCP_CONFIG.read_text(encoding="utf-8"))
    servers = config.get("mcpServers")
    assert isinstance(servers, dict), ".mcp.json に mcpServers オブジェクトが必要です"
    return servers


def _serena_args() -> list[str]:
    serena = _mcp_servers()["serena"]
    args = serena.get("args")
    assert isinstance(args, list) and all(isinstance(arg, str) for arg in args)
    return args


def _option_values(args: list[str], option: str) -> list[str]:
    """`--option value` 形式で与えられた値をすべて取り出す。"""
    return [
        args[i + 1] for i, arg in enumerate(args) if arg == option and i + 1 < len(args)
    ]


def test_serena_mcp_server_is_declared_for_the_project():
    """開発セッションから解析ツールへ接続できる設定がリポジトリで管理されている。"""
    servers = _mcp_servers()

    assert "serena" in servers, ".mcp.json に serena サーバが宣言されていません"
    assert servers["serena"].get("command"), "serena サーバに起動コマンドが必要です"
    assert "start-mcp-server" in _serena_args()


def test_serena_dependency_version_is_strictly_pinned():
    """再現可能な導入のため、依存バージョンを厳密に固定する。"""
    requirements = _option_values(_serena_args(), "--from")

    assert requirements, "--from による依存指定がありません"
    for requirement in requirements:
        assert _PINNED_REQUIREMENT_PATTERN.fullmatch(
            requirement
        ), f"依存は serena-agent==<x.y.z> の形で固定してください: {requirement}"


def test_serena_launch_does_not_embed_environment_specific_paths():
    """設定にローカル環境固有の絶対パスを混入させない。"""
    args = _serena_args()

    for arg in args:
        assert not arg.startswith("/"), f"絶対パスを設定へ書かないでください: {arg}"
        assert not re.match(
            r"^[A-Za-z]:[\\/]", arg
        ), f"絶対パスを設定へ書かないでください: {arg}"
        assert "file://" not in arg, f"絶対パスURIを設定へ書かないでください: {arg}"

    assert (
        "--project" not in args
    ), "固定パスでのプロジェクト指定はworktreeを取り違えます"


def test_serena_project_is_resolved_from_the_working_directory():
    """worktree ごとに索引を分離するため、プロジェクトを cwd から解決する。"""
    assert "--project-from-cwd" in _serena_args()


def test_serena_is_restricted_to_read_only_analysis():
    """用途は実装前の調査であり、解析ツール側からの書き込みは行わない。"""
    modes = _option_values(_serena_args(), "--mode")

    assert "planning" in modes, "書き込み系ツールを除外する planning モードが必要です"


def test_serena_local_artifacts_are_not_committed():
    """索引・キャッシュ・memories をリポジトリへコミットしない。"""
    ignored = {
        line.strip() for line in GITIGNORE.read_text(encoding="utf-8").splitlines()
    }

    assert (
        ".serena/" in ignored
    ), ".gitignore が Serena のローカル成果物を除外していません"


@pytest.mark.parametrize("document", CONTRIBUTING_DOCS, ids=lambda p: p.name)
def test_contributing_documents_setup_and_fallback(document: pathlib.Path):
    """導入手順と、接続・索引失敗時のフォールバックが日英双方で読める。"""
    text = document.read_text(encoding="utf-8")

    assert "serena-agent==" in text, f"{document.name} に固定バージョンの記載が必要です"
    assert ".mcp.json" in text
    assert "uv" in text, f"{document.name} に前提ツールの記載が必要です"
    assert (
        "ripgrep" in text or "rg " in text or "grep" in text
    ), f"{document.name} に既存検索へのフォールバックの記載が必要です"


def test_workflow_requires_impact_scope_before_implementation():
    """実装着手前に影響先を列挙・仕分けする手順が開発ワークフローへ組み込まれている。"""
    assert IMPACT_SCOPE_REFERENCE.is_file()

    skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "references/impact-scope.md" in skill_text

    reference_text = IMPACT_SCOPE_REFERENCE.read_text(encoding="utf-8")
    assert "implementation_plan.md" in reference_text
    # 検索結果なしを影響なしの証明にしない補完調査
    assert "dynamic" in reference_text.lower()
    # 実装後の照合
    assert "out of scope" in reference_text.lower()


def test_agent_rules_require_impact_scope_before_implementation():
    """規範文書側にも実装前の影響範囲確定を明記する。"""
    instructions = AGENT_RULES.read_text(encoding="utf-8")

    assert "影響範囲" in instructions
    assert "references/impact-scope.md" in instructions

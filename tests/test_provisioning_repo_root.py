"""provision_issuesのrepo_root起点のふるまい（#359 symbols検証 / #398・#404
dag_ignore_patterns）に関するテスト。

`tests/test_provisioning.py`の肥大化解消のため分割している。
`FakeForge`/`template_path`フィクスチャはそちらと同じ定義を用いる。
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestune.dag.similarity import DEFAULT_SIMILARITY_THRESHOLD
from orchestune.provisioning.flow import provision_issues
from tests.test_provisioning_support import FakeForge

_TEMPLATE = (
    "# [FEAT] {{subtask_id}}: {{description}}\n\n"
    "## Overview\n{{overview}}\n\n"
    "## Proposed Changes\n{{proposed_changes}}\n\n"
    "## Acceptance Criteria\n{{acceptance_criteria}}\n\n"
    "## Verification Plan\n{{verification_plan}}\n\n"
    "```yaml\n"
    "subtask_id: {{subtask_id_yaml}}\n"
    "footprint: {{footprint}}\n"
    "symbols: {{symbols}}\n"
    "depends_on: {{depends_on}}\n"
    "parent_issue_number: {{parent_issue_number}}\n"
    "execution_profile: {{execution_profile}}\n"
    "model_tier: {{model_tier}}\n"
    "```\n"
)


@pytest.fixture
def template_path(tmp_path: Path) -> Path:
    path = tmp_path / "issue_template.md"
    path.write_text(_TEMPLATE, encoding="utf-8")
    return path


class TestSymbolVerificationWarning:
    """#359: symbolsが実コードに存在しない場合、Issue本文に注記を追加する。"""

    def _plan(self, symbols: str) -> str:
        return (
            "---\n"
            'title: "Example big rock"\n'
            "parent_issue_number: null\n"
            "subtasks:\n"
            "  - id: task-a\n"
            '    description: "Implement feature XX"\n'
            "    footprint: [src/foo.py]\n"
            f"    symbols: [{symbols}]\n"
            "    depends_on: []\n"
            "    issue_number: null\n"
            "---\n"
            "\n"
            "# Decomposition Plan\n"
        )

    def test_missing_symbol_appends_warning_on_create(
        self, tmp_path: Path, template_path: Path
    ):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "foo.py").write_text(
            "def actual_function():\n    pass\n", encoding="utf-8"
        )
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(self._plan("renamed_away_function"), encoding="utf-8")

        forge = FakeForge()
        provision_issues(
            plan_path,
            forge=forge,
            template_path=template_path,
            repo_root=tmp_path,
        )

        subtask_body = next(
            body for title, body, _ in forge.create_issue_calls if "task-a" in title
        )
        assert "symbols未検出" in subtask_body
        assert "renamed_away_function" in subtask_body
        # レビュー指摘(#372): 「見つからない=リファクタで陳腐化した」と
        # 断定せず、新規追加の可能性にも触れる中立な文言であることを確認。
        assert "新規追加する予定であれば問題ありません" in subtask_body

    def test_existing_symbol_does_not_append_warning(
        self, tmp_path: Path, template_path: Path
    ):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "foo.py").write_text(
            "def actual_function():\n    pass\n", encoding="utf-8"
        )
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(self._plan("actual_function"), encoding="utf-8")

        forge = FakeForge()
        provision_issues(
            plan_path,
            forge=forge,
            template_path=template_path,
            repo_root=tmp_path,
        )

        subtask_body = next(
            body for title, body, _ in forge.create_issue_calls if "task-a" in title
        )
        assert "symbols未検出" not in subtask_body

    def test_missing_symbol_appends_warning_in_no_apply_preview(
        self, tmp_path: Path, template_path: Path
    ):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "foo.py").write_text(
            "def actual_function():\n    pass\n", encoding="utf-8"
        )
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(self._plan("renamed_away_function"), encoding="utf-8")

        result = provision_issues(
            plan_path,
            apply=False,
            template_path=template_path,
            repo_root=tmp_path,
        )

        assert "symbols未検出" in result.previews[0].body

    def test_footprint_file_not_yet_existing_skips_warning(
        self, tmp_path: Path, template_path: Path
    ):
        """新規作成予定のfootprintファイルのみのsubtaskでは、検証材料が
        無いためfalse positiveを避けて注記を付けない。"""
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(self._plan("brand_new_function"), encoding="utf-8")

        forge = FakeForge()
        provision_issues(
            plan_path,
            forge=forge,
            template_path=template_path,
            repo_root=tmp_path,
        )

        subtask_body = next(
            body for title, body, _ in forge.create_issue_calls if "task-a" in title
        )
        assert "symbols未検出" not in subtask_body

    def test_repo_root_defaults_to_cwd_without_crashing(
        self, tmp_path: Path, template_path: Path
    ):
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(self._plan("actual_function"), encoding="utf-8")
        forge = FakeForge()
        result = provision_issues(plan_path, forge=forge, template_path=template_path)
        assert result.applied is True


class TestConflictGraphConfig:
    """#398/#404: provision_issuesもorchestune-dagと同じdag_ignore_patternsを
    尊重すること。#659以降、共有`package.json`はConflict Graphだけへ入り、
    `task-c -> task-a -> task-b`というPrecedence DAGのcycle判定には影響しない。"""

    def _plan(self) -> str:
        return (
            "---\n"
            'title: "Example big rock"\n'
            "parent_issue_number: null\n"
            "subtasks:\n"
            "  - id: task-a\n"
            '    description: "A"\n'
            "    footprint: [src/a.py]\n"
            "    depends_on: [task-c]\n"
            "    issue_number: null\n"
            "  - id: task-b\n"
            '    description: "B"\n'
            "    footprint: [package.json]\n"
            "    depends_on: [task-a]\n"
            "    issue_number: null\n"
            "  - id: task-c\n"
            '    description: "C"\n'
            "    footprint: [package.json]\n"
            "    depends_on: []\n"
            "    issue_number: null\n"
            "---\n"
            "\n"
            "# Decomposition Plan\n"
        )

    def test_similarity_conflict_never_triggers_cycle_warning(
        self, tmp_path: Path, template_path: Path, caplog
    ):
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(self._plan(), encoding="utf-8")
        forge = FakeForge()

        with caplog.at_level("WARNING"):
            provision_issues(
                plan_path, forge=forge, template_path=template_path, repo_root=tmp_path
            )

        assert not any("循環参照" in record.message for record in caplog.records)

    def test_dag_ignore_patterns_remains_accepted_after_graph_split(
        self, tmp_path: Path, template_path: Path, caplog
    ):
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(self._plan(), encoding="utf-8")
        (tmp_path / "orchestune.toml").write_text(
            'dag_ignore_patterns = ["(^|/)package.json$"]\n', encoding="utf-8"
        )
        forge = FakeForge()

        with caplog.at_level("WARNING"):
            result = provision_issues(
                plan_path, forge=forge, template_path=template_path, repo_root=tmp_path
            )

        assert result.applied is True
        assert not any("循環参照" in record.message for record in caplog.records)

    def test_empty_string_pattern_is_rejected(
        self, tmp_path: Path, template_path: Path
    ):
        # #404レビュー指摘: 空文字列はre.compile("")で常に全パスにマッチし、
        # 診断なしに全ての類似度競合辺を消してしまうため拒否する
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(self._plan(), encoding="utf-8")
        (tmp_path / "orchestune.toml").write_text(
            'dag_ignore_patterns = [""]\n', encoding="utf-8"
        )

        with pytest.raises(ValueError, match="non-empty"):
            provision_issues(
                plan_path,
                forge=FakeForge(),
                template_path=template_path,
                repo_root=tmp_path,
            )

    def test_dag_similarity_threshold_config_is_forwarded_to_build_dag(
        self, tmp_path: Path, template_path: Path
    ):
        """#407: `orchestune-dag --threshold`で永続化された
        `dag_similarity_threshold`設定を`orchestune-provision`も読み、
        同じ閾値で`build_dag`を呼ぶこと。#659以降はConflict Graphの再現性を
        保ち、`topological_order`自体は明示依存だけから決まる。"""
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(self._plan(), encoding="utf-8")
        (tmp_path / "orchestune.toml").write_text(
            "dag_similarity_threshold = 0.1\n", encoding="utf-8"
        )

        with patch(
            "orchestune.provisioning.flow.build_dag", wraps=lambda *a, **kw: MagicMock()
        ) as mock_build_dag:
            provision_issues(
                plan_path,
                forge=FakeForge(),
                template_path=template_path,
                repo_root=tmp_path,
            )

        assert mock_build_dag.call_args.kwargs.get("threshold") == 0.1

    def test_dag_similarity_threshold_defaults_when_unset(
        self, tmp_path: Path, template_path: Path
    ):
        """#407: 設定ファイル未指定時は`DEFAULT_SIMILARITY_THRESHOLD`のまま
        （既定挙動を変えない）。"""
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(self._plan(), encoding="utf-8")

        with patch(
            "orchestune.provisioning.flow.build_dag", wraps=lambda *a, **kw: MagicMock()
        ) as mock_build_dag:
            provision_issues(
                plan_path,
                forge=FakeForge(),
                template_path=template_path,
                repo_root=tmp_path,
            )

        assert (
            mock_build_dag.call_args.kwargs.get("threshold")
            == DEFAULT_SIMILARITY_THRESHOLD
        )

    def test_invalid_dag_similarity_threshold_raises_clear_error(
        self, tmp_path: Path, template_path: Path
    ):
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(self._plan(), encoding="utf-8")
        (tmp_path / "orchestune.toml").write_text(
            "dag_similarity_threshold = 2\n", encoding="utf-8"
        )

        with pytest.raises(ValueError, match=r"\[0, 1\]"):
            provision_issues(
                plan_path,
                forge=FakeForge(),
                template_path=template_path,
                repo_root=tmp_path,
            )

    def test_malformed_orchestune_toml_raises_clear_error(
        self, tmp_path: Path, template_path: Path
    ):
        # #404レビュー指摘: 新設したload_orchestune_config呼び出しが不正な
        # 設定ファイルを検出した場合、原因不明のスタックトレースではなく
        # 他のツール（dag_cli.py/dispatcher.py）と同様の明確なエラーになること
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(self._plan(), encoding="utf-8")
        (tmp_path / "orchestune.toml").write_text(
            "not valid toml [[[", encoding="utf-8"
        )

        with pytest.raises(ValueError, match="orchestune.toml"):
            provision_issues(
                plan_path,
                forge=FakeForge(),
                template_path=template_path,
                repo_root=tmp_path,
            )

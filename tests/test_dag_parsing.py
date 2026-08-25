"""decomposition_plan.mdのフロントマターパース・フィールド契約のテスト。

test_dag.py (1050行) からパース関連(#479)を分割。類似度計算は
test_dag_similarity.py、グラフ構築は test_dag_graph.py を参照。
"""

import logging

import pytest

from orchestune.dag.parsing import parse_decomposition_plan
from tests.dag_test_support import BASIC_PLAN, _write_plan


class TestParseDecompositionPlan:
    def test_parses_basic_subtasks(self, tmp_path):
        path = _write_plan(tmp_path, BASIC_PLAN)
        subtasks = parse_decomposition_plan(path)

        assert [s.id for s in subtasks] == ["task-a", "task-b"]
        assert subtasks[0].footprint == ("src/foo.py",)
        assert subtasks[0].symbols == ("foo.Foo",)
        assert subtasks[1].depends_on == ("task-a",)
        assert subtasks[0].priority == "medium"  # デフォルト

    def test_parses_priority(self, tmp_path):
        plan = """\
        ---
        subtasks:
          - id: task-a
            priority: high
          - id: task-b
            priority: low
        ---
        """
        path = _write_plan(tmp_path, plan)
        subtasks = parse_decomposition_plan(path)
        assert subtasks[0].priority == "high"
        assert subtasks[1].priority == "low"

    def test_parses_overview_and_acceptance_criteria(self, tmp_path):
        plan = """\
        ---
        subtasks:
          - id: task-a
            overview: "This is a detailed overview of task-a."
            acceptance_criteria:
              - "Criterion 1"
              - "Criterion 2"
          - id: task-b
        ---
        """
        path = _write_plan(tmp_path, plan)
        subtasks = parse_decomposition_plan(path)
        assert subtasks[0].overview == "This is a detailed overview of task-a."
        assert subtasks[0].acceptance_criteria == ("Criterion 1", "Criterion 2")
        assert subtasks[1].overview == ""
        assert subtasks[1].acceptance_criteria == ()

        # to_dictのテスト
        from orchestune.dag.models import DagResult

        res = DagResult(
            subtasks={subtasks[0].id: subtasks[0]},
            edges=[],
            topological_order=["task-a"],
            parallel_leaves=["task-a"],
            risky_subtask_ids=[],
        )
        d = res.to_dict()
        assert (
            d["subtasks"]["task-a"]["overview"]
            == "This is a detailed overview of task-a."
        )
        assert d["subtasks"]["task-a"]["acceptance_criteria"] == [
            "Criterion 1",
            "Criterion 2",
        ]

    def test_parses_proposed_changes_and_verification_plan(self, tmp_path):
        plan = """\
        ---
        subtasks:
          - id: task-a
            proposed_changes:
              - "Modify src/foo.py"
            verification_plan:
              - "Run pytest tests/test_foo.py"
          - id: task-b
        ---
        """
        path = _write_plan(tmp_path, plan)
        subtasks = parse_decomposition_plan(path)
        assert subtasks[0].proposed_changes == ("Modify src/foo.py",)
        assert subtasks[0].verification_plan == ("Run pytest tests/test_foo.py",)
        assert subtasks[1].proposed_changes == ()
        assert subtasks[1].verification_plan == ()

        # to_dictのテスト
        from orchestune.dag.models import DagResult

        res = DagResult(
            subtasks={subtasks[0].id: subtasks[0]},
            edges=[],
            topological_order=["task-a"],
            parallel_leaves=["task-a"],
            risky_subtask_ids=[],
        )
        d = res.to_dict()
        assert d["subtasks"]["task-a"]["proposed_changes"] == [
            "Modify src/foo.py",
        ]
        assert d["subtasks"]["task-a"]["verification_plan"] == [
            "Run pytest tests/test_foo.py",
        ]

    def test_missing_frontmatter_raises(self, tmp_path):
        path = _write_plan(tmp_path, "# no frontmatter here\n")
        with pytest.raises(ValueError, match="フロントマター"):
            parse_decomposition_plan(path)

    def test_duplicate_id_raises(self, tmp_path):
        plan = """\
        ---
        subtasks:
          - id: dup
            footprint: []
            symbols: []
          - id: dup
            footprint: []
            symbols: []
        ---
        """
        path = _write_plan(tmp_path, plan)
        with pytest.raises(ValueError, match="重複"):
            parse_decomposition_plan(path)

    def test_unknown_depends_on_raises(self, tmp_path):
        plan = """\
        ---
        subtasks:
          - id: task-a
            footprint: []
            symbols: []
            depends_on: ["ghost"]
        ---
        """
        path = _write_plan(tmp_path, plan)
        with pytest.raises(ValueError, match="未知のdepends_on"):
            parse_decomposition_plan(path)


class TestRiskFlagParsing:
    def test_flags_data_sources_path(self, tmp_path):
        plan = """\
        ---
        subtasks:
          - id: task-a
            footprint: ["data/sources/plot.yaml"]
            symbols: []
        ---
        """
        path = _write_plan(tmp_path, plan)
        subtasks = parse_decomposition_plan(path)
        assert subtasks[0].risk is True
        assert any("data/sources" in r for r in subtasks[0].risk_reasons)

    def test_flags_credentials_path(self, tmp_path):
        plan = """\
        ---
        subtasks:
          - id: task-a
            footprint: ["credentials/service_account.json"]
            symbols: []
        ---
        """
        path = _write_plan(tmp_path, plan)
        subtasks = parse_decomposition_plan(path)
        assert subtasks[0].risk is True

    def test_flags_subprocess_keyword_in_symbols(self, tmp_path):
        plan = """\
        ---
        subtasks:
          - id: task-a
            footprint: ["src/utils/ai_cli_executor.py"]
            symbols: ["run_subprocess_command"]
        ---
        """
        path = _write_plan(tmp_path, plan)
        subtasks = parse_decomposition_plan(path)
        assert subtasks[0].risk is True
        assert any("subprocess" in r for r in subtasks[0].risk_reasons)

    def test_explicit_risk_override(self, tmp_path):
        plan = """\
        ---
        subtasks:
          - id: task-a
            footprint: ["src/plain.py"]
            symbols: []
            risk: true
        ---
        """
        path = _write_plan(tmp_path, plan)
        subtasks = parse_decomposition_plan(path)
        assert subtasks[0].risk is True
        assert "explicit" in subtasks[0].risk_reasons

    def test_no_risk_for_plain_subtask(self, tmp_path):
        path = _write_plan(tmp_path, BASIC_PLAN)
        subtasks = parse_decomposition_plan(path)
        assert subtasks[0].risk is False
        assert subtasks[0].risk_reasons == ()


class TestSubtaskFieldContract:
    """#272: 必須フィールドの欠落時挙動を、文書上の契約と一致させる。"""

    def test_missing_id_raises_value_error(self, tmp_path):
        plan = """\
        ---
        subtasks:
          - description: "idの無いサブタスク"
            footprint: ["src/foo.py"]
        ---
        """
        path = _write_plan(tmp_path, plan)
        with pytest.raises(ValueError, match="id"):
            parse_decomposition_plan(path)

    def test_blank_id_raises_value_error(self, tmp_path):
        plan = """\
        ---
        subtasks:
          - id: "   "
            footprint: ["src/foo.py"]
        ---
        """
        path = _write_plan(tmp_path, plan)
        with pytest.raises(ValueError, match="id"):
            parse_decomposition_plan(path)

    @pytest.mark.parametrize(
        ("literal", "label"),
        [
            ("", "null"),
            ("null", "null"),
            ("[]", "空リスト"),
            ("123", "整数"),
            ("2026-01-01", "日付"),
            ("true", "真偽値"),
        ],
    )
    def test_non_string_id_raises_value_error(self, tmp_path, literal, label):
        """#279レビュー対応: `str()`での暗黙の強制変換は、`None`や`[]`を
        `"None"`/`"[]"`というIssue・ブランチ識別子へ黙って昇格させてしまう。
        文書上の契約（文字列）どおり、非文字列は明示的に拒否する。"""
        plan = f"""\
        ---
        subtasks:
          - id: {literal}
            footprint: ["src/foo.py"]
        ---
        """
        path = _write_plan(tmp_path, plan)
        with pytest.raises(ValueError, match="id"):
            parse_decomposition_plan(path)

    def test_numeric_looking_id_is_accepted_when_quoted(self, tmp_path):
        plan = """\
        ---
        subtasks:
          - id: "123"
            footprint: ["src/foo.py"]
        ---
        """
        path = _write_plan(tmp_path, plan)
        assert parse_decomposition_plan(path)[0].id == "123"

    def test_missing_optional_fields_fall_back_to_defaults(self, tmp_path):
        plan = """\
        ---
        subtasks:
          - id: task-a
        ---
        """
        path = _write_plan(tmp_path, plan)
        subtask = parse_decomposition_plan(path)[0]

        assert subtask.description == ""
        assert subtask.footprint == ()
        assert subtask.depends_on == ()
        assert subtask.symbols == ()
        assert subtask.priority == "medium"
        assert subtask.shared_contract is None
        assert subtask.writes_shared_contract is False

    def test_missing_description_and_footprint_are_warned(self, tmp_path, caplog):
        plan = """\
        ---
        subtasks:
          - id: task-a
        ---
        """
        path = _write_plan(tmp_path, plan)
        with caplog.at_level(logging.WARNING, logger="orchestune.dag.parsing"):
            parse_decomposition_plan(path)

        messages = " ".join(record.getMessage() for record in caplog.records)
        assert "task-a" in messages
        assert "description" in messages
        assert "footprint" in messages

    def test_complete_subtask_emits_no_warning(self, tmp_path, caplog):
        path = _write_plan(tmp_path, BASIC_PLAN)
        with caplog.at_level(logging.WARNING, logger="orchestune.dag.parsing"):
            parse_decomposition_plan(path)

        assert [r for r in caplog.records if r.name == "orchestune.dag.parsing"] == []

    def test_parses_shared_contract_fields(self, tmp_path):
        plan = """\
        ---
        subtasks:
          - id: task-a
            description: "共有契約を作る"
            footprint: ["src/registry.py"]
            shared_contract: plugin-registry
            writes_shared_contract: true
        ---
        """
        path = _write_plan(tmp_path, plan)
        subtask = parse_decomposition_plan(path)[0]

        assert subtask.shared_contract == "plugin-registry"
        assert subtask.writes_shared_contract is True

    def test_parses_execution_profile(self, tmp_path):
        plan = """\
        ---
        subtasks:
          - id: task-a
            execution_profile: fast-code_1
          - id: task-b
        ---
        """
        path = _write_plan(tmp_path, plan)
        subtasks = parse_decomposition_plan(path)

        assert subtasks[0].execution_profile == "fast-code_1"
        assert subtasks[1].execution_profile is None

        from orchestune.dag.models import DagResult

        res = DagResult(
            subtasks={subtasks[0].id: subtasks[0], subtasks[1].id: subtasks[1]},
            edges=[],
            topological_order=["task-a", "task-b"],
            parallel_leaves=["task-a", "task-b"],
            risky_subtask_ids=[],
        )
        d = res.to_dict()
        assert d["subtasks"]["task-a"]["execution_profile"] == "fast-code_1"
        assert d["subtasks"]["task-b"]["execution_profile"] is None

    @pytest.mark.parametrize(
        "invalid_profile",
        [
            "Fast-code",
            "fast code",
            "fast.code",
            "fast@code",
            "",
            "a" * 33,
            123,
            True,
            [],
            {},
        ],
    )
    def test_invalid_execution_profile_raises_value_error(
        self, tmp_path, invalid_profile
    ):
        plan = {
            "subtasks": [
                {
                    "id": "task-a",
                    "execution_profile": invalid_profile,
                }
            ]
        }
        import yaml

        plan_yaml = f"---\n{yaml.dump(plan)}---\n"
        path = _write_plan(tmp_path, plan_yaml)
        with pytest.raises(ValueError, match="execution_profile"):
            parse_decomposition_plan(path)

    @pytest.mark.parametrize(
        ("field_name", "scalar_value"),
        [
            ("depends_on", "task-a"),
            ("depends_on", 123),
            ("footprint", "src/foo.py"),
            ("symbols", "foo_func"),
            ("acceptance_criteria", "passes"),
            ("proposed_changes", "change"),
            ("verification_plan", "test"),
        ],
    )
    def test_scalar_sequence_fields_raise_value_error(
        self, tmp_path, field_name, scalar_value
    ):
        plan = {
            "subtasks": [
                {
                    "id": "task-a",
                    field_name: scalar_value,
                }
            ]
        }
        import yaml

        plan_yaml = f"---\n{yaml.dump(plan)}---\n"
        path = _write_plan(tmp_path, plan_yaml)
        with pytest.raises(ValueError, match=field_name):
            parse_decomposition_plan(path)

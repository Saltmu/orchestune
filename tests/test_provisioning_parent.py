from __future__ import annotations

from pathlib import Path

import pytest

from orchestune.issue_parsing import (
    PARENT_MARKER,
)
from orchestune.provisioning import (
    PlanMetadata,
    _parent_body,
    _resolve_parent_issue,
    provision_issues,
)
from tests.test_provisioning_support import (
    FakeForge,
)


class TestResolveParentIssue:
    def test_creates_new_parent_and_persists_when_none_persisted(self, tmp_path: Path):
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(
            "---\ntitle: 'My Big Rock'\nparent_issue_number: null\nsubtasks: []\n---\n",
            encoding="utf-8",
        )
        forge = FakeForge()
        metadata = PlanMetadata(
            title="My Big Rock",
            parent_issue_number=None,
            description="Epic details",
        )
        number, _ = _resolve_parent_issue(forge, metadata, plan_path)
        assert number in forge.issues
        assert forge.issues[number]["title"] == "[EPIC] My Big Rock"
        assert f"parent_issue_number: {number}" in plan_path.read_text(encoding="utf-8")

    def test_recovers_orphaned_parent(self, tmp_path: Path):
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(
            "---\ntitle: 'My Big Rock'\nparent_issue_number: null\nsubtasks: []\n---\n",
            encoding="utf-8",
        )
        forge = FakeForge()
        orphan_number = forge.create_issue(
            "[EPIC] My Big Rock", _parent_body("My Big Rock")
        )
        metadata = PlanMetadata(
            title="My Big Rock",
            parent_issue_number=None,
            description="",
        )
        number, _ = _resolve_parent_issue(forge, metadata, plan_path)
        assert number == orphan_number
        assert f"parent_issue_number: {orphan_number}" in plan_path.read_text(
            encoding="utf-8"
        )

    def test_explicit_parent_issue_normalizes_plain_title_and_missing_marker(
        self, tmp_path: Path
    ):
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(
            "---\ntitle: 'My Big Rock'\nparent_issue_number: null\nsubtasks: []\n---\n",
            encoding="utf-8",
        )
        forge = FakeForge()
        existing_number = forge.create_issue(
            "Human-filed epic", "Some pre-existing description written by a human."
        )
        metadata = PlanMetadata(
            title="My Big Rock", parent_issue_number=None, description=""
        )
        number, _ = _resolve_parent_issue(
            forge, metadata, plan_path, explicit_parent_issue=existing_number
        )
        assert number == existing_number
        assert forge.issues[existing_number]["title"] == "[EPIC] Human-filed epic"
        body = forge.issues[existing_number]["body"]
        assert body.startswith("Some pre-existing description written by a human.")
        assert PARENT_MARKER in body
        assert f"parent_issue_number: {existing_number}" in plan_path.read_text(
            encoding="utf-8"
        )

    def test_explicit_parent_issue_already_conformant_is_left_untouched(
        self, tmp_path: Path
    ):
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(
            "---\ntitle: 'My Big Rock'\nparent_issue_number: null\nsubtasks: []\n---\n",
            encoding="utf-8",
        )
        forge = FakeForge()
        plan_dict = {
            "title": "My Big Rock",
            "parent_issue_number": 100,
            "parent_issue_source": "adopted",
            "subtasks": [],
        }
        existing_number = forge.create_issue(
            "[EPIC] Already Proper",
            _parent_body("Already Proper", plan_data=plan_dict),
        )
        original_body = forge.issues[existing_number]["body"]
        metadata = PlanMetadata(
            title="My Big Rock", parent_issue_number=None, description=""
        )
        number, _ = _resolve_parent_issue(
            forge, metadata, plan_path, explicit_parent_issue=existing_number
        )
        assert number == existing_number
        assert forge.issues[existing_number]["title"] == "[EPIC] Already Proper"
        assert forge.issues[existing_number]["body"] == original_body

    def test_explicit_parent_issue_missing_number_raises(self, tmp_path: Path):
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(
            "---\ntitle: 'My Big Rock'\nparent_issue_number: null\nsubtasks: []\n---\n",
            encoding="utf-8",
        )
        forge = FakeForge()
        metadata = PlanMetadata(
            title="My Big Rock", parent_issue_number=None, description=""
        )
        with pytest.raises(
            RuntimeError, match="Adopted parent issue #999 does not exist"
        ):
            _resolve_parent_issue(forge, metadata, plan_path, explicit_parent_issue=999)

    def test_explicit_parent_issue_overrides_stale_persisted_value(
        self, tmp_path: Path
    ):
        plan_path = tmp_path / "plan.md"
        forge = FakeForge()
        stale_number = forge.create_issue(
            "[EPIC] My Big Rock", _parent_body("My Big Rock")
        )
        new_number = forge.create_issue("Human-filed epic", "Description.")
        plan_path.write_text(
            "---\ntitle: 'My Big Rock'\n"
            f"parent_issue_number: {stale_number}\nsubtasks: []\n---\n",
            encoding="utf-8",
        )
        metadata = PlanMetadata(
            title="My Big Rock", parent_issue_number=stale_number, description=""
        )
        number, _ = _resolve_parent_issue(
            forge, metadata, plan_path, explicit_parent_issue=new_number
        )
        assert number == new_number
        assert f"parent_issue_number: {new_number}" in plan_path.read_text(
            encoding="utf-8"
        )

    def test_adopted_parent_issue_reused_without_flag_even_if_title_differs(
        self, tmp_path: Path
    ):
        """#533: parent_issue_source: adopted の親は、titleがplanと異なっていても
        --parent-issue の再指定なしに再利用される。"""
        plan_path = tmp_path / "plan.md"
        forge = FakeForge()
        adopted_number = forge.create_issue(
            "[EPIC] Human-filed title",
            f"Description.\n\n{PARENT_MARKER}",
        )
        plan_path.write_text(
            "---\ntitle: 'My Big Rock'\n"
            f"parent_issue_number: {adopted_number}\n"
            "parent_issue_source: adopted\n"
            "subtasks: []\n---\n",
            encoding="utf-8",
        )
        metadata = PlanMetadata(
            title="My Big Rock",
            parent_issue_number=adopted_number,
            parent_issue_source="adopted",
            description="",
        )
        number, _ = _resolve_parent_issue(forge, metadata, plan_path)
        assert number == adopted_number
        assert len(forge.issues) == 1

    def test_adopted_parent_issue_nonexistent_raises_runtime_error(
        self, tmp_path: Path
    ):
        """#533: parent_issue_source: adopted の親が存在しない場合、新規作成へ倒れずエラーで停止する。"""
        plan_path = tmp_path / "plan.md"
        forge = FakeForge()
        plan_path.write_text(
            "---\ntitle: 'My Big Rock'\n"
            "parent_issue_number: 999\n"
            "parent_issue_source: adopted\n"
            "subtasks: []\n---\n",
            encoding="utf-8",
        )
        metadata = PlanMetadata(
            title="My Big Rock",
            parent_issue_number=999,
            parent_issue_source="adopted",
            description="",
        )
        with pytest.raises(
            RuntimeError, match="Adopted parent issue #999 does not exist"
        ):
            _resolve_parent_issue(forge, metadata, plan_path)
        assert len(forge.issues) == 0

    def test_adopted_parent_issue_normalizes_plain_title_and_missing_marker(
        self, tmp_path: Path
    ):
        """#533: 初回採用時、未正規化のIssueを --parent-issue で指定した場合、
        [EPIC] プレフィックスと PARENT_MARKER が付与されて正規化・採用される。"""
        plan_path = tmp_path / "plan.md"
        forge = FakeForge()
        existing_number = forge.create_issue(
            "Human-filed plain title", "Plain description without marker"
        )
        plan_path.write_text(
            "---\ntitle: 'My Big Rock'\n"
            "parent_issue_number: null\n"
            "subtasks: []\n---\n",
            encoding="utf-8",
        )
        metadata = PlanMetadata(
            title="My Big Rock",
            parent_issue_number=None,
            description="",
        )
        number, _ = _resolve_parent_issue(
            forge, metadata, plan_path, explicit_parent_issue=existing_number
        )
        assert number == existing_number
        assert (
            forge.issues[existing_number]["title"] == "[EPIC] Human-filed plain title"
        )
        assert PARENT_MARKER in forge.issues[existing_number]["body"]
        assert len(forge.issues) == 1

    def test_adopted_parent_auto_reuse_unconfirmed_plain_issue_raises_runtime_error(
        self, tmp_path: Path
    ):
        """#533: 自動再利用時、フロントマターで指定されたIssueがOrchestune形式でなければ
        誤爆防止のためエラーで停止し、--parent-issue の明示指定を促す。"""
        plan_path = tmp_path / "plan.md"
        forge = FakeForge()
        unconfirmed_number = forge.create_issue(
            "Unrelated plain issue", "Unrelated description"
        )
        plan_path.write_text(
            "---\ntitle: 'My Big Rock'\n"
            f"parent_issue_number: {unconfirmed_number}\n"
            "parent_issue_source: adopted\n"
            "subtasks: []\n---\n",
            encoding="utf-8",
        )
        metadata = PlanMetadata(
            title="My Big Rock",
            parent_issue_number=unconfirmed_number,
            parent_issue_source="adopted",
            description="",
        )
        with pytest.raises(RuntimeError, match="is not an Orchestune EPIC issue"):
            _resolve_parent_issue(forge, metadata, plan_path)

    def test_adopted_parent_issue_closed_raises_runtime_error(self, tmp_path: Path):
        """#533: parent_issue_source: adopted の親が CLOSED の場合、エラーで停止する。"""
        plan_path = tmp_path / "plan.md"
        forge = FakeForge()
        closed_number = forge.create_issue("Closed issue", "Description.")
        forge.issues[closed_number]["state"] = "CLOSED"
        plan_path.write_text(
            "---\ntitle: 'My Big Rock'\n"
            f"parent_issue_number: {closed_number}\n"
            "parent_issue_source: adopted\n"
            "subtasks: []\n---\n",
            encoding="utf-8",
        )
        metadata = PlanMetadata(
            title="My Big Rock",
            parent_issue_number=closed_number,
            parent_issue_source="adopted",
            description="",
        )
        with pytest.raises(RuntimeError, match="is closed; refusing to adopt"):
            _resolve_parent_issue(forge, metadata, plan_path)

    def test_explicit_parent_issue_persists_parent_issue_source_adopted(
        self, tmp_path: Path
    ):
        """#533: --parent-issue で指定された親は parent_issue_source: adopted として永続化される。"""
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(
            "---\ntitle: 'My Big Rock'\nparent_issue_number: null\nsubtasks: []\n---\n",
            encoding="utf-8",
        )
        forge = FakeForge()
        parent_number = forge.create_issue("Pre-existing", "Description.")
        metadata = PlanMetadata(
            title="My Big Rock", parent_issue_number=None, description=""
        )
        _resolve_parent_issue(
            forge, metadata, plan_path, explicit_parent_issue=parent_number
        )
        text = plan_path.read_text(encoding="utf-8")
        assert f"parent_issue_number: {parent_number}" in text
        assert "parent_issue_source: adopted" in text

    def test_derived_parent_issue_persists_parent_issue_source_derived(
        self, tmp_path: Path
    ):
        """#533: プランから新規作成された親は parent_issue_source: derived として永続化される。"""
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(
            "---\ntitle: 'My Big Rock'\nparent_issue_number: null\nsubtasks: []\n---\n",
            encoding="utf-8",
        )
        forge = FakeForge()
        metadata = PlanMetadata(
            title="My Big Rock", parent_issue_number=None, description=""
        )
        number, _ = _resolve_parent_issue(forge, metadata, plan_path)
        text = plan_path.read_text(encoding="utf-8")
        assert f"parent_issue_number: {number}" in text
        assert "parent_issue_source: derived" in text

    def test_load_plan_validates_parent_issue_source(self, tmp_path: Path):
        """#533: _load_plan が parent_issue_source を正しくパースし、不正な値を弾く。"""
        from orchestune.provisioning import _load_plan

        plan_path = tmp_path / "plan.md"
        plan_path.write_text(
            "---\ntitle: 'T'\nparent_issue_number: 10\nparent_issue_source: invalid\nsubtasks:\n  - id: task-a\n    description: 'd'\n---\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="parent_issue_source"):
            _load_plan(plan_path)

    def test_adopted_parent_without_parent_issue_number_raises_value_error(
        self, tmp_path: Path
    ):
        """#533: parent_issue_source: adopted なのに parent_issue_number が null の場合 _load_plan でエラー。"""
        from orchestune.provisioning import _load_plan

        plan_path = tmp_path / "plan.md"
        plan_path.write_text(
            "---\ntitle: 'T'\nparent_issue_source: adopted\nsubtasks:\n  - id: task-a\n    description: 'd'\n---\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="parent_issue_number"):
            _load_plan(plan_path)

    def test_adopted_parent_skips_rewriting_plan_when_already_persisted(
        self, tmp_path: Path
    ):
        """#533: 既に parent_issue_number と parent_issue_source: adopted が永続化されている場合、
        余計な plan 書き戻し (write_issue_numbers) をスキップする。"""
        plan_path = tmp_path / "plan.md"
        forge = FakeForge()
        parent_num = forge.create_issue("[EPIC] Title", f"Body.\n\n{PARENT_MARKER}")
        plan_path.write_text(
            f"---\ntitle: 'Title'\nparent_issue_number: {parent_num}\nparent_issue_source: adopted\nsubtasks: []\n---\n",
            encoding="utf-8",
        )
        metadata = PlanMetadata(
            title="Title",
            parent_issue_number=parent_num,
            parent_issue_source="adopted",
            description="",
        )
        # ファイルの更新日時を記録
        mtime_before = plan_path.stat().st_mtime_ns
        _resolve_parent_issue(forge, metadata, plan_path)
        mtime_after = plan_path.stat().st_mtime_ns
        assert mtime_before == mtime_after

    def test_provision_issues_second_run_without_flag_reuses_adopted_parent(
        self, tmp_path: Path, template_path: Path
    ):
        """#533: 1回目のprovision_issuesで--parent-issueを指定後、2回目に指定がなくても親と子を再利用する。"""
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(
            "---\ntitle: 'My Big Rock'\nparent_issue_number: null\nsubtasks:\n  - id: task-a\n    description: 'Task A'\n    depends_on: []\n---\n",
            encoding="utf-8",
        )
        forge = FakeForge()
        existing_parent = forge.create_issue("Human Pre-existing EPIC", "Body.")
        # 1回目の実行（--parent-issue を指定）
        res1 = provision_issues(
            plan_path,
            forge=forge,
            apply=True,
            template_path=template_path,
            parent_issue=existing_parent,
        )
        assert res1.parent_issue_number == existing_parent
        assert len(res1.created) == 1
        assert len(res1.reused) == 0
        child_num = res1.created["task-a"]

        # 2回目の実行（--parent-issue を渡さない）
        res2 = provision_issues(
            plan_path,
            forge=forge,
            apply=True,
            template_path=template_path,
            parent_issue=None,
        )
        assert res2.parent_issue_number == existing_parent
        assert len(res2.created) == 0
        assert len(res2.reused) == 1
        assert res2.reused["task-a"] == child_num
        # 新しい親Issueは作られていない（最初の1件 + 子Issue 1件 = 合計2件）
        assert len(forge.issues) == 2

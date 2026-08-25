"""#664: 親Issue本文への分解計画同期の安全弁。

- 本文がGitHubの上限（65,536文字）を超える書き込みは行わない。
- 同期に失敗した`orchestune provision`は成功終了しない（親Issueだけ古いまま、
  という状態に気付けなくなるため）。
- 同じ`issue_number`を計画ファイルへ二重書き込みしない。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import orchestune.provisioning.cli as cli_module
from orchestune.issue_parsing import DECOMPOSITION_PLAN_MARKER, PARENT_MARKER
from orchestune.provisioning.cli import main as provisioning_main
from orchestune.provisioning.flow import ProvisionResult
from orchestune.provisioning.plan import (
    GITHUB_ISSUE_BODY_LIMIT,
    sync_parent_decomposition_plan,
)
from tests.test_provisioning_support import _PLAN, _TEMPLATE, FakeForge


def _write_plan(tmp_path: Path) -> tuple[Path, Path]:
    plan_path = tmp_path / "decomposition_plan.md"
    plan_path.write_text(_PLAN, encoding="utf-8")
    template_path = tmp_path / "issue_template.md"
    template_path.write_text(_TEMPLATE, encoding="utf-8")
    return plan_path, template_path


def test_sync_refuses_to_write_body_over_github_limit(tmp_path, capsys):
    plan_path, _ = _write_plan(tmp_path)
    forge = FakeForge()
    oversized_prose = "x" * GITHUB_ISSUE_BODY_LIMIT
    parent = forge.create_issue(
        "[EPIC] Example", f"{oversized_prose}\n\n{PARENT_MARKER}"
    )

    assert sync_parent_decomposition_plan(forge, parent, plan_path) is False

    assert DECOMPOSITION_PLAN_MARKER not in forge.issues[parent]["body"]
    assert "65536" in capsys.readouterr().err


def test_sync_writes_body_within_limit(tmp_path):
    plan_path, _ = _write_plan(tmp_path)
    forge = FakeForge()
    parent = forge.create_issue("[EPIC] Example", f"prose\n\n{PARENT_MARKER}")

    assert sync_parent_decomposition_plan(forge, parent, plan_path) is True

    assert forge.issues[parent]["body"].count(DECOMPOSITION_PLAN_MARKER) == 1


def _stub_provision_result(monkeypatch, **overrides: object) -> None:
    """`main`のexit code決定だけを見るため、provision本体を差し替える。"""
    fields: dict = {
        "parent_issue_number": 100,
        "applied": True,
        "created": {"task-a": 101},
        "reused": {},
    }
    fields.update(overrides)
    result = ProvisionResult(**fields)
    monkeypatch.setattr(cli_module, "_provision_issues", lambda *args, **kwargs: result)


def test_provision_cli_exits_non_zero_when_plan_sync_fails(
    tmp_path, monkeypatch, capsys
):
    _stub_provision_result(monkeypatch, plan_synced=False)

    with pytest.raises(SystemExit) as excinfo:
        provisioning_main(["--plan", str(tmp_path / "decomposition_plan.md")])

    assert excinfo.value.code != 0
    assert "could not sync decomposition plan" in capsys.readouterr().out


def test_provision_cli_exits_zero_when_plan_sync_succeeds(tmp_path, monkeypatch):
    _stub_provision_result(monkeypatch, plan_synced=True)

    with pytest.raises(SystemExit) as excinfo:
        provisioning_main(["--plan", str(tmp_path / "decomposition_plan.md")])

    assert excinfo.value.code == 0


def test_provision_cli_exits_zero_on_dry_run(tmp_path, monkeypatch):
    """`--no-apply`は書き込みを伴わないため同期未実施でも成功終了する。"""
    _stub_provision_result(monkeypatch, applied=False, plan_synced=False)

    with pytest.raises(SystemExit) as excinfo:
        provisioning_main(
            ["--plan", str(tmp_path / "decomposition_plan.md"), "--no-apply"]
        )

    assert excinfo.value.code == 0


class _CrlfFakeForge(FakeForge):
    """GitHubと同じく本文をCRLFで保存・返却するForge（#486の再現条件）。"""

    def create_issue(self, title, body, labels=()):  # type: ignore[no-untyped-def]
        return super().create_issue(title, body.replace("\n", "\r\n"), labels)

    def update_issue_body(self, issue_number: int | str, body: str) -> None:
        super().update_issue_body(issue_number, body.replace("\n", "\r\n"))


def test_provision_against_crlf_forge_keeps_one_plan_block(tmp_path):
    """#486回帰: CRLF本文のforgeでも計画ブロックは1個のまま。

    修正前は「親Issue解決時 + サブタスク数」個のブロックが追記されていた。
    """
    plan_path, template_path = _write_plan(tmp_path)
    forge = _CrlfFakeForge()

    import orchestune.provisioning.flow as flow_module

    result = flow_module.provision_issues(
        plan_path,
        forge=forge,
        apply=True,
        template_path=template_path,
        repo_root=tmp_path,
    )

    parent_body = forge.issues[result.parent_issue_number]["body"]
    assert parent_body.count(DECOMPOSITION_PLAN_MARKER) == 1
    assert result.plan_synced is True


def test_issue_number_is_written_to_plan_once_per_subtask(tmp_path, monkeypatch):
    """作成直後と統合後で同じ値を2回書き戻していた重複を防ぐ。"""
    plan_path, template_path = _write_plan(tmp_path)
    calls: list[dict[str, int]] = []

    import orchestune.provisioning.flow as flow_module
    import orchestune.provisioning.subtasks as subtasks_module

    def _record(target, module):
        original = module.write_issue_numbers

        def wrapper(path, issue_numbers=None, **kwargs):
            if issue_numbers:
                calls.append(dict(issue_numbers))
            return original(path, issue_numbers, **kwargs)

        monkeypatch.setattr(module, "write_issue_numbers", wrapper)

    _record("flow", flow_module)
    _record("subtasks", subtasks_module)

    flow_module.provision_issues(
        plan_path,
        forge=FakeForge(),
        apply=True,
        template_path=template_path,
        repo_root=tmp_path,
    )

    written_subtask_ids = [key for call in calls for key in call]
    assert sorted(written_subtask_ids) == ["task-a", "task-b"]

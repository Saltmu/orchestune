from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from orchestune.dag_models import SubTask
from orchestune.issue_parsing import (
    FOOTPRINT_BLOCK_PATTERN,
    PARENT_MARKER,
    is_epic_issue,
)
from orchestune.models import IssueRecord
from orchestune.provisioning import (
    _build_provisioning_dag,
    _build_subtask_issue_body,
    _derive_labels,
    _render_issue_body,
    _subtask_id_from_body,
    _validate_template_identity_marker,
    main,
    provision_issues,
)
from tests.test_provisioning_support import (
    _PLAN,
    _TEMPLATE,
    FakeForge,
)


class TestDeriveLabels:
    def _subtask(
        self,
        *,
        depends_on: tuple[str, ...] = (),
        risk: bool = False,
        priority: str = "medium",
    ) -> SubTask:
        return SubTask(
            id="x",
            description="d",
            footprint=(),
            symbols=(),
            depends_on=depends_on,
            risk=risk,
            risk_reasons=(),
            priority=priority,
        )

    def test_no_dependencies_is_queued(self):
        labels = _derive_labels(self._subtask(depends_on=()), dependencies_done=False)
        assert labels == ("status:queued", "priority:medium")

    def test_unresolved_dependency_is_blocked(self):
        labels = _derive_labels(
            self._subtask(depends_on=("y",)), dependencies_done=False
        )
        assert labels[0] == "status:blocked"

    def test_resolved_dependencies_is_queued(self):
        labels = _derive_labels(
            self._subtask(depends_on=("y",)), dependencies_done=True
        )
        assert labels[0] == "status:queued"

    def test_risk_flag_appends_label(self):
        labels = _derive_labels(self._subtask(risk=True), dependencies_done=False)
        assert "risk:flagged" in labels

    def test_priority_label_reflects_field(self):
        labels = _derive_labels(self._subtask(priority="low"), dependencies_done=False)
        assert "priority:low" in labels


class TestRenderIssueBodySubtaskIdSafety:
    """#323 review (P2): a subtask id containing `:` or `#` must still
    round-trip through the rendered Footprint YAML block."""

    def _subtask(self, subtask_id: str) -> SubTask:
        return SubTask(
            id=subtask_id,
            description="d",
            footprint=(),
            symbols=(),
            depends_on=(),
            risk=False,
            risk_reasons=(),
        )

    @pytest.mark.parametrize(
        "subtask_id", ["auth: login", "task#1", "plain-id", "setup-database"]
    )
    def test_id_round_trips_through_rendered_yaml_block(self, subtask_id):
        # Use the real (multi-key) template: a bug that only manifests when
        # more YAML follows `subtask_id:` in the fence (see the `...`
        # document-terminator regression below) wouldn't show up in a
        # single-key fence.
        body = _render_issue_body(self._subtask(subtask_id), _TEMPLATE)

        # The heading keeps the raw (human-readable) id.
        assert body.startswith(f"# [FEAT] {subtask_id}: d")

        # The Footprint block is valid YAML and yields the exact id back,
        # including the fields declared after `subtask_id:` in the fence.
        match = FOOTPRINT_BLOCK_PATTERN.search(body)
        assert match
        data = yaml.safe_load(match.group(1))
        assert data["subtask_id"] == subtask_id
        assert "footprint" in data  # would be silently dropped by a `...` marker
        assert _subtask_id_from_body(body) == subtask_id

    def test_plain_id_scalar_has_no_yaml_document_terminator(self):
        """#323 review (P1): `yaml.dump("task-a")` emits a trailing `...`
        document-end marker for bare scalars; embedding that verbatim turns
        the rest of the Footprint block into an unparseable second
        document."""
        body = _render_issue_body(self._subtask("task-a"), _TEMPLATE)
        match = FOOTPRINT_BLOCK_PATTERN.search(body)
        assert match
        assert "..." not in match.group(1)

    def test_a_fields_own_value_is_not_reprocessed_as_a_template_token(self):
        """#323 review (P2): substituting one field at a time (as opposed to
        a single pass over the original template) means an earlier field's
        *value* can itself contain a literal `{{token}}`, which then gets
        corrupted by a later placeholder's substitution — even though that
        text was never part of the template."""
        subtask = SubTask(
            id="x",
            description="Preserve {{overview}} in the template",
            footprint=(),
            symbols=(),
            depends_on=(),
            risk=False,
            risk_reasons=(),
            overview="THE REAL OVERVIEW",
        )

        body = _render_issue_body(subtask, _TEMPLATE)

        assert "Preserve {{overview}} in the template" in body
        assert "Preserve THE REAL OVERVIEW in the template" not in body


class TestValidateTemplateIdentityMarker:
    """#485 review round 8 (P1): a custom `--template` missing metadata
    fields silently degrades data the fallback discovery/dependency
    resolution paths rely on, without any signal until it's too late."""

    def test_accepts_the_real_template(self, template_path: Path):
        # Must not raise: _TEMPLATE (used by the `template_path` fixture)
        # already includes every required placeholder.
        _validate_template_identity_marker(
            template_path.read_text(encoding="utf-8"), template_path
        )

    def test_rejects_template_missing_depends_on_placeholder(self, tmp_path: Path):
        template = (
            "# [FEAT] {{subtask_id}}\n\n"
            "```yaml\n"
            "subtask_id: {{subtask_id_yaml}}\n"
            "parent_issue_number: {{parent_issue_number}}\n"
            "```\n"
        )
        with pytest.raises(ValueError, match="depends_on"):
            _validate_template_identity_marker(template, tmp_path / "t.md")

    def test_rejects_template_missing_parent_issue_number_placeholder(
        self, tmp_path: Path
    ):
        template = (
            "# [FEAT] {{subtask_id}}\n\n"
            "```yaml\n"
            "subtask_id: {{subtask_id_yaml}}\n"
            "depends_on: {{depends_on}}\n"
            "```\n"
        )
        with pytest.raises(ValueError, match="parent_issue_number"):
            _validate_template_identity_marker(template, tmp_path / "t.md")

    def test_rejects_template_missing_subtask_id_placeholder(self, tmp_path: Path):
        template = (
            "# [FEAT]\n\n"
            "```yaml\n"
            "parent_issue_number: {{parent_issue_number}}\n"
            "depends_on: {{depends_on}}\n"
            "```\n"
        )
        with pytest.raises(ValueError, match="subtask_id"):
            _validate_template_identity_marker(template, tmp_path / "t.md")


class TestIsEpicIssue:
    """`--parent-issue`検証用: `is_epic_issue`がEPIC構造（タイトル接頭辞+本文
    マーカー）の有無だけを見て判定することを確認する。"""

    def _issue(self, *, title: str, body: str, state: str = "OPEN") -> IssueRecord:
        return IssueRecord(
            number=1,
            title=title,
            body=body,
            labels=(),
            created_at="",
            state=state,
        )

    def test_true_when_title_prefixed_and_marker_present(self):
        issue = self._issue(title="[EPIC] Some plan", body=f"...\n{PARENT_MARKER}")
        assert is_epic_issue(issue) is True

    def test_false_when_marker_missing(self):
        issue = self._issue(title="[EPIC] Some plan", body="no marker here")
        assert is_epic_issue(issue) is False

    def test_false_when_title_missing_prefix(self):
        issue = self._issue(title="[BUG] some bug", body=f"...\n{PARENT_MARKER}")
        assert is_epic_issue(issue) is False

    def test_false_when_neither_present(self):
        issue = self._issue(title="[BUG] some bug", body="no marker here")
        assert is_epic_issue(issue) is False

    def test_true_regardless_of_issue_state(self):
        """完了済み（クローズ済み）EPICへの後続処理を妨げないよう、状態は問わない。"""
        issue = self._issue(
            title="[EPIC] Some plan", body=f"...\n{PARENT_MARKER}", state="CLOSED"
        )
        assert is_epic_issue(issue) is True


class TestProvisionIssuesNoApply:
    def test_no_apply_makes_no_forge_calls_and_returns_preview(
        self, plan_path: Path, template_path: Path
    ):
        class ExplodingForge:
            def __getattr__(self, name):
                raise AssertionError(f"forge.{name} must not be called in --no-apply")

        result = provision_issues(
            plan_path, forge=ExplodingForge(), apply=False, template_path=template_path
        )

        assert result.applied is False
        assert result.created == {}
        assert result.reused == {}
        subtask_ids = [p.subtask_id for p in result.previews]
        assert subtask_ids == ["task-a", "task-b"]
        assert "[FEAT] task-a: Implement feature XX" == result.previews[0].title
        assert "status:queued" in result.previews[0].labels
        assert "status:blocked" in result.previews[1].labels

    def test_no_apply_honors_explicit_parent_issue_in_preview_bodies(
        self, plan_path: Path, template_path: Path
    ):
        """codex review (PR #506): `--no-apply --parent-issue N` must preview
        the bodies that the subsequent `--apply` run would actually file,
        not the plan's stale/absent persisted `parent_issue_number`."""

        class ExplodingForge:
            def __getattr__(self, name):
                raise AssertionError(f"forge.{name} must not be called in --no-apply")

        result = provision_issues(
            plan_path,
            forge=ExplodingForge(),
            apply=False,
            template_path=template_path,
            parent_issue=999,
        )

        assert result.applied is False
        for preview in result.previews:
            assert "parent_issue_number: 999" in preview.body

    def test_no_apply_rejects_invalid_parent_issue_before_previewing(
        self, plan_path: Path, template_path: Path
    ):
        """codex review (PR #506): a `--no-apply --parent-issue 0` dry run
        must fail the same way the corresponding `--apply` run would,
        instead of successfully previewing an invocation that can never
        actually be applied."""

        class ExplodingForge:
            def __getattr__(self, name):
                raise AssertionError(f"forge.{name} must not be called in --no-apply")

        with pytest.raises(ValueError):
            provision_issues(
                plan_path,
                forge=ExplodingForge(),
                apply=False,
                template_path=template_path,
                parent_issue=0,
            )


class TestMain:
    def test_no_apply_prints_preview_and_exits_0(
        self, plan_path: Path, template_path: Path, capsys
    ):
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "--plan",
                    str(plan_path),
                    "--template",
                    str(template_path),
                    "--no-apply",
                ]
            )
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "Dry run" in captured.out
        assert "task-a" in captured.out

    def test_apply_mode_prints_summary_and_exits_0(
        self, plan_path: Path, template_path: Path, capsys, monkeypatch
    ):
        forge = FakeForge()
        monkeypatch.setattr("orchestune.provisioning.GitHubForge", lambda: forge)
        with pytest.raises(SystemExit) as exc_info:
            main(["--plan", str(plan_path), "--template", str(template_path)])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "Parent issue:" in captured.out
        assert "task-a" in captured.out

    def test_resolves_repo_root_from_plan_location_not_cwd(
        self, plan_path: Path, template_path: Path, capsys
    ):
        """#404レビュー指摘: dag_cli.pyと同様に、--planファイル自身の位置を
        リポジトリルートとして orchestune.toml を読むこと（プロセスのcwdに
        依存しない）。plan_path/template_pathフィクスチャはtmp_path配下に
        あるが、テスト実行時のcwdはリポジトリルートのままである点がポイント。"""
        plan_path.parent.joinpath("orchestune.toml").write_text(
            "not valid toml [[[", encoding="utf-8"
        )

        with pytest.raises(SystemExit) as exc_info:
            main(["--plan", str(plan_path), "--template", str(template_path)])

        assert exc_info.value.code == 2
        captured = capsys.readouterr()
        assert "orchestune.toml" in captured.err

    def test_nested_plan_reads_config_from_git_repository_root(
        self, tmp_path: Path, template_path: Path, capsys
    ):
        """#418: `--plan`がリポジトリルートより下のネストしたパスを指す場合、
        `orchestune-dag`（`dag_cli._resolve_repo_root`、#410）と同様に`.git`を
        上位探索してリポジトリルート直下の`orchestune.toml`を読むべきで、
        `--plan`自身の親ディレクトリを単純に使ってはならない。"""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (tmp_path / "orchestune.toml").write_text(
            "not valid toml [[[", encoding="utf-8"
        )
        plans_dir = tmp_path / "plans"
        plans_dir.mkdir()
        plan_path = plans_dir / "decomposition_plan.md"
        plan_path.write_text(_PLAN, encoding="utf-8")

        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "--plan",
                    str(plan_path),
                    "--template",
                    str(template_path),
                    "--no-apply",
                ]
            )

        assert exc_info.value.code == 2
        captured = capsys.readouterr()
        assert "orchestune.toml" in captured.err

    def test_missing_plan_file_exits_1_with_error(self, tmp_path: Path, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(["--plan", str(tmp_path / "nonexistent.md")])
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Error:" in captured.err


def test_missing_subtask_id_yaml_placeholder_raises(tmp_path: Path, plan_path: Path):
    """#323 review round 7 (P2): without `{{subtask_id_yaml}}`, no rendered
    issue body can ever contain a `subtask_id:` line, so
    `_subtask_id_from_body` could never succeed and idempotency would break
    silently — every future run would create a duplicate issue for every
    subtask instead of failing loudly up front."""
    bad_template = tmp_path / "bad_template.md"
    bad_template.write_text(
        "# {{subtask_id}}: {{description}}\n{{overview}}\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="subtask_id_yaml"):
        provision_issues(plan_path, forge=FakeForge(), template_path=bad_template)


def test_subtask_id_yaml_placeholder_outside_the_footprint_fence_raises(
    tmp_path: Path, plan_path: Path
):
    """#323 review round 8 (P2): the raw token `{{subtask_id_yaml}}` can be
    present in the template text (satisfying a naive substring check) while
    sitting outside any Footprint YAML fence, so no rendered body can ever
    yield an extractable `subtask_id` via `_subtask_id_from_body`. The
    validation must render a probe and check extraction actually succeeds,
    not just that the token string appears somewhere."""
    bad_template = tmp_path / "bad_template.md"
    bad_template.write_text(
        "# {{subtask_id}}: {{description}}\n"
        "Not inside a fence: {{subtask_id_yaml}}\n"
        "{{overview}}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="subtask_id"):
        provision_issues(plan_path, forge=FakeForge(), template_path=bad_template)


def test_template_that_redundantly_quotes_the_placeholder_raises(
    tmp_path: Path, plan_path: Path
):
    """#323 review round 9 (P2): a custom template that wraps
    `{{subtask_id_yaml}}` in its own literal quotes (`subtask_id:
    "{{subtask_id_yaml}}"`) double-quotes any id that already needed
    quoting (e.g. one containing `:`), corrupting the fence for real
    subtask ids. A plain-word probe id round-trips fine either way and
    can't detect this, so the probe must use an id that forces
    `_yaml_scalar` to quote it."""
    bad_template = tmp_path / "bad_template.md"
    bad_template.write_text(
        "# {{subtask_id}}: {{description}}\n"
        "```yaml\n"
        'subtask_id: "{{subtask_id_yaml}}"\n'
        "footprint: {{footprint}}\n"
        "symbols: {{symbols}}\n"
        "depends_on: {{depends_on}}\n"
        "```\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="subtask_id"):
        provision_issues(plan_path, forge=FakeForge(), template_path=bad_template)


def test_missing_title_raises(tmp_path: Path, template_path: Path):
    path = tmp_path / "decomposition_plan.md"
    path.write_text(
        "---\nsubtasks:\n  - id: task-a\n    description: x\n---\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="title"):
        provision_issues(path, forge=FakeForge(), template_path=template_path)


class TestRejectsInvalidIssueNumbers:
    """#323 review (P2): a malformed issue_number must never be silently
    coerced (e.g. `int(1.5) == 1`, `int(True) == 1`) into treating an
    unrelated real issue as the subtask/parent."""

    @pytest.mark.parametrize("bad_value", ["1.5", "true", "-1", "0", '"abc"'])
    def test_rejects_bad_subtask_issue_number(
        self, tmp_path: Path, template_path: Path, bad_value
    ):
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            "  - id: task-a\n"
            '    description: "d"\n'
            f"    issue_number: {bad_value}\n"
            "---\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError):
            provision_issues(path, forge=FakeForge(), template_path=template_path)

    @pytest.mark.parametrize("bad_value", ["1.5", "true", "-1", "0"])
    def test_rejects_bad_parent_issue_number(
        self, tmp_path: Path, template_path: Path, bad_value
    ):
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            f"parent_issue_number: {bad_value}\n"
            "subtasks:\n"
            "  - id: task-a\n"
            '    description: "d"\n'
            "---\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError):
            provision_issues(path, forge=FakeForge(), template_path=template_path)


class TestBuildProvisioningDag:
    def test_builds_dag_with_config_and_threshold(self, tmp_path: Path):
        (tmp_path / "orchestune.toml").write_text(
            "[tool.orchestune]\ndag_similarity_threshold = 0.8\n",
            encoding="utf-8",
        )
        subtasks = [
            SubTask(
                id="task-1",
                description="task 1",
                footprint=("a.py",),
                symbols=(),
                depends_on=(),
                risk=False,
                risk_reasons=(),
            ),
            SubTask(
                id="task-2",
                description="task 2",
                footprint=("a.py",),
                symbols=(),
                depends_on=(),
                risk=False,
                risk_reasons=(),
            ),
        ]
        dag = _build_provisioning_dag(subtasks, tmp_path)
        assert set(dag.subtasks) == {"task-1", "task-2"}


class TestBuildSubtaskIssueBody:
    def test_renders_body_and_appends_missing_symbol_warning(self, tmp_path: Path):
        file_path = tmp_path / "foo.py"
        file_path.write_text("class Existing:\n    pass\n", encoding="utf-8")
        subtask = SubTask(
            id="task-a",
            description="test subtask",
            footprint=(str(file_path.relative_to(tmp_path)),),
            symbols=("Existing", "MissingSymbol"),
            depends_on=(),
            risk=False,
            risk_reasons=(),
        )
        body = _build_subtask_issue_body(subtask, _TEMPLATE, tmp_path)
        assert "# [FEAT] task-a: test subtask" in body
        assert "⚠️ **symbols未検出**" in body
        assert "`MissingSymbol`" in body
        assert "`Existing`" not in body.split("⚠️ **symbols未検出**")[1]

    def test_persists_parent_issue_number_in_body_metadata(self, tmp_path: Path):
        """#485: 親番号を本文metadataにも永続化しておくことで、ネイティブ
        Sub-issue関係を作れない環境でもDispatcherが子Issueを発見できる。"""
        subtask = SubTask(
            id="task-a",
            description="d",
            footprint=(),
            symbols=(),
            depends_on=(),
            risk=False,
            risk_reasons=(),
        )
        body = _build_subtask_issue_body(subtask, _TEMPLATE, tmp_path, 100)
        assert "parent_issue_number: 100" in body

    def test_omits_parent_issue_number_when_not_yet_resolved(self, tmp_path: Path):
        subtask = SubTask(
            id="task-a",
            description="d",
            footprint=(),
            symbols=(),
            depends_on=(),
            risk=False,
            risk_reasons=(),
        )
        body = _build_subtask_issue_body(subtask, _TEMPLATE, tmp_path)
        assert "parent_issue_number: null" in body

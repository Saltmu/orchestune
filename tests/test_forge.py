import ast
import json
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestune.dispatch_config import DispatcherConfig
from orchestune.forge import (
    REQUIRED_LABELS,
    BootstrapResult,
    Forge,
    ForgeAuthError,
    ForgeError,
    GitHubForge,
    IssueForge,
    LabelSpec,
    PullRequestForge,
    RepoAdminForge,
)
from orchestune.forge_admin import GitHubRepoAdminMixin
from orchestune.forge_issues import GitHubIssueMixin
from orchestune.forge_prs import GitHubPullRequestMixin
from orchestune.models import IssueRecord
from orchestune.validation import validate_label as _validate_label


class TestGitHubForgeCheckAuth:
    def test_raises_when_gh_binary_missing(self):
        with (
            patch("orchestune.forge.shutil.which", return_value=None),
            patch("orchestune.forge.subprocess.run") as mock_run,
        ):
            with pytest.raises(ForgeAuthError, match="gh"):
                GitHubForge().check_auth()
            mock_run.assert_not_called()

    def test_passes_when_gh_auth_status_succeeds(self):
        with (
            patch("orchestune.forge.shutil.which", return_value="/usr/bin/gh"),
            patch("orchestune.forge.subprocess.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            GitHubForge().check_auth()

        called_args = mock_run.call_args.args[0]
        assert called_args == ["gh", "auth", "status"]

    def test_raises_when_gh_auth_status_fails(self):
        with (
            patch("orchestune.forge.shutil.which", return_value="/usr/bin/gh"),
            patch("orchestune.forge.subprocess.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="not logged in"
            )
            with pytest.raises(ForgeAuthError, match="not logged in"):
                GitHubForge().check_auth()

    def test_check_auth_does_not_use_check_true(self):
        with (
            patch("orchestune.forge.shutil.which", return_value="/usr/bin/gh"),
            patch("orchestune.forge.subprocess.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            GitHubForge().check_auth()

        assert mock_run.call_args.kwargs.get("check") is not True


class TestGitHubForgeEnsureLabels:
    def _labels(self):
        return (
            LabelSpec("status:queued", "0E8A16", "queued"),
            LabelSpec("status:blocked", "B60205", "blocked"),
            LabelSpec("priority:high", "D93F0B", "high priority"),
        )

    def test_lists_existing_labels_once(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout='[{"name": "status:queued"}]',
                    stderr="",
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
            ]
            GitHubForge().ensure_labels(self._labels())

        list_calls = [
            call for call in mock_run.call_args_list if "list" in call.args[0]
        ]
        assert len(list_calls) == 1

    def test_creates_only_missing_labels(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout='[{"name": "status:queued"}]',
                    stderr="",
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
            ]
            result = GitHubForge().ensure_labels(self._labels())

        create_calls = [
            call for call in mock_run.call_args_list if "create" in call.args[0]
        ]
        assert len(create_calls) == 2
        assert set(result.created_labels) == {"status:blocked", "priority:high"}
        assert set(result.existing_labels) == {"status:queued"}

    def test_ensure_labels_validates_label_names_before_subprocess(self):
        bad_labels = (LabelSpec("bad label; rm -rf", "000000", "bad"),)
        with patch("orchestune.forge.subprocess.run") as mock_run:
            with pytest.raises(ValueError, match="ラベル"):
                GitHubForge().ensure_labels(bad_labels)
            mock_run.assert_not_called()

    def test_create_label_command_shape(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="[]", stderr=""
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
            ]
            GitHubForge().ensure_labels((LabelSpec("risk:flagged", "E11D21", "risky"),))

        create_call = [
            call for call in mock_run.call_args_list if "create" in call.args[0]
        ][0]
        argv = create_call.args[0]
        assert argv == [
            "gh",
            "label",
            "create",
            "risk:flagged",
            "--color",
            "E11D21",
            "--description",
            "risky",
        ]
        assert "--force" not in argv

    def test_ensure_labels_is_idempotent_when_all_labels_exist(self):
        labels = self._labels()
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=str([{"name": label.name} for label in labels]).replace(
                    "'", '"'
                ),
                stderr="",
            )
            result = GitHubForge().ensure_labels(labels)

        create_calls = [
            call for call in mock_run.call_args_list if "create" in call.args[0]
        ]
        assert create_calls == []
        assert result.created_labels == ()
        assert set(result.existing_labels) == {label.name for label in labels}

    def test_detects_required_label_beyond_first_100(self):
        # 100件を超えるlabelsが存在し、必須labelが101件目(一覧の末尾)にある
        # ケースでも既存として検出され、誤って再作成されないことを確認する。
        existing = [{"name": f"other-label-{i}"} for i in range(99)]
        existing.append({"name": "status:queued"})
        existing.append({"name": "status:blocked"})
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps(existing),
                    stderr="",
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
            ]
            result = GitHubForge().ensure_labels(self._labels())

        list_call = [
            call for call in mock_run.call_args_list if "list" in call.args[0]
        ][0]
        argv = list_call.args[0]
        limit_index = argv.index("--limit") + 1
        assert int(argv[limit_index]) > 100

        create_calls = [
            call for call in mock_run.call_args_list if "create" in call.args[0]
        ]
        assert {call.args[0][3] for call in create_calls} == {"priority:high"}
        assert "status:queued" in result.existing_labels

    def test_raises_forge_error_when_label_list_may_be_truncated(self):
        from orchestune.forge import _LABEL_LIST_LIMIT

        existing = [{"name": f"label-{i}"} for i in range(_LABEL_LIST_LIMIT)]
        with (
            patch("orchestune.forge.subprocess.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(existing),
                stderr="",
            )
            with pytest.raises(ForgeError, match="打ち切"):
                GitHubForge().ensure_labels(self._labels())

        create_calls = [
            call for call in mock_run.call_args_list if "create" in call.args[0]
        ]
        assert create_calls == []


class TestRequiredLabels:
    _EXPECTED_NAMES = {
        "status:queued",
        "status:blocked",
        "status:blocked-recompute",
        "status:blocked-human-review",
        "status:done",
        "status:external-lock",
        "status:force-serial",
        "status:in-progress",
        "status:manual-merge-required",
        "status:not-needed",
        "priority:high",
        "priority:medium",
        "priority:low",
        "risk:flagged",
        "progress:partial",
        "not-needed-review:passed",
        "not-needed-review:failed",
        "integration:included",
    }

    def test_required_labels_contains_all_canonical_labels(self):
        assert {label.name for label in REQUIRED_LABELS} == self._EXPECTED_NAMES

    def test_required_labels_all_pass_validate_label(self):
        for label in REQUIRED_LABELS:
            assert _validate_label(label.name) == label.name

    def test_required_labels_have_non_empty_descriptions_and_valid_colors(self):
        for label in REQUIRED_LABELS:
            assert label.description
            assert len(label.color) == 6
            int(label.color, 16)


class TestBootstrapResult:
    def test_is_frozen_dataclass_with_expected_fields(self):
        result = BootstrapResult(created_labels=("a",), existing_labels=("b",))
        assert result.created_labels == ("a",)
        assert result.existing_labels == ("b",)
        with pytest.raises(FrozenInstanceError):
            result.created_labels = ("c",)


class TestForgeProtocols:
    def test_github_forge_composes_focused_implementation_mixins(self):
        assert issubclass(GitHubForge, GitHubIssueMixin)
        assert issubclass(GitHubForge, GitHubPullRequestMixin)
        assert issubclass(GitHubForge, GitHubRepoAdminMixin)

    def test_legacy_facade_resolves_to_extracted_implementations(self):
        assert GitHubForge.list_issues_by_label is GitHubIssueMixin.list_issues_by_label
        assert GitHubForge.list_prs is GitHubPullRequestMixin.list_prs
        assert GitHubForge.ensure_labels is GitHubRepoAdminMixin.ensure_labels

    def test_github_forge_implements_each_focused_protocol(self):
        forge = GitHubForge()

        assert isinstance(forge, IssueForge)
        assert isinstance(forge, PullRequestForge)
        assert isinstance(forge, RepoAdminForge)
        assert isinstance(forge, Forge)

    def test_dispatcher_config_creates_default_github_forge(self):
        assert isinstance(DispatcherConfig().forge, GitHubForge)

    def test_dispatcher_config_accepts_fake_forge_without_mock_patch(self):
        class FakeForge(GitHubForge):
            def list_issues_by_label(
                self, label: str, state: str = "open", limit: int = 1000
            ) -> list[IssueRecord]:
                return [
                    IssueRecord(
                        number=290,
                        title=label,
                        body=state,
                        labels=(),
                        created_at=str(limit),
                    )
                ]

        fake_forge = FakeForge()
        config = DispatcherConfig(forge=fake_forge)

        assert config.forge is fake_forge
        assert config.forge.list_issues_by_label("status:queued") == [
            IssueRecord(
                number=290,
                title="status:queued",
                body="open",
                labels=(),
                created_at="1000",
            )
        ]

    def test_gh_command_literals_exist_only_in_focused_forge_modules(self):
        package_root = Path(__file__).parents[1] / "orchestune"
        modules_with_gh_commands: set[str] = set()

        for path in package_root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.List | ast.Tuple) or not node.elts:
                    continue
                command = node.elts[0]
                if isinstance(command, ast.Constant) and command.value == "gh":
                    modules_with_gh_commands.add(path.name)

        assert modules_with_gh_commands == {
            "forge_admin.py",
            "forge_issues.py",
            "forge_prs.py",
        }

import subprocess
from dataclasses import FrozenInstanceError
from unittest.mock import patch

import pytest

from orchestune.forge import (
    REQUIRED_LABELS,
    BootstrapResult,
    ForgeAuthError,
    GitHubForge,
    LabelSpec,
)
from orchestune.github import _validate_label


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

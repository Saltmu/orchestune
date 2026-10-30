"""#1304: a label-only `status:force-serial` survives the claim's label strip.

`claim` removes every `status:*` label except `status:in-progress` (the
`(LAUNCH, CLAIM) strip=True` rule). Before that, the single Issue-body write
that publishes the ownership metadata also carries `forced_serial: true`, so the
flag outlives the label without an extra forge call.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from orchestune.claim.contracts import (
    ClaimFailureReason,
    ClaimRequest,
    OwnerKind,
    ReservationKind,
)
from orchestune.claim.issue_metadata import (
    publish_claim_ownership_to_issue,
    update_issue_claim_metadata,
)
from orchestune.claim.service import claim_task
from orchestune.issue_parsing import (
    FOOTPRINT_BLOCK_PATTERN,
    recovery_counters_from_body,
)
from orchestune.labels import StatusLabel
from orchestune.worktree_ops.preparation import WorktreePreparation
from tests.claim_helpers import MockForge, _make_issue

ISSUE = 1304


def _body(*extra: str) -> str:
    lines = "".join(f"{line}\n" for line in extra)
    return (
        "## Footprint\n\n```yaml\n"
        f"subtask_id: t\nfootprint: [orchestune/foo.py]\n{lines}```\n"
    )


def _fence(body: str) -> dict[str, object]:
    match = FOOTPRINT_BLOCK_PATTERN.search(body)
    assert match is not None
    data = yaml.safe_load(match.group(1))
    assert isinstance(data, dict)
    return data


def _update(body: str, **kwargs: bool) -> str:
    return update_issue_claim_metadata(
        body, "interactive", "claim-1", "footprint", **kwargs
    )


class TestUpdateIssueClaimMetadataKeepsLayout:
    """#1465: the claim appends its keys without re-sorting or re-wrapping the fence."""

    def test_only_the_claim_keys_are_appended(self) -> None:
        description = "長い説明文が一行で書かれている。" + "あいうえお" * 30

        def body(*extra: str) -> str:
            tail = "".join(f"{line}\n" for line in extra)
            return (
                "Intro text.\n\n## Footprint\n\n```yaml\n"
                "subtask_id: example\n"
                f"description: {description}\n"
                "depends_on: []\n"
                "footprint:\n"
                "  - tests/a.py\n"
                "  - tests/b.py\n"
                f"{tail}```\n\nTrailing text.\n"
            )

        result = update_issue_claim_metadata(
            body(), "interactive", "claim-1", "footprint"
        )

        assert result == body(
            "owner_kind: interactive",
            "claim_id: claim-1",
            "reservation_kind: footprint",
        )


class TestUpdateIssueClaimMetadata:
    @pytest.mark.parametrize(
        "body",
        [
            _body(),
            _body("forced_serial: false"),
            _body("forced_serial: true"),
            _body("recompute_count: 1"),
            "no fence at all\n",
        ],
        ids=["no-key", "explicit-false", "already-true", "with-count", "no-fence"],
    )
    def test_forced_serial_true_is_written(self, body: str) -> None:
        assert _fence(_update(body, forced_serial=True))["forced_serial"] is True

    @pytest.mark.parametrize(
        "body", [_body(), _body("recompute_count: 1"), "no fence at all\n"]
    )
    def test_forced_serial_false_adds_no_key(self, body: str) -> None:
        assert "forced_serial" not in _fence(_update(body))

    @pytest.mark.parametrize("flag", [False, True])
    def test_existing_true_is_never_lowered(self, flag: bool) -> None:
        body = _body("forced_serial: true")
        assert _fence(_update(body, forced_serial=flag))["forced_serial"] is True

    def test_explicit_false_is_kept_when_the_label_is_absent(self) -> None:
        body = _body("forced_serial: false")
        assert _fence(_update(body))["forced_serial"] is False

    def test_recompute_count_is_untouched(self) -> None:
        body = _body("recompute_count: 2")
        updated = _update(body, forced_serial=True)
        assert recovery_counters_from_body(updated) == (2, True)
        assert recovery_counters_from_body(_update(body)) == (2, False)
        assert "recompute_count" not in _fence(_update(_body(), forced_serial=True))


class TestPublishClaimOwnership:
    def _publish(self, labels: tuple[str, ...]) -> MockForge:
        issue = _make_issue(number=ISSUE, labels=labels)
        forge = MockForge({ISSUE: issue})
        error = publish_claim_ownership_to_issue(
            forge, issue, OwnerKind.INTERACTIVE, "claim-1", ReservationKind.FOOTPRINT
        )
        assert error is None
        return forge

    def test_the_label_is_moved_into_the_body_in_one_write(self) -> None:
        forge = self._publish((StatusLabel.QUEUED, StatusLabel.FORCE_SERIAL))
        assert len(forge.bodies_updated) == 1
        assert recovery_counters_from_body(forge.bodies_updated[0][1]) == (0, True)

    def test_without_the_label_the_body_gains_no_forced_serial_key(self) -> None:
        forge = self._publish((StatusLabel.QUEUED,))
        assert len(forge.bodies_updated) == 1
        assert "forced_serial" not in _fence(forge.bodies_updated[0][1])


def _claim(claim_env: dict[str, Path], forge: MockForge):
    worktree_path = claim_env["worktrees_dir"] / f"claude-issue-{ISSUE}-test-task"

    def prepare(*args, **kwargs):
        worktree_path.mkdir(parents=True, exist_ok=True)
        return WorktreePreparation(
            worktree_path=worktree_path,
            branch=f"claude/issue-{ISSUE}-test-task",
            accepted=True,
            created=True,
            base_sha="sha",
        )

    with (
        patch("orchestune.claim.service.run_git", return_value=MagicMock(returncode=0)),
        patch("orchestune.claim.service.prepare_task_worktree", side_effect=prepare),
    ):
        request = ClaimRequest(issue_number=ISSUE, state_path=claim_env["state_path"])
        return claim_task(request, forge=forge, cwd=claim_env["repo_root"])


class TestClaimKeepsLabelOnlyForceSerial:
    def test_the_flag_outlives_the_stripped_label(
        self, claim_env: dict[str, Path]
    ) -> None:
        labels = (StatusLabel.QUEUED, StatusLabel.FORCE_SERIAL)
        forge = MockForge({ISSUE: _make_issue(number=ISSUE, labels=labels)})
        outcome = _claim(claim_env, forge)
        assert outcome.success is True
        assert (ISSUE, StatusLabel.FORCE_SERIAL) in forge.labels_removed
        assert StatusLabel.FORCE_SERIAL not in forge.get_issue_labels(ISSUE)
        issue = forge.get_issue(ISSUE)
        assert issue is not None
        assert recovery_counters_from_body(issue.body) == (0, True)
        assert len(forge.bodies_updated) == 1

    def test_no_label_adds_no_key_and_the_same_single_write(
        self, claim_env: dict[str, Path]
    ) -> None:
        forge = MockForge({ISSUE: _make_issue(number=ISSUE)})
        assert _claim(claim_env, forge).success is True
        assert len(forge.bodies_updated) == 1
        issue = forge.get_issue(ISSUE)
        assert issue is not None
        assert "forced_serial" not in _fence(issue.body)

    def test_a_body_write_failure_fails_the_claim_and_keeps_the_label(
        self, claim_env: dict[str, Path]
    ) -> None:
        labels = (StatusLabel.QUEUED, StatusLabel.FORCE_SERIAL)
        forge = MockForge({ISSUE: _make_issue(number=ISSUE, labels=labels)})
        forge.fail_update_body = True
        outcome = _claim(claim_env, forge)
        assert outcome.success is False
        assert outcome.failure is not None
        assert outcome.failure.reason == ClaimFailureReason.STATE_SAVE_FAILED
        assert (ISSUE, StatusLabel.FORCE_SERIAL) not in forge.labels_removed
        assert StatusLabel.FORCE_SERIAL in forge.get_issue_labels(ISSUE)

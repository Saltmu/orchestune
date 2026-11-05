"""Completion exception boundaries preserve expected failures and expose bugs."""

import logging
import subprocess
from types import SimpleNamespace

import pytest


def test_preflight_does_not_swallow_unexpected_forge_runtime_error():
    from orchestune.complete.preflight import _fetch_pr

    class BrokenForge:
        def get_pull_request(self, pr_number):
            raise RuntimeError("adapter bug")

    with pytest.raises(RuntimeError, match="adapter bug"):
        _fetch_pr(BrokenForge(), 1)


@pytest.mark.parametrize(
    "lookup", ["get_pull_request", "list_prs", "list_prs_fallback"]
)
def test_preflight_reports_ordinary_lookup_timeout_as_unavailable(lookup):
    from unittest.mock import Mock

    from orchestune.complete.preflight import _fetch_pr

    timeout = subprocess.TimeoutExpired(["gh", "pr", "view"], timeout=30)
    if lookup == "get_pull_request":
        forge = SimpleNamespace(get_pull_request=Mock(side_effect=timeout))
    elif lookup == "list_prs_fallback":
        forge = SimpleNamespace(list_prs=Mock(side_effect=[TypeError(), timeout]))
    else:
        forge = SimpleNamespace(list_prs=Mock(side_effect=timeout))

    pr, available, error = _fetch_pr(forge, 12)

    assert pr is None
    assert available is True
    assert "timed out" in error


def test_status_observation_does_not_swallow_unexpected_forge_runtime_error():
    from orchestune.complete.status_labels import _completion_observe

    class BrokenForge:
        def get_issue_labels(self, issue_number):
            raise RuntimeError("adapter bug")

    with pytest.raises(RuntimeError, match="adapter bug"):
        _completion_observe(BrokenForge(), 1, "status:done", lambda: True)


def test_status_mutation_does_not_swallow_unexpected_forge_runtime_error():
    from orchestune.complete.status_labels import _completion_mutate

    class BrokenForge:
        def add_label(self, issue_number, label):
            raise RuntimeError("adapter bug")

    with pytest.raises(RuntimeError, match="adapter bug"):
        _completion_mutate(
            BrokenForge(), 1, "status:done", lambda: True, "add", "status:done"
        )


def test_status_mutation_forge_error_is_observed_by_readback():
    from orchestune.complete.status_labels import _completion_mutate
    from orchestune.forge import ForgeError

    class UnavailableForge:
        def add_label(self, issue_number, label):
            raise ForgeError("write response lost")

        def get_issue_labels(self, issue_number):
            return ()

    # The expected Forge failure is intentionally unlogged because read-back
    # decides the result; this test fixes that observation contract.
    assert (
        _completion_mutate(
            UnavailableForge(), 1, "status:done", lambda: True, "add", "status:done"
        )
        == ()
    )


def test_status_mutation_subprocess_error_is_observed_by_readback():
    from orchestune.complete.status_labels import _completion_mutate

    class UnavailableForge:
        def add_label(self, issue_number, label):
            raise subprocess.CalledProcessError(1, ["gh", "issue", "edit"])

        def get_issue_labels(self, issue_number):
            return ("status:done",)

    assert _completion_mutate(
        UnavailableForge(), 1, "status:done", lambda: True, "add", "status:done"
    ) == ("status:done",)


def test_status_mutation_timeout_is_observed_by_readback():
    from orchestune.complete.status_labels import _completion_mutate

    class UnavailableForge:
        def add_label(self, issue_number, label):
            raise subprocess.TimeoutExpired(["gh", "issue", "edit"], timeout=30)

        def get_issue_labels(self, issue_number):
            return ("status:done",)

    assert _completion_mutate(
        UnavailableForge(), 1, "status:done", lambda: True, "add", "status:done"
    ) == ("status:done",)


def test_follow_up_lookup_does_not_swallow_unexpected_forge_runtime_error():
    from orchestune.complete.review_evidence import _FollowUps

    class BrokenForge:
        def get_issue_reference(self, number):
            raise RuntimeError("adapter bug")

    with pytest.raises(RuntimeError, match="adapter bug"):
        _FollowUps(BrokenForge(), frozenset())._lookup(2)


def test_follow_up_response_validation_failure_does_not_hide_later_valid_issue():
    from orchestune.complete.review_evidence import _FollowUps

    class Forge:
        def __init__(self):
            self.lookups = []

        def get_issue_reference(self, number):
            self.lookups.append(number)
            if number == 2:
                raise ValueError("unusable Issue payload")
            return SimpleNamespace(is_pull_request=False, state="OPEN")

    forge = Forge()
    _FollowUps(forge, frozenset()).verify("follow-up #2 and #3")

    assert forge.lookups == [2, 3]


def test_follow_up_timeout_does_not_hide_later_valid_issue():
    from orchestune.complete.review_evidence import _FollowUps

    class Forge:
        def __init__(self):
            self.lookups = []

        def get_issue_reference(self, number):
            self.lookups.append(number)
            if number == 2:
                raise subprocess.TimeoutExpired(["gh", "api"], timeout=30)
            return SimpleNamespace(is_pull_request=False, state="OPEN")

    forge = Forge()
    _FollowUps(forge, frozenset()).verify("follow-up #2 and #3")

    assert forge.lookups == [2, 3]


@pytest.mark.parametrize(
    "error",
    [
        subprocess.TimeoutExpired(["gh", "api"], timeout=30),
        ValueError("invalid response JSON"),
    ],
)
def test_escalation_comment_response_failure_is_confirmed_by_readback(error):
    from unittest.mock import Mock

    from orchestune.complete.policy_actions import _EscalationForge

    body = "escalation comment"
    forge = SimpleNamespace(
        list_all_issue_comments=Mock(side_effect=[[], [{"body": body}]]),
        create_issue_comment=Mock(side_effect=error),
    )

    _EscalationForge(forge, body).add_comment(1, body)

    assert forge.list_all_issue_comments.call_count == 2


def test_unexpected_completion_failure_is_logged_and_returned(monkeypatch, caplog):
    from orchestune.complete import service
    from orchestune.complete.contracts import CompleteRequest

    request = CompleteRequest.done(
        issue_number=1,
        pr=2,
        claim_id="claim-test",
        owner_token="owner-token",
    )
    monkeypatch.setattr(
        service,
        "_complete",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("unexpected failure")),
    )

    with caplog.at_level(logging.ERROR):
        result = service.complete_task(request)

    assert not result.success
    assert "unexpected failure" in result.failure.message
    assert "Unexpected completion failure" in caplog.text

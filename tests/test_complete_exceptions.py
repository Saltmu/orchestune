"""Completion exception boundaries preserve expected failures and expose bugs."""

import logging

import pytest


def test_preflight_does_not_swallow_unexpected_forge_runtime_error():
    from orchestune.complete.preflight import _fetch_pr

    class BrokenForge:
        def get_pull_request(self, pr_number):
            raise RuntimeError("adapter bug")

    with pytest.raises(RuntimeError, match="adapter bug"):
        _fetch_pr(BrokenForge(), 1)


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


def test_follow_up_lookup_does_not_swallow_unexpected_forge_runtime_error():
    from orchestune.complete.review_evidence import _FollowUps

    class BrokenForge:
        def get_issue_reference(self, number):
            raise RuntimeError("adapter bug")

    with pytest.raises(RuntimeError, match="adapter bug"):
        _FollowUps(BrokenForge(), frozenset())._lookup(2)


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

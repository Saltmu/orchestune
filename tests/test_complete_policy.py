"""Publication limit decisions never interpret unknown bounded usage as unlimited."""

import pytest

from orchestune.complete.policy import token_limit_decision
from orchestune.models import Usage


def test_unknown_usage_is_safe_only_for_unlimited_claim():
    assert token_limit_decision(None, None) == "allowed"
    assert token_limit_decision(100, None) == "unknown"


def test_limit_boundary_and_exceeded():
    assert (
        token_limit_decision(
            100, Usage(input_tokens=70, output_tokens=30, total_tokens=100)
        )
        == "allowed"
    )
    assert (
        token_limit_decision(
            100, Usage(input_tokens=71, output_tokens=30, total_tokens=101)
        )
        == "exceeded"
    )


def _active(config=None, **launch):
    from orchestune.ledger.active_records import (
        ActiveCompletionJournal,
        ActiveWorktree,
        ActiveWorktreeCore,
        ClaimInfo,
        LaunchInfo,
    )

    return ActiveWorktree.from_records(
        core=ActiveWorktreeCore(
            issue_number=1110, branch="task", worktree_path="/wt", declared_footprint=()
        ),
        launch=LaunchInfo(**{"started_at": 1, **launch}),
        claim=ClaimInfo(claim_id="claim-test"),
        completion=ActiveCompletionJournal(completion_policy_config=config),
    )


def test_effective_snapshot_overrides_repository_config_and_uses_shared_usage(tmp_path):
    from orchestune.complete.policy import evaluate_publication_policy

    (tmp_path / "orchestune.toml").write_text("max_tokens_per_task = 999")
    (tmp_path / "task.log").write_text(
        '{"usage":{"input_tokens":60,"output_tokens":41}}'
    )
    active = _active(
        {
            "max_tokens_per_task": 100,
            "source": "dispatcher-effective-config",
            "dispatch_target": "claude-cli",
            "log_dir": str(tmp_path),
        }
    )
    verdict = evaluate_publication_policy(active, tmp_path, None)
    assert verdict["decision"] == "exceeded"
    assert verdict["limit"] == 100
    assert verdict["usage"]["total_tokens"] == 101


def test_unlimited_snapshot_skips_usage_collection(tmp_path):
    from orchestune.complete.policy import evaluate_publication_policy

    active = _active({"max_tokens_per_task": None})
    assert evaluate_publication_policy(active, tmp_path, None)["decision"] == "allowed"


def test_missing_snapshot_reads_repository_config(tmp_path):
    from orchestune.complete.policy import evaluate_publication_policy

    (tmp_path / "orchestune.toml").write_text("max_tokens_per_task = 50")
    verdict = evaluate_publication_policy(_active(), tmp_path, None)
    assert verdict["limit"] == 50


def test_finite_limit_with_unknown_usage_is_unknown(tmp_path):
    from orchestune.complete.policy import evaluate_publication_policy

    active = _active(
        {"max_tokens_per_task": 10, "dispatch_target": "external", "log_dir": "x"}
    )
    assert evaluate_publication_policy(active, tmp_path, None)["decision"] == "unknown"


class _FlatOnly:
    """No nested records, but plausible flat values."""

    completion_policy_config = {"max_tokens_per_task": 1}
    branch = "task"
    pid = None
    external_id = None
    external_url = None
    issue_number = 1
    started_at = 1
    launch_attempt_id = None


def test_missing_completion_record_raises_at_entry(tmp_path):
    from orchestune.complete.policy import evaluate_publication_policy

    with pytest.raises(AttributeError):
        evaluate_publication_policy(_FlatOnly(), tmp_path, None)


def test_missing_core_or_launch_during_usage_is_unknown_with_error(tmp_path):
    from types import SimpleNamespace

    from orchestune.complete.policy import evaluate_publication_policy

    active = SimpleNamespace(
        completion=SimpleNamespace(
            completion_policy_config={
                "max_tokens_per_task": 10,
                "dispatch_target": "local",
                "log_dir": str(tmp_path),
            }
        ),
        branch="task",
        pid=None,
        issue_number=1,
    )
    verdict = evaluate_publication_policy(active, tmp_path, None)
    assert verdict["decision"] == "unknown" and verdict["error"]


@pytest.mark.parametrize("decision", ["unknown", "exceeded"])
def test_held_done_never_publishes_outcome_or_done_label(
    tmp_path, monkeypatch, decision
):
    from complete_lifecycle_test_support import lifecycle_environment

    from orchestune.complete.service import complete_task

    request, forge, _ = lifecycle_environment(tmp_path, monkeypatch, "done")
    monkeypatch.setattr(
        "orchestune.complete.service.evaluate_publication_policy",
        lambda *_: {"decision": decision, "limit": 100, "usage": None},
    )
    first = complete_task(request, forge=forge)
    second = complete_task(request, forge=forge)
    assert not first.success and not second.success
    assert first.completion_id == second.completion_id
    assert "status:done" not in forge.labels
    assert all(
        "orchestune:outcome" not in comment["body"] for comment in forge.comments
    )
    assert len(forge.comments) == (1 if decision == "exceeded" else 0)


@pytest.mark.parametrize("operation", ["add", "post"])
def test_escalation_response_loss_retries_without_duplicate_reason(
    tmp_path, monkeypatch, operation
):
    from complete_lifecycle_test_support import lifecycle_environment

    from orchestune.complete.service import complete_task
    from orchestune.ledger.run_state import load_run_state_readonly

    request, forge, _ = lifecycle_environment(tmp_path, monkeypatch, "done")
    monkeypatch.setattr(
        "orchestune.complete.service.evaluate_publication_policy",
        lambda *_: {"decision": "exceeded", "limit": 100},
    )
    fired = False

    def inject(current, after):
        nonlocal fired
        if current == operation and after and not fired:
            fired = True
            raise OSError("lost response")

    forge.inject = inject
    assert not complete_task(request, forge=forge).success
    forge.inject = lambda *_: None
    result = complete_task(request, forge=forge)
    assert not result.success
    assert forge.labels == {"status:blocked-human-review"}
    assert len(forge.comments) == 1
    state = load_run_state_readonly(request.state_path)
    assert next(iter(state.completion_journal.values()))[
        "prepublication_policy_evidence"
    ]["escalation"]["confirmed"]


def test_completion_and_dispatch_share_the_usage_provider_method():
    from orchestune.dispatch.targets import LocalProcessDispatchTarget
    from orchestune.targets.usage import LocalUsageProvider

    assert LocalProcessDispatchTarget.collect_usage is LocalUsageProvider.collect_usage


def test_legacy_gc_uses_the_shared_pure_token_verdict():
    from orchestune.dispatch.gc.completion import token_limit_decision as gc_decision
    from orchestune.targets.completion_policy import (
        token_limit_decision as shared_decision,
    )

    assert gc_decision is shared_decision is token_limit_decision

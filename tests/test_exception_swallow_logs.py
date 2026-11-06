"""The swallows left by #1707 keep their control flow and record the exception."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import orchestune.dag.symbol_verification as symbol_verification
import orchestune.dispatch.cycle.execution as cycle_execution
import orchestune.dispatch.gc.completion as gc_completion
import orchestune.dispatch.gc.rule_chain as gc_rule_chain
import orchestune.dispatch.gc.zombies as gc_zombies
import orchestune.dispatch.gc_service as gc_service
import orchestune.dispatch.phase.rebase as phase_rebase
import orchestune.dispatch.rebase as dispatch_rebase
import orchestune.dispatch.reconciliation as reconciliation
import orchestune.dispatch.recovery.restoration as restoration
import orchestune.dispatch.runtime_reader as runtime_reader
import orchestune.dispatch.status_repair as status_repair
import orchestune.dispatch.worktree as dispatch_worktree
from orchestune.dag.symbol_verification import _parse_with_tree_sitter
from orchestune.dispatch.cycle.execution import execute_cycle
from orchestune.dispatch.gc.completion import _apply_escalated_base_branch_red
from orchestune.dispatch.gc.rule_chain import _cleanup_stale_active_worktree
from orchestune.dispatch.gc.zombies import _stop_reclaimed_process
from orchestune.dispatch.gc_service import _gc_forge
from orchestune.dispatch.phase.rebase import _collect_completed_dependency_evidence
from orchestune.dispatch.rebase import (
    _handle_rebase_failure,
    _prepare_wip_backup_for_rebase,
)
from orchestune.dispatch.reconciliation import (
    _apply_base_branch_red_escalate,
    _get_branch_commit_sha,
    _live_verify_queued_transition,
    _release_recompute_for_promotion,
)
from orchestune.dispatch.recovery.restoration import _parse_claim_info_from_issue
from orchestune.dispatch.reporting.progress_output import RichDispatchProgress
from orchestune.dispatch.runtime_reader import provider_config
from orchestune.dispatch.semantic_review.lease import SemanticReviewLease
from orchestune.dispatch.status_repair import reconcile_status_repair_intents
from orchestune.dispatch.timings import TimingProgress, timing_snapshot
from orchestune.dispatch.worktree import (
    _cleanup_existing_worktree,
    _cleanup_failed_worktree,
)
from orchestune.forge import IssueRecord
from orchestune.integrator.timeout_retry import ExecutionBudgetStore, ExecutionEvent
from orchestune.provisioning.cli import _print_resume_hint

MARK = "swallowed broad exception"


def _logged(caplog: pytest.LogCaptureFixture) -> str:
    return caplog.text


def test_resume_hint_logs_a_plan_that_cannot_be_loaded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        _print_resume_hint("/no/such/provision-plan.md")
    assert MARK in _logged(caplog)


def test_unconfirmed_comment_write_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = ExecutionBudgetStore.__new__(ExecutionBudgetStore)
    store.forge = SimpleNamespace(
        create_issue_comment=Mock(side_effect=RuntimeError("response lost"))
    )
    store.parent = 1590
    store.scope = None
    store._present = lambda _event: True  # type: ignore[assignment]
    event = ExecutionEvent(
        parent_issue_number=1590,
        generation=1,
        attempt_id="attempt",
        event="finished",
        executed_at="2026-11-05T00:00:00Z",
    )
    with caplog.at_level(logging.WARNING):
        store.write_event(event)
    assert MARK in _logged(caplog)


def test_worktree_rollback_failures_are_logged(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    with (
        patch.object(
            dispatch_worktree,
            "run_git",
            side_effect=RuntimeError("git remove failed"),
        ),
        patch.object(
            dispatch_worktree.dispatch_gc,
            "backup_wip_commit",
            return_value=None,
        ),
        caplog.at_level(logging.WARNING),
    ):
        _cleanup_failed_worktree(tmp_path / "missing")
        assert _cleanup_existing_worktree(tmp_path, 1) is None
    assert _logged(caplog).count(MARK) >= 2


def test_timing_failures_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    class Broken:
        def snapshot(self) -> dict[str, object]:
            raise RuntimeError("snapshot failed")

        def observe(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("observe failed")

        def phase_unavailable(self) -> None:
            raise RuntimeError("phase failed")

    sink = Mock()
    with caplog.at_level(logging.WARNING):
        snapshot = timing_snapshot(Broken())  # type: ignore[arg-type]
        assert snapshot["collection_status"] == "unavailable"
        TimingProgress(sink, Broken()).emit("cycle", "started")  # type: ignore[arg-type]
    sink.emit.assert_called_once()
    assert _logged(caplog).count(MARK) >= 2


def test_status_repair_verification_failure_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Journal:
        def __init__(self, _path: object) -> None:
            pass

        def pending(self, *, now: object) -> list[SimpleNamespace]:
            del now
            return [SimpleNamespace(subject_id="not-an-issue", expected_changes=())]

    config = SimpleNamespace(apply=True, run_state_path="state.json")
    with (
        patch.object(
            status_repair,
            "status_intent_journal_path",
            return_value="journal",
        ),
        patch.object(status_repair, "IntentJournal", Journal),
        caplog.at_level(logging.WARNING),
    ):
        assert reconcile_status_repair_intents(config) == ()  # type: ignore[arg-type]
    assert MARK in _logged(caplog)


def test_provider_config_logs_target_build_failure(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    workspace = SimpleNamespace(
        repository_root=tmp_path,
        run_state_path=tmp_path / "run.json",
        worktree_root=tmp_path / "worktrees",
    )
    raw = {"dispatch_target": "cloud-routine", "reviewer_bot": "auto"}
    with (
        patch.object(
            runtime_reader.claim_workspace,
            "resolve_checkout_roots",
            return_value=(tmp_path, tmp_path),
        ),
        patch.object(
            runtime_reader,
            "find_and_load_config_file",
            return_value={},
        ),
        patch.object(
            runtime_reader,
            "validate_toml_config",
            return_value=raw,
        ),
        patch.object(
            runtime_reader,
            "_resolve_cloud_settings",
            return_value=("routine", "token", None),
        ),
        patch.object(
            runtime_reader,
            "extract_reviewer_by_target",
            return_value={},
        ),
        patch.object(
            runtime_reader,
            "build_dispatch_target",
            side_effect=RuntimeError("target unavailable"),
        ),
        caplog.at_level(logging.WARNING),
    ):
        assert provider_config(workspace) is None  # type: ignore[arg-type]
    assert MARK in _logged(caplog)


def test_reconciliation_fail_closed_paths_are_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    forge = SimpleNamespace(
        get_issue_state=Mock(side_effect=RuntimeError("state")),
        get_issue=Mock(side_effect=RuntimeError("issue")),
        remove_label=Mock(side_effect=RuntimeError("label")),
    )
    config = SimpleNamespace(resolved_forge=forge, apply=True)
    decision = SimpleNamespace(issue_number=4, attempt=2)
    with (
        patch.object(
            reconciliation,
            "resolve_local_or_remote_branch",
            side_effect=RuntimeError("branch"),
        ),
        patch.object(reconciliation, "apply_human_review_escalation"),
        caplog.at_level(logging.WARNING),
    ):
        assert (
            _live_verify_queued_transition(
                config,  # type: ignore[arg-type]
                issue_number=4,
                before_labels=(),
            )
            is None
        )
        assert (
            _release_recompute_for_promotion(
                4,
                SimpleNamespace(),  # type: ignore[arg-type]
                SimpleNamespace(),  # type: ignore[arg-type]
                config,  # type: ignore[arg-type]
                None,
            )
            is None
        )
        assert _get_branch_commit_sha("missing") is None
        _apply_base_branch_red_escalate(decision, config)  # type: ignore[arg-type]
    assert _logged(caplog).count(MARK) >= 4


def test_rebase_cleanup_failures_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    active = SimpleNamespace(
        launch=SimpleNamespace(pid=424242),
        core=SimpleNamespace(worktree_path="wt", issue_number=8),
    )
    forge = SimpleNamespace(add_comment=Mock())
    config = SimpleNamespace(resolved_forge=forge)
    ctx = SimpleNamespace(
        key="k", run_state=SimpleNamespace(active_worktrees={"k": active})
    )
    with (
        patch.object(dispatch_rebase.os, "kill", side_effect=ProcessLookupError),
        patch.object(
            dispatch_rebase.dispatch_gc,
            "backup_wip_commit",
            return_value="backup failed",
        ),
        patch.object(dispatch_rebase, "transition_status_label"),
        patch.object(
            dispatch_rebase,
            "run_git",
            side_effect=RuntimeError("abort failed"),
        ),
        caplog.at_level(logging.WARNING),
    ):
        assert (
            _prepare_wip_backup_for_rebase(active, config, ctx)  # type: ignore[arg-type]
            is False
        )
        ctx.run_state.active_worktrees["k"] = active
        _handle_rebase_failure(
            active,  # type: ignore[arg-type]
            "parent/issue-1",
            RuntimeError("rebase"),
            config,  # type: ignore[arg-type]
            ctx,  # type: ignore[arg-type]
        )
    assert _logged(caplog).count(MARK) >= 2


def test_optional_forge_setup_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    with (
        patch.object(gc_service, "_is_handoff_ready", return_value=True),
        caplog.at_level(logging.WARNING),
    ):
        assert (
            _gc_forge(
                [("k", SimpleNamespace())],  # type: ignore[list-item]
                SimpleNamespace(),  # type: ignore[arg-type]
                Mock(side_effect=RuntimeError("forge")),
            )
            is None
        )
    assert MARK in _logged(caplog)


def test_parser_failure_is_logged(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    source = tmp_path / "main.go"
    source.write_text("package main\n", encoding="utf-8")
    parser = SimpleNamespace(parse=Mock(side_effect=RuntimeError("parser")))
    with (
        patch.object(
            symbol_verification,
            "_get_tree_sitter_parser",
            return_value=parser,
        ),
        caplog.at_level(logging.WARNING),
    ):
        assert _parse_with_tree_sitter(source, "go") is None
    assert MARK in _logged(caplog)


def test_claim_release_failure_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    lease = SemanticReviewLease(
        forge=SimpleNamespace(),  # type: ignore[arg-type]
        repository_root=Path("."),
        claims=SimpleNamespace(  # type: ignore[arg-type]
            release=Mock(side_effect=RuntimeError("release"))
        ),
        worker_id="w1",
        clock=lambda: datetime.now(UTC),
    )
    ctx = SimpleNamespace(claim=object())
    with caplog.at_level(logging.WARNING):
        lease.release(ctx)  # type: ignore[arg-type]
    assert ctx.claim is None
    assert MARK in _logged(caplog)


def test_terminal_restore_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    progress = RichDispatchProgress.__new__(RichDispatchProgress)
    progress.owns_stream = False
    progress._stop_live = Mock(side_effect=RuntimeError("tty"))  # type: ignore[method-assign]
    progress.close()
    assert MARK in capsys.readouterr().err


def test_legacy_claim_metadata_parse_failure_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    issue = IssueRecord(
        number=1,
        title="t",
        body="```yaml\nowner_kind: interactive\n```",
        labels=(),
        created_at="2026-11-05T00:00:00Z",
    )
    with (
        patch.object(
            restoration,
            "_yaml_safe_load",
            side_effect=RuntimeError("yaml"),
        ),
        caplog.at_level(logging.WARNING),
    ):
        info = _parse_claim_info_from_issue(issue)
    assert info.owner_kind == "dispatch"
    assert MARK in _logged(caplog)


def test_dependency_tip_lookup_failure_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    forge = SimpleNamespace(
        get_current_branch_tip_sha_if_merged_into=Mock(side_effect=RuntimeError("sha"))
    )
    task = SimpleNamespace(issue_number=9)
    with (
        patch.object(
            phase_rebase,
            "_completed_dependency_candidates",
            return_value={2: "feature"},
        ),
        caplog.at_level(logging.WARNING),
    ):
        evidence = _collect_completed_dependency_evidence(
            [task],  # type: ignore[list-item]
            SimpleNamespace(),  # type: ignore[arg-type]
            {"feature"},
            forge,  # type: ignore[arg-type]
            1590,
        )
    assert evidence == {}
    assert MARK in _logged(caplog)


def test_process_stop_failures_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    reclaim = SimpleNamespace(
        active=SimpleNamespace(launch=SimpleNamespace(pid=424242)),
        process_alive=True,
    )
    active = SimpleNamespace(
        core=SimpleNamespace(worktree_path="/no/such/worktree", issue_number=3),
        launch=SimpleNamespace(pid=424242),
    )
    with (
        patch.object(gc_zombies.os, "kill", side_effect=ProcessLookupError),
        patch.object(gc_rule_chain.os, "kill", side_effect=ProcessLookupError),
        patch.object(gc_rule_chain, "is_process_alive", return_value=True),
        caplog.at_level(logging.WARNING),
    ):
        _stop_reclaimed_process(reclaim)  # type: ignore[arg-type]
        assert (
            _cleanup_stale_active_worktree(
                active,  # type: ignore[arg-type]
                "stale",
                SimpleNamespace(),  # type: ignore[arg-type]
            )
            is True
        )
    assert _logged(caplog).count(MARK) >= 2


def test_escalated_label_removal_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    forge = SimpleNamespace(remove_label=Mock(side_effect=RuntimeError("label")))
    ctx = SimpleNamespace(
        active=SimpleNamespace(core=SimpleNamespace(issue_number=6)),
        config=SimpleNamespace(resolved_forge=forge),
        active_task=None,
    )
    decision = SimpleNamespace(outcome=None)
    with (
        patch.object(gc_completion, "_apply_escalation"),
        caplog.at_level(logging.WARNING),
    ):
        _apply_escalated_base_branch_red(ctx, decision)  # type: ignore[arg-type]
    assert MARK in _logged(caplog)


def test_cycle_timing_setup_failure_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = SimpleNamespace(progress=object())
    with (
        patch.object(
            cycle_execution,
            "CycleTimingCollector",
            side_effect=RuntimeError("collector"),
        ),
        patch.object(
            cycle_execution,
            "progress_phase",
            side_effect=RuntimeError("stop"),
        ),
        caplog.at_level(logging.WARNING),
        pytest.raises(RuntimeError, match="stop"),
    ):
        execute_cycle(SimpleNamespace(), config)  # type: ignore[arg-type]
    assert MARK in _logged(caplog)


def test_cleanup_comments_and_noqa_reasons_name_the_boundary() -> None:
    root = Path(__file__).resolve().parents[1]
    progress = (root / "orchestune/dispatch/reporting/progress.py").read_text(
        encoding="utf-8"
    )
    plan = (root / "orchestune/plan_writer.py").read_text(encoding="utf-8")
    transaction = (root / "orchestune/installer/transaction.py").read_text(
        encoding="utf-8"
    )
    provisioning = (root / "orchestune/provisioning/cli.py").read_text(encoding="utf-8")
    cleanup = (root / "orchestune/dispatch/semantic_review/cleanup.py").read_text(
        encoding="utf-8"
    )
    effects = (root / "orchestune/dispatch/semantic_review/effects.py").read_text(
        encoding="utf-8"
    )
    forge = (root / "tests/test_forge.py").read_text(encoding="utf-8")
    managed = (root / "tests/test_managed_process.py").read_text(encoding="utf-8")
    phrase = "cleanup: must run on BaseException"
    assert phrase in progress
    assert phrase in plan
    assert phrase in transaction
    assert phrase in provisioning
    assert "boundary: an unknown poll result is not settled" in cleanup
    assert (
        "boundary: an unreadable escalation record counts as already recorded"
        in effects
    )
    assert "boundary: prove the scoped timeout is not swallowed" in forge
    assert "boundary: prove the scoped timeout is not swallowed" in managed

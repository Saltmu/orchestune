"""Tests for LocalTrialStore in orchestune.local_trial.state."""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest

from orchestune.local_trial.contracts import (
    TrialError,
    TrialStore,
)
from orchestune.local_trial.models import (
    AttemptInput,
    AttemptKind,
    AttemptResult,
    Budget,
    Checkpoint,
    Decision,
    DecisionValue,
    DisciplineStatus,
    Observation,
    ProcessRecord,
    ProcessStatus,
    RepositoryIdentity,
    RequestSnapshot,
    TrialPhase,
    TrialPlan,
    TrialState,
    WorkspaceIdentity,
)
from orchestune.local_trial.state import LocalTrialStore


def _sample_repo() -> RepositoryIdentity:
    return RepositoryIdentity(
        source_root="/repo",
        primary_root="/repo",
        common_dir="/repo/.git",
        base_ref="refs/heads/main",
        base_sha="0" * 40,
    )


def _sample_workspace(trial_id: str) -> WorkspaceIdentity:
    return WorkspaceIdentity(
        implementation_path=f"/repo/.orchestune/worktrees/trial-{trial_id}",
        review_path=f"/repo/.orchestune/worktrees/trial-review-{trial_id}",
        branch=f"trial/{trial_id}",
    )


def _sample_state(
    trial_id: str = "a" * 32,
    *,
    phase: TrialPhase = TrialPhase.PREPARING,
    checkpoint: Checkpoint = Checkpoint.PREPARE,
    revision: int = 0,
    pending_attempt: AttemptInput | None = None,
    decision: Decision | None = None,
) -> TrialState:
    from orchestune.local_trial.models import AgentInfo

    return TrialState(
        schema_version=1,
        trial_id=trial_id,
        repository=_sample_repo(),
        workspace=_sample_workspace(trial_id),
        request=RequestSnapshot(
            text="Implement feature",
            digest="d" * 64,
            ci_argv_override=None,
        ),
        agent=AgentInfo(name="codex", executable="codex", version="1.0"),
        plan=TrialPlan(
            goal="Goal",
            acceptance_criteria=("Done",),
            write_paths=("foo.py",),
            discipline_paths=(),
            discipline_notes=(),
            ci_argv=("pytest",),
            discipline_status=DisciplineStatus.COMPATIBLE,
        ),
        approved_plan_digest=None,
        approved_worktree_digest=None,
        approved_index_digest=None,
        phase=phase,
        checkpoint=checkpoint,
        reason=None,
        revision=revision,
        budget=Budget(plan_revision=0, cycles_started=0, reviews_started=0),
        head_sha="0" * 40,
        pending_attempt=pending_attempt,
        pending_git=None,
        attempt_ids=(),
        ci_evidence=None,
        review_evidence=None,
        findings=(),
        responses=(),
        stop_confirmations=(),
        controller=None,
        observation=Observation(
            started_at="2026-11-07T12:00:00Z",
            updated_at="2026-11-07T12:00:00Z",
            elapsed_seconds=0.0,
        ),
        decision=decision,
    )


def _sample_attempt(trial_id: str, attempt_id: str = "b" * 32) -> AttemptResult:
    return AttemptResult(
        schema_version=1,
        input=AttemptInput(
            trial_id=trial_id,
            attempt_id=attempt_id,
            kind=AttemptKind.PLAN,
            input_head_sha="0" * 40,
            base_sha="0" * 40,
            plan_digest=None,
            plan_revision=0,
            cwd="/repo",
            artifact_dir="/repo/artifacts",
            started_at="2026-11-07T12:00:00Z",
        ),
        process=ProcessRecord(
            outcome=ProcessStatus.SUCCESS,
            returncode=0,
            stop_confirmed=None,
            elapsed_seconds=1.0,
            timeout_seconds=300.0,
            stdout_tail="",
            stderr_tail="",
            detail="",
        ),
        agent_payload=None,
        ended_at="2026-11-07T12:00:01Z",
    )


def test_local_trial_store_protocol_satisfaction(tmp_path: Path) -> None:
    store = LocalTrialStore(
        common_dir=tmp_path / "common", primary_root=tmp_path / "primary"
    )
    assert isinstance(store, TrialStore)


def test_lock_and_reentrancy_rejection(tmp_path: Path) -> None:
    store = LocalTrialStore(
        common_dir=tmp_path / "common", primary_root=tmp_path / "primary"
    )
    with store.lock():
        with pytest.raises(TrialError) as exc_info:
            with store.lock():
                pass
        assert exc_info.value.code == "already_active"


def test_writer_methods_require_lock(tmp_path: Path) -> None:
    store = LocalTrialStore(
        common_dir=tmp_path / "common", primary_root=tmp_path / "primary"
    )
    state = _sample_state()

    with pytest.raises(TrialError) as exc:
        store.reserve(state)
    assert exc.value.code == "already_active"

    with pytest.raises(TrialError) as exc:
        store.save(state, expected_revision=None)
    assert exc.value.code == "already_active"

    with pytest.raises(TrialError) as exc:
        store.record_attempt(_sample_attempt(state.trial_id))
    assert exc.value.code == "already_active"

    with pytest.raises(TrialError) as exc:
        store.close(state)
    assert exc.value.code == "already_active"


def _worker_lock_test(
    common_dir: str, primary_root: str, result_queue: multiprocessing.Queue
) -> None:
    try:
        store = LocalTrialStore(common_dir=common_dir, primary_root=primary_root)
        with store.lock():
            result_queue.put("acquired")
    except TrialError as e:
        result_queue.put(e.code)
    except Exception as e:  # noqa: BLE001
        result_queue.put(str(e))


def test_multiprocess_lock_contention(tmp_path: Path) -> None:
    common = tmp_path / "common"
    primary = tmp_path / "primary"
    store = LocalTrialStore(common_dir=common, primary_root=primary)

    with store.lock():
        ctx = multiprocessing.get_context("spawn")
        q: multiprocessing.Queue = ctx.Queue()
        p = ctx.Process(
            target=_worker_lock_test,
            args=(str(common), str(primary), q),
        )
        p.start()
        p.join(timeout=5)
        res = q.get(timeout=2)
        assert res == "already_active"


def test_reserve_and_load_seed_when_state_unwritten(tmp_path: Path) -> None:
    store = LocalTrialStore(
        common_dir=tmp_path / "common", primary_root=tmp_path / "primary"
    )
    state = _sample_state("1" * 32)

    assert store.active_trial_id() is None

    with store.lock():
        store.reserve(state)

    assert store.active_trial_id() == "1" * 32

    # If already active, second reserve fails with already_active
    with store.lock():
        with pytest.raises(TrialError) as exc:
            store.reserve(state)
        assert exc.value.code == "already_active"

    # Loading before state.json exists returns seed state without side-effects
    loaded = store.load("1" * 32)
    assert loaded.trial_id == "1" * 32
    assert loaded.phase == TrialPhase.PREPARING
    assert loaded.checkpoint == Checkpoint.PREPARE


def test_reserve_rejects_non_preparing_seed(tmp_path: Path) -> None:
    store = LocalTrialStore(
        common_dir=tmp_path / "common", primary_root=tmp_path / "primary"
    )
    state = _sample_state("1" * 32, phase=TrialPhase.PLANNING)
    with store.lock():
        with pytest.raises(TrialError) as exc:
            store.reserve(state)
        assert exc.value.code == "invalid_input"


def test_save_initial_and_revisions(tmp_path: Path) -> None:
    store = LocalTrialStore(
        common_dir=tmp_path / "common", primary_root=tmp_path / "primary"
    )
    s0 = _sample_state("2" * 32, revision=0)

    with store.lock():
        store.reserve(s0)
        store.save(s0, expected_revision=None)
        # Idempotent re-save of same canonical content
        store.save(s0, expected_revision=None)

    loaded = store.load("2" * 32)
    assert loaded.revision == 0

    # Revisioned update
    s1 = _sample_state(
        "2" * 32, revision=1, phase=TrialPhase.PLANNING, checkpoint=Checkpoint.PLAN
    )
    with store.lock():
        # Stale revision mismatch
        with pytest.raises(TrialError) as exc:
            store.save(s1, expected_revision=1)
        assert exc.value.code == "stale_revision"

        # Correct revision update
        store.save(s1, expected_revision=0)

    loaded = store.load("2" * 32)
    assert loaded.revision == 1
    assert loaded.phase == TrialPhase.PLANNING


def test_record_and_load_attempt(tmp_path: Path) -> None:
    store = LocalTrialStore(
        common_dir=tmp_path / "common", primary_root=tmp_path / "primary"
    )
    attempt_id = "3" * 32
    trial_id = "4" * 32
    pending = AttemptInput(
        trial_id=trial_id,
        attempt_id=attempt_id,
        kind=AttemptKind.PLAN,
        input_head_sha="0" * 40,
        base_sha="0" * 40,
        plan_digest=None,
        plan_revision=0,
        cwd="/repo",
        artifact_dir="/repo/artifacts",
        started_at="2026-11-07T12:00:00Z",
    )
    s0 = _sample_state(trial_id, revision=0, pending_attempt=None)

    with store.lock():
        store.reserve(s0)
        store.save(s0, expected_revision=None)
        s1 = _sample_state(trial_id, revision=1, pending_attempt=pending)
        store.save(s1, expected_revision=0)

    attempt_res = _sample_attempt(trial_id, attempt_id)
    with store.lock():
        store.record_attempt(attempt_res)
        # Idempotent re-record
        store.record_attempt(attempt_res)

    loaded_att = store.load_attempt(trial_id, attempt_id)
    assert loaded_att is not None
    assert loaded_att.input.attempt_id == attempt_id

    # Non-existent attempt returns None
    assert store.load_attempt(trial_id, "9" * 32) is None


def test_close_flow_and_idempotence(tmp_path: Path) -> None:
    store = LocalTrialStore(
        common_dir=tmp_path / "common", primary_root=tmp_path / "primary"
    )
    trial_id = "5" * 32
    s0 = _sample_state(trial_id, revision=0)

    with store.lock():
        store.reserve(s0)
        store.save(s0, expected_revision=None)

    closed_state = _sample_state(
        trial_id,
        phase=TrialPhase.CLOSED,
        checkpoint=Checkpoint.CLOSED,
        revision=1,
        decision=Decision(
            value=DecisionValue.ADOPTED, recorded_at="2026-11-07T12:00:00Z"
        ),
    )

    with store.lock():
        store.close(closed_state)
        assert store.active_trial_id() is None
        # Idempotent re-close
        store.close(closed_state)

    # State still readable after close
    loaded = store.load(trial_id)
    assert loaded.phase == TrialPhase.CLOSED


def test_corrupted_pointer_raises_state_invalid(tmp_path: Path) -> None:
    common = tmp_path / "common"
    primary = tmp_path / "primary"
    store = LocalTrialStore(common_dir=common, primary_root=primary)

    active_file = common / "orchestune" / "trial-active.json"
    active_file.parent.mkdir(parents=True, exist_ok=True)
    active_file.write_text("corrupted json", encoding="utf-8")

    with pytest.raises(TrialError) as exc:
        store.active_trial_id()
    assert exc.value.code == "state_invalid"


def test_path_traversal_rejection(tmp_path: Path) -> None:
    store = LocalTrialStore(
        common_dir=tmp_path / "common", primary_root=tmp_path / "primary"
    )
    with store.lock():
        with pytest.raises(TrialError) as exc:
            store.load("../" + "a" * 30)
        assert exc.value.code == "invalid_input"


def test_schema_unsupported_and_corrupted_files(tmp_path: Path) -> None:
    common = tmp_path / "common"
    primary = tmp_path / "primary"
    store = LocalTrialStore(common_dir=common, primary_root=primary)
    trial_id = "7" * 32

    # Pointer with unsupported schema_version 2
    pointer_file = common / "orchestune" / "trial-active.json"
    pointer_file.parent.mkdir(parents=True, exist_ok=True)
    pointer_file.write_text(
        json.dumps({"schema_version": 2, "trial_id": trial_id, "seed": {}}),
        encoding="utf-8",
    )
    with pytest.raises(TrialError) as exc:
        store.active_trial_id()
    assert exc.value.code == "schema_unsupported"

    # Corrupted state.json
    state_file = primary / ".orchestune" / "trials" / trial_id / "state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("invalid json", encoding="utf-8")
    with pytest.raises(TrialError) as exc:
        store.load(trial_id)
    assert exc.value.code == "state_invalid"

    # Corrupted attempt.json
    attempt_file = state_file.parent / "attempts" / f"{'8' * 32}.json"
    attempt_file.parent.mkdir(parents=True, exist_ok=True)
    attempt_file.write_text("{bad json", encoding="utf-8")
    with pytest.raises(TrialError) as exc:
        store.load_attempt(trial_id, "8" * 32)
    assert exc.value.code == "state_invalid"


def test_attempt_mismatch_and_differing_content(tmp_path: Path) -> None:
    store = LocalTrialStore(
        common_dir=tmp_path / "common", primary_root=tmp_path / "primary"
    )
    trial_id = "9" * 32
    att_1 = "1" * 32
    att_2 = "2" * 32

    pending = AttemptInput(
        trial_id=trial_id,
        attempt_id=att_1,
        kind=AttemptKind.PLAN,
        input_head_sha="0" * 40,
        base_sha="0" * 40,
        plan_digest=None,
        plan_revision=0,
        cwd="/repo",
        artifact_dir="/repo/artifacts",
        started_at="2026-11-07T12:00:00Z",
    )
    s0 = _sample_state(trial_id, revision=0)
    with store.lock():
        store.reserve(s0)
        store.save(s0, expected_revision=None)
        s1 = _sample_state(trial_id, revision=1, pending_attempt=pending)
        store.save(s1, expected_revision=0)

        # Record attempt for different attempt_id than pending raises state_invalid
        mismatched_res = _sample_attempt(trial_id, att_2)
        with pytest.raises(TrialError) as exc:
            store.record_attempt(mismatched_res)
        assert exc.value.code == "state_invalid"

        # Record valid attempt
        valid_res = _sample_attempt(trial_id, att_1)
        store.record_attempt(valid_res)

        # Overwrite attempt with different content raises state_invalid
        diff_res = _sample_attempt(trial_id, att_1)
        # Modify ended_at to change content
        object.__setattr__(diff_res, "ended_at", "2026-11-07T12:30:00Z")
        with pytest.raises(TrialError) as exc:
            store.record_attempt(diff_res)
        assert exc.value.code == "state_invalid"


def test_state_path_and_attempt_path_escape(tmp_path: Path) -> None:
    store = LocalTrialStore(
        common_dir=tmp_path / "common", primary_root=tmp_path / "primary"
    )
    with store.lock():
        with pytest.raises(TrialError) as exc:
            store._state_path("invalid_short_id")
        assert exc.value.code == "invalid_input"

        with pytest.raises(TrialError) as exc:
            store._attempt_path("a" * 32, "invalid_attempt")
        assert exc.value.code == "invalid_input"


def test_save_error_branches(tmp_path: Path) -> None:
    store = LocalTrialStore(
        common_dir=tmp_path / "common", primary_root=tmp_path / "primary"
    )
    t1 = "1" * 32
    t2 = "2" * 32
    s0 = _sample_state(t1, revision=0)
    with store.lock():
        store.reserve(s0)

        # Save with trial_id mismatching active pointer
        s_mismatch = _sample_state(t2, revision=0)
        with pytest.raises(TrialError) as exc:
            store.save(s_mismatch, expected_revision=None)
        assert exc.value.code == "state_invalid"

        # Save initial with revision != 0
        s_bad_rev = _sample_state(t1, revision=1)
        with pytest.raises(TrialError) as exc:
            store.save(s_bad_rev, expected_revision=None)
        assert exc.value.code == "stale_revision"

        # Save revision when state file not found
        with pytest.raises(TrialError) as exc:
            store.save(s_bad_rev, expected_revision=0)
        assert exc.value.code == "not_found"

        # Normal initial save
        store.save(s0, expected_revision=None)

        # Initial save again with different content
        s0_diff = _sample_state(t1, revision=0)
        object.__setattr__(s0_diff, "reason", "different reason")
        with pytest.raises(TrialError) as exc:
            store.save(s0_diff, expected_revision=None)
        assert exc.value.code == "stale_revision"


def test_close_error_branches(tmp_path: Path) -> None:
    store = LocalTrialStore(
        common_dir=tmp_path / "common", primary_root=tmp_path / "primary"
    )
    t1 = "3" * 32
    s0 = _sample_state(t1, revision=0)
    with store.lock():
        store.reserve(s0)
        store.save(s0, expected_revision=None)

        # Close with missing decision raises invalid_input
        bad_close = _sample_state(
            t1,
            phase=TrialPhase.CLOSED,
            checkpoint=Checkpoint.CLOSED,
            revision=1,
            decision=None,
        )
        with pytest.raises(TrialError) as exc:
            store.close(bad_close)
        assert exc.value.code == "invalid_input"

        # Close with mismatching revision
        bad_rev_close = _sample_state(
            t1,
            phase=TrialPhase.CLOSED,
            checkpoint=Checkpoint.CLOSED,
            revision=5,
            decision=Decision(
                value=DecisionValue.ADOPTED, recorded_at="2026-11-07T12:00:00Z"
            ),
        )
        with pytest.raises(TrialError) as exc:
            store.close(bad_rev_close)
        assert exc.value.code == "stale_revision"


def test_close_pointer_mismatch(tmp_path: Path) -> None:
    common = tmp_path / "common"
    primary = tmp_path / "primary"
    store = LocalTrialStore(common_dir=common, primary_root=primary)
    t1 = "4" * 32
    t2 = "5" * 32
    s0 = _sample_state(t1, revision=0)
    with store.lock():
        store.reserve(s0)

        # Try to close t2 while t1 is active
        close_t2 = _sample_state(
            t2,
            phase=TrialPhase.CLOSED,
            checkpoint=Checkpoint.CLOSED,
            revision=1,
            decision=Decision(
                value=DecisionValue.ADOPTED, recorded_at="2026-11-07T12:00:00Z"
            ),
        )
        with pytest.raises(TrialError) as exc:
            store.close(close_t2)
        assert exc.value.code == "identity_mismatch"

"""Canonical subrecord validation without tightening legacy persistence."""

from dataclasses import replace

import pytest

from orchestune.ledger.active_codec import (
    decode_active_worktree,
    encode_active_worktree,
)
from orchestune.ledger.active_records import (
    ActiveCompletionJournal,
    ClaimInfo,
    LaunchInfo,
)


@pytest.mark.parametrize(
    "values",
    [
        {"pid": True},
        {"started_at": float("inf")},
        {"started_at": 10**1000},
        {"recompute_count": -1},
        {"forced_serial": 1},
        {"estimated_tokens": -1},
        {"token_estimate_recorded": "yes"},
        {"launch_phase": "invalid"},
        {"external_id": 12},
        {"launch_target": 3},
        {"launch_log_path": 3},
        {"launch_log_offset": -1},
        {"launch_log_offset": True},
        {"launch_log_offset": "5"},
    ],
)
def test_launch_rejects_invalid_canonical_values(values: dict) -> None:
    with pytest.raises(ValueError):
        LaunchInfo(**values)


@pytest.mark.parametrize(
    "values",
    [
        {"owner_kind": "invalid"},
        {"claim_stage": "invalid"},
        {"reservation_kind": "invalid"},
        {"claimed_at": float("nan")},
        {"claim_id": 7},
        {"owner_token_digest": False},
    ],
)
def test_claim_rejects_invalid_canonical_values(values: dict) -> None:
    with pytest.raises(ValueError):
        ClaimInfo(**values)


@pytest.mark.parametrize(
    "values",
    [
        {"completion_stage": "posting"},
        {"completion_handoff_ready": True},
        {"completion_id": "c", "completion_stage": "invalid"},
        {"completion_id": "c", "completion_result": "invalid"},
        {"completion_id": "c", "completion_handoff_ready": "yes"},
        {"completion_id": "c", "completion_payload": ["bad"]},
        {"completion_comment_id": "comment"},
    ],
)
def test_completion_rejects_invalid_canonical_values(values: dict) -> None:
    with pytest.raises(ValueError):
        ActiveCompletionJournal(**values)


def test_valid_overlapping_owner_facts_are_not_prohibited() -> None:
    assert LaunchInfo(pid=4_200_012, launch_phase="launched").pid == 4_200_012
    assert (
        ClaimInfo(owner_kind="dispatch", claim_stage="completed").claim_stage
        == "completed"
    )
    assert (
        ActiveCompletionJournal(
            completion_id="c", completion_stage="posting"
        ).completion_id
        == "c"
    )
    assert (
        ActiveCompletionJournal(completion_policy_config={"key": "value"}).completion_id
        is None
    )


def test_legacy_codec_preserves_noncanonical_values_and_replacement() -> None:
    raw = {
        "issue_number": 1,
        "branch": "b",
        "worktree_path": "p",
        "declared_footprint": [],
        "launch_phase": "historical",
        "forced_serial": "historical",
        "completion_stage": "handed_off",
        "completion_handoff_ready": True,
    }
    active = decode_active_worktree(raw)
    # Keep the compatibility mode across owner updates using dataclasses.replace.
    launch = replace(active.launch, recompute_count=1)
    completion = replace(active.completion, completion_comment_url="url")
    active.launch = launch
    active.completion = completion
    data = encode_active_worktree(active)
    assert data["launch_phase"] == "historical"
    assert data["forced_serial"] == "historical"
    assert data["completion_stage"] == "handed_off"
    assert data["completion_id"] is None
    assert data["completion_comment_url"] == "url"


def test_replace_canonical_subrecord_keeps_validation_enabled() -> None:
    with pytest.raises(ValueError):
        replace(ClaimInfo(), owner_kind="invalid")


def test_canonical_factory_revalidates_and_clears_codec_compatibility_mode() -> None:
    from orchestune.ledger.active_records import ActiveWorktree

    legacy = decode_active_worktree(
        {
            "issue_number": 1,
            "branch": "b",
            "worktree_path": "p",
            "declared_footprint": [],
        }
    )
    canonical = ActiveWorktree.from_records(
        core=legacy.core,
        launch=legacy.launch,
        claim=legacy.claim,
        completion=legacy.completion,
    )
    with pytest.raises(ValueError):
        replace(canonical.launch, launch_phase="historical")
    malformed = decode_active_worktree(
        {
            "issue_number": 1,
            "branch": "b",
            "worktree_path": "p",
            "declared_footprint": [],
            "completion_stage": "handed_off",
        }
    )
    with pytest.raises(ValueError, match="completion progress requires"):
        ActiveWorktree.from_records(
            core=malformed.core,
            launch=malformed.launch,
            claim=malformed.claim,
            completion=malformed.completion,
        )


def test_valid_enum_values_match_owner_contracts() -> None:
    from orchestune.complete.contracts import CompleteStage
    from orchestune.ledger.active_records import COMPLETION_STAGES

    assert COMPLETION_STAGES == {stage.value for stage in CompleteStage}


_PERSISTED_COMPLETION_STAGES = frozenset(
    {
        "initializing",
        "preflight_validating",
        "evidence_verifying",
        "journaling",
        "posting",
        "handed_off_to_gc",
        "reserved",
        "outcome_posted",
        "label_confirmed",
        "handed_off",
    }
)


def test_completion_stages_are_the_persisted_names() -> None:
    from orchestune.ledger.active_records import COMPLETION_STAGES

    assert COMPLETION_STAGES == _PERSISTED_COMPLETION_STAGES


@pytest.mark.parametrize("stage", sorted(_PERSISTED_COMPLETION_STAGES))
def test_every_persisted_completion_stage_round_trips(stage: str) -> None:
    raw = {
        "issue_number": 1,
        "branch": "b",
        "worktree_path": "p",
        "declared_footprint": [],
        "completion_id": "c"
        if stage not in {"handed_off", "handed_off_to_gc"}
        else None,
        "completion_stage": stage,
    }
    active = decode_active_worktree(raw)
    assert active.completion.completion_stage == stage
    assert encode_active_worktree(active)["completion_stage"] == stage


@pytest.mark.parametrize("stage", ["", "Handed_Off", "handed-off", "done", 7])
def test_unknown_completion_stage_is_rejected(stage: object) -> None:
    with pytest.raises(ValueError, match="completion_stage"):
        ActiveCompletionJournal(completion_stage=stage)  # type: ignore[arg-type]


def test_ledger_records_do_not_import_the_complete_package() -> None:
    import subprocess
    import sys

    code = (
        "import sys; import orchestune.ledger.active_records, "
        "orchestune.ledger.active_lifecycle, orchestune.ledger.completion_reservations; "
        "sys.exit(1 if any(m == 'orchestune.complete' or m.startswith('orchestune.complete.') "
        "for m in sys.modules) else 0)"
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], check=False)
    assert result.returncode == 0

"""Typed values and JSON codec behavior for completion ledger entries."""

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from orchestune.complete.contracts import CompleteStage
from orchestune.complete.journal_models import CompletionJournalRecord as LegacyRecord
from orchestune.ledger.completion_records import (
    CompletionJournalRecord,
    CompletionReplayReceipt,
    CompletionReservation,
    InvalidLedgerRecord,
)
from orchestune.ledger.run_state import RunState, load_run_state, save_run_state


def journal_json(**updates):
    value = {
        "schema_version": 1,
        "repository_id": "Saltmu/orchestune",
        "issue_number": 1541,
        "generation_id": "claim-1541",
        "completion_id": "completion-1541",
        "owner_token_digest": "a" * 64,
        "request_fingerprint": "b" * 64,
        "result": "done",
        "target_label": "status:done",
        "outcome_payload": {"result": "done", "issue": 1541, "pr": 1542},
        "stage": "reserved",
    }
    value.update(updates)
    return value


def test_record_models_are_frozen_slotted_and_reexported_from_legacy_module():
    assert LegacyRecord is CompletionJournalRecord
    record = CompletionJournalRecord.from_dict(journal_json())
    assert not hasattr(record, "__dict__")
    with pytest.raises(FrozenInstanceError):
        record.stage = CompleteStage.HANDED_OFF


def test_run_state_loads_typed_completion_ledgers_and_round_trips_source_json(tmp_path):
    raw = journal_json(extra_extension={"preserve": [1, 2]})
    key = CompletionJournalRecord.from_dict(raw).journal_key
    state_path = tmp_path / "run_state.json"
    state_path.write_text(
        json.dumps({"completion_journal": {key: raw}}), encoding="utf-8"
    )

    state = load_run_state(state_path)
    assert isinstance(state.completion_journal[key], CompletionJournalRecord)
    save_run_state(state, state_path)
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["completion_journal"][key] == raw
    assert "posting_evidence" not in persisted["completion_journal"][key]


def test_semantically_invalid_record_is_typed_and_preserved_without_rejecting_state(
    tmp_path,
):
    raw = journal_json(stage="future-stage")
    state_path = tmp_path / "run_state.json"
    state_path.write_text(
        json.dumps({"completion_journal": {"bad": raw}}), encoding="utf-8"
    )

    state = load_run_state(state_path)
    invalid = state.completion_journal["bad"]
    assert isinstance(invalid, InvalidLedgerRecord)
    assert invalid.raw_json == raw
    save_run_state(state, state_path)
    assert (
        json.loads(state_path.read_text(encoding="utf-8"))["completion_journal"]["bad"]
        == raw
    )


def test_reservation_and_replay_receipt_are_distinct_typed_values():
    record = CompletionJournalRecord.from_dict(journal_json())
    reservation = CompletionReservation.from_journal(record)
    handed_off = CompletionJournalRecord.from_dict(
        journal_json(
            stage="handed_off",
            posting_evidence={
                "comment_id": "1",
                "comment_url": "https://example.test/1",
            },
            label_evidence={
                "status": "confirmed",
                "target_label": "status:done",
                "observed_labels": ["status:done"],
            },
        )
    )
    receipt = CompletionReplayReceipt(handed_off)

    assert reservation.reservation_key == "Saltmu/orchestune::1541"
    assert receipt.record == handed_off
    assert receipt.to_dict() == handed_off.to_dict()


def test_run_state_constructor_accepts_only_typed_completion_records():
    record = CompletionJournalRecord.from_dict(journal_json())
    state = RunState(completion_journal={record.journal_key: record})
    assert state.completion_journal[record.journal_key] is record


def test_completion_map_mutation_apis_keep_values_typed_and_check_record_keys():
    record = CompletionJournalRecord.from_dict(journal_json())
    state = RunState()
    state.completion_journal.update({record.journal_key: journal_json()})
    state.completion_journal.setdefault(
        "wrong-key", journal_json(completion_id="other")
    )
    assert isinstance(
        state.completion_journal[record.journal_key], CompletionJournalRecord
    )
    assert isinstance(state.completion_journal["wrong-key"], InvalidLedgerRecord)

    handed_off = CompletionJournalRecord.from_dict(
        journal_json(
            stage="handed_off",
            posting_evidence={
                "comment_id": "1",
                "comment_url": "https://example.test/1",
            },
            label_evidence={
                "status": "confirmed",
                "target_label": "status:done",
                "observed_labels": ["status:done"],
            },
        )
    )
    receipts = {handed_off.receipt_key: handed_off}
    state.completion_replay_receipts |= receipts
    assert isinstance(
        state.completion_replay_receipts[handed_off.receipt_key],
        CompletionReplayReceipt,
    )

    state.completion_journal = {record.journal_key: journal_json()}
    assert isinstance(
        state.completion_journal[record.journal_key], CompletionJournalRecord
    )


def test_typed_completion_map_assignment_does_not_encode_again(monkeypatch):
    import orchestune.ledger.run_state as run_state_module

    record = CompletionJournalRecord.from_dict(journal_json())
    state = RunState()

    def fail_encode(_value):
        raise AssertionError("typed assignment must not encode the record")

    monkeypatch.setattr(run_state_module, "encode_completion_value", fail_encode)
    state.completion_journal[record.journal_key] = record
    assert state.completion_journal[record.journal_key] is record
    monkeypatch.undo()
    with pytest.raises(TypeError, match="completion_journal values"):
        state.completion_journal["unsupported"] = None


def test_record_replace_and_reservation_copy_nested_mutable_fields():
    raw_payload = {"result": "done", "issue": 1541, "pr": 1542, "extra": []}
    record = CompletionJournalRecord.from_dict(
        journal_json(outcome_payload=raw_payload)
    )
    reservation = CompletionReservation.from_journal(record)
    record.outcome_payload["extra"].append("record-only")
    assert reservation.outcome_payload["extra"] == []

    replacement = replace(record, completion_id="copy")
    replacement.outcome_payload["extra"].append("copy-only")
    assert record.outcome_payload["extra"] == ["record-only"]

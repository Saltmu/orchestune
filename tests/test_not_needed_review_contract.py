"""The optional fixed Outcome extension preserves legacy bodies."""

import json

import pytest

from orchestune.not_needed_review_contract import NotNeededReview, review_operation_id
from orchestune.outcome_record import OutcomeRecord, parse_from_comments


@pytest.mark.parametrize(
    "requirement",
    [NotNeededReview("required", "a" * 64), NotNeededReview("exempt-local")],
)
def test_requirement_roundtrip(requirement):
    record = OutcomeRecord("not-needed", 12, not_needed_review=requirement)
    assert parse_from_comments([{"body": record.render()}]) == record


def test_legacy_render_does_not_invent_exemption():
    record = OutcomeRecord("not-needed", 12)
    assert "not_needed_review" not in record.render()
    assert parse_from_comments([{"body": record.render()}]).not_needed_review is None


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        False,
        {"schema_version": True, "requirement": "required", "operation_id": "a" * 64},
        {"schema_version": 1, "requirement": "required", "operation_id": "op"},
        {"schema_version": 1, "requirement": "exempt-local", "operation_id": "a" * 64},
        {"schema_version": 2, "requirement": "required", "operation_id": "a" * 64},
    ],
)
def test_malformed_explicit_requirement_is_rejected(value):
    record = OutcomeRecord("not-needed", 12)
    body = record.render().replace(
        '"result": "not-needed",',
        '"result": "not-needed", "not_needed_review": ' + json.dumps(value) + ",",
    )
    assert parse_from_comments([{"body": body}]) is None


def test_other_result_cannot_carry_requirement():
    with pytest.raises(ValueError):
        OutcomeRecord("done", 12, not_needed_review=NotNeededReview("exempt-local"))


def test_shared_operation_id_matches_existing_policy_key_hash():
    import hashlib

    key = "repo:12:generation:completion:not-needed-review"
    assert review_operation_id(key) == hashlib.sha256(key.encode()).hexdigest()

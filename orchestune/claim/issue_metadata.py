"""Non-secret claim ownership metadata published to the Issue body."""

from __future__ import annotations

import yaml

from orchestune.claim.contracts import (
    ClaimFailure,
    ClaimFailureReason,
    OwnerKind,
    ReservationKind,
)
from orchestune.forge import Forge
from orchestune.issue_parsing import FOOTPRINT_BLOCK_PATTERN
from orchestune.models import IssueRecord


def update_issue_claim_metadata(
    body: str,
    owner_kind: str,
    claim_id: str,
    reservation_kind: str,
) -> str:
    """Inject or update owner_kind, claim_id, and reservation_kind in Issue body."""
    match = FOOTPRINT_BLOCK_PATTERN.search(body)
    if match:
        try:
            data = yaml.safe_load(match.group(1))
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        data["owner_kind"] = owner_kind
        data["claim_id"] = claim_id
        data["reservation_kind"] = reservation_kind
        new_block = yaml.dump(data, allow_unicode=True, default_flow_style=False)
        start, end = match.span(1)
        return body[:start] + new_block + body[end:]

    new_yaml = yaml.dump(
        {
            "owner_kind": owner_kind,
            "claim_id": claim_id,
            "reservation_kind": reservation_kind,
        },
        allow_unicode=True,
        default_flow_style=False,
    )
    separator = "" if body.endswith("\n") else "\n"
    return f"{body}{separator}\n## Footprint\n```yaml\n{new_yaml}```\n"


def publish_claim_ownership_to_issue(
    forge: Forge,
    issue: IssueRecord,
    owner_kind: OwnerKind,
    claim_id: str,
    reservation_kind: ReservationKind,
) -> ClaimFailure | None:
    """Persist non-secret recovery metadata to the Issue body before completion."""
    try:
        new_body = update_issue_claim_metadata(
            issue.body,
            owner_kind.value,
            claim_id,
            reservation_kind.value,
        )
        if new_body != issue.body:
            forge.update_issue_body(issue.number, new_body)
        return None
    except Exception as e:
        return ClaimFailure(
            reason=ClaimFailureReason.STATE_SAVE_FAILED,
            message=f"Failed to publish claim ownership metadata to issue #{issue.number}: {e}",
        )

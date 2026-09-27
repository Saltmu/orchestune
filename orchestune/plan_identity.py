"""Canonical contracts for plan identity, revisions, and generation markers."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass

_PLAN_REVISION_PATTERN = re.compile(r"replan-v1:sha256:[0-9a-f]{64}")


class PlanRevision(str):
    """Validated semantic revision of a decomposition plan."""

    def __new__(cls, value: str) -> PlanRevision:
        if not _PLAN_REVISION_PATTERN.fullmatch(value):
            raise ValueError(f"invalid plan revision: {value!r}")
        return str.__new__(cls, value)


@dataclass(frozen=True)
class PlanGeneration:
    """Identity of a newly created SubIssue generation."""

    plan_revision: PlanRevision
    subtask_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.plan_revision, PlanRevision):
            object.__setattr__(
                self, "plan_revision", PlanRevision(str(self.plan_revision))
            )
        if (
            not isinstance(self.subtask_id, str)
            or not self.subtask_id.strip()
            or self.subtask_id != self.subtask_id.strip()
        ):
            raise ValueError("subtask_id must be a non-empty, trimmed string")

    @property
    def marker(self) -> str:
        """Return the exact marker used to re-find this generation."""
        encoded_id = (
            base64.urlsafe_b64encode(self.subtask_id.encode("utf-8"))
            .decode("ascii")
            .rstrip("=")
        )
        return (
            "<!-- orchestune:replan-generation "
            f"plan_revision={self.plan_revision} subtask_id_b64={encoded_id} -->"
        )

    def matches_body(self, body: str) -> bool:
        return self.marker in body


__all__ = [
    "PlanGeneration",
    "PlanRevision",
]

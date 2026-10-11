"""Immutable, generation-specific not-needed review evidence."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any


def review_operation_id(policy_key: str) -> str:
    return hashlib.sha256(policy_key.encode()).hexdigest()


@dataclass(frozen=True)
class NotNeededReview:
    requirement: str
    operation_id: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != 1
        ):
            raise ValueError("unsupported not-needed review schema")
        if self.requirement == "required":
            if not isinstance(self.operation_id, str) or not re.fullmatch(
                r"[0-9a-f]{64}", self.operation_id
            ):
                raise ValueError("invalid review operation ID")
        elif self.requirement != "exempt-local" or self.operation_id is not None:
            raise ValueError("invalid not-needed review requirement")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "requirement": self.requirement,
            "operation_id": self.operation_id,
        }

    @classmethod
    def from_dict(cls, value: Any) -> NotNeededReview:
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "requirement",
            "operation_id",
        }:
            raise ValueError("invalid not-needed review contract")
        return cls(**value)

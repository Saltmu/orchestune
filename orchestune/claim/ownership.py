"""Reservation models and conflict evaluation for claimed tasks."""

from __future__ import annotations

from typing import Any


def build_reservation(*args: Any, **kwargs: Any) -> Any:
    """Build an active reservation record from an issue and its footprint."""
    raise NotImplementedError("build_reservation is not yet implemented")


def evaluate_claim_conflicts(*args: Any, **kwargs: Any) -> Any:
    """Evaluate conflict between a prospective claim and current active reservations."""
    raise NotImplementedError("evaluate_claim_conflicts is not yet implemented")

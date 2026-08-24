"""Issue provisioning subsystem."""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "IssuePreview",
    "PlanMetadata",
    "ProvisionResult",
    "main",
    "provision_issues",
]


def __getattr__(name: str) -> Any:
    if name in (
        "IssuePreview",
        "PlanMetadata",
        "ProvisionResult",
        "main",
        "provision_issues",
        "_build_provisioning_dag",
        "_build_subtask_issue_body",
        "_derive_labels",
        "_link_subtask_relationships",
        "_load_plan",
        "_parent_body",
        "_print_result",
        "_provision_subtask",
        "_render_issue_body",
        "_resolve_parent_issue",
        "_subtask_id_from_body",
        "_validate_template_identity_marker",
    ):
        cli = importlib.import_module("orchestune.provisioning.cli")
        return getattr(cli, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + __all__)

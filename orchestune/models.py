"""ドメインモデル。L2ドメイン層の最下層に位置し、他のorchestuneモジュールに依存しない。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Task:
    issue_number: int
    subtask_id: str
    footprint: tuple[str, ...]
    symbols: tuple[str, ...]
    risk: bool
    priority: str
    progress_partial: bool
    status_labels: tuple[str, ...]
    created_at: str
    depends_on: tuple[str, ...] = ()
    yaml_error: bool = False
    parent_number: int | None = None
    issue_state: str = "OPEN"
    parent_state: str | None = None

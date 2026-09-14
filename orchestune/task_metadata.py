"""Raw依存を持たないタスクmetadataの型（#887, 接続契約v3）。

`models.Task`はraw依存宣言（`depends_on` / `native_depends_on`）を含む全域DTOの
まま維持する。本モジュールはpolicy/consumer側が誤ってraw宣言を参照できないよう、
非raw17フィールドだけを公開する`TaskMetadata` Protocolと、それを満たす
frozen/slotsの値型`CycleTask`を提供する。raw宣言の保持・DAG変換は別モジュール
（#888 `cycle-identity-dag-bridge`）の責務であり、ここでは扱わない。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from orchestune.models import Task


@runtime_checkable
class TaskMetadata(Protocol):
    """`Task`の非raw17フィールドを読み取り専用propertyとして公開する契約。"""

    @property
    def issue_number(self) -> int: ...
    @property
    def subtask_id(self) -> str: ...
    @property
    def footprint(self) -> tuple[str, ...]: ...
    @property
    def symbols(self) -> tuple[str, ...]: ...
    @property
    def risk(self) -> bool: ...
    @property
    def priority(self) -> str: ...
    @property
    def progress_partial(self) -> bool: ...
    @property
    def status_labels(self) -> tuple[str, ...]: ...
    @property
    def created_at(self) -> str: ...
    @property
    def yaml_error(self) -> bool: ...
    @property
    def parent_number(self) -> int | None: ...
    @property
    def issue_state(self) -> str: ...
    @property
    def parent_state(self) -> str | None: ...
    @property
    def shared_contract(self) -> str | None: ...
    @property
    def writes_shared_contract(self) -> bool: ...
    @property
    def execution_profile(self) -> str | None: ...
    @property
    def model_tier(self) -> str | None: ...


@dataclass(frozen=True, slots=True)
class CycleTask:
    """`TaskMetadata`を満たすfrozen/slots値型。既定値は`Task`と同一。"""

    issue_number: int
    subtask_id: str
    footprint: tuple[str, ...]
    symbols: tuple[str, ...]
    risk: bool
    priority: str
    progress_partial: bool
    status_labels: tuple[str, ...]
    created_at: str
    yaml_error: bool = False
    parent_number: int | None = None
    issue_state: str = "OPEN"
    parent_state: str | None = None
    shared_contract: str | None = None
    writes_shared_contract: bool = False
    execution_profile: str | None = None
    model_tier: str | None = None

    @classmethod
    def from_task(cls, task: Task) -> CycleTask:
        """`task`の非raw値だけをコピーする（`depends_on`等は読まない）。"""
        return cls(
            issue_number=task.issue_number,
            subtask_id=task.subtask_id,
            footprint=tuple(task.footprint),
            symbols=tuple(task.symbols),
            risk=task.risk,
            priority=task.priority,
            progress_partial=task.progress_partial,
            status_labels=tuple(task.status_labels),
            created_at=task.created_at,
            yaml_error=task.yaml_error,
            parent_number=task.parent_number,
            issue_state=task.issue_state,
            parent_state=task.parent_state,
            shared_contract=task.shared_contract,
            writes_shared_contract=task.writes_shared_contract,
            execution_profile=task.execution_profile,
            model_tier=task.model_tier,
        )

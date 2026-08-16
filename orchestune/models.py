"""ドメインモデル。他のorchestuneモジュールに一切依存しないL0インフラ層に属する。

L1アダプタ（`forge.py`）が`IssueRecord` / `PrRecord`を返すため、これらのDTOは
アダプタより下に位置している必要がある（詳細は`docs/ja/architecture.md`第5節）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    model: str | None = None
    cost_usd: float | None = None


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


@dataclass(frozen=True)
class IssueRecord:
    number: int
    title: str
    body: str
    labels: tuple[str, ...]
    created_at: str
    state: str = "OPEN"
    parent: dict | None = None
    blocked_by: tuple[int, ...] = ()


@dataclass(frozen=True)
class PrRecord:
    """A GitHub pull request represented as a domain data transfer object."""

    number: int
    head_ref: str
    changed_files: tuple[str, ...]
    created_at: str = ""
    closes_issue_numbers: tuple[int, ...] = ()
    review_decision: str = ""
    is_ci_passing: bool = True
    state: str = "OPEN"
    closed_at: str = ""
    base_ref: str = ""
    is_cross_repository: bool | None = None
    is_files_truncated: bool = False

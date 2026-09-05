"""ドメインモデル。他のorchestuneモジュールに一切依存しないL0インフラ層に属する。

L1アダプタ（`forge.py`）が`IssueRecord` / `PrRecord`を返すため、これらのDTOは
アダプタより下に位置している必要がある（詳細は`docs/ja/architecture.md`第4節）。
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
    # #799: 本文の`depends_on`（subtask_id文字列、1分解計画内でのみ一意）と
    # ネイティブSub-issue関係由来の`native_depends_on`（Issue番号、常に一意）は
    # 別フィールドとして保持する。ここで両方をsubtask_id文字列へ統合してしまうと、
    # 別EPICが同名subtask_idを使った場合に依存解決が取り違わる
    # （`orchestune.dispatch.dependency_resolution`が両方を見て解決する）。
    depends_on: tuple[str, ...] = ()
    native_depends_on: tuple[int, ...] = ()
    yaml_error: bool = False
    parent_number: int | None = None
    issue_state: str = "OPEN"
    parent_state: str | None = None
    shared_contract: str | None = None
    writes_shared_contract: bool = False
    execution_profile: str | None = None
    model_tier: str | None = None


def normalize_newlines(text: str) -> str:
    """#664: 改行をLFへ正規化する。

    GitHubはIssue本文をCRLFで保存・返却するが、本文中のマーカーブロックを
    読み書きする正規表現（`issue_parsing`）はLFを前提にしている。読み出しの
    時点でLFへ揃えることで、往復（読み→書き）をLFで閉じる。
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


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

    def __post_init__(self) -> None:
        """#664: `body`の改行をLFへ正規化した状態を不変条件にする。

        Forge実装が複数（`GitHubForge`のGraphQL/REST経路、テストのFakeForge）
        あるため、生成箇所ごとの正規化漏れを防ぐにはDTO側で閉じるのが確実。
        """
        normalized = normalize_newlines(self.body)
        if normalized != self.body:
            object.__setattr__(self, "body", normalized)


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
    title: str = ""
    body: str = ""

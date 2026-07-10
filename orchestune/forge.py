from __future__ import annotations

import json
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass

from orchestune.github import _validate_label


class ForgeAuthError(RuntimeError):
    """フォージCLI(gh等)が未インストール、または未認証の場合に送出する。"""


@dataclass(frozen=True)
class LabelSpec:
    name: str
    color: str
    description: str


@dataclass(frozen=True)
class BootstrapResult:
    created_labels: tuple[str, ...]
    existing_labels: tuple[str, ...]


class Forge(ABC):
    @abstractmethod
    def check_auth(self) -> None:
        """認証が利用できない場合はForgeAuthErrorを送出する。"""

    @abstractmethod
    def ensure_labels(self, labels: tuple[LabelSpec, ...]) -> BootstrapResult:
        """未作成のラベルのみ作成する（既存ラベルは変更しない）。"""


class GitHubForge(Forge):
    def check_auth(self) -> None:
        if shutil.which("gh") is None:
            raise ForgeAuthError(
                "gh CLIが見つかりません。https://cli.github.com/ からインストールしてください。"
            )

        # `gh auth status`の非0終了は「未認証」という想定内の結果であり、
        # github.pyの`_run`(check=True)のような例外的失敗とは扱いを分ける。
        result = subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True
        )
        if result.returncode != 0:
            raise ForgeAuthError(
                f"gh認証が未設定です。`gh auth login`を実行してください: {result.stderr.strip()}"
            )

    def ensure_labels(self, labels: tuple[LabelSpec, ...]) -> BootstrapResult:
        for label in labels:
            _validate_label(label.name)

        existing_names = self._list_existing_label_names()

        created: list[str] = []
        existing: list[str] = []
        for label in labels:
            if label.name in existing_names:
                existing.append(label.name)
                continue
            subprocess.run(
                [
                    "gh",
                    "label",
                    "create",
                    label.name,
                    "--color",
                    label.color,
                    "--description",
                    label.description,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            created.append(label.name)

        return BootstrapResult(
            created_labels=tuple(created), existing_labels=tuple(existing)
        )

    def _list_existing_label_names(self) -> set[str]:
        result = subprocess.run(
            ["gh", "label", "list", "--json", "name", "--limit", "100"],
            capture_output=True,
            text=True,
            check=True,
        )
        raw = json.loads(result.stdout)
        return {entry["name"] for entry in raw}


REQUIRED_LABELS: tuple[LabelSpec, ...] = (
    LabelSpec(
        "status:queued", "0E8A16", "Issue is ready to be picked up by the dispatcher"
    ),
    LabelSpec(
        "status:blocked", "B60205", "Issue is blocked on unresolved dependencies"
    ),
    LabelSpec(
        "status:blocked-recompute", "B60205", "Blocked pending DAG recomputation"
    ),
    LabelSpec("status:blocked-human-review", "B60205", "Blocked pending human review"),
    LabelSpec("status:done", "0E8A16", "Subtask work is complete"),
    LabelSpec(
        "status:external-lock",
        "5319E7",
        "Blocked by an externally-held footprint lock",
    ),
    LabelSpec(
        "status:force-serial",
        "5319E7",
        "Forced to run serially after recompute retries exhausted",
    ),
    LabelSpec(
        "status:in-progress", "1D76DB", "Currently being worked by a dispatched agent"
    ),
    LabelSpec(
        "status:manual-merge-required", "FBCA04", "Needs a human to manually merge"
    ),
    LabelSpec("status:not-needed", "CCCCCC", "Subtask determined to be unnecessary"),
    LabelSpec("priority:high", "D93F0B", "High priority subtask"),
    LabelSpec("priority:medium", "FBCA04", "Medium priority subtask"),
    LabelSpec("priority:low", "C2E0C6", "Low priority subtask"),
    LabelSpec("risk:flagged", "E11D21", "Flagged as risky by the decomposition step"),
    LabelSpec(
        "progress:partial", "BFD4F2", "Partial progress recorded on this subtask"
    ),
    LabelSpec(
        "not-needed-review:passed",
        "0E8A16",
        "Not-needed determination verified as correct",
    ),
    LabelSpec(
        "not-needed-review:failed",
        "B60205",
        "Not-needed determination verified as incorrect",
    ),
)

"""GitHub repository administration operations and Forge bootstrap values."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from orchestune.infra.execution_deadline import (
    command_timeout_signal,
    scoped_command_timeout,
)
from orchestune.labels import StatusLabel
from orchestune.validation import validate_label


class ForgeError(RuntimeError):
    """フォージ操作(gh CLI呼び出し等)が失敗した場合に送出する。"""


class ForgeAuthError(ForgeError):
    """フォージCLI(gh等)が未インストール、または未認証の場合に送出する。"""


class RelationshipUnavailableError(ForgeError):
    """ネイティブなIssue関係操作をForgeが提供できないことを示す。"""


_LABEL_LIST_LIMIT = 1000


@contextmanager
def _scoped_gh(stage: str) -> Iterator[dict[str, Any]]:
    """Yield ``subprocess.run`` kwargs bounding a ``gh`` call by the Integrator scope.

    Outside a scope nothing is added. A scope-bound timeout surfaces as an
    ``ExecutionInterrupt`` so best-effort ``except Exception`` code cannot absorb it
    (#820). The ``gh`` argv stays a literal at each call site (architecture partition).
    """
    timeout = scoped_command_timeout(None, f"gh {stage}")
    try:
        yield {} if timeout is None else {"timeout": timeout}
    except subprocess.TimeoutExpired as error:
        if timeout is None:
            raise
        raise command_timeout_signal(f"gh {stage}", timeout, error) from error


@dataclass(frozen=True)
class LabelSpec:
    name: str
    color: str
    description: str


@dataclass(frozen=True)
class BootstrapResult:
    created_labels: tuple[str, ...]
    existing_labels: tuple[str, ...]


class GitHubRepoAdminMixin:
    """Repository-admin implementation mixed into :class:`GitHubForge`."""

    def check_auth(self) -> None:
        if shutil.which("gh") is None:
            raise ForgeAuthError(
                "gh CLIが見つかりません。https://cli.github.com/ からインストールしてください。"
            )
        with _scoped_gh("auth status") as bound:
            result = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                **bound,
            )
        if result.returncode != 0:
            raise ForgeAuthError(
                f"gh認証が未設定です。`gh auth login`を実行してください: {result.stderr.strip()}"
            )

    def ensure_labels(self, labels: tuple[LabelSpec, ...]) -> BootstrapResult:
        for label in labels:
            validate_label(label.name)
        existing_names = self._list_existing_label_names()
        created: list[str] = []
        existing: list[str] = []
        for label in labels:
            if label.name in existing_names:
                existing.append(label.name)
                continue
            with _scoped_gh("label create") as bound:
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
                    encoding="utf-8",
                    errors="replace",
                    check=True,
                    **bound,
                )
            created.append(label.name)
        return BootstrapResult(
            created_labels=tuple(created), existing_labels=tuple(existing)
        )

    def _list_existing_label_names(self) -> set[str]:
        with _scoped_gh("label list") as bound:
            result = subprocess.run(
                [
                    "gh",
                    "label",
                    "list",
                    "--json",
                    "name",
                    "--limit",
                    str(_LABEL_LIST_LIMIT),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
                **bound,
            )
        raw = json.loads(result.stdout)

        if len(raw) >= _LABEL_LIST_LIMIT:
            raise ForgeError(
                f"既存ラベル一覧の取得件数が上限({_LABEL_LIST_LIMIT}件)に達しました。"
                "取得が打ち切られた(truncateされた)可能性があるため、"
                "誤って重複ラベルを作成しないようbootstrapを中断します。"
            )
        return {entry["name"] for entry in raw}


REQUIRED_LABELS: tuple[LabelSpec, ...] = (
    LabelSpec(
        StatusLabel.QUEUED, "0E8A16", "Issue is ready to be picked up by the dispatcher"
    ),
    LabelSpec(
        StatusLabel.BLOCKED, "B60205", "Issue is blocked on unresolved dependencies"
    ),
    LabelSpec(
        StatusLabel.BLOCKED_RECOMPUTE,
        "B60205",
        "Blocked pending Conflict Graph recomputation",
    ),
    LabelSpec(
        StatusLabel.BLOCKED_HUMAN_REVIEW, "B60205", "Blocked pending human review"
    ),
    LabelSpec(StatusLabel.DONE, "0E8A16", "Subtask work is complete"),
    LabelSpec(
        StatusLabel.EXTERNAL_LOCK,
        "5319E7",
        "Blocked by an externally-held footprint lock",
    ),
    LabelSpec(
        StatusLabel.FORCE_SERIAL,
        "5319E7",
        "Forced to run serially after recompute retries exhausted",
    ),
    LabelSpec(
        StatusLabel.IN_PROGRESS,
        "1D76DB",
        "Currently being worked by a dispatched agent",
    ),
    LabelSpec(
        StatusLabel.MANUAL_MERGE_REQUIRED, "FBCA04", "Needs a human to manually merge"
    ),
    LabelSpec(StatusLabel.NOT_NEEDED, "CCCCCC", "Subtask determined to be unnecessary"),
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
    LabelSpec(
        "integration:included",
        "BFD4F2",
        "Already merged into an integration branch/PR by the Integrator",
    ),
    LabelSpec(
        "integration:parent-branch-stale",
        "B60205",
        "Parent branch push was rejected (CAS) in the previous integration cycle",
    ),
    LabelSpec(
        "integration:finalization-blocked",
        "B60205",
        "Child branch deletion was refused repeatedly by the remote; needs a human",
    ),
    LabelSpec(
        "ci:base-branch-red",
        "B60205",
        "CI failed due to base branch failure; blocked until base_sha advances",
    ),
)

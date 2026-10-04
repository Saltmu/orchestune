"""Interactive wizard state machine and prompt loop."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from orchestune.config_wizard.document import ConfigDocument
from orchestune.config_wizard.questions import (
    ADVANCED_CATEGORIES,
    ADVANCED_QUESTIONS,
    BASIC_QUESTIONS,
    QuestionSpec,
)
from orchestune.config_wizard.storage import (
    SaveReceipt,
    save_config_document,
)
from orchestune.config_wizard.validation import (
    diagnose_existing_document,
    validate_candidate_document,
)


class PromptPort:
    """Protocol / base interface for interacting with the user."""

    def prompt_text(
        self, message: str, default: str | None = None, allow_empty: bool = True
    ) -> str | None:
        raise NotImplementedError

    def prompt_choice(
        self,
        message: str,
        choices: list[str],
        default: str | None = None,
        allow_empty: bool = True,
    ) -> str | None:
        raise NotImplementedError

    def prompt_integer(
        self,
        message: str,
        default: int | None = None,
        min_val: int | None = None,
        max_val: int | None = None,
        allow_empty: bool = True,
    ) -> int | None:
        raise NotImplementedError

    def prompt_array(
        self, message: str, current: list[str] | None = None
    ) -> list[str] | None:
        raise NotImplementedError

    def prompt_confirm(self, message: str, default: bool = False) -> bool:
        raise NotImplementedError

    def display_message(self, msg: str) -> None:
        raise NotImplementedError

    def display_diff(self, diff_str: str) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class WizardResult:
    status: Literal["saved", "cancelled", "unchanged"]
    receipt: SaveReceipt | None = None
    message: str = ""


def _ask_single_question(q: QuestionSpec, current_val: Any, prompt: PromptPort) -> Any:
    """Ask a single question and return the new value, or current_val if kept."""
    default_code = q.get_code_default()
    hint = f" [現在値: {current_val}]" if current_val is not None else ""
    if current_val is None and default_code is not None:
        hint = f" [既定値: {default_code}]"

    msg = f"{q.label}{hint}\n  {q.help_text}"

    if q.input_kind == "choice":
        assert q.choices is not None
        dflt_str = str(current_val) if current_val is not None else None
        return prompt.prompt_choice(
            msg, q.choices, default=dflt_str, allow_empty=q.allow_unset
        )

    if q.input_kind == "integer":
        dflt_int = (
            int(current_val)
            if current_val is not None
            else (default_code if not q.allow_unset else None)
        )
        return prompt.prompt_integer(
            msg,
            default=dflt_int,
            min_val=q.min_value,
            max_val=q.max_value,
            allow_empty=q.allow_unset,
        )

    if q.input_kind == "text":
        dflt_str = str(current_val) if current_val is not None else None
        return prompt.prompt_text(msg, default=dflt_str, allow_empty=q.allow_unset)

    return current_val


def _run_basic_questions(doc: ConfigDocument, prompt: PromptPort) -> None:
    for q in BASIC_QUESTIONS:
        curr = doc.get_value(q.key)
        new_val = _ask_single_question(q, curr, prompt)
        if new_val is not None:
            doc.set_value(q.key, new_val)
        elif curr is not None and new_val is None and q.allow_unset:
            doc.delete_value(q.key)


def _handle_preview_and_save(
    doc: ConfigDocument, prompt: PromptPort, mode: Literal["init", "edit"]
) -> WizardResult | None:
    candidate_str = doc.to_toml_string()
    ok, errors = validate_candidate_document(candidate_str)
    if not ok:
        prompt.display_message("\n[エラー] 設定検証に失敗しました。修正が必要です:")
        for err in errors:
            prompt.display_message(f" - {err}")
        return None

    if (
        mode == "edit"
        and doc.snapshot.raw_bytes is not None
        and candidate_str.encode("utf-8") == doc.snapshot.raw_bytes
    ):
        prompt.display_message("変更はありませんでした。")
        return WizardResult(status="unchanged")

    prompt.display_message("\n--- 設定差分プレビュー ---")
    diff = doc.generate_diff(candidate_str)
    if diff:
        prompt.display_diff(diff)
    else:
        prompt.display_message("(新規作成)")
        prompt.display_diff(candidate_str)

    confirm_choice = prompt.prompt_choice(
        "保存しますか？",
        choices=["save", "edit", "cancel"],
        default="cancel",
    )

    if confirm_choice == "save":
        receipt = save_config_document(
            doc.target_path,
            doc.snapshot,
            candidate_str.encode("utf-8"),
            mode=mode,
        )
        prompt.display_message("設定を保存しました。")
        if receipt.backup_path:
            prompt.display_message(f"バックアップ: {receipt.backup_path}")
        return WizardResult(status="saved", receipt=receipt)
    if confirm_choice == "edit":
        return None

    prompt.display_message("取消。設定は保存していません")
    return WizardResult(status="cancelled")


def run_config_wizard(
    mode: Literal["init", "edit"],
    project_dir: Path,
    prompt: PromptPort,
) -> WizardResult:
    """Run the interactive configuration wizard."""
    try:
        doc = ConfigDocument.load(project_dir, mode=mode)
    except Exception as exc:
        prompt.display_message(f"設定の読み込みに失敗しました: {exc}")
        raise

    try:
        prompt.display_message("=== Orchestune 設定ウィザード ===")
        prompt.display_message(f"対象プロジェクト: {project_dir}")
        prompt.display_message(f"保存先: {doc.target_path}")

        if doc.source_type == "pyproject.toml":
            prompt.display_message(
                "pyproject.toml の [tool.orchestune] を初期値として読み込みました。\n"
                "保存後、新規の orchestune.toml は pyproject.toml 側の設定全体に優先します。"
            )
        elif doc.source_type == "orchestune.toml":
            prompt.display_message("既存の orchestune.toml を編集します。")

        diagnostics = diagnose_existing_document(doc.doc.unwrap())
        if diagnostics:
            prompt.display_message("\n[警告] 既存設定に診断メッセージがあります:")
            for d in diagnostics:
                prompt.display_message(f" - {d}")

        prompt.display_message("\n--- 基本設定 ---")
        _run_basic_questions(doc, prompt)

        while True:
            prompt.display_message("\n--- メインメニュー ---")
            action = prompt.prompt_choice(
                "次の操作を選択してください:",
                choices=["preview", "advanced", "basic", "cancel"],
                default="preview",
            )
            if action in ("cancel", None):
                prompt.display_message("取消。設定は保存していません")
                return WizardResult(status="cancelled")

            if action == "basic":
                prompt.display_message("\n--- 基本設定の再編集 ---")
                _run_basic_questions(doc, prompt)
            elif action == "advanced":
                _run_advanced_menu(doc, prompt)
            elif action == "preview":
                res = _handle_preview_and_save(doc, prompt, mode)
                if res is not None:
                    return res

    except (KeyboardInterrupt, EOFError):
        prompt.display_message("\n取消。設定は保存していません")
        return WizardResult(status="cancelled")


def _run_advanced_menu(doc: ConfigDocument, prompt: PromptPort) -> None:
    """Sub-menu loop for advanced configuration categories."""
    while True:
        cat_choices = [cat[0] for cat in ADVANCED_CATEGORIES] + ["back"]
        prompt.display_message("\n--- 詳細設定メニュー ---")
        for code, label in ADVANCED_CATEGORIES:
            prompt.display_message(f" - {code}: {label}")

        selected = prompt.prompt_choice(
            "編集するカテゴリを選択してください:", choices=cat_choices, default="back"
        )
        if selected is None or selected == "back":
            break

        questions = ADVANCED_QUESTIONS.get(selected, [])
        for q in questions:
            curr = doc.get_value(q.key)
            new_val = _ask_single_question(q, curr, prompt)
            if new_val is not None:
                doc.set_value(q.key, new_val)
            elif curr is not None and new_val is None and q.allow_unset:
                doc.delete_value(q.key)

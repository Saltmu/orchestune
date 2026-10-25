"""CLI entrypoint for orchestune config init and orchestune config edit."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from orchestune.config_contracts import ConfigError
from orchestune.config_wizard.storage import (
    ConfigConflictError,
    ConfigStorageError,
)
from orchestune.config_wizard.wizard import (
    PromptPort,
    run_config_wizard,
)
from orchestune.infra.cli_output import Level, make_output


def _report_error(text: str) -> None:
    make_output(sys.stderr).message(Level.FAILURE, text)


def resolve_project_dir(
    explicit: Path | str | None,
    cwd: Path | None = None,
) -> Path:
    """Resolve project directory from explicit flag or nearest .git marker."""
    if explicit is not None:
        p = Path(explicit).resolve()
        if not p.exists() or not p.is_dir():
            _report_error(
                f"Error: Project directory '{explicit}' does not exist or is not a directory."
            )
            sys.exit(2)
        return p

    start = (cwd or Path.cwd()).resolve()
    for parent in [start] + list(start.parents):
        git_marker = parent / ".git"
        if git_marker.exists() or git_marker.is_symlink():
            return parent

    return start


class TerminalPromptPort(PromptPort):
    """Real interactive prompt implementation using stdin and stdout."""

    def prompt_text(
        self, message: str, default: str | None = None, allow_empty: bool = True
    ) -> str | None:
        dflt_hint = f" [{default}]" if default is not None else ""
        prompt_str = f"{message}{dflt_hint}\n> "
        while True:
            val = input(prompt_str).strip()
            if not val:
                if default is not None:
                    return default
                if allow_empty:
                    return None
                self.display_message("値を入力してください。")
                continue
            return val

    def prompt_choice(
        self,
        message: str,
        choices: list[str],
        default: str | None = None,
        allow_empty: bool = True,
    ) -> str | None:
        dflt_hint = f" [既定: {default}]" if default is not None else ""
        options = "\n".join(
            f"  {idx}) {c}{' *' if c == default else ''}"
            for idx, c in enumerate(choices, start=1)
        )
        self.display_message(f"\n{message}{dflt_hint}\n{options}")

        while True:
            raw = input("番号または選択肢を入力 (Enterで既定): ").strip()
            if not raw:
                if default is not None:
                    return default
                if allow_empty:
                    return None
                self.display_message("選択肢を入力してください。")
                continue

            if raw.isdigit():
                num = int(raw)
                if 1 <= num <= len(choices):
                    return choices[num - 1]

            if raw in choices:
                return raw

            self.display_message(
                f"無効な入力です。1〜{len(choices)}の番号または選択肢名を入力してください。"
            )

    def prompt_integer(
        self,
        message: str,
        default: int | None = None,
        min_val: int | None = None,
        max_val: int | None = None,
        allow_empty: bool = True,
    ) -> int | None:
        dflt_hint = f" [既定: {default}]" if default is not None else ""
        prompt_str = f"{message}{dflt_hint}\n> "
        while True:
            raw = input(prompt_str).strip()
            if not raw:
                if default is not None:
                    return default
                if allow_empty:
                    return None
                self.display_message("数値を入力してください。")
                continue

            try:
                val = int(raw)
            except ValueError:
                self.display_message("整数を入力してください。")
                continue

            if min_val is not None and val < min_val:
                self.display_message(f"{min_val}以上の整数を入力してください。")
                continue
            if max_val is not None and val > max_val:
                self.display_message(f"{max_val}以下の整数を入力してください。")
                continue
            return val

    def prompt_array(
        self, message: str, current: list[str] | None = None
    ) -> list[str] | None:
        curr = list(current or [])
        self.display_message(f"\n{message}\n現在の値: {curr}")
        while True:
            ans = input("新しい要素を追加 (空Enterで完了, 'clear'で全削除): ").strip()
            if not ans:
                break
            if ans == "clear":
                curr = []
                self.display_message("クリアしました。")
                continue
            curr.append(ans)
        return curr if curr else None

    def prompt_confirm(self, message: str, default: bool = False) -> bool:
        hint = " [Y/n]" if default else " [y/N]"
        while True:
            raw = input(f"{message}{hint}: ").strip().lower()
            if not raw:
                return default
            if raw in ("y", "yes"):
                return True
            if raw in ("n", "no"):
                return False
            self.display_message("'y' または 'n' を入力してください。")

    def display_message(self, msg: str) -> None:
        out = make_output(sys.stdout)
        out.emit(out.text_block(msg))

    def display_diff(self, diff_str: str) -> None:
        out = make_output(sys.stdout)
        out.emit(out.text_block(diff_str))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchestune config",
        description="orchestune.toml を対話形式で新規作成または編集します。",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    init_parser = subparsers.add_parser(
        "init",
        help="新しい orchestune.toml を対話形式で作成します。",
    )
    init_parser.add_argument(
        "--project-dir",
        type=Path,
        default=None,
        help="対象プロジェクトディレクトリ（未指定時は最寄りのGitルートまたはカレントディレクトリ）",
    )

    edit_parser = subparsers.add_parser(
        "edit",
        help="既存の orchestune.toml を対話形式で編集します。",
    )
    edit_parser.add_argument(
        "--project-dir",
        type=Path,
        default=None,
        help="対象プロジェクトディレクトリ（未指定時は最寄りのGitルートまたはカレントディレクトリ）",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        make_output(sys.stderr).message(
            Level.FAILURE,
            "エラー: orchestune config は対話環境 (TTY) でのみ実行できます。",
            detail="非対話環境での利用はサポートされていません。直接 orchestune.toml を編集してください。",
        )
        return 2

    project_dir = resolve_project_dir(args.project_dir)
    prompt = TerminalPromptPort()

    try:
        run_config_wizard(
            mode=args.subcommand,
            project_dir=project_dir,
            prompt=prompt,
        )
        return 0
    except ConfigConflictError as exc:
        _report_error(f"競合エラー: {exc}")
        return 3
    except ConfigError as exc:
        _report_error(f"設定エラー: {exc}")
        return 2
    except ConfigStorageError as exc:
        _report_error(f"ストレージエラー: {exc}")
        return 1
    except KeyboardInterrupt:
        err = make_output(sys.stderr)
        err.emit(err.text_block(""))
        err.message(Level.WARNING, "中断されました (Ctrl+C)。設定は保存していません。")
        return 130
    except EOFError:
        err = make_output(sys.stderr)
        err.emit(err.text_block(""))
        err.message(Level.WARNING, "EOFが入力されました。設定は保存していません。")
        return 0
    except Exception as exc:
        _report_error(f"予期しないエラー: {exc}")
        return 1

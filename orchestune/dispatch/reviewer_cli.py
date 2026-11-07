"""Read-only `orchestune reviewer resolve` for interactive review selection."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from orchestune.claim.workspace import resolve_checkout_roots
from orchestune.config_contracts import ConfigError
from orchestune.dispatch.config.schema import (
    extract_reviewer_by_target,
    validate_toml_config,
)
from orchestune.dispatch.reviewer import resolve_reviewer_bot
from orchestune.infra.repository_config import find_and_load_config_file
from orchestune.targets.contracts import ReviewerBot, ReviewerBotSetting

_OVERRIDES = ("claude", "codex", "skip")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchestune reviewer",
        description=(
            "Resolve the interactive review bot from checkout config (read-only)."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    resolve = commands.add_parser(
        "resolve",
        help="print claude, codex, or skip for one target",
        description=(
            "Read checkout config and print one reviewer. "
            "Does not post to GitHub or change files."
        ),
    )
    resolve.add_argument(
        "--target",
        required=True,
        help="concrete execution target, for example codex-cli",
    )
    resolve.add_argument(
        "--override",
        choices=_OVERRIDES,
        help="explicit claude, codex, or skip; wins over config",
    )
    resolve.add_argument("--json", action="store_true", help="print a JSON object")
    return parser


def _config_source(checkout_root: Path) -> str:
    if (checkout_root / "orchestune.toml").exists():
        return "orchestune.toml"
    if (checkout_root / "pyproject.toml").exists():
        return "pyproject.toml"
    return "none"


def _load_config(checkout_root: Path) -> dict[str, Any]:
    raw = find_and_load_config_file(checkout_root)
    if not raw:
        return {}
    return validate_toml_config(raw)


def _selection_source(
    setting: str,
    target: str,
    table: Mapping[str, ReviewerBot],
    override: str | None,
) -> str:
    if override is not None:
        return "override"
    if setting in {"claude", "codex"}:
        return "reviewer-bot"
    if target in table:
        return "reviewer_by_target"
    return "builtin"


def _resolved(loaded: Mapping[str, Any], target: str) -> tuple[str, str]:
    table = extract_reviewer_by_target(dict(loaded))
    setting = cast(ReviewerBotSetting, loaded.get("reviewer_bot", "auto"))
    source = _selection_source(setting, target, table, None)
    reviewer = resolve_reviewer_bot(setting, target, table)
    if reviewer is None:
        raise ValueError(target)
    return reviewer, source


def _emit(payload: dict[str, str], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return
    print(payload["reviewer"])


def _checkout(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    _primary, checkout = resolve_checkout_roots(Path.cwd())
    return checkout


def _payload(
    root: Path, target: str, reviewer: str, source: str, config_source: str
) -> dict[str, str]:
    return {
        "config_source": config_source,
        "reviewer": reviewer,
        "source": source,
        "target": target,
    }


def _with_override(root: Path, target: str, override: str) -> dict[str, str]:
    try:
        _load_config(root)
    except ConfigError:
        return _payload(root, target, override, "override", "unreadable")
    return _payload(root, target, override, "override", _config_source(root))


def main(
    argv: Sequence[str] | None = None, *, checkout_root: Path | None = None
) -> int:
    """Print one reviewer, or exit 1 without guessing when resolution fails."""
    args = _parser().parse_args(argv)
    root = _checkout(checkout_root)
    if args.override is not None:
        _emit(_with_override(root, args.target, args.override), args.json)
        return 0
    try:
        reviewer, source = _resolved(_load_config(root), args.target)
    except ConfigError as error:
        print(f"orchestune reviewer: {error}", file=sys.stderr)
        return 1
    except ValueError:
        print(
            "orchestune reviewer: cannot resolve a reviewer for target "
            f"{args.target!r}",
            file=sys.stderr,
        )
        return 1
    _emit(
        _payload(root, args.target, reviewer, source, _config_source(root)),
        args.json,
    )
    return 0

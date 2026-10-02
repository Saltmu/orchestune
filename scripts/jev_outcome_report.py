"""Read-only report joining Jev evaluation logs with PR review-thread outcomes.

Each row of ``.orchestune/jev/evaluations.jsonl`` is matched (PR number, path and
normalized comment body) to the review thread on GitHub and labelled with what
happened to it: ``resolved`` / ``outdated`` / ``replied`` / ``unresolved``, or
``unmatched`` when no thread could be found. The report cross-tabulates these
labels against Jev's ``decision_reason`` and ``validity`` band.

Usage (run from the repository root)::

    uv run python -m scripts.jev_outcome_report
    uv run python -m scripts.jev_outcome_report --log a.jsonl --log b.jsonl
    uv run python -m scripts.jev_outcome_report --format json
    uv run python -m scripts.jev_outcome_report --threads-json DIR  # offline

``--threads-json DIR`` reads ``DIR/<pr>.json`` (the ``reviewThreads`` nodes as
returned by GitHub GraphQL) instead of calling ``gh``. The script only runs a
GraphQL *query*; it never writes to GitHub or to the evaluation log.

The labels are **not** ground truth. A resolved thread does not prove the finding
was valid and an ignored one does not prove it was not. Jev's decision is advisory:
``filtered`` findings stay in ``inline_comments`` and the agent still sees them, but
the Jev label may sway how it judges them, so outcomes mix Jev's accuracy with its
influence on the agent. Treat the output as a rough signal and sample-check by hand.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts.jev_filter import DEFAULT_JEV_LOG_PATH, _shared_log_root

LABELS = ("resolved", "outdated", "replied", "unresolved", "unmatched")
VALIDITY_BANDS = ("<0.4", "0.4-0.6", "0.6-0.8", ">=0.8", "n/a")
MAX_THREAD_PAGES = 20
GH_TIMEOUT_SECONDS = 60

THREADS_QUERY = (
    "query($owner:String!,$repo:String!,$number:Int!,$cursor:String){"
    "repository(owner:$owner,name:$repo){pullRequest(number:$number){"
    "reviewThreads(first:100,after:$cursor){"
    "pageInfo{hasNextPage endCursor}"
    "nodes{isResolved isOutdated comments(first:50){nodes{body path line originalLine}}}"
    "}}}}"
)

CAVEATS = (
    "ラベルは有効性の証拠ではありません。resolved でも指摘が妥当だったとは限らず、"
    "未解決・無視でも妥当でなかったとは限りません。",
    "交絡: Jev の判定は助言であり、filtered の指摘も inline_comments に残ってエージェントが"
    "確認します。ただし判定結果が対応判断に影響しうるため、帰結には Jev の精度と判定が"
    "与えた影響が混ざります。少数を人手で確認してください。",
    "applicability（SPECULATIVE など）の妥当性はコード上の事実の問題で、PR の経過では測れません。",
)


class ThreadFetchError(Exception):
    """Review threads for a PR could not be obtained."""


@dataclass
class LoadedLog:
    records: list[dict[str, Any]] = field(default_factory=list)
    skipped_lines: int = 0
    missing_files: list[str] = field(default_factory=list)


@dataclass
class Thread:
    path: str
    body: str
    line: int | None
    original_line: int | None
    outdated: bool
    resolved: bool
    comment_count: int


def normalize_body(text: Any) -> str:
    return " ".join(str(text or "").split())


def load_log(paths: Iterable[Path]) -> LoadedLog:
    loaded = LoadedLog()
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            loaded.missing_files.append(str(path))
            continue
        for raw in text.splitlines():
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except ValueError:
                loaded.skipped_lines += 1
                continue
            if isinstance(row, dict):
                loaded.records.append(row)
            else:
                loaded.skipped_lines += 1
    return loaded


def _finding_key(row: dict[str, Any]) -> tuple[Any, str, str]:
    return (
        row.get("pr"),
        str(row.get("path") or ""),
        normalize_body(row.get("comment")),
    )


def dedupe(rows: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Keep the latest evaluation per finding; return (rows, collapsed count)."""
    latest: dict[tuple[Any, str, str], dict[str, Any]] = {}
    for row in rows:
        key = _finding_key(row)
        current = latest.get(key)
        if current is None or str(row.get("timestamp") or "") >= str(
            current.get("timestamp") or ""
        ):
            latest[key] = row
    return list(latest.values()), len(rows) - len(latest)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def parse_threads(nodes: Iterable[Any]) -> list[Thread]:
    threads: list[Thread] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        comments = (node.get("comments") or {}).get("nodes") or []
        if not comments or not isinstance(comments[0], dict):
            continue
        first = comments[0]
        threads.append(
            Thread(
                path=str(first.get("path") or ""),
                body=normalize_body(first.get("body")),
                line=_as_int(first.get("line")),
                original_line=_as_int(first.get("originalLine")),
                outdated=bool(node.get("isOutdated")),
                resolved=bool(node.get("isResolved")),
                comment_count=len(comments),
            )
        )
    return threads


def classify(thread: Thread) -> str:
    if thread.resolved:
        return "resolved"
    if thread.outdated:
        return "outdated"
    if thread.comment_count > 1:
        return "replied"
    return "unresolved"


def _line_distance(thread: Thread, row_line: int) -> float:
    # The log stores the comment's current line, or its original line when the
    # current one is gone, so compare against whichever coordinate is closer.
    distances = [
        abs(line - row_line)
        for line in (thread.line, thread.original_line)
        if line is not None
    ]
    return min(distances) if distances else float("inf")


def match_thread(row: dict[str, Any], threads: Sequence[Thread]) -> Thread | None:
    """Return the thread for a finding.

    ``dedupe()`` keeps the latest evaluation of a repeated finding, so among
    identical threads the one closest to the logged line wins and, on a tie, the
    latest posted thread (GitHub returns threads in creation order).
    """
    path = str(row.get("path") or "")
    body = normalize_body(row.get("comment"))
    candidates = [t for t in threads if t.path == path and t.body == body]
    if not candidates:
        return None
    row_line = _as_int(row.get("line"))
    if row_line is None or len(candidates) == 1:
        return candidates[-1]
    return min(reversed(candidates), key=lambda t: _line_distance(t, row_line))


def _run_gh(cmd: list[str]) -> dict[str, Any]:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=GH_TIMEOUT_SECONDS,
        )
        data = json.loads(result.stdout)
    except FileNotFoundError as exc:
        raise ThreadFetchError("gh CLI not found") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().splitlines()[:1]
        raise ThreadFetchError(f"gh failed: {detail[0] if detail else exc}") from exc
    except (subprocess.TimeoutExpired, ValueError) as exc:
        raise ThreadFetchError(f"gh call failed: {exc}") from exc
    if not isinstance(data, dict):
        raise ThreadFetchError("unexpected gh response")
    return data


def fetch_threads(pr: int) -> list[dict[str, Any]]:
    """Fetch all review-thread nodes of a PR with a read-only GraphQL query."""
    nodes: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(MAX_THREAD_PAGES):
        cmd = [
            "gh",
            "api",
            "graphql",
            "-F",
            "owner={owner}",
            "-F",
            "repo={repo}",
            "-F",
            f"number={pr}",
            "-f",
            f"query={THREADS_QUERY}",
        ]
        if cursor:
            cmd += ["-f", f"cursor={cursor}"]
        data = _run_gh(cmd)
        try:
            threads = data["data"]["repository"]["pullRequest"]["reviewThreads"]
            page_nodes = threads["nodes"]
            page_info = threads["pageInfo"]
        except (KeyError, TypeError) as exc:
            raise ThreadFetchError(f"PR #{pr} not found or malformed response") from exc
        nodes.extend(page_nodes)
        if not page_info.get("hasNextPage"):
            return nodes
        cursor = page_info.get("endCursor")
        if not cursor:
            break
    raise ThreadFetchError(f"PR #{pr} review threads exceed the page limit")


def _offline_fetcher(directory: Path) -> Callable[[int], list[dict[str, Any]]]:
    def fetch(pr: int) -> list[dict[str, Any]]:
        try:
            data = json.loads((directory / f"{pr}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ThreadFetchError(f"cannot read {directory / f'{pr}.json'}") from exc
        if not isinstance(data, list):
            raise ThreadFetchError(f"{pr}.json is not a list of threads")
        return data

    return fetch


def _validity_band(row: dict[str, Any]) -> str:
    validity = row.get("validity")
    if (
        row.get("bypassed") is True
        or isinstance(validity, bool)
        or not isinstance(validity, int | float)
    ):
        return "n/a"
    if validity < 0.4:
        return "<0.4"
    if validity < 0.6:
        return "0.4-0.6"
    if validity < 0.8:
        return "0.6-0.8"
    return ">=0.8"


def _bump(table: dict[str, dict[str, int]], key: str, label: str) -> None:
    cell = table.setdefault(key, {})
    cell[label] = cell.get(label, 0) + 1


def build_report(
    rows: Sequence[dict[str, Any]],
    fetcher: Callable[[int], list[dict[str, Any]]],
    *,
    skipped_lines: int = 0,
    duplicates_collapsed: int = 0,
    missing_files: Sequence[str] = (),
) -> dict[str, Any]:
    cache: dict[int, list[Thread]] = {}
    fetch_errors: dict[str, str] = {}
    by_reason: dict[str, dict[str, int]] = {}
    by_band: dict[str, dict[str, int]] = {}
    by_pr: dict[str, dict[str, int]] = {}
    unmatched_reasons: dict[str, int] = {}
    matched = 0

    for row in rows:
        pr = _as_int(row.get("pr"))
        label, why = "unmatched", "no_pr"
        if pr is not None:
            if pr not in cache:
                try:
                    cache[pr] = parse_threads(fetcher(pr))
                except ThreadFetchError as exc:
                    fetch_errors[str(pr)] = str(exc)
                    cache[pr] = []
            if str(pr) in fetch_errors:
                why = "fetch_failed"
            else:
                thread = match_thread(row, cache[pr])
                if thread is None:
                    why = "no_match"
                else:
                    label = classify(thread)
        if label == "unmatched":
            unmatched_reasons[why] = unmatched_reasons.get(why, 0) + 1
        else:
            matched += 1
        reason = str(row.get("decision_reason") or "unknown")
        _bump(by_reason, reason, label)
        _bump(by_band, _validity_band(row), label)
        _bump(by_pr, str(pr), label)

    return {
        "summary": {
            "evaluations": len(rows),
            "matched": matched,
            "unmatched": len(rows) - matched,
            "log_lines_skipped": skipped_lines,
            "duplicates_collapsed": duplicates_collapsed,
            "missing_log_files": list(missing_files),
        },
        "by_decision_reason": by_reason,
        "by_validity_band": by_band,
        "by_pr": by_pr,
        "unmatched_reasons": unmatched_reasons,
        "fetch_errors": fetch_errors,
        "caveats": list(CAVEATS),
    }


def _table(title: str, table: dict[str, dict[str, int]], order: Sequence[str]) -> str:
    keys = [k for k in order if k in table] + sorted(set(table) - set(order))
    lines = [
        f"### {title}",
        "",
        "| " + " | ".join([title.split(" x ")[0], *LABELS, "total"]) + " |",
        "| " + " | ".join(["---"] * (len(LABELS) + 2)) + " |",
    ]
    for key in keys:
        cell = table[key]
        counts = [str(cell.get(label, 0)) for label in LABELS]
        lines.append("| " + " | ".join([key, *counts, str(sum(cell.values()))]) + " |")
    return "\n".join(lines)


def render_markdown(result: dict[str, Any]) -> str:
    summary = result["summary"]
    total = summary["evaluations"]
    matched = summary["matched"]
    pct = f" ({matched / total:.0%})" if total else ""
    parts = [
        "# Jev 評価ログ × PR スレッド帰結レポート",
        "",
        f"- 評価件数（重複集約後）: {total}",
        f"- スレッドと照合できた件数: {matched}/{total}{pct}",
        f"- 重複として集約: {summary['duplicates_collapsed']}",
        f"- 読み飛ばしたログ行: {summary['log_lines_skipped']}",
    ]
    if summary["missing_log_files"]:
        parts.append(f"- 読めなかったログ: {', '.join(summary['missing_log_files'])}")
    if result["unmatched_reasons"]:
        reasons = ", ".join(f"{k}={v}" for k, v in result["unmatched_reasons"].items())
        parts.append(f"- 未照合の内訳: {reasons}")
    parts += [
        "",
        _table(
            "decision_reason x 帰結",
            result["by_decision_reason"],
            ("accepted", "bypass", "low_validity", "low_impact", "speculative"),
        ),
        "",
        _table("validity 帯 x 帰結", result["by_validity_band"], VALIDITY_BANDS),
        "",
        _table("PR x 帰結", result["by_pr"], ()),
    ]
    if result["fetch_errors"]:
        parts += ["", "### 取得エラー", ""]
        parts += [f"- PR {pr}: {msg}" for pr, msg in result["fetch_errors"].items()]
    parts += ["", "## 注意", ""] + [f"- {text}" for text in result["caveats"]]
    return "\n".join(parts) + "\n"


def _default_log_path() -> Path:
    target = Path(os.environ.get("JEV_LOG_PATH") or DEFAULT_JEV_LOG_PATH)
    return target if target.is_absolute() else _shared_log_root() / target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--log",
        action="append",
        type=Path,
        help="evaluation JSONL (repeatable; default: JEV_LOG_PATH or the shared log)",
    )
    parser.add_argument(
        "--threads-json",
        type=Path,
        help="directory of <pr>.json reviewThreads nodes (offline; skips gh)",
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    args = parser.parse_args(argv)

    loaded = load_log(args.log or [_default_log_path()])
    if loaded.missing_files and not loaded.records:
        print(
            "No readable evaluation log: " + ", ".join(loaded.missing_files),
            file=sys.stderr,
        )
        return 2

    rows, collapsed = dedupe(loaded.records)
    fetcher = (
        _offline_fetcher(args.threads_json) if args.threads_json else fetch_threads
    )
    result = build_report(
        rows,
        fetcher,
        skipped_lines=loaded.skipped_lines,
        duplicates_collapsed=collapsed,
        missing_files=loaded.missing_files,
    )
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(result), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())

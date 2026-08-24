"""Data models shared by DAG parsing and graph operations."""

from __future__ import annotations

import posixpath
import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

_IGNORED_FOOTPRINT_PATTERNS = (
    re.compile(r"(^|/)pyproject\.toml$"),
    re.compile(r"(^|/)poetry\.lock$"),
    re.compile(r"(^|/)logging\.py$"),
    re.compile(r"(^|/)logger\.py$"),
    re.compile(r"(^|/)config\.py$"),
    re.compile(r"(^|/)settings\.py$"),
)

# orchestune.toml / pyproject.toml の [tool.orchestune] のキーのうち、
# DAG計算ツール（orchestune-dag/orchestune-provision）専用でdispatcher
# 自身の引数ではないもの（#398/#404/#407）。dispatcher.pyの未知キー検知が
# これらをtypoと誤判定してクラッシュしないよう、ここで一元管理する
# （#415レビュー指摘: `dag_`prefixによる無条件許可は`dag_ignore_pattern`
# のようなtypoまで黙って見逃してしまうため、完全一致リストのまま
# 定義場所だけを一元化し、新しい共有DAGキーを追加するextract_*関数の
# すぐ側に追記させることで手動追記漏れを防ぐ）。
#
# #415レビュー再指摘: dispatcher.pyはこのセットを、他の設定キーのような
# ハイフン→アンダースコア正規化を経た後の名前とではなく、config_dataの
# 生のキー文字列と直接比較する（正規化後に比較すると、`dag_similarity-
# threshold`のような区切り文字混在のtypoまで正規のスペリングへ丸め込まれて
# "unknown key"検知をすり抜けてしまい、しかもextract_*関数は生のキーでしか
# 値を読まないため気づかれないまま黙って無視されてしまう）。そのため
# ここには、extract_*関数が実際に受け付ける2つの正規のスペリング
# （全部アンダースコア／全部ハイフン）を両方とも列挙する。
DAG_TOOL_CONFIG_KEYS = frozenset(
    {
        "dag_ignore_patterns",
        "dag-ignore-patterns",
        "dag_similarity_threshold",
        "dag-similarity-threshold",
    }
)


class ConfigError(ValueError):
    """Raised for invalid `orchestune.toml` / `pyproject.toml` config values.

    A `ValueError` subclass so existing `except ValueError` call sites keep
    working unchanged, but distinct from other `ValueError`s (e.g.
    `DagCycleError`, footprint validation) so callers can catch config
    problems specifically and map them to a dedicated exit code (#428:
    orchestune-dag / orchestune-provision / orchestune-dispatch all exit 2
    for config errors, distinct from exit 1 for other failures).
    """


def compile_extra_ignore_patterns(
    patterns: Iterable[str],
) -> tuple[re.Pattern[str], ...]:
    """Compile user-supplied regex strings for use as extra ignore patterns.

    Raises ConfigError if any pattern is not a valid regular expression.
    """
    # A malicious/pathological pattern doesn't only fail with `re.error`:
    # an oversized repetition count (`a{9999...}`) raises `OverflowError`,
    # and ~500+ nested groups raise `RecursionError` (Codex review, #441).
    # `re.compile` is the only operation here, so any failure compiling
    # user-supplied config data is a config problem, not an internal bug —
    # catch broadly rather than chase the compiler's exception types one by
    # one.
    try:
        return tuple(re.compile(pattern) for pattern in patterns)
    except Exception as e:
        raise ConfigError(f"invalid 'dag_ignore_patterns' regex: {e}") from e


def extract_dag_ignore_patterns(config: dict[str, Any]) -> list[str]:
    """Validate and return the `dag_ignore_patterns` list from a loaded config dict.

    Shared by orchestune-dag (dag_cli.py) and orchestune-dispatch
    (dispatcher.py), which may both read this setting from the same
    orchestune.toml / pyproject.toml `[tool.orchestune]` table.

    Every other dispatcher setting in that file is written with hyphens
    (e.g. `max-concurrent`) and normalized internally, so `dag-ignore-patterns`
    is accepted as an alias for `dag_ignore_patterns` to avoid a silently
    no-op config for users following that convention (#404 review).
    """
    patterns = config.get("dag_ignore_patterns")
    if patterns is None:
        patterns = config.get("dag-ignore-patterns", [])
    # 空文字列はre.compile("")で有効な正規表現になり、あらゆるパスに
    # マッチしてしまう（=全ての類似度競合辺が無診断で消える）ため、
    # 文字列であることに加えて空でないことも検証する（#404レビュー指摘）。
    if not isinstance(patterns, list) or not all(
        isinstance(p, str) and p for p in patterns
    ):
        raise ConfigError(
            "'dag_ignore_patterns' (or 'dag-ignore-patterns') must be a list of "
            "non-empty strings"
        )
    return patterns


def extract_dag_similarity_threshold(config: dict[str, Any]) -> float | None:
    """Validate and return the `dag_similarity_threshold` value from a loaded config dict.

    Shared by orchestune-dag (dag_cli.py) and orchestune-provision
    (provisioning.py, #407) so a threshold chosen via `orchestune-dag
    --threshold` and persisted here is also honored by
    `orchestune-provision`'s own conflict-graph computation, instead of that tool
    silently falling back to `DEFAULT_SIMILARITY_THRESHOLD` and producing a
    a different conflict-edge set than what was validated.

    Follows the same hyphen-alias convention as `dag_ignore_patterns`:
    `dag-similarity-threshold` is accepted as an alias, and the
    underscore form takes precedence when both are present.

    Returns `None` when the key is absent, so callers fall back to their own
    default (typically `DEFAULT_SIMILARITY_THRESHOLD`, or a `--threshold`
    CLI flag when one takes precedence). Raises `ValueError` if present but
    not a number, or outside the [0, 1] similarity-score range.
    """
    value = config.get("dag_similarity_threshold")
    if value is None:
        value = config.get("dag-similarity-threshold")
    if value is None:
        return None
    # bool is an int subclass, so isinstance(value, int) alone would let
    # True/False silently pass through as 1.0/0.0 (#404's dag_ignore_patterns
    # review raised the same concern for its own type check).
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(
            "'dag_similarity_threshold' (or 'dag-similarity-threshold') must be a number"
        )
    try:
        threshold = float(value)
    except OverflowError as e:
        # A TOML integer too large for `float` (e.g. a 500-digit integer,
        # which tomllib accepts as-is) raises OverflowError here rather
        # than reaching the range check below (Codex review, #441).
        raise ConfigError(
            "'dag_similarity_threshold' (or 'dag-similarity-threshold') must be "
            f"within [0, 1] (a similarity score): {e}"
        ) from e
    if not (0 <= threshold <= 1):
        raise ConfigError(
            "'dag_similarity_threshold' (or 'dag-similarity-threshold') must be "
            f"within [0, 1] (a similarity score), got {threshold}"
        )
    return threshold


def resolve_repo_root(plan_path: str | Path) -> Path:
    """Locate the Git repository root a plan file lives under.

    Walks upward from `plan_path`'s parent directory looking for a `.git`
    marker (a directory in a normal checkout, or a file pointing at the
    real gitdir in a linked worktree). Outside a Git repository, falls back
    to the plan's own parent directory to preserve pre-#410 behavior.

    Shared by orchestune-dag (dag_cli.py) and orchestune-provision
    (provisioning.py) so that both tools agree on which directory's
    orchestune.toml / pyproject.toml `[tool.orchestune]` table to read when
    `--plan` points to a file nested below the repository root (#410, #418).
    """
    plan_parent = Path(plan_path).resolve().parent
    for candidate in (plan_parent, *plan_parent.parents):
        git_marker = candidate / ".git"
        if git_marker.is_file() or (git_marker / "HEAD").is_file():
            return candidate
    return plan_parent


def load_orchestune_config(repo_root: str | Path) -> dict[str, Any]:
    """Load the orchestune.toml / pyproject.toml `[tool.orchestune]` config table.

    Checks `<repo_root>/orchestune.toml` first (its entire top level is the
    config table); falls back to `<repo_root>/pyproject.toml`'s
    `[tool.orchestune]` table. Missing files return an empty dict.

    Shared by orchestune-dag (dag_cli.py), orchestune-dispatch
    (dispatcher.py), and orchestune-provision (provisioning.py) so all three
    agree on the same discovery order and error semantics (#404 review).
    """
    repo_root = Path(repo_root)

    # Beyond `OSError`/`tomllib.TOMLDecodeError`, a pathological file can
    # make `open()`/`tomllib.load()` fail in other ways too — invalid UTF-8
    # raises `UnicodeDecodeError`, ~500+ levels of nested arrays/tables
    # raise `RecursionError` (Codex review, #441). `tomllib.load` is the
    # only risky operation in each block below, so any failure loading
    # user-supplied config data is a config problem, not an internal bug —
    # catch broadly rather than chase the parser's exception types one by
    # one (same reasoning as `compile_extra_ignore_patterns` above).
    orchestune_toml = repo_root / "orchestune.toml"
    if orchestune_toml.exists():
        try:
            with open(orchestune_toml, "rb") as f:
                return cast(dict[str, Any], tomllib.load(f))
        except Exception as e:
            raise ConfigError(f"failed to load {orchestune_toml}: {e}") from e

    pyproject_toml = repo_root / "pyproject.toml"
    if pyproject_toml.exists():
        try:
            with open(pyproject_toml, "rb") as f:
                data = tomllib.load(f)
        except Exception as e:
            raise ConfigError(f"failed to load {pyproject_toml}: {e}") from e
        tool = data.get("tool", {})
        if not isinstance(tool, dict):
            raise ConfigError(f"{pyproject_toml}: [tool] must be a table")
        config = tool.get("orchestune", {})
        if not isinstance(config, dict):
            raise ConfigError(f"{pyproject_toml}: [tool.orchestune] must be a table")
        return cast(dict[str, Any], config)

    return {}


def normalize_footprint_path(path: str) -> str:
    """Normalize a footprint path into a canonical repository-relative POSIX path.

    - Converts Windows backslashes '\\' to POSIX forward slashes '/'
    - Rejects absolute paths (starting with '/') and drive-qualified paths (e.g. 'C:')
    - Resolves redundant './', duplicate slashes '//', and relative parent references '..'
    - Raises ValueError if the path is absolute/drive-qualified, resolves to empty/root ('.'), or escapes the repository root
    """
    if not isinstance(path, str):
        path = str(path)

    clean_path = path.replace("\\", "/")

    if re.match(r"^[a-zA-Z]:", clean_path):
        raise ValueError(
            f"Invalid footprint path: '{path}' is a drive-qualified absolute path"
        )
    if clean_path.startswith("/"):
        raise ValueError(f"Invalid footprint path: '{path}' is an absolute path")

    normalized = posixpath.normpath(clean_path)

    if normalized in ("", "."):
        raise ValueError(
            f"Invalid footprint path: '{path}' resolves to empty or root path"
        )

    if normalized == ".." or normalized.startswith("../"):
        raise ValueError(f"Invalid footprint path: '{path}' escapes repository root")

    return normalized


def is_ignored_footprint(
    path: str, extra_patterns: Iterable[re.Pattern[str]] = ()
) -> bool:
    normalized = normalize_footprint_path(path)
    return any(
        pattern.search(normalized)
        for pattern in (*_IGNORED_FOOTPRINT_PATTERNS, *extra_patterns)
    )


class DagCycleError(ValueError):
    """Raised when a dependency graph contains an unresolvable cycle."""


@dataclass(frozen=True)
class SubTask:
    id: str
    description: str
    footprint: tuple[str, ...]
    symbols: tuple[str, ...]
    depends_on: tuple[str, ...]
    risk: bool
    risk_reasons: tuple[str, ...]
    priority: str = "medium"
    overview: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    proposed_changes: tuple[str, ...] = ()
    verification_plan: tuple[str, ...] = ()
    shared_contract: str | None = None
    writes_shared_contract: bool = False
    issue_number: int | None = None

    def __post_init__(self) -> None:
        normalized = tuple(normalize_footprint_path(p) for p in self.footprint)
        object.__setattr__(self, "footprint", normalized)

    def touch_set(
        self, extra_ignored_patterns: Iterable[re.Pattern[str]] = ()
    ) -> frozenset[str]:
        # is_ignored_footprintをself.footprintの各パスに対して複数回呼ぶため、
        # ジェネレータ等の使い捨てiterableが渡されると2件目以降が空になる問題を
        # 避けてtupleへ実体化する。
        extra_ignored_patterns = tuple(extra_ignored_patterns)
        footprint = frozenset(
            path
            for path in self.footprint
            if not is_ignored_footprint(path, extra_ignored_patterns)
        )
        return footprint | frozenset(self.symbols)


@dataclass(frozen=True)
class DagEdge:
    source: str
    target: str
    reason: str
    score: float | None = None


@dataclass(frozen=True)
class ConflictEdge:
    """A symmetric exclusion constraint between two subtasks.

    Endpoints and resources are canonicalized so the same relationship has one
    stable representation regardless of discovery or input order.
    """

    left: str
    right: str
    reason: str
    score: float | None = None
    resources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.left == self.right:
            raise ValueError("a conflict edge must connect two distinct subtasks")
        left, right = sorted((self.left, self.right))
        object.__setattr__(self, "left", left)
        object.__setattr__(self, "right", right)
        object.__setattr__(self, "resources", tuple(sorted(set(self.resources))))

    @property
    def pair(self) -> frozenset[str]:
        return frozenset((self.left, self.right))


@dataclass(frozen=True)
class ConflictGraph:
    """Deterministically ordered collection of symmetric conflict edges."""

    edges: tuple[ConflictEdge, ...] = ()

    def __post_init__(self) -> None:
        unique = set(self.edges)
        ordered = tuple(
            sorted(
                unique,
                key=lambda edge: (
                    edge.left,
                    edge.right,
                    edge.reason,
                    edge.score is None,
                    edge.score or 0.0,
                    edge.resources,
                ),
            )
        )
        object.__setattr__(self, "edges", ordered)

    def has_conflict(self, first: str, second: str) -> bool:
        pair = frozenset((first, second))
        return first != second and any(edge.pair == pair for edge in self.edges)

    def neighbors(self, subtask_id: str) -> frozenset[str]:
        return frozenset(
            edge.right if edge.left == subtask_id else edge.left
            for edge in self.edges
            if subtask_id in (edge.left, edge.right)
        )


@dataclass(frozen=True)
class DagResult:
    subtasks: dict[str, SubTask]
    edges: list[DagEdge]
    topological_order: list[str]
    parallel_leaves: list[str]
    risky_subtask_ids: list[str]
    conflict_graph: ConflictGraph = field(default_factory=ConflictGraph)
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        precedence_edges = [
            {
                "source": edge.source,
                "target": edge.target,
                "reason": edge.reason,
                "score": edge.score,
            }
            for edge in self.edges
        ]
        conflict_edges = [
            {
                "left": edge.left,
                "right": edge.right,
                "reason": edge.reason,
                "score": edge.score,
                "resources": list(edge.resources),
            }
            for edge in self.conflict_graph.edges
        ]
        return {
            "subtasks": {
                subtask_id: {
                    "description": subtask.description,
                    "footprint": list(subtask.footprint),
                    "symbols": list(subtask.symbols),
                    "depends_on": list(subtask.depends_on),
                    "risk": subtask.risk,
                    "risk_reasons": list(subtask.risk_reasons),
                    "overview": subtask.overview,
                    "acceptance_criteria": list(subtask.acceptance_criteria),
                    "proposed_changes": list(subtask.proposed_changes),
                    "verification_plan": list(subtask.verification_plan),
                    "shared_contract": subtask.shared_contract,
                    "writes_shared_contract": subtask.writes_shared_contract,
                }
                for subtask_id, subtask in self.subtasks.items()
            },
            # `edges` remains as a compatibility alias, but now has the only
            # semantically valid meaning for a DAG: causal precedence edges.
            "edges": precedence_edges,
            "precedence_edges": precedence_edges,
            "conflict_edges": conflict_edges,
            "topological_order": list(self.topological_order),
            "parallel_leaves": list(self.parallel_leaves),
            "risky_subtask_ids": list(self.risky_subtask_ids),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class FootprintConflict:
    subtask_id: str
    other_subtask_id: str
    similarity: float
    blocked_subtask_id: str
    reason: str = "similarity"
    resources: tuple[str, ...] = ()

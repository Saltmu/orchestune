"""Backward-compatible facade for Orchestune DAG functionality."""
# ruff: noqa: F401 -- compatibility re-exports

from orchestune.dag_cli import main
from orchestune.dag_contracts import find_unowned_shared_contract_hotspots
from orchestune.dag_graph import (
    _assemble_dag,
    _collect_explicit_edges,
    _detect_cycle,
    _merge_explicit_and_similarity,
    _resolve_cycles_if_possible,
    _topological_sort,
    build_dag,
    build_dag_from_plan,
    recompute_dag_for_footprint_change,
)
from orchestune.dag_models import (
    DagCycleError,
    DagEdge,
    DagResult,
    FootprintConflict,
    SubTask,
)
from orchestune.dag_models import (
    is_ignored_footprint as _is_ignored_footprint,
)
from orchestune.dag_parsing import (
    detect_risk_from_values as _detect_risk_from_values,
)
from orchestune.dag_parsing import (
    extract_frontmatter as _extract_frontmatter,
)
from orchestune.dag_parsing import (
    parse_decomposition_plan,
)
from orchestune.dag_similarity import (
    DEFAULT_SIMILARITY_THRESHOLD,
    _determine_edge_direction,
    _document_frequencies,
    _idf_weights,
    _weighted_otsuka_ochiai,
    build_similarity_edges,
)
from orchestune.dag_similarity import (
    find_candidate_pairs as _find_candidate_pairs,
)
from orchestune.dag_similarity import (
    otsuka_ochiai as _otsuka_ochiai,
)

__all__ = [
    "DEFAULT_SIMILARITY_THRESHOLD",
    "DagCycleError",
    "DagEdge",
    "DagResult",
    "FootprintConflict",
    "SubTask",
    "build_dag",
    "build_dag_from_plan",
    "build_similarity_edges",
    "find_unowned_shared_contract_hotspots",
    "main",
    "parse_decomposition_plan",
    "recompute_dag_for_footprint_change",
]


if __name__ == "__main__":
    main()

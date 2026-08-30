"""Orchestune: multi-agent implementation orchestrator.

`__all__` below is the package's public API. Every other module is internal and
may change without notice; import it only from inside `orchestune`.

The package is organised in five layers, documented with their machine-checked
invariants in `docs/en/architecture.md` (and `docs/ja/architecture.md`):

    L4 entrypoints  the modules that expose a `main()`
    L3 workflows    dispatch cycle and integration pipelines
    L2 domain       DAG construction, scoring, dispatch mechanics
    L1 adapters     the only place `gh` (forge) and `git` (infra.git_cli) are run
    L0 infra        pure DTOs and dependency-free helpers

Entrypoint modules are deliberately absent from `__all__`: re-exporting them
here would make the package root itself an importer of L4 and break the
"nothing but `cli` imports an entrypoint" invariant that this file exists to
declare. `cli` is the one permitted importer because it dispatches to the other
five; `tests/test_architecture.py` encodes that as `ALLOWED_L4_DEPENDENTS`.
"""

from orchestune.consistency import (
    ConsistencyCycleReport,
    ConsistencyEngine,
    ConsistencyFinding,
    ConsistencyMode,
    ConsistencyRepairOutcome,
    ConsistencyRepairPass,
    ConsistencyReport,
    ConsistencyScanResult,
    ConsistencyScope,
    ConsistencySupervisor,
    DesiredFact,
    DesiredRepositoryState,
    Evidence,
    FactValue,
    FindingSeverity,
    IntentStatus,
    Invariant,
    Observation,
    ObservationCertainty,
    ObservedRepositoryState,
    Observer,
    Repairability,
    RepairCommand,
    RepairDisposition,
    RepairExecutor,
    RepairPlanner,
    RepairResult,
    RepairStatus,
    ScopedObservations,
    StateChanged,
    TransitionIntent,
)
from orchestune.forge import (
    REQUIRED_LABELS,
    BootstrapResult,
    Forge,
    ForgeAuthError,
    ForgeError,
    GitHubForge,
    IssueForge,
    LabelSpec,
    PullRequestForge,
    RepoAdminForge,
)
from orchestune.models import IssueRecord, PrRecord, Task
from orchestune.outcome_record import (
    OUTCOME_MARKER,
    RESULT_BLOCKED,
    RESULT_DONE,
    RESULT_NOT_NEEDED,
    OutcomeRecord,
    ReviewSummary,
    parse_from_comments,
)
from orchestune.version import get_version

__version__ = get_version()

__all__ = [
    "BootstrapResult",
    "ConsistencyCycleReport",
    "ConsistencyEngine",
    "ConsistencyFinding",
    "ConsistencyMode",
    "ConsistencyRepairOutcome",
    "ConsistencyRepairPass",
    "ConsistencyReport",
    "ConsistencyScanResult",
    "ConsistencyScope",
    "ConsistencySupervisor",
    "DesiredFact",
    "DesiredRepositoryState",
    "Evidence",
    "FactValue",
    "FindingSeverity",
    "Forge",
    "ForgeAuthError",
    "ForgeError",
    "GitHubForge",
    "IntentStatus",
    "Invariant",
    "IssueForge",
    "IssueRecord",
    "LabelSpec",
    "OUTCOME_MARKER",
    "Observation",
    "ObservationCertainty",
    "ObservedRepositoryState",
    "Observer",
    "OutcomeRecord",
    "PrRecord",
    "PullRequestForge",
    "REQUIRED_LABELS",
    "RESULT_BLOCKED",
    "RESULT_DONE",
    "RESULT_NOT_NEEDED",
    "RepairCommand",
    "RepairDisposition",
    "RepairExecutor",
    "RepairPlanner",
    "RepairResult",
    "RepairStatus",
    "Repairability",
    "RepoAdminForge",
    "ReviewSummary",
    "ScopedObservations",
    "StateChanged",
    "Task",
    "TransitionIntent",
    "__version__",
    "parse_from_comments",
]

"""Quint dependency-liveness traces replayed on the production harness (#1276).

``specs/quint/dependency_liveness.qnt`` models bounded liveness of dependency
resolution.  ``quint run --mbt`` writes ITF traces whose every state records the
action that led to it (``mbt::actionTaken``), the values the model picked
(``mbt::nondetPicks``) and the promotion *obligations* of the last cycle
(``mustNot`` / ``mustQueue``).  This module

* runs the pinned Quint (no global install; simulator only, never a proof),
* decodes ITF strictly (an unknown value, action or pick is an error, never a skip),
* executes every transition on ``LivenessWorld`` -- the real
  ``_prepare_cycle_context`` and ``execute_pipeline`` -- and
* compares the production observation (labels, ``PromotionEvent`` previews)
  with the obligations.  Expectations come from the model state only; nothing
  asks production whether a dependency is complete.

The correspondence between model actions and harness operations is the
``ACTIONS`` table below; ``docs/*/verification-contracts.md`` describes it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from tests.dependency_liveness_test_support import (
    BASE_RED,
    DEPENDENT,
    RECOMPUTE,
    CompletionPath,
    CycleObservation,
    FaultPlan,
    LivenessTopology,
    LivenessWorld,
    StatusLabel,
)
from tests.verification_contract_test_support import ContractViolation, require

ROOT = Path(__file__).resolve().parent.parent
MODEL = ROOT / "specs" / "quint" / "dependency_liveness.qnt"
#: The TypeScript simulator ships inside the pinned npm package.  The default
#: ``rust`` backend downloads a binary at first use, which no lockfile pins.
BACKEND = "typescript"
INVARIANTS = ("safety", "liveness", "eventMatchesLabel", "dryRunReadOnly")
WITNESSES = (
    "reachedApplyPromotion",
    "reachedDryRunPreview",
    "reachedIntermediatePromotion",
    "reachedLiveness",
)
#: Variables every ITF state must carry (checked against the trace's ``vars``).
REQUIRED_VARS = frozenset(
    {
        "shape",
        "recompute",
        "base",
        "res",
        "lab",
        "ev",
        "stale",
        "staleApplied",
        "planned",
        "pc",
        "applied",
        "events",
        "newlyQueued",
        "mustNot",
        "mustQueue",
        "mbt::actionTaken",
        "mbt::nondetPicks",
    }
)
TRACE_NAME = "trace_{seq}.itf.json"


class ReplayFormatError(Exception):
    """The trace cannot be replayed: bad ITF, unsupported action or pick type."""


class ToolError(Exception):
    """Quint is missing, has the wrong version, failed or timed out."""


# ---- tool -------------------------------------------------------------------------


def pinned_quint_version() -> str:
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    version = str(package["devDependencies"]["@informalsystems/quint"])
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ToolError(f"quint must be pinned to an exact version, got {version!r}")
    return version


@lru_cache(maxsize=1)
def quint_executable() -> Path:
    name = "quint.cmd" if os.name == "nt" else "quint"
    executable = ROOT / "node_modules" / ".bin" / name
    if not executable.exists():
        raise ToolError(
            f"{executable} is missing: run scripts/quint-check.sh "
            "(scripts/quint-check.ps1 on Windows); Quint is a required tool"
        )
    done = subprocess.run(
        [str(executable), "--version"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if done.returncode != 0 or done.stdout.strip() != pinned_quint_version():
        raise ToolError(
            f"quint {done.stdout.strip() or done.stderr.strip()!r} is installed, "
            f"package.json pins {pinned_quint_version()}: run scripts/quint-check.sh"
        )
    return executable


@dataclass(frozen=True, slots=True)
class Exploration:
    """Bounds of one ``quint run``; recorded with every result."""

    seed: str
    max_samples: int
    max_steps: int
    n_traces: int = 1
    timeout: float = 300.0


@dataclass(slots=True)
class ModelRun:
    """Result of one ``quint run``.  ``outcome`` separates a property violation
    from every other failure, so a broken model never counts as a detection."""

    outcome: str  # "ok" | "violation" | "error"
    returncode: int
    violated: tuple[str, ...]
    command: tuple[str, ...]
    seconds: float
    stdout: str
    stderr: str
    traces: tuple[Path, ...] = ()


def command_line(
    bounds: Exploration,
    *,
    init: str | None,
    out_dir: Path | None,
    model: str,
    executable: str,
    witnesses: Iterable[str] = (),
) -> list[str]:
    command = [
        executable,
        "run",
        model,
        f"--backend={BACKEND}",
        f"--seed={bounds.seed}",
        f"--max-samples={bounds.max_samples}",
        f"--max-steps={bounds.max_steps}",
        "--verbosity=1",
    ]
    if init is not None:
        command.append(f"--init={init}")
    if out_dir is not None:
        command += [
            "--mbt",
            f"--out-itf={(out_dir / TRACE_NAME).as_posix()}",
            f"--n-traces={bounds.n_traces}",
        ]
    command += ["--invariants", *INVARIANTS]
    if witnesses := tuple(witnesses):
        command += ["--witnesses", *witnesses]
    return command


def witness_counts(stdout: str) -> dict[str, int]:
    """How many sampled runs reached each witness (``--witnesses`` report)."""
    return {
        name: int(count)
        for name, count in re.findall(r"(\w+) was witnessed in (\d+) trace", stdout)
    }


def run_model(
    bounds: Exploration,
    *,
    init: str | None = None,
    out_dir: Path | None = None,
    model: Path = MODEL,
    witnesses: Iterable[str] = (),
) -> ModelRun:
    """Run the simulator; with ``out_dir`` also write ITF traces with MBT metadata."""
    command = command_line(
        bounds,
        init=init,
        out_dir=out_dir,
        model=str(model),
        executable=str(quint_executable()),
        witnesses=witnesses,
    )
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        done = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=bounds.timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as expired:
        raise ToolError(
            f"quint timed out after {bounds.timeout}s: {command}"
        ) from expired
    seconds = time.monotonic() - started
    violated = tuple(re.findall(r"❌\s+(\w+)", done.stdout))
    if done.returncode == 0 and "[ok]" in done.stdout:
        outcome = "ok"
    elif done.returncode == 1 and "[violation]" in done.stdout and violated:
        outcome = "violation"
    else:
        outcome = "error"
    traces = tuple(sorted(out_dir.glob("trace_*.itf.json"))) if out_dir else ()
    return ModelRun(
        outcome,
        done.returncode,
        violated,
        tuple(command),
        seconds,
        done.stdout,
        done.stderr,
        traces,
    )


# ---- ITF ----------------------------------------------------------------------------


def decode(value: Any) -> Any:
    """ITF value -> Python: ``#bigint`` int, ``#set`` frozenset, ``#map`` dict,
    ``#tup`` tuple.  Any other ``#`` form is rejected."""
    if isinstance(value, list):
        return [decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    marked = [key for key in value if key.startswith("#")]
    if not marked:
        return {key: decode(item) for key, item in value.items()}
    if len(value) != 1:
        raise ReplayFormatError(f"mixed ITF value: {sorted(value)}")
    (key,) = marked
    body = value[key]
    if key == "#bigint":
        return int(body)
    if key == "#set":
        return frozenset(decode(item) for item in body)
    if key == "#map":
        return {decode(k): decode(v) for k, v in body}
    if key == "#tup":
        return tuple(decode(item) for item in body)
    raise ReplayFormatError(f"unsupported ITF value {key!r}")


@dataclass(frozen=True, slots=True)
class Step:
    """One ITF state: the transition that reached it and the state itself."""

    index: int
    action: str
    picks: Mapping[str, Any]
    state: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Trace:
    steps: tuple[Step, ...]  # steps[0] is the initial state

    @property
    def transitions(self) -> int:
        return len(self.steps) - 1


def parse_trace(raw: object) -> Trace:
    if not isinstance(raw, dict) or "states" not in raw or "vars" not in raw:
        raise ReplayFormatError("not an ITF trace (no 'states' / 'vars')")
    meta = raw.get("#meta")
    if not isinstance(meta, dict) or meta.get("format") != "ITF":
        raise ReplayFormatError("'#meta.format' is not ITF")
    if meta.get("status") != "ok":
        raise ReplayFormatError(f"trace status is {meta.get('status')!r}, not 'ok'")
    missing = REQUIRED_VARS - set(raw["vars"])
    if missing:
        raise ReplayFormatError(f"trace lacks variables {sorted(missing)}")
    if not raw["states"]:
        raise ReplayFormatError("trace has no state")
    steps = []
    for index, encoded in enumerate(raw["states"]):
        recorded = encoded.get("#meta", {}).get("index")
        if recorded != index:
            raise ReplayFormatError(
                f"state {index} is recorded as index {recorded!r}: the trace is "
                "truncated, reordered or not an ITF trace of this model"
            )
        state = decode({k: v for k, v in encoded.items() if k != "#meta"})
        action = state.get("mbt::actionTaken")
        if not isinstance(action, str) or not action:
            raise ReplayFormatError(f"state {index}: no mbt::actionTaken")
        picks = state.get("mbt::nondetPicks")
        if not isinstance(picks, dict):
            raise ReplayFormatError(f"state {index}: no mbt::nondetPicks")
        chosen = {
            name: option["value"]
            for name, option in picks.items()
            if isinstance(option, dict) and option.get("tag") == "Some"
        }
        steps.append(Step(index, action, chosen, state))
    last = steps[-1].state
    if last["pc"] != last["planned"] and last["planned"] != 0:
        raise ReplayFormatError(
            f"the scripted run stopped at action {last['pc']} of {last['planned']}"
        )
    return Trace(tuple(steps))


def normalized(raw: dict[str, Any]) -> dict[str, Any]:
    """Drop what changes between runs: the timestamp, the description, the absolute
    source path, and the order of ``vars`` (it follows hash order, so any edit of the
    model shuffles it without changing a state)."""
    meta = {
        key: value
        for key, value in raw["#meta"].items()
        if key not in {"description", "timestamp", "source"}
    }
    return {**raw, "#meta": meta, "vars": sorted(set(raw["vars"]))}


# ---- action table -------------------------------------------------------------------

_ISSUES = {11, 12, 13, 14}
_SUBJECTS = _ISSUES | {DEPENDENT}


def _enum(*allowed: str) -> Callable[[object], bool]:
    return lambda value: isinstance(value, str) and value in allowed


def _one_of(allowed: Iterable[int]) -> Callable[[object], bool]:
    values = frozenset(allowed)
    return lambda value: isinstance(value, int) and value in values


def _flag(value: object) -> bool:
    return isinstance(value, bool)


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """Model action -> harness operation.  ``picks`` lists every required choice
    with its value check; any other pick, or a missing one, is a format error."""

    harness: str
    picks: Mapping[str, Callable[[object], bool]]


ACTIONS: Mapping[str, ActionSpec] = {
    "complete_dependency": ActionSpec(
        "LivenessWorld.complete(dep, CompletionPath(path))",
        {
            "path": _enum(*(path.value for path in CompletionPath)),
            "dep": _one_of(_ISSUES),
        },
    ),
    "duplicate_completion": ActionSpec(
        "LivenessWorld.duplicate(dep)", {"dep": _one_of(_ISSUES)}
    ),
    "cycle": ActionSpec(
        "LivenessWorld.cycle(apply=apply) with faults.listing_lag=lag",
        {"apply": _flag, "lag": _flag},
    ),
    "restart": ActionSpec(
        "LivenessWorld.lose_ledger() when ledgerLoss", {"ledgerLoss": _flag}
    ),
    "fail_next": ActionSpec(
        "FaultPlan.forge_operation = (op, mode)",
        {"op": _enum("add", "remove"), "mode": _enum("before", "after")},
    ),
    "toggle_hold": ActionSpec(
        "set_t_label(BASE_RED | RECOMPUTE) / set_reservation(target)",
        {
            "kind": _enum("base_red", "recompute", "reservation"),
            "target": _one_of(_SUBJECTS),
        },
    ),
    "stale_snapshot": ActionSpec(
        "change applied between context build and promotion of the next cycle",
        {
            "kind": _enum("add_hold", "revoke", "complete"),
            "target": _one_of(_SUBJECTS),
        },
    ),
}
INIT_ACTION = re.compile(r"init(_[a-z0-9_]+)?")


def check_picks(step: Step) -> None:
    spec = ACTIONS.get(step.action)
    if spec is None:
        raise ReplayFormatError(
            f"state {step.index}: unsupported action {step.action!r}"
        )
    if set(step.picks) != set(spec.picks):
        raise ReplayFormatError(
            f"state {step.index}: {step.action} needs picks {sorted(spec.picks)}, "
            f"got {sorted(step.picks)}"
        )
    for name, valid in spec.picks.items():
        if not valid(step.picks[name]):
            raise ReplayFormatError(
                f"state {step.index}: {step.action}.{name} = {step.picks[name]!r} "
                "is not a supported value"
            )


# ---- replay -------------------------------------------------------------------------


def build_topology(shape: Mapping[str, Any]) -> LivenessTopology:
    """The production graph for the model's own topology record."""
    names = {11: "dep-a", 12: "dep-b", 13: "dep-c", 14: "dep-d", DEPENDENT: "dependent"}

    def depends(needs: Iterable[int], unresolved: bool) -> tuple[str, ...]:
        found = tuple(names[number] for number in sorted(needs))
        return found + (("unresolved-missing",) if unresolved else ())

    required = tuple(sorted(shape["required"]))
    return LivenessTopology(
        dep_issues=tuple(sorted(shape["deps"])),
        t_depends_on=depends(required, not shape["tRequired"]),
        issue_depends_on={
            number: depends(needs, number in shape["interUnresolved"])
            for number, needs in shape["inter"].items()
        },
        required_to_promote=required if shape["tRequired"] else None,
    )


@dataclass(frozen=True, slots=True)
class KnownDefect:
    """A production counterexample pinned to one trace step (strict xfail).

    ``violates`` is the generic contract the replay reports at ``step`` for
    ``node``; it is re-labelled with the defect's own ``contract``.  Any other
    mismatch stays what it is and fails the test.
    """

    issue: int
    contract: str
    step: int
    node: int
    violates: str


@dataclass(slots=True)
class ReplayReport:
    transitions: int = 0
    cycles: int = 0
    obligations: int = 0
    actions: list[str] = field(default_factory=list)


def _status(labels: frozenset[str]) -> str:
    return "+".join(sorted(label.removeprefix("status:") for label in labels))


class Replayer:
    """Executes one trace on a fresh ``LivenessWorld``."""

    def __init__(
        self,
        root: Path,
        known: KnownDefect | None = None,
        wiring: FaultPlan | None = None,
    ) -> None:
        self.root = root
        self.known = known
        self.wiring = wiring
        self.world: LivenessWorld
        self.report = ReplayReport()
        self._pending: tuple[str, int] | None = None

    def replay(self, trace: Trace) -> ReplayReport:
        first, *rest = trace.steps
        if not INIT_ACTION.fullmatch(first.action):
            raise ReplayFormatError(f"state 0 is {first.action!r}, not an init action")
        if not rest:
            raise ReplayFormatError("trace has an initial state only (no transition)")
        self._start(first)
        previous = first
        for step in rest:
            if INIT_ACTION.fullmatch(step.action):
                raise ReplayFormatError(f"state {step.index}: init after the start")
            check_picks(step)
            getattr(self, f"_do_{step.action}")(step, previous)
            self.report.transitions += 1
            self.report.actions.append(step.action)
            previous = step
        return self.report

    # ---- initial state -----------------------------------------------------------

    def _start(self, first: Step) -> None:
        state = first.state
        labels = (StatusLabel.BLOCKED.value,) + (
            (RECOMPUTE,) if state["recompute"] else ()
        )
        self.world = LivenessWorld(
            self.root, t_labels=labels, topology=build_topology(state["shape"])
        )
        if self.wiring is not None:  # a control: production mis-wiring (#902)
            self.world.faults.empty_completion_set = self.wiring.empty_completion_set
            self.world.faults.throwaway_repair_context = (
                self.wiring.throwaway_repair_context
            )
        for number, model_label in sorted(state["lab"].items()):
            expected = {"blocked": "blocked", "queued": "queued"}[model_label]
            found = self.world.labels(number) - {RECOMPUTE}
            self._observed(
                _status(found) == expected,
                f"initial label of #{number}: model {expected}, production "
                f"{_status(found)}",
            )

    # ---- operations --------------------------------------------------------------

    def _observed(self, condition: bool, detail: str) -> None:
        require("P3C-QUINT-OBSERVATION", condition, detail)

    def _do_complete_dependency(self, step: Step, previous: Step) -> None:
        dep, path = step.picks["dep"], CompletionPath(step.picks["path"])
        self._observed(
            StatusLabel.QUEUED.value in self.world.labels(dep)
            and dep not in self.world.evidence,
            f"state {step.index}: #{dep} is not a queued Issue without evidence",
        )
        self.world.complete(dep, path)

    def _do_duplicate_completion(self, step: Step, previous: Step) -> None:
        dep = step.picks["dep"]
        self._observed(
            dep in self.world.evidence,
            f"state {step.index}: #{dep} has no evidence to deliver again",
        )
        self.world.duplicate(dep)

    def _do_restart(self, step: Step, previous: Step) -> None:
        if step.picks["ledgerLoss"]:
            self.world.lose_ledger()

    def _do_fail_next(self, step: Step, previous: Step) -> None:
        self.world.faults.forge_operation = (step.picks["op"], step.picks["mode"])

    def _do_toggle_hold(self, step: Step, previous: Step) -> None:
        kind, target = step.picks["kind"], step.picks["target"]
        if kind == "reservation":
            self.world.set_reservation(target, target in step.state["res"])
        elif kind == "base_red":
            self.world.set_t_label(BASE_RED, bool(step.state["base"]))
        else:
            self.world.set_t_label(RECOMPUTE, bool(step.state["recompute"]))

    def _do_stale_snapshot(self, step: Step, previous: Step) -> None:
        self._pending = (step.picks["kind"], step.picks["target"])

    # ---- the cycle ---------------------------------------------------------------

    def _stale_change(
        self, step: Step
    ) -> tuple[Callable[[LivenessWorld], None] | None, frozenset[int]]:
        """The change the model applied in this cycle (a no-op one is skipped) and
        the Issue the fixture changes with it."""
        applied, pending = step.state["staleApplied"], self._pending
        self._pending = None
        if applied == "none":
            return None, frozenset()
        if pending is None or pending[0] != applied:
            raise ReplayFormatError(
                f"state {step.index}: the model applied stale change {applied!r} "
                "that no stale_snapshot action prepared"
            )
        target = pending[1]
        changes: dict[str, Callable[[LivenessWorld], None]] = {
            "add_hold": lambda world: world.set_t_label(BASE_RED, True),
            "revoke": lambda world: world.revoke(target),
            "complete": lambda world: world.complete(target, CompletionPath.LABEL),
        }
        return changes[applied], frozenset({target})

    def _do_cycle(self, step: Step, previous: Step) -> None:
        apply, lag = step.picks["apply"], step.picks["lag"]
        self._observed(
            apply == step.state["applied"],
            f"state {step.index}: model cycle mode disagrees with its own pick",
        )
        self.world.faults.listing_lag = lag
        change, fixture_touched = self._stale_change(step)
        observation = self.world.cycle(apply=apply, before_promotion=change)
        self.report.cycles += 1
        self._compare(step, observation, fixture_touched)

    def _compare(
        self,
        step: Step,
        observation: CycleObservation,
        fixture_touched: frozenset[int] = frozenset(),
    ) -> None:
        if not observation.apply:
            _require_read_only(step, observation, fixture_touched)
        for node in sorted(step.state["mustNot"]):
            self.report.obligations += 1
            self._expect(
                step, node, "safety", not _promoted(observation, node), observation
            )
        for node in sorted(step.state["mustQueue"]):
            self.report.obligations += 1
            self._expect(
                step, node, "liveness", _queued(observation, node), observation
            )
        if observation.apply:
            _require_events_are_label_changes(step, observation)

    def _expect(
        self, step: Step, node: int, kind: str, holds: bool, observation: object
    ) -> None:
        target = node == DEPENDENT
        contract = {
            ("safety", True): "P3C-SAFETY",
            ("safety", False): "P3C-INTERMEDIATE-SAFETY",
            ("liveness", True): "P3C-LIVENESS-BOUND",
            ("liveness", False): "P3C-INTERMEDIATE-LIVENESS",
        }[(kind, target)]
        if holds:
            return
        if self.known and (self.known.step, self.known.node, self.known.violates) == (
            step.index,
            node,
            contract,
        ):
            contract = self.known.contract
        verb = "was promoted against" if kind == "safety" else "was not promoted under"
        raise ContractViolation(
            contract,
            f"state {step.index}: #{node} {verb} the model's obligation: {observation}",
        )


def _labels(observation: CycleObservation, node: int, *, after: bool) -> frozenset[str]:
    """Labels of T or of a dependency Issue at the start / end of the cycle."""
    if node == DEPENDENT:
        return observation.t_after if after else observation.t_before
    found = observation.labels_after if after else observation.labels_before
    return found.get(node, frozenset())


def _became_queued(observation: CycleObservation, node: int) -> bool:
    before = _labels(observation, node, after=False)
    return (
        StatusLabel.BLOCKED.value in before
        and StatusLabel.QUEUED.value not in before
        and StatusLabel.QUEUED.value in _labels(observation, node, after=True)
    )


def _promoted(observation: CycleObservation, node: int) -> bool:
    """A promotion event or, in an apply cycle, a real blocked -> queued change.

    The model counts both (``events`` and ``newlyQueued``), so an event without
    the label is a promotion too.
    """
    if node in observation.promotion_issue_numbers:
        return True
    return observation.apply and _became_queued(observation, node)


def _queued(observation: CycleObservation, node: int) -> bool:
    """Apply: the real label after the cycle.  Dry run: queued when the cycle
    started, or this cycle's own preview -- never a label the dry run wrote."""
    if observation.apply:
        return StatusLabel.QUEUED.value in _labels(observation, node, after=True)
    return (
        StatusLabel.QUEUED.value in _labels(observation, node, after=False)
        or node in observation.promotion_issue_numbers
    )


def _status_labels(
    observation: CycleObservation, node: int, *, after: bool
) -> set[str]:
    return {
        label
        for label in _labels(observation, node, after=after)
        if label.startswith("status:")
    }


def _require_read_only(
    step: Step, observation: CycleObservation, fixture_touched: frozenset[int]
) -> None:
    """A dry run changes no lifecycle label (the model's ``dryRunReadOnly``).

    Hold labels and the Issue the fixture's own stale change touches are the
    test's doing, not the cycle's, and are left out.
    """
    changed = sorted(
        node
        for node in {DEPENDENT, *observation.labels_before} - fixture_touched
        if _status_labels(observation, node, after=False)
        != _status_labels(observation, node, after=True)
    )
    require(
        "P3C-DRYRUN-READONLY",
        not changed,
        f"state {step.index}: a dry run changed the labels of {changed}: {observation}",
    )


def _require_events_are_label_changes(
    step: Step, observation: CycleObservation
) -> None:
    """An undisturbed apply cycle's promotion event is a real label change (the
    model's ``eventMatchesLabel``); a cycle with an injected failure is not judged."""
    if observation.error is not None or observation.fault_injected:
        return
    fake = sorted(
        node
        for node in observation.promotion_issue_numbers
        if not _became_queued(observation, node)
    )
    require(
        "P3C-LIVENESS-BOUND",
        not fake,
        f"state {step.index}: promotion events of {fake} changed no label: {observation}",
    )


def replay_trace(
    trace: Trace,
    root: Path,
    known: KnownDefect | None = None,
    wiring: FaultPlan | None = None,
) -> ReplayReport:
    return Replayer(root, known, wiring).replay(trace)


def replay_file(path: Path, root: Path) -> ReplayReport:
    return replay_trace(parse_trace(json.loads(path.read_text(encoding="utf-8"))), root)

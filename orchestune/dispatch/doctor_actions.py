"""Offline diagnostics for Actions-mode ``orchestune doctor``.

Reads already-parsed workflow documents and the repository config; never runs
workflow shell, touches the network or writes files.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from orchestune.dispatch.doctor_entrypoints import (
    DispatchEntrypoint,
    JobScan,
    WorkflowScan,
    scan_workflow,
)
from orchestune.dispatch.doctor_models import (
    CODE_ACTIONS_CANCEL,
    CODE_ACTIONS_CREDENTIALS,
    CODE_ACTIONS_ENTRYPOINT,
    CODE_ACTIONS_GROUP,
    CODE_ACTIONS_PARALLELISM,
    CODE_ACTIONS_TARGET,
    Diagnostic,
    DiagnosticStatus,
    DoctorContext,
    worst_status,
)
from orchestune.dispatch.targets import (
    CODEX_CLOUD_ENV_VAR,
    ROUTINE_ID_ENV_VAR,
    ROUTINE_TOKEN_ENV_VAR,
    resolve_default_dispatch_target_name,
)

STANDARD_GROUP = "orchestune-control-${{ github.repository }}"
EXTERNAL_TARGETS = frozenset({"cloud-routine", "codex-cloud"})
LOCAL_PROCESS_TARGETS = frozenset(
    {"local", "claude-cli", "agy-cli", "codex-cli", "auto"}
)
ACTIONS_DEFAULT_TARGET = resolve_default_dispatch_target_name(
    {"GITHUB_ACTIONS": "true"}
)
MIGRATION_DOC_REF = 'docs/ja/setup.md「既存導入先の移行」/ docs/en/setup.md "Migrating Existing Installations"'

_EXPR_RE = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
_CANCEL_KEY = "cancel-in-progress"


@dataclass(frozen=True)
class ConcurrencyShape:
    kind: Literal["missing", "shorthand", "mapping", "invalid"]
    group: str | None
    cancel_present: bool
    cancel_value: Any


def normalize_group_expression(value: str) -> str:
    """Normalize only the whitespace just inside ``${{`` and ``}}``."""
    return _EXPR_RE.sub(r"${{ \1 }}", value)


def concurrency_shape(container: Mapping[str, Any]) -> ConcurrencyShape:
    if "concurrency" not in container:
        return ConcurrencyShape("missing", None, False, None)
    value = container["concurrency"]
    if isinstance(value, str):
        return ConcurrencyShape("shorthand", value, False, None)
    if isinstance(value, Mapping):
        group = value.get("group")
        return ConcurrencyShape(
            "mapping",
            group if isinstance(group, str) else None,
            _CANCEL_KEY in value,
            value.get(_CANCEL_KEY),
        )
    return ConcurrencyShape("invalid", None, False, None)


def is_standard_group(document: Mapping[str, Any]) -> bool:
    shape = concurrency_shape(document)
    return (
        shape.kind == "mapping"
        and shape.group is not None
        and normalize_group_expression(shape.group) == STANDARD_GROUP
    )


@dataclass(frozen=True)
class _Workflow:
    path: str
    document: Mapping[str, Any]
    scan: WorkflowScan

    @property
    def applied(self) -> tuple[DispatchEntrypoint, ...]:
        return tuple(e for e in self.scan.dispatch if e.apply)

    def job_document(self, name: str) -> Mapping[str, Any]:
        jobs = self.document.get("jobs")
        job = jobs.get(name) if isinstance(jobs, Mapping) else None
        if job is None and isinstance(jobs, Mapping):
            job = next((v for k, v in jobs.items() if str(k) == name), None)
        return job if isinstance(job, Mapping) else {}

    def step_document(self, entry: DispatchEntrypoint) -> Mapping[str, Any]:
        steps = self.job_document(entry.location.job).get("steps")
        index = entry.location.step_index
        if isinstance(steps, list) and 0 <= index < len(steps):
            step = steps[index]
            return step if isinstance(step, Mapping) else {}
        return {}

    def control_jobs(self) -> tuple[JobScan, ...]:
        return tuple(j for j in self.scan.jobs if any(e.apply for e in j.dispatch))


# (status, evidence lines without the workflow prefix)
_Result = tuple[DiagnosticStatus, tuple[str, ...]]


def _where(entry: DispatchEntrypoint) -> str:
    loc = entry.location
    return f"{loc.job} / step[{loc.step_index}] line {loc.line_no}: {entry.command}"


def _finish(
    code: str,
    results: list[tuple[str, _Result]],
    messages: Mapping[DiagnosticStatus, str],
    remediation: str,
) -> Diagnostic:
    status = worst_status(r[0] for _, r in results)
    evidence = tuple(
        f"{path}: {line}" for path, (_, lines) in results for line in lines
    )
    return Diagnostic(
        code, status, messages[status], evidence, "" if status == "ok" else remediation
    )


def _group_result(wf: _Workflow) -> _Result:
    shape = concurrency_shape(wf.document)
    if shape.kind in {"missing", "invalid"}:
        return "error", ("concurrency is missing or not a mapping",)
    if shape.kind == "shorthand":
        return "error", (f"string shorthand cannot set {_CANCEL_KEY}: {shape.group}",)
    if not shape.group:
        return "error", ("concurrency.group is missing, empty or not a string",)
    if normalize_group_expression(shape.group) == STANDARD_GROUP:
        return "ok", (f"group: {shape.group}",)
    return "error", (f"non-standard group: {shape.group}",)


def _cancel_result(wf: _Workflow) -> _Result:
    shape = concurrency_shape(wf.document)
    if shape.kind != "mapping" or not shape.cancel_present:
        return "error", (f"{_CANCEL_KEY} is not set",)
    value = shape.cancel_value
    if isinstance(value, bool) and value is False:
        return "ok", (f"{_CANCEL_KEY}: false",)
    return "error", (f"{_CANCEL_KEY} must be literal false, got {value!r}",)


def _parallelism_result(wf: _Workflow) -> _Result:
    jobs = wf.control_jobs()
    if not jobs:
        return "not_checked", ("no direct dispatch entrypoint found",)
    problems: list[str] = []
    if len(jobs) > 1:
        problems.append("multiple control jobs: " + ", ".join(j.job for j in jobs))
    for job in jobs:
        doc = wf.job_document(job.job)
        strategy = doc.get("strategy")
        if isinstance(strategy, Mapping) and "matrix" in strategy:
            problems.append(f"{job.job}: strategy.matrix on a control job")
        if "concurrency" in doc:
            problems.append(f"{job.job}: job-level concurrency on a control job")
    problems.extend(
        f"background launch: {_where(e)}" for e in wf.applied if e.background
    )
    if problems:
        return "error", tuple(problems)
    return "ok", (f"single control job: {jobs[0].job}",)


def _entrypoint_result(wf: _Workflow) -> _Result:
    applied = wf.applied
    lines = [f"entrypoint: {_where(e)}" for e in applied]
    lines.extend(
        f"not counted (--no-apply): {_where(e)}"
        for e in wf.scan.dispatch
        if not e.apply
    )
    blind = [
        f"undetectable {u.kind}: {u.location.job} / step[{u.location.step_index}] "
        f"line {u.location.line_no}: {u.detail}"
        for u in wf.scan.undetectable
    ]
    broken = [f"unparsable arguments: {_where(e)}" for e in applied if e.args_error]
    if applied and not blind and not broken:
        return "ok", tuple(lines)
    if not applied:
        lines.append("no synchronous dispatch entrypoint found")
    return "not_checked", tuple(lines + blind + broken)


@dataclass(frozen=True)
class _Target:
    status: DiagnosticStatus
    value: str | None
    route: str


def _decide_target(entry: DispatchEntrypoint, context: DoctorContext) -> _Target:
    if entry.args_error:
        return _Target("not_checked", None, "unparsable arguments")
    if entry.dispatch_target is not None:
        if entry.target_is_dynamic:
            return _Target("not_checked", None, "argument is dynamic")
        value: str | None = entry.dispatch_target
        route = "argument"
    elif not context.config_valid:
        return _Target("not_checked", None, "config unreadable")
    else:
        configured = context.config.get("dispatch_target")
        value = configured if isinstance(configured, str) and configured else None
        route = "config"
        if value is None:
            value, route = ACTIONS_DEFAULT_TARGET, "actions-default"
    if value in EXTERNAL_TARGETS:
        return _Target("ok", value, route)
    if value in LOCAL_PROCESS_TARGETS:
        return _Target("error", value, route)
    return _Target("not_checked", value, route)


def _target_result(wf: _Workflow, context: DoctorContext) -> _Result:
    if not wf.applied:
        return "not_checked", ("no synchronous dispatch entrypoint found",)
    statuses: list[DiagnosticStatus] = []
    lines: list[str] = []
    for entry in wf.applied:
        target = _decide_target(entry, context)
        statuses.append(target.status)
        note = ""
        if target.status == "error":
            note = " (process and worktree are lost when the Actions job ends)"
        lines.append(
            f"{target.value or 'undetermined'} via {target.route}{note}: {_where(entry)}"
        )
    return worst_status(statuses), tuple(lines)


def _required_env(target: str, config: Mapping[str, Any]) -> tuple[str, ...]:
    def configured(key: str) -> bool:
        value = config.get(key)
        return isinstance(value, str) and bool(value)

    if target == "cloud-routine":
        needed = [ROUTINE_TOKEN_ENV_VAR]
        if not configured("routine_id"):
            needed.append(ROUTINE_ID_ENV_VAR)
        return tuple(needed)
    return () if configured("codex_cloud_env") else (CODEX_CLOUD_ENV_VAR,)


def _visible_env(wf: _Workflow, entry: DispatchEntrypoint) -> set[str] | None:
    """Env names set at workflow/job/step level; ``None`` if any layer is an expression."""
    layers = (
        wf.document,
        wf.job_document(entry.location.job),
        wf.step_document(entry),
    )
    names: set[str] = set()
    for layer in layers:
        env = layer.get("env")
        if env is None:
            continue
        if not isinstance(env, Mapping):
            return None
        names.update(str(k) for k, v in env.items() if v is not None and v != "")
    return names


def _credentials_result(wf: _Workflow, context: DoctorContext) -> _Result:
    statuses: list[DiagnosticStatus] = []
    lines: list[str] = []
    for entry in wf.applied:
        target = _decide_target(entry, context)
        if target.status == "error":
            continue
        if target.status != "ok" or target.value is None:
            statuses.append("not_checked")
            lines.append(f"target undetermined: {_where(entry)}")
            continue
        names = _visible_env(wf, entry)
        if names is None:
            statuses.append("not_checked")
            lines.append(f"env is an expression: {_where(entry)}")
            continue
        needed = _required_env(target.value, context.config)
        missing = [n for n in needed if n not in names]
        statuses.append("error" if missing else "ok")
        lines.append(
            f"{target.value} needs {', '.join(needed) or 'no env'}"
            f"; missing: {', '.join(missing) or 'none'}: {_where(entry)}"
        )
    if not statuses:
        return "not_checked", ("no entrypoint with an external target to check",)
    return worst_status(statuses), tuple(lines)


def _messages(subject: str) -> dict[DiagnosticStatus, str]:
    return {
        "ok": f"{subject} is configured for the control workflow.",
        "error": f"{subject} is misconfigured for the control workflow.",
        "warning": f"{subject} needs attention.",
        "not_checked": f"{subject} could not be verified statically.",
    }


_GROUP_FIX = (
    f"Use `concurrency: {{group: {STANDARD_GROUP}, cancel-in-progress: false}}`"
    f" at the workflow top level. See {MIGRATION_DOC_REF}."
)

_CHECKS: tuple[tuple[str, str, str], ...] = (
    (CODE_ACTIONS_GROUP, "Concurrency group", _GROUP_FIX),
    (
        CODE_ACTIONS_CANCEL,
        "cancel-in-progress",
        f"Set `cancel-in-progress: false` as a literal boolean. See {MIGRATION_DOC_REF}.",
    ),
    (
        CODE_ACTIONS_PARALLELISM,
        "Dispatch parallelism",
        "Run dispatch in one non-matrix job without job-level concurrency or "
        "background launch.",
    ),
    (
        CODE_ACTIONS_ENTRYPOINT,
        "Dispatch entrypoint",
        "Call `orchestune dispatch` directly in a run step, not through wrappers.",
    ),
    (
        CODE_ACTIONS_TARGET,
        "Dispatch target",
        "Use an external target (cloud-routine or codex-cloud) in Actions.",
    ),
    (
        CODE_ACTIONS_CREDENTIALS,
        "Target credentials",
        "Pass the required ORCHESTUNE_* variables via env: on the workflow, job or step.",
    ),
)


def _per_workflow(
    code: str, wfs: list[_Workflow], context: DoctorContext
) -> list[tuple[str, _Result]]:
    plain: dict[str, Callable[[_Workflow], _Result]] = {
        CODE_ACTIONS_GROUP: _group_result,
        CODE_ACTIONS_CANCEL: _cancel_result,
        CODE_ACTIONS_PARALLELISM: _parallelism_result,
        CODE_ACTIONS_ENTRYPOINT: _entrypoint_result,
    }
    if code in plain:
        return [(wf.path, plain[code](wf)) for wf in wfs]
    fn = _target_result if code == CODE_ACTIONS_TARGET else _credentials_result
    return [(wf.path, fn(wf, context)) for wf in wfs]


def run_actions_checks(context: DoctorContext) -> tuple[Diagnostic, ...]:
    """Return the six ``dispatch.actions.*`` diagnostics, in a fixed order."""
    wfs = [
        _Workflow(f.path, f.document, scan_workflow(f.path, f.document))
        for f in context.specified
        if f.document is not None
    ]
    if not wfs:
        return tuple(
            Diagnostic(
                code,
                "not_checked",
                _messages(subject)["not_checked"],
                ("no readable workflow",),
                fix,
            )
            for code, subject, fix in _CHECKS
        )
    return tuple(
        _finish(code, _per_workflow(code, wfs, context), _messages(subject), fix)
        for code, subject, fix in _CHECKS
    )

"""Deterministic detection of dispatch / control entrypoints in workflow ``run`` lines.

Pure functions only: no subprocess, network, or file access. Results use their own
dataclasses so that the diagnostic layer can convert them to findings.
"""

from __future__ import annotations

import argparse
import posixpath
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, NoReturn

from orchestune.dispatch.config_loader import _add_cli_arguments

EntrypointVia = Literal["orchestune", "orchestune-dispatch", "python-m"]
ControlKind = Literal["gc", "recover"]
UndetectableKind = Literal[
    "command_expansion",
    "nested_shell",
    "script",
    "unparsable_line",
    "local_action",
    "docker_action",
    "reusable_workflow",
]

GHA_EXPR_PLACEHOLDER = "__ORCHESTUNE_GHA_EXPR__"
_EVIDENCE_LIMIT = 200
_GHA_EXPR_RE = re.compile(r"\$\{\{.*?\}\}")
_QUOTED = r"\"[^\"]*\"|'[^']*'"
_REDIRECT_RE = re.compile(
    rf"({_QUOTED})|(?<![\w$])\d*(?:&>>?|>>?&?|<<?<?|<&?)\s*(?:\d+-?|{_QUOTED}|[^\s;&|()<>\"']+)"
)
_PYTHON_RE = re.compile(r"^python(3(\.\d+)?)?(\.exe)?$")
_DISPATCH_MODULE = "orchestune.dispatch.dispatcher"
_SEPARATORS = frozenset({";", "&", "&&", "||", "|", "|&", ";;", "(", ")"})
_PUNCT_PARTS = ("&&", "||", "|&", ";;", ";", "&", "|", "(", ")")
_SHELLS = frozenset({"bash", "sh", "zsh", "dash"})
_BACKGROUND_WORDS = frozenset({"nohup", "setsid", "screen", "tmux"})
_PREFIX_WORDS = frozenset(
    {"uv", "run", "uvx", "poetry", "pipx", "env", "sudo", "time", "bash", "sh", "pwsh"}
    | {"python", "python3"}
)
_SCRIPT_SUFFIXES = (".sh", ".bash", ".py", ".ps1")
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass(frozen=True)
class StepLocation:
    workflow: str
    job: str
    step_index: int
    line_no: int


@dataclass(frozen=True)
class DispatchEntrypoint:
    location: StepLocation
    command: str
    via: EntrypointVia
    apply: bool
    dispatch_target: str | None
    target_is_dynamic: bool
    background: bool
    args_error: str | None


@dataclass(frozen=True)
class ControlEntrypoint:
    location: StepLocation
    command: str
    kind: ControlKind


@dataclass(frozen=True)
class UndetectableCall:
    location: StepLocation
    kind: UndetectableKind
    detail: str


@dataclass(frozen=True)
class JobScan:
    job: str
    dispatch: tuple[DispatchEntrypoint, ...]
    control: tuple[ControlEntrypoint, ...]
    undetectable: tuple[UndetectableCall, ...]


@dataclass(frozen=True)
class WorkflowScan:
    workflow: str
    jobs: tuple[JobScan, ...]

    @property
    def dispatch(self) -> tuple[DispatchEntrypoint, ...]:
        return tuple(item for job in self.jobs for item in job.dispatch)

    @property
    def control(self) -> tuple[ControlEntrypoint, ...]:
        return tuple(item for job in self.jobs for item in job.control)

    @property
    def undetectable(self) -> tuple[UndetectableCall, ...]:
        return tuple(item for job in self.jobs for item in job.undetectable)


class _ArgsError(Exception):
    pass


class _RaisingParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _ArgsError(message)


def _build_dispatch_parser() -> tuple[argparse.ArgumentParser, frozenset[str]]:
    parser = _RaisingParser(prog="orchestune dispatch", add_help=False)
    _add_cli_arguments(parser)
    choices: frozenset[str] = frozenset()
    for action in parser._actions:  # noqa: SLF001
        if action.dest == "dispatch_target" and action.choices:
            choices = frozenset(action.choices)
    # Values may be shell expansions, so type conversion and choices are checked later.
    for action in parser._actions:  # noqa: SLF001
        action.type = None
        action.choices = None
    return parser, choices


_PARSER, _TARGET_CHOICES = _build_dispatch_parser()


def _clip(text: str) -> str:
    return text[:_EVIDENCE_LIMIT]


def _is_dynamic(value: str) -> bool:
    return "$" in value or GHA_EXPR_PLACEHOLDER in value


def _logical_lines(run: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    pending: list[str] = []
    start = 0
    for number, raw in enumerate(run.split("\n"), start=1):
        line = raw.rstrip()
        if not pending:
            start = number
        if line.endswith("\\"):
            pending.append(line[:-1].strip())
            continue
        pending.append(line.strip())
        text = " ".join(part for part in pending if part)
        pending = []
        if text and not text.startswith("#"):
            lines.append((start, text))
    text = " ".join(part for part in pending if part)
    if text:
        lines.append((start, text))
    return lines


def _split_punct(token: str) -> list[str]:
    if token in _SEPARATORS or not token or any(c not in ";&|()" for c in token):
        return [token]
    parts: list[str] = []
    rest = token
    while rest:
        part = next(p for p in _PUNCT_PARTS if rest.startswith(p))
        parts.append(part)
        rest = rest[len(part) :]
    return parts


def _tokenize(line: str) -> list[str]:
    replaced = _GHA_EXPR_RE.sub(GHA_EXPR_PLACEHOLDER, line)
    # Drop redirections (``>log``, ``2>&1``) outside quotes so they are not read as arguments.
    replaced = _REDIRECT_RE.sub(lambda m: m.group(1) or "", replaced)
    lex = shlex.shlex(replaced, posix=True, punctuation_chars=";&|()")
    lex.whitespace_split = True
    tokens: list[str] = []
    for token in lex:
        tokens.extend(_split_punct(token))
    return tokens


def _group_background(tokens: list[str]) -> list[tuple[list[str], bool]]:
    """Segment tokens, propagating a trailing ``&`` after ``( ... )`` to its members."""
    segments: list[tuple[list[str], bool]] = []
    current: list[str] = []
    stack: list[int] = []
    last_group: tuple[int, int] | None = None
    for token in tokens:
        if token not in _SEPARATORS:
            current.append(token)
            continue
        if current:
            segments.append((current, token == "&"))
            current = []
        if token == "(":
            stack.append(len(segments))
        elif token == ")":
            begin = stack.pop() if stack else len(segments)
            last_group = (begin, len(segments))
            continue
        elif token == "&" and last_group is not None:
            begin, end = last_group
            for index in range(begin, end):
                segments[index] = (segments[index][0], True)
        last_group = None
    if current:
        segments.append((current, False))
    return segments


def _strip_assignments(segment: list[str]) -> list[str]:
    index = 0
    while index < len(segment) and _ASSIGNMENT_RE.match(segment[index]):
        index += 1
    return segment[index:]


def _basename(token: str) -> str:
    return posixpath.basename(token.replace("\\", "/"))


def _is_orchestune(token: str) -> bool:
    return _basename(token) in {"orchestune", "orchestune.exe"}


def _interpret_dispatch(args: list[str]) -> tuple[bool, str | None, bool, str | None]:
    try:
        ns, unknown = _PARSER.parse_known_args(args)
    except _ArgsError as exc:
        return True, None, False, str(exc)
    target = ns.dispatch_target
    dynamic = isinstance(target, str) and _is_dynamic(target)
    error: str | None = None
    if unknown:
        error = f"unrecognized arguments: {' '.join(unknown)}"
    elif isinstance(target, str) and not dynamic and target not in _TARGET_CHOICES:
        error = f"invalid --dispatch-target: {target}"
    return ns.apply is not False, target, dynamic, error


def _is_recover_apply(arg: str) -> bool:
    if arg.startswith("--apply="):
        return True
    return len(arg) >= 3 and "--apply".startswith(arg)


def _find_dispatch(segment: list[str]) -> tuple[str, list[str]] | None:
    """Return (kind, args); kind is a dispatch ``via`` or ``control:<gc|recover>``."""
    for index, token in enumerate(segment):
        rest = segment[index + 1 :]
        if _is_orchestune(token):
            sub = next((i for i, t in enumerate(rest) if not t.startswith("-")), None)
            if sub is None:
                continue
            name, args = rest[sub], rest[sub + 1 :]
            if name == "dispatch":
                return "orchestune", args
            if name == "gc" and "--no-apply" not in args:
                return "control:gc", args
            if name == "recover" and any(_is_recover_apply(a) for a in args):
                return "control:recover", args
        elif _basename(token) == "orchestune-dispatch":
            return "orchestune-dispatch", rest
        elif _PYTHON_RE.match(_basename(token)):
            if rest[:2] == ["-m", _DISPATCH_MODULE]:
                return "python-m", rest[2:]
            if rest[:1] == [f"-m{_DISPATCH_MODULE}"]:
                return "python-m", rest[1:]
    return None


def _undetectable_segment(segment: list[str]) -> tuple[UndetectableKind, str] | None:
    head = segment[0]
    if head.startswith("$") or head.startswith(GHA_EXPR_PLACEHOLDER):
        return "command_expansion", head
    if head == "eval" or (head in _SHELLS and len(segment) > 1 and segment[1] == "-c"):
        return "nested_shell", " ".join(segment[:2])
    if "-m" in segment[:3] or "-c" in segment[:3]:
        return None
    rest = [t for t in segment if t not in _PREFIX_WORDS]
    if rest and (
        rest[0].startswith(("./", "../")) or rest[0].endswith(_SCRIPT_SUFFIXES)
    ):
        return "script", rest[0]
    return None


@dataclass
class _Acc:
    dispatch: list[DispatchEntrypoint]
    control: list[ControlEntrypoint]
    undetectable: list[UndetectableCall]


def _scan_line(
    workflow: str, job: str, step: int, line_no: int, line: str, acc: _Acc
) -> None:
    location = StepLocation(workflow, job, step, line_no)
    try:
        tokens = _tokenize(line)
    except ValueError as exc:
        acc.undetectable.append(
            UndetectableCall(location, "unparsable_line", _clip(str(exc)))
        )
        return
    has_disown = "disown" in tokens
    for segment, background in _group_background(tokens):
        segment = _strip_assignments(segment)
        if not segment:
            continue
        found = _find_dispatch(segment)
        if found is None:
            problem = _undetectable_segment(segment)
            if problem:
                acc.undetectable.append(
                    UndetectableCall(location, problem[0], _clip(problem[1]))
                )
            continue
        kind, args = found
        if kind.startswith("control:"):
            control_kind: ControlKind = "gc" if kind.endswith("gc") else "recover"
            acc.control.append(ControlEntrypoint(location, _clip(line), control_kind))
            continue
        via: EntrypointVia = kind  # type: ignore[assignment]
        apply, target, dynamic, error = _interpret_dispatch(args)
        detached = (
            background or has_disown or any(t in _BACKGROUND_WORDS for t in segment)
        )
        acc.dispatch.append(
            DispatchEntrypoint(
                location, _clip(line), via, apply, target, dynamic, detached, error
            )
        )


def _classify_uses(uses: str) -> UndetectableKind | None:
    if uses.startswith("./"):
        return "local_action"
    if uses.startswith("docker://"):
        return "docker_action"
    if "/.github/workflows/" in uses:
        return "reusable_workflow"
    return None


def _scan_step(
    workflow: str, job: str, index: int, step: Mapping[str, Any], acc: _Acc
) -> None:
    uses = step.get("uses")
    if isinstance(uses, str):
        kind = _classify_uses(uses)
        if kind:
            acc.undetectable.append(
                UndetectableCall(
                    StepLocation(workflow, job, index, 0), kind, _clip(uses)
                )
            )
    run = step.get("run")
    if isinstance(run, str):
        for line_no, line in _logical_lines(run):
            _scan_line(workflow, job, index, line_no, line, acc)


def _scan_job(workflow: str, name: str, job: Mapping[str, Any]) -> JobScan:
    acc = _Acc([], [], [])
    uses = job.get("uses")
    if isinstance(uses, str):
        acc.undetectable.append(
            UndetectableCall(
                StepLocation(workflow, name, -1, 0), "reusable_workflow", _clip(uses)
            )
        )
    steps = job.get("steps")
    if isinstance(steps, list):
        for index, step in enumerate(steps):
            if isinstance(step, Mapping):
                _scan_step(workflow, name, index, step, acc)
    return JobScan(
        name, tuple(acc.dispatch), tuple(acc.control), tuple(acc.undetectable)
    )


def scan_workflow(path: str, document: Mapping[str, Any]) -> WorkflowScan:
    jobs = document.get("jobs")
    scans: list[JobScan] = []
    if isinstance(jobs, Mapping):
        for name, job in jobs.items():
            if isinstance(job, Mapping):
                scans.append(_scan_job(path, str(name), job))
    return WorkflowScan(path, tuple(scans))

"""Offline, read-only executor for ``orchestune doctor``.

Later diagnostics attach to :func:`run_doctor`; this module owns the result
assembly, the workflow YAML loader and the repository/config readers.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from orchestune.dag.models import ConfigError
from orchestune.dispatch.config_loader import validate_toml_config
from orchestune.dispatch.doctor_actions import run_actions_checks
from orchestune.dispatch.doctor_models import (
    CODE_CONFIG_READABLE,
    CODE_EXTERNAL_OWNERSHIP,
    CODE_LOCAL_SERIALIZATION,
    CODE_STATE_CONTINUITY,
    CODE_WORKFLOW_READABLE,
    Diagnostic,
    DoctorContext,
    DoctorReport,
    ExecutionMode,
    WorkflowFile,
)
from orchestune.infra.git_cli import get_git_repository_paths
from orchestune.infra.repository_config import find_and_load_config_file

WORKFLOWS_DIR = ".github/workflows"


class DoctorInputError(Exception):
    """Invalid arguments or location; the CLI maps this to exit 2."""


@dataclass(frozen=True)
class DoctorRequest:
    mode: ExecutionMode
    repo_root: Path
    workflows: tuple[str, ...] = ()


def resolve_repository_root(cwd: Path | None = None) -> Path:
    try:
        return get_git_repository_paths(cwd)[0]
    except (subprocess.CalledProcessError, OSError, RuntimeError) as exc:
        raise DoctorInputError(f"not a git repository: {cwd or Path.cwd()}") from exc


def normalize_workflow_path(raw: str, repo_root: Path, cwd: Path) -> str:
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    resolved = candidate.resolve()
    try:
        return resolved.relative_to(repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise DoctorInputError(f"workflow is outside the repository: {raw}") from exc


class _WorkflowLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate keys and keeps ``on`` a string."""

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        if isinstance(node, yaml.MappingNode):
            seen: set[Any] = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=True)
                try:
                    duplicate = key in seen
                except TypeError:
                    continue  # unhashable key: let the base class report it
                if duplicate:
                    raise yaml.constructor.ConstructorError(
                        None,
                        None,
                        f"found duplicate key {key!r}",
                        key_node.start_mark,
                    )
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


_WorkflowLoader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:bool"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_WorkflowLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def load_workflow_yaml(text: str) -> Any:
    return yaml.load(text, Loader=_WorkflowLoader)  # noqa: S506 - SafeLoader subclass


def discover_workflow_files(repo_root: Path) -> tuple[str, ...]:
    directory = repo_root / WORKFLOWS_DIR
    if not directory.is_dir():
        return ()
    names = [
        p.name
        for p in directory.iterdir()
        if p.suffix in {".yml", ".yaml"} and p.is_file()
    ]
    return tuple(f"{WORKFLOWS_DIR}/{name}" for name in sorted(names))


def _one_line(text: str) -> str:
    return " ".join(text.split())


def read_workflow(repo_root: Path, rel_path: str) -> WorkflowFile:
    def failed(reason: str) -> WorkflowFile:
        return WorkflowFile(
            rel_path, None, _one_line(reason).replace(str(repo_root), ".")
        )

    try:
        text = (repo_root / rel_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return failed("FileNotFoundError: file does not exist")
    except IsADirectoryError:
        return failed("IsADirectoryError: path is a directory")
    except UnicodeDecodeError:
        return failed("UnicodeDecodeError: file is not valid utf-8")
    except OSError as exc:
        return failed(f"{type(exc).__name__}: {exc.strerror or 'unreadable'}")
    try:
        document = load_workflow_yaml(text)
    except yaml.YAMLError as exc:
        return failed(f"{type(exc).__name__}: {exc}")
    if not isinstance(document, dict):
        return failed("top-level must be a mapping")
    return WorkflowFile(rel_path, document, None)


def load_repository_config(repo_root: Path) -> tuple[dict[str, Any], Diagnostic]:
    try:
        config = validate_toml_config(find_and_load_config_file(repo_root))
    except ConfigError as exc:
        reason = _one_line(str(exc)).replace(str(repo_root), ".")
        return {}, Diagnostic(
            CODE_CONFIG_READABLE,
            "error",
            "Dispatch configuration could not be loaded.",
            (reason,),
            "Fix orchestune.toml or [tool.orchestune] in pyproject.toml.",
        )
    return config, Diagnostic(
        CODE_CONFIG_READABLE, "ok", "Dispatch configuration is readable."
    )


def check_workflow_readable(files: Sequence[WorkflowFile]) -> Diagnostic:
    failures = [f"{f.path}: {f.error}" for f in files if f.error is not None]
    if failures:
        return Diagnostic(
            CODE_WORKFLOW_READABLE,
            "error",
            "Specified workflow files could not be read.",
            tuple(failures),
            "Fix the file path or YAML syntax (duplicate keys are rejected).",
        )
    return Diagnostic(
        CODE_WORKFLOW_READABLE,
        "ok",
        "Specified workflow files are readable.",
        tuple(f.path for f in files),
    )


def ownership_diagnostics(mode: ExecutionMode) -> tuple[Diagnostic, ...]:
    items: list[Diagnostic] = []
    if mode == "local":
        items.append(
            Diagnostic(
                CODE_LOCAL_SERIALIZATION,
                "not_checked",
                "Whether only one local dispatcher runs at a time is not verified.",
                remediation="Run a single local dispatcher per repository.",
            )
        )
    items.append(
        Diagnostic(
            CODE_EXTERNAL_OWNERSHIP,
            "not_checked",
            "Whether another system also dispatches this repository is not verified.",
            remediation="Confirm no other scheduler or runner owns dispatch.",
        )
    )
    items.append(
        Diagnostic(
            CODE_STATE_CONTINUITY,
            "not_checked",
            "Whether run state persists between dispatch runs is not verified.",
            remediation="Confirm the run-state location survives between runs.",
        )
    )
    return tuple(items)


def build_context(
    request: DoctorRequest,
    config_result: tuple[dict[str, Any], Diagnostic] | None = None,
) -> DoctorContext:
    config, config_diag = config_result or load_repository_config(request.repo_root)
    specified = tuple(read_workflow(request.repo_root, p) for p in request.workflows)
    discovered = tuple(
        read_workflow(request.repo_root, p)
        for p in discover_workflow_files(request.repo_root)
    )
    return DoctorContext(
        mode=request.mode,
        specified=specified if request.mode == "actions" else (),
        discovered=discovered,
        config=config,
        config_valid=config_diag.status != "error",
    )


def run_doctor(request: DoctorRequest) -> DoctorReport:
    """Order: config -> workflow.readable (actions) -> later checks -> ownership."""
    config_result = load_repository_config(request.repo_root)
    config_diag = config_result[1]
    context = build_context(request, config_result)
    diagnostics: list[Diagnostic] = [config_diag]
    if request.mode == "actions":
        diagnostics.append(check_workflow_readable(context.specified))
        diagnostics.extend(run_actions_checks(context))
    # Later sub-tasks add: repository-wide checks here.
    diagnostics.extend(ownership_diagnostics(request.mode))
    return DoctorReport(request.mode, tuple(diagnostics))

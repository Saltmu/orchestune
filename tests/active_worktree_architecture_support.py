"""AST guard support for ActiveWorktree subrecord and owner boundaries (#1135)."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

SUBRECORDS = frozenset({"core", "launch", "claim", "completion"})
SUBRECORD_CLASSES = frozenset(
    {"ActiveWorktreeCore", "LaunchInfo", "ClaimInfo", "ActiveCompletionJournal"}
)
FLAT_ATTRIBUTES = frozenset(
    {
        "pid",
        "started_at",
        "recompute_count",
        "forced_serial",
        "external_id",
        "external_url",
        "estimated_tokens",
        "token_estimate_recorded",
        "profile",
        "model",
        "reasoning_effort",
        "selection_reason",
        "launch_attempt_id",
        "launch_phase",
        "owner_kind",
        "claim_id",
        "claim_stage",
        "base_ref",
        "base_sha",
        "reservation_kind",
        "repository_id",
        "claimed_at",
        "owner_token_digest",
        "completion_id",
        "completion_result",
        "completion_stage",
        "completion_payload",
        "completion_comment_id",
        "completion_comment_url",
        "completion_handoff_ready",
        "completion_policy_config",
    }
)

CLAIM_OWNER_MODULES = frozenset(
    {
        "orchestune.claim.ownership",
        "orchestune.claim.service",
        "orchestune.claim.amend",
    }
)

COMPLETE_OWNER_MODULES = frozenset(
    {
        "orchestune.complete.journal",
    }
)

LAUNCH_OWNER_MODULES = frozenset(
    {
        "orchestune.dispatch.launch_state",
    }
)

CORE_OWNER_MODULES = frozenset(
    {
        "orchestune.ledger.active_records",
    }
)

ALLOWED_CONSTRUCTOR_MODULES = frozenset(
    {
        "orchestune.ledger.active_records",
        "orchestune.ledger.active_codec",
        "orchestune.claim.ownership",
        "orchestune.claim.service",
        "orchestune.claim.amend",
        "orchestune.dispatch.launch_state",
    }
)

PAYLOAD_MUTATING_METHODS = frozenset({"update", "pop", "clear", "setdefault"})


@dataclass(frozen=True, slots=True)
class ActiveWorktreeBoundaryException:
    module: str
    function: str
    target: str
    kind: str
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("boundary exception reason must not be empty")


@dataclass(frozen=True, slots=True)
class ActiveWorktreeBoundaryViolation:
    module: str
    function: str
    target: str
    kind: str
    line: int


def _exception(
    module: str, function: str, target: str, kind: str, reason: str
) -> ActiveWorktreeBoundaryException:
    return ActiveWorktreeBoundaryException(module, function, target, kind, reason)


_PERSISTENCE = "Materialize recovery sentinel in memory before persistence."
_RECOVERY = (
    "In-place recovery counter update of in-memory active entry after with_launch."
)
_REBASE = "In-place rebase update of in-memory active entry."
_RECOVERY_PR = "Recovery PR head_ref adoption before completion resolution."
_LIFECYCLE = "Authoritative candidate lifecycle calculation from persisted state."
_RESERVATION_CHECK = "Completion reservation check before action or exclusion."

ACTIVE_WORKTREE_PRODUCTION_EXCEPTIONS: frozenset[ActiveWorktreeBoundaryException] = (
    frozenset(
        {
            _exception(
                "orchestune.ledger.run_state",
                "_materialize_active_worktree_for_persistence",
                "claim",
                "subrecord-direct-assign",
                _PERSISTENCE,
            ),
            _exception(
                "orchestune.dispatch.rebase",
                "_apply_forced_serial_event",
                "launch",
                "subrecord-direct-assign",
                _RECOVERY,
            ),
            _exception(
                "orchestune.dispatch.rebase",
                "_apply_recomputed_event",
                "launch",
                "subrecord-direct-assign",
                _RECOVERY,
            ),
            _exception(
                "orchestune.dispatch.rebase",
                "_apply_auto_rebase",
                "launch",
                "subrecord-direct-assign",
                _REBASE,
            ),
            _exception(
                "orchestune.dispatch.rebase",
                "_apply_auto_rebase",
                "core",
                "subrecord-direct-assign",
                _REBASE,
            ),
            _exception(
                "orchestune.dispatch.gc",
                "_resolve_recovered_completion",
                "launch",
                "unauthorized-replace",
                _RECOVERY_PR,
            ),
            _exception(
                "orchestune.dispatch.gc",
                "_resolve_recovered_completion",
                "core",
                "unauthorized-replace",
                _RECOVERY_PR,
            ),
            _exception(
                "orchestune.ledger.active_lifecycle",
                "lifecycle",
                "completion_id",
                "completion-stage-is-none",
                _LIFECYCLE,
            ),
            _exception(
                "orchestune.claim.ownership",
                "has_completion_reservation",
                "completion_id",
                "completion-stage-is-none",
                _RESERVATION_CHECK,
            ),
            _exception(
                "orchestune.complete.journal",
                "_reserve_legacy_completion",
                "completion_id",
                "completion-stage-is-none",
                _RESERVATION_CHECK,
            ),
            _exception(
                "orchestune.dispatch.cycle_actions",
                "_run_active_worktree_rules",
                "completion_id",
                "completion-stage-is-none",
                _RESERVATION_CHECK,
            ),
            _exception(
                "orchestune.dispatch.gc",
                "_rule_not_needed",
                "completion_id",
                "completion-stage-is-none",
                _RESERVATION_CHECK,
            ),
            _exception(
                "orchestune.dispatch.gc",
                "_resolve_completion",
                "completion_id",
                "completion-stage-is-none",
                _RESERVATION_CHECK,
            ),
            _exception(
                "orchestune.dispatch.gc",
                "_rule_completed",
                "completion_id",
                "completion-stage-is-none",
                _RESERVATION_CHECK,
            ),
            _exception(
                "orchestune.dispatch.gc.completion",
                "_is_worktree_complete",
                "completion_id",
                "completion-stage-is-none",
                _RESERVATION_CHECK,
            ),
            _exception(
                "orchestune.dispatch.gc.zombies",
                "_is_completing_or_handoff",
                "completion_id",
                "completion-stage-is-none",
                _RESERVATION_CHECK,
            ),
        }
    )
)


class _ActiveWorktreeVisitor(ast.NodeVisitor):
    def __init__(self, module: str) -> None:
        self.module = module
        self.functions: list[str] = ["<module>"]
        self.violations: list[ActiveWorktreeBoundaryViolation] = []
        self.replace_aliases: set[str] = set()
        self.constructor_names: set[str] = {
            "ActiveWorktree",
            "ActiveWorktree.from_records",
        }
        self.active_vars_stack: list[set[str]] = [set()]
        self.completion_vars_stack: list[set[str]] = [set()]
        self.other_vars_stack: list[set[str]] = [set()]

    def _record(self, target: str, kind: str, line: int) -> None:
        self.violations.append(
            ActiveWorktreeBoundaryViolation(
                self.module, self.functions[-1], target, kind, line
            )
        )

    def _is_active_var(self, name: str) -> bool:
        for scope in reversed(self.active_vars_stack):
            if name in scope:
                return True
        for scope in reversed(self.other_vars_stack):
            if name in scope:
                return False
        return name in {"active", "active_worktree"}

    def _is_completion_var(self, name: str) -> bool:
        for scope in reversed(self.completion_vars_stack):
            if name in scope:
                return True
        return False

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "dataclasses":
                name = alias.asname or "dataclasses"
                self.replace_aliases.add(f"{name}.replace")
            if alias.name.endswith("active_records"):
                mod_name = alias.asname or alias.name
                self.constructor_names.add(f"{mod_name}.ActiveWorktree")
                self.constructor_names.add(f"{mod_name}.ActiveWorktree.from_records")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "dataclasses":
            for alias in node.names:
                if alias.name == "replace":
                    self.replace_aliases.add(alias.asname or "replace")
                elif alias.name == "*":
                    self.replace_aliases.add("replace")
        for alias in node.names:
            if alias.name == "ActiveWorktree":
                ctor_name = alias.asname or "ActiveWorktree"
                self.constructor_names.add(ctor_name)
                self.constructor_names.add(f"{ctor_name}.from_records")
            elif alias.name == "active_records":
                mod_name = alias.asname or "active_records"
                self.constructor_names.add(f"{mod_name}.ActiveWorktree")
                self.constructor_names.add(f"{mod_name}.ActiveWorktree.from_records")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter_function(node)

    def _enter_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.functions.append(node.name)
        active_scope: set[str] = set()
        completion_scope: set[str] = set()
        other_scope: set[str] = set()
        for arg in node.args.args + node.args.kwonlyargs:
            if arg.annotation is not None:
                types = _extract_type_names(arg.annotation)
                if "ActiveWorktree" in types:
                    active_scope.add(arg.arg)
                elif "ActiveCompletionJournal" in types:
                    completion_scope.add(arg.arg)
                elif types - {"None", "NoneType"}:
                    other_scope.add(arg.arg)
                elif arg.arg in {"active", "active_worktree"}:
                    active_scope.add(arg.arg)
                else:
                    other_scope.add(arg.arg)
            elif arg.arg in {"active", "active_worktree"}:
                active_scope.add(arg.arg)
            else:
                other_scope.add(arg.arg)
        self.active_vars_stack.append(active_scope)
        self.completion_vars_stack.append(completion_scope)
        self.other_vars_stack.append(other_scope)
        self.generic_visit(node)
        self.active_vars_stack.pop()
        self.completion_vars_stack.pop()
        self.other_vars_stack.pop()
        self.functions.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        self._check_assignment(node.targets, node.lineno)
        self._track_assigned_var(node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._check_assignment([node.target], node.lineno)
        if isinstance(node.target, ast.Name):
            types = _extract_type_names(node.annotation)
            if "ActiveWorktree" in types:
                self.active_vars_stack[-1].add(node.target.id)
            elif "ActiveCompletionJournal" in types:
                self.completion_vars_stack[-1].add(node.target.id)
            elif types - {"None", "NoneType"}:
                self.other_vars_stack[-1].add(node.target.id)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._check_assignment([node.target], node.lineno)
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            if isinstance(target, ast.Subscript) and self._is_payload_expr(
                target.value
            ):
                self._record("completion_payload", "payload-mutation", node.lineno)
        self.generic_visit(node)

    def _is_constructor_call(self, func_name: str | None) -> bool:
        if func_name is None:
            return False
        if func_name in self.constructor_names:
            return True
        return (
            func_name == "ActiveWorktree"
            or func_name.endswith(".ActiveWorktree")
            or func_name == "ActiveWorktree.from_records"
            or func_name.endswith(".ActiveWorktree.from_records")
        )

    def _track_assigned_var(self, targets: list[ast.expr], value: ast.expr) -> None:
        for target in targets:
            if isinstance(target, ast.Name):
                if isinstance(value, ast.Call):
                    func_name = _extract_func_name(value.func)
                    if self._is_constructor_call(func_name) or (
                        func_name is not None
                        and (
                            func_name
                            in {"with_claim", "with_completion", "with_launch"}
                            or func_name.endswith(".with_claim")
                            or func_name.endswith(".with_completion")
                            or func_name.endswith(".with_launch")
                        )
                    ):
                        self.active_vars_stack[-1].add(target.id)
                    elif (
                        func_name is not None and func_name.split(".")[-1][0].isupper()
                    ):
                        self.other_vars_stack[-1].add(target.id)
                elif isinstance(value, ast.Attribute) and value.attr == "completion":
                    if isinstance(value.value, ast.Name) and self._is_active_var(
                        value.value.id
                    ):
                        self.completion_vars_stack[-1].add(target.id)

    def _check_assignment(self, targets: list[ast.expr], lineno: int) -> None:
        for target in targets:
            if isinstance(target, ast.Subscript):
                if self._is_payload_expr(target.value):
                    self._record("completion_payload", "payload-mutation", lineno)
                elif self._is_policy_config_expr(target.value):
                    self._record("completion_policy_config", "payload-mutation", lineno)
            elif isinstance(target, ast.Attribute):
                # active.launch = ...
                if isinstance(target.value, ast.Name):
                    if self._is_active_var(target.value.id):
                        if target.attr in SUBRECORDS:
                            self._record(target.attr, "subrecord-direct-assign", lineno)
                        elif target.attr in FLAT_ATTRIBUTES:
                            self._record(target.attr, "flat-attribute-access", lineno)
                # active.launch.pid = ...
                elif isinstance(target.value, ast.Attribute):
                    sub = target.value
                    if isinstance(sub.value, ast.Name) and self._is_active_var(
                        sub.value.id
                    ):
                        if sub.attr in SUBRECORDS:
                            self._record(
                                f"{sub.attr}.{target.attr}",
                                "subrecord-direct-assign",
                                lineno,
                            )

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load | ast.Del):
            if isinstance(node.value, ast.Name) and self._is_active_var(node.value.id):
                if node.attr in FLAT_ATTRIBUTES:
                    self._record(node.attr, "flat-attribute-access", node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func_name = _extract_func_name(node.func)
        # Check constructor: ActiveWorktree(...) or ActiveWorktree.from_records(...)
        if self._is_constructor_call(func_name):
            if self.module not in ALLOWED_CONSTRUCTOR_MODULES:
                self._record("ActiveWorktree", "unauthorized-constructor", node.lineno)

        # Check setattr
        elif func_name == "setattr" and len(node.args) >= 2:
            first = node.args[0]
            if isinstance(first, ast.Name) and self._is_active_var(first.id):
                second = node.args[1]
                attr_name = (
                    second.value
                    if isinstance(second, ast.Constant)
                    and isinstance(second.value, str)
                    else "subrecord"
                )
                self._record(attr_name, "subrecord-setattr", node.lineno)
            elif (
                isinstance(first, ast.Attribute)
                and isinstance(first.value, ast.Name)
                and self._is_active_var(first.value.id)
            ):
                self._record(first.attr, "subrecord-setattr", node.lineno)

        # Check dataclasses.replace
        elif func_name in self.replace_aliases or (
            func_name and func_name.endswith(".replace")
        ):
            if node.args:
                first = node.args[0]
                if isinstance(first, ast.Name) and self._is_active_var(first.id):
                    self._check_replace_call(node)

        # Check mutating method on payload
        elif (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in PAYLOAD_MUTATING_METHODS
        ):
            if self._is_payload_expr(node.func.value):
                self._record("completion_payload", "payload-mutation", node.lineno)
            elif self._is_policy_config_expr(node.func.value):
                self._record(
                    "completion_policy_config", "payload-mutation", node.lineno
                )

        self.generic_visit(node)

    def _check_replace_call(self, node: ast.Call) -> None:
        kw_names = {kw.arg for kw in node.keywords if kw.arg is not None}
        if "launch" in kw_names:
            if self.module not in LAUNCH_OWNER_MODULES:
                self._record("launch", "unauthorized-replace", node.lineno)
        if "claim" in kw_names:
            if self.module not in CLAIM_OWNER_MODULES:
                self._record("claim", "unauthorized-replace", node.lineno)
        if "completion" in kw_names:
            if self.module not in COMPLETE_OWNER_MODULES:
                self._record("completion", "unauthorized-replace", node.lineno)
        if "core" in kw_names:
            if self.module not in CORE_OWNER_MODULES:
                self._record("core", "unauthorized-replace", node.lineno)
        flat_overlap = kw_names & FLAT_ATTRIBUTES
        for flat_attr in flat_overlap:
            self._record(flat_attr, "flat-attribute-access", node.lineno)
        if not (kw_names & SUBRECORDS) and not flat_overlap and kw_names:
            if self.module not in ALLOWED_CONSTRUCTOR_MODULES:
                self._record("active", "unauthorized-replace", node.lineno)

    def visit_Compare(self, node: ast.Compare) -> None:
        has_is_none = any(
            isinstance(op, ast.Is | ast.IsNot) for op in node.ops
        ) and any(
            isinstance(cmp, ast.Constant) and cmp.value is None
            for cmp in [node.left] + node.comparators
        )
        if has_is_none:
            for expr in [node.left] + node.comparators:
                if self._is_active_completion_id_expr(expr):
                    self._record(
                        "completion_id", "completion-stage-is-none", node.lineno
                    )
        self.generic_visit(node)

    def _is_payload_expr(self, expr: ast.expr) -> bool:
        if isinstance(expr, ast.Attribute) and expr.attr == "completion_payload":
            if (
                isinstance(expr.value, ast.Attribute)
                and expr.value.attr == "completion"
            ):
                base = expr.value.value
                return isinstance(base, ast.Name) and self._is_active_var(base.id)
            if isinstance(expr.value, ast.Name):
                return self._is_completion_var(expr.value.id) or self._is_active_var(
                    expr.value.id
                )
        return False

    def _is_policy_config_expr(self, expr: ast.expr) -> bool:
        if isinstance(expr, ast.Attribute) and expr.attr == "completion_policy_config":
            if (
                isinstance(expr.value, ast.Attribute)
                and expr.value.attr == "completion"
            ):
                base = expr.value.value
                return isinstance(base, ast.Name) and self._is_active_var(base.id)
            if isinstance(expr.value, ast.Name):
                return self._is_completion_var(expr.value.id) or self._is_active_var(
                    expr.value.id
                )
        return False

    def _is_active_completion_id_expr(self, expr: ast.expr) -> bool:
        if isinstance(expr, ast.Attribute) and expr.attr == "completion_id":
            # active.completion.completion_id
            if (
                isinstance(expr.value, ast.Attribute)
                and expr.value.attr == "completion"
            ):
                base = expr.value.value
                if isinstance(base, ast.Name):
                    return self._is_active_var(base.id)
                return True
            # active.completion_id (flat)
            if isinstance(expr.value, ast.Name):
                return self._is_active_var(expr.value.id) or self._is_completion_var(
                    expr.value.id
                )
        return False


def _extract_type_names(node: ast.expr) -> set[str]:
    names: set[str] = set()
    if isinstance(node, ast.Name):
        names.add(node.id)
    elif isinstance(node, ast.Attribute):
        names.add(node.attr)
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        names.update(_extract_type_names(node.left))
        names.update(_extract_type_names(node.right))
    elif isinstance(node, ast.Subscript):
        if isinstance(node.slice, ast.Tuple):
            for elt in node.slice.elts:
                names.update(_extract_type_names(elt))
        else:
            names.update(_extract_type_names(node.slice))
    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            parsed = ast.parse(node.value, mode="eval")
            names.update(_extract_type_names(parsed.body))
        except SyntaxError:
            pass
    return names


def _extract_func_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _extract_func_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _match_exception(
    violation: ActiveWorktreeBoundaryViolation,
    exceptions: frozenset[ActiveWorktreeBoundaryException],
) -> bool:
    for exc in exceptions:
        if exc.module != violation.module:
            continue
        if exc.function not in {"*", violation.function}:
            continue
        if exc.target not in {"*", violation.target}:
            continue
        if exc.kind not in {"*", violation.kind}:
            continue
        return True
    return False


def active_worktree_boundary_violations(
    source: str,
    *,
    module: str,
    exceptions: frozenset[ActiveWorktreeBoundaryException] = frozenset(),
) -> tuple[ActiveWorktreeBoundaryViolation, ...]:
    visitor = _ActiveWorktreeVisitor(module)
    visitor.visit(ast.parse(source))
    return tuple(v for v in visitor.violations if not _match_exception(v, exceptions))


def _module_name(package_root: Path, path: Path) -> str:
    parts = list(path.relative_to(package_root.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _production_observations(
    repo_root: Path,
) -> tuple[ActiveWorktreeBoundaryViolation, ...]:
    package_root = repo_root / "orchestune"
    paths = sorted(package_root.rglob("*.py"))
    observations = [
        violation
        for path in paths
        for violation in active_worktree_boundary_violations(
            path.read_text(encoding="utf-8"),
            module=_module_name(package_root, path),
        )
    ]
    return tuple(
        sorted(observations, key=lambda item: (item.module, item.line, item.kind))
    )


def production_active_worktree_violations(
    repo_root: Path,
) -> tuple[ActiveWorktreeBoundaryViolation, ...]:
    return tuple(
        v
        for v in _production_observations(repo_root)
        if not _match_exception(v, ACTIVE_WORKTREE_PRODUCTION_EXCEPTIONS)
    )


def unused_active_worktree_exceptions(
    repo_root: Path,
) -> tuple[ActiveWorktreeBoundaryException, ...]:
    observed = _production_observations(repo_root)
    unused = []
    for exc in ACTIVE_WORKTREE_PRODUCTION_EXCEPTIONS:
        matched = any(_match_exception(v, frozenset({exc})) for v in observed)
        if not matched:
            unused.append(exc)
    return tuple(
        sorted(unused, key=lambda item: (item.module, item.function, item.target))
    )

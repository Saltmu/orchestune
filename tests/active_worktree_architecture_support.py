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
        "orchestune.ledger.active_records",
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

SUBRECORD_OWNER_MODULES = {
    "launch": LAUNCH_OWNER_MODULES,
    "claim": CLAIM_OWNER_MODULES,
    "completion": COMPLETE_OWNER_MODULES,
    "core": CORE_OWNER_MODULES,
}

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


ACTIVE_WORKTREE_PRODUCTION_EXCEPTIONS: frozenset[ActiveWorktreeBoundaryException] = (
    frozenset(
        _exception(
            "orchestune.ledger.active_lifecycle",
            function,
            "completion_id",
            "completion-stage-is-none",
            "Central candidate lifecycle and journal reservation predicates.",
        )
        for function in ("lifecycle", "has_completion_reservation")
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
        self.active_type_aliases: set[str] = {"ActiveWorktree"}
        self.completion_type_aliases: set[str] = {"ActiveCompletionJournal"}
        self.active_vars_stack: list[set[str]] = [set()]
        self.completion_vars_stack: list[set[str]] = [set()]
        self.payload_vars_stack: list[set[str]] = [set()]
        self.policy_config_vars_stack: list[set[str]] = [set()]
        self.other_vars_stack: list[set[str]] = [set()]

    def _record(self, target: str, kind: str, line: int) -> None:
        self.violations.append(
            ActiveWorktreeBoundaryViolation(
                self.module, self.functions[-1], target, kind, line
            )
        )

    def _is_active_var(self, name: str) -> bool:
        for active_scope, other_scope in zip(
            reversed(self.active_vars_stack),
            reversed(self.other_vars_stack),
            strict=True,
        ):
            if name in active_scope:
                return True
            if name in other_scope:
                return False
        return name in {"active", "active_worktree"}

    def _is_completion_var(self, name: str) -> bool:
        for comp_scope, other_scope in zip(
            reversed(self.completion_vars_stack),
            reversed(self.other_vars_stack),
            strict=True,
        ):
            if name in comp_scope:
                return True
            if name in other_scope:
                return False
        return False

    def _is_payload_var(self, name: str) -> bool:
        for payload_scope, other_scope in zip(
            reversed(self.payload_vars_stack),
            reversed(self.other_vars_stack),
            strict=True,
        ):
            if name in payload_scope:
                return True
            if name in other_scope:
                return False
        return False

    def _is_policy_config_var(self, name: str) -> bool:
        for config_scope, other_scope in zip(
            reversed(self.policy_config_vars_stack),
            reversed(self.other_vars_stack),
            strict=True,
        ):
            if name in config_scope:
                return True
            if name in other_scope:
                return False
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
                self.active_type_aliases.add(f"{mod_name}.ActiveWorktree")
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
                self.active_type_aliases.add(ctor_name)
            elif alias.name == "ActiveCompletionJournal":
                self.completion_type_aliases.add(
                    alias.asname or "ActiveCompletionJournal"
                )
            elif alias.name == "active_records":
                mod_name = alias.asname or "active_records"
                self.constructor_names.add(f"{mod_name}.ActiveWorktree")
                self.constructor_names.add(f"{mod_name}.ActiveWorktree.from_records")
                self.active_type_aliases.add(f"{mod_name}.ActiveWorktree")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter_function(node)

    def _enter_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.functions.append(node.name)
        active_scope: set[str] = set()
        completion_scope: set[str] = set()
        payload_scope: set[str] = set()
        policy_config_scope: set[str] = set()
        other_scope: set[str] = set()
        for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
            if arg.annotation is not None:
                types = _extract_type_names(arg.annotation)
                if any(t in self.active_type_aliases for t in types):
                    active_scope.add(arg.arg)
                elif any(t in self.completion_type_aliases for t in types):
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
        self.payload_vars_stack.append(payload_scope)
        self.policy_config_vars_stack.append(policy_config_scope)
        self.other_vars_stack.append(other_scope)
        self.generic_visit(node)
        self.active_vars_stack.pop()
        self.completion_vars_stack.pop()
        self.payload_vars_stack.pop()
        self.policy_config_vars_stack.pop()
        self.other_vars_stack.pop()
        self.functions.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        self._check_assignment(node.targets, node.lineno)
        self._track_assigned_var(node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._check_assignment([node.target], node.lineno)
        if isinstance(node.target, ast.Name):
            self._clear_var_classifications(node.target.id)
            types = _extract_type_names(node.annotation)
            if any(t in self.active_type_aliases for t in types):
                self.active_vars_stack[-1].add(node.target.id)
            elif any(t in self.completion_type_aliases for t in types):
                self.completion_vars_stack[-1].add(node.target.id)
            elif types - {"None", "NoneType"}:
                self.other_vars_stack[-1].add(node.target.id)
        if node.value is not None:
            self._track_assigned_var([node.target], node.value)
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

    def _clear_var_classifications(self, name: str) -> None:
        self.active_vars_stack[-1].discard(name)
        self.completion_vars_stack[-1].discard(name)
        self.payload_vars_stack[-1].discard(name)
        self.policy_config_vars_stack[-1].discard(name)
        self.other_vars_stack[-1].discard(name)

    def _track_name_assignment(self, target_id: str, value: ast.Name) -> None:
        if self._is_active_var(value.id):
            self.active_vars_stack[-1].add(target_id)
        elif self._is_completion_var(value.id):
            self.completion_vars_stack[-1].add(target_id)
        elif self._is_payload_var(value.id):
            self.payload_vars_stack[-1].add(target_id)
        elif self._is_policy_config_var(value.id):
            self.policy_config_vars_stack[-1].add(target_id)
        else:
            for other_scope in reversed(self.other_vars_stack):
                if value.id in other_scope:
                    self.other_vars_stack[-1].add(target_id)
                    break

    def _track_call_assignment(self, target_id: str, value: ast.Call) -> None:
        func_name = _extract_func_name(value.func)
        if self._is_constructor_call(func_name):
            self.active_vars_stack[-1].add(target_id)
            return

        if isinstance(value.func, ast.Attribute) and value.func.attr in {
            "with_claim",
            "with_completion",
            "with_launch",
            "with_core",
        }:
            if isinstance(value.func.value, ast.Name) and self._is_active_var(
                value.func.value.id
            ):
                self.active_vars_stack[-1].add(target_id)
                return

        if func_name in {"with_claim", "with_completion", "with_launch"} or (
            func_name is not None
            and (
                func_name.endswith(".with_claim")
                or func_name.endswith(".with_completion")
                or func_name.endswith(".with_launch")
            )
        ):
            if not value.args or (
                isinstance(value.args[0], ast.Name)
                and self._is_active_var(value.args[0].id)
            ):
                self.active_vars_stack[-1].add(target_id)
                return

        if func_name is not None and func_name.split(".")[-1][0].isupper():
            self.other_vars_stack[-1].add(target_id)

    def _track_assigned_var(self, targets: list[ast.expr], value: ast.expr) -> None:
        for target in targets:
            if isinstance(target, ast.Name):
                self._clear_var_classifications(target.id)
                if isinstance(value, ast.Name):
                    self._track_name_assignment(target.id, value)
                elif isinstance(value, ast.Call):
                    self._track_call_assignment(target.id, value)
                elif isinstance(value, ast.Attribute) and value.attr == "completion":
                    if isinstance(value.value, ast.Name) and self._is_active_var(
                        value.value.id
                    ):
                        self.completion_vars_stack[-1].add(target.id)
                elif self._is_payload_expr(value):
                    self.payload_vars_stack[-1].add(target.id)
                elif self._is_policy_config_expr(value):
                    self.policy_config_vars_stack[-1].add(target.id)

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
                        if (
                            target.attr in SUBRECORDS
                            and self.module not in SUBRECORD_OWNER_MODULES[target.attr]
                        ):
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
        if any(
            keyword.arg == "_legacy" for keyword in node.keywords
        ) and self.module not in {
            "orchestune.ledger.active_records",
            "orchestune.ledger.active_codec",
        }:
            self._record("_legacy", "unauthorized-legacy-construction", node.lineno)
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
        if isinstance(expr, ast.Name):
            return self._is_payload_var(expr.id)
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
        if isinstance(expr, ast.Name):
            return self._is_policy_config_var(expr.id)
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

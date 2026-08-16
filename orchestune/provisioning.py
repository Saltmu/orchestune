"""Codifies `decomposition_plan.md` -> GitHub Issue provisioning.

Turns the prose procedure in `skills/orchestune-provision/SKILL.md` into a
deterministic, idempotent, resumable transformation: an approved plan's
`SubTask` fields carry everything `.github/issue_template.md` needs, so
filing is pure code rather than an agent re-interpreting instructions each
run.
"""

from __future__ import annotations

import argparse
import dataclasses
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from orchestune.dag_graph import build_dag
from orchestune.dag_models import (
    ConfigError,
    SubTask,
    compile_extra_ignore_patterns,
    extract_dag_ignore_patterns,
    extract_dag_similarity_threshold,
    load_orchestune_config,
    resolve_repo_root,
)
from orchestune.dag_parsing import (
    extract_frontmatter_and_body,
    parse_decomposition_plan,
)
from orchestune.dag_similarity import DEFAULT_SIMILARITY_THRESHOLD
from orchestune.forge import GitHubForge, IssueForge
from orchestune.issue_parsing import FOOTPRINT_BLOCK_PATTERN, PARENT_MARKER
from orchestune.plan_writer import write_issue_numbers
from orchestune.symbol_verification import find_missing_symbols
from orchestune.validation import validate_issue_number

_PLACEHOLDERS = (
    "subtask_id",
    "subtask_id_yaml",
    "description",
    "overview",
    "proposed_changes",
    "acceptance_criteria",
    "verification_plan",
    "footprint",
    "symbols",
    "depends_on",
)
_PLACEHOLDER_PATTERN = re.compile(
    "{{(" + "|".join(re.escape(name) for name in _PLACEHOLDERS) + ")}}"
)


@dataclass(frozen=True)
class PlanMetadata:
    title: str
    parent_issue_number: int | None
    description: str = ""


@dataclass(frozen=True)
class IssuePreview:
    subtask_id: str
    title: str
    body: str
    labels: tuple[str, ...]
    already_has_issue: bool


@dataclass(frozen=True)
class ProvisionResult:
    parent_issue_number: int | None
    applied: bool
    created: dict[str, int]
    reused: dict[str, int]
    previews: tuple[IssuePreview, ...] = ()


def _load_plan(path: str | Path) -> tuple[list[SubTask], PlanMetadata]:
    subtasks = parse_decomposition_plan(path)
    text = Path(path).read_text(encoding="utf-8")
    raw, description = extract_frontmatter_and_body(text)

    issue_numbers: dict[str, int] = {}
    for entry in raw.get("subtasks") or []:
        if isinstance(entry, dict) and entry.get("issue_number") not in (None, ""):
            # `dag_parsing._parse_subtask_id` strips the id before it becomes
            # `SubTask.id`, which is what `issue_numbers.get(subtask.id)`
            # below looks up by; mirror that here or a raw id with padding
            # whitespace would never match its own subtask.
            issue_numbers[str(entry["id"]).strip()] = validate_issue_number(
                entry["issue_number"]
            )

    enriched = [
        dataclasses.replace(subtask, issue_number=issue_numbers.get(subtask.id))
        for subtask in subtasks
    ]
    raw_parent = raw.get("parent_issue_number")
    parent_issue_number = (
        None
        if raw_parent is None or raw_parent == ""
        else validate_issue_number(raw_parent)
    )

    metadata = PlanMetadata(
        title=str(raw.get("title") or "").strip(),
        parent_issue_number=parent_issue_number,
        description=description,
    )
    return enriched, metadata


def _derive_labels(subtask: SubTask, *, dependencies_done: bool) -> tuple[str, ...]:
    """Pure label derivation: status/priority/risk from the subtask's own fields."""
    labels = [
        "status:blocked"
        if subtask.depends_on and not dependencies_done
        else "status:queued"
    ]
    labels.append(f"priority:{subtask.priority}")
    if subtask.risk:
        labels.append("risk:flagged")
    return tuple(labels)


def _issue_title(subtask: SubTask) -> str:
    return f"[FEAT] {subtask.id}: {subtask.description}"


def _parent_body(title: str, description: str = "") -> str:
    body = f"{title}"
    if description:
        body += f"\n\n{description}"
    body += (
        f"\n\n配下のサブタスクはこのIssueのSub-issueとして紐付けられます。"
        f"\n\n{PARENT_MARKER}"
    )
    return body


def _yaml_inline_list(items: Sequence[str]) -> str:
    return yaml.dump(list(items), default_flow_style=True, allow_unicode=True).strip()


def _yaml_scalar(value: str) -> str:
    """Render `value` as a safe YAML scalar (quoting it if it contains `:`, `#`, etc.).

    `yaml.dump(value)` on a bare scalar emits a trailing `...` document-end
    marker, which breaks the surrounding Footprint block (the fields after
    it become an unexpected second YAML document). Dumping a throwaway
    single-key mapping instead avoids the marker, since a mapping root isn't
    ambiguous the way a bare scalar root is; the fixed `k: ` prefix is then
    stripped back off.
    """
    dumped = yaml.dump({"k": value}, allow_unicode=True, default_flow_style=False)
    return dumped.removeprefix("k: ").rstrip("\n")


def _bullet_list(items: Sequence[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "特になし"


def _render_issue_body(subtask: SubTask, template: str) -> str:
    values = {
        "subtask_id": subtask.id,
        "subtask_id_yaml": _yaml_scalar(subtask.id),
        "description": subtask.description,
        "overview": subtask.overview or "特になし",
        "proposed_changes": _bullet_list(subtask.proposed_changes),
        "acceptance_criteria": _bullet_list(subtask.acceptance_criteria),
        "verification_plan": _bullet_list(subtask.verification_plan),
        "footprint": _yaml_inline_list(subtask.footprint),
        "symbols": _yaml_inline_list(subtask.symbols),
        "depends_on": _yaml_inline_list(subtask.depends_on),
    }
    # A single-pass substitution (not one `.replace()` call per placeholder):
    # a field's own value could otherwise contain a literal `{{token}}` (a
    # `description` that mentions the template, say) and get corrupted by a
    # later replacement reprocessing text that isn't part of the template.
    return _PLACEHOLDER_PATTERN.sub(lambda match: values[match.group(1)], template)


def _append_symbol_warning(body: str, subtask: SubTask, repo_root: Path) -> str:
    """#359: `subtask.symbols`のうち現在のコードベースに見つからないものが
    あれば、Issue本文末尾に注記を追加する。

    未検出は「陳腐化している（リファクタで改名・移動された）」と「まだ
    存在しない（このsubtaskで新規追加する）」のどちらもありうるため
    （`find_missing_symbols`のdocstring参照）、注記は断定せず中立な
    確認依頼として書く。
    """
    missing = find_missing_symbols(subtask, repo_root)
    if not missing:
        return body

    bullet_list = "\n".join(f"- `{symbol}`" for symbol in missing)
    warning = (
        "\n\n---\n\n"
        "⚠️ **symbols未検出**: 以下のシンボルは、Footprintに列挙されたファイル内に"
        "見つかりませんでした。このsubtaskで新規追加する予定であれば問題ありません"
        "が、リファクタによる改名・移動で古い名称が残っている可能性もあるため、"
        f"着手前にコードを確認してください。\n{bullet_list}"
    )
    return body + warning


def _subtask_id_from_body(body: str) -> str | None:
    """Extract `subtask_id` from a Footprint YAML fence the same way
    `issue_parsing.parse_task_from_issue` does, so IDs containing `:`/`#`
    (rendered as quoted YAML scalars) are matched correctly."""
    match = FOOTPRINT_BLOCK_PATTERN.search(body or "")
    if not match:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    subtask_id = data.get("subtask_id")
    return str(subtask_id) if subtask_id else None


def _validate_template_identity_marker(
    template: str, template_path: str | Path
) -> None:
    """Render a throwaway probe subtask through `template` and confirm
    `_subtask_id_from_body` can extract its id back out.

    Checking for the raw `{{subtask_id_yaml}}` token's presence isn't
    enough: it could sit outside any Footprint YAML fence (a heading, or a
    second unrelated fence) and still "be present" in the template text
    while never producing an extractable `subtask_id:` key, silently
    breaking idempotency in exactly the same way an entirely missing
    placeholder would.

    The probe id is deliberately one that forces `_yaml_scalar` to quote it
    (it contains `:` and `#`), not a plain word: a plain-scalar probe would
    round-trip fine even through a *buggy* custom template that wraps
    `{{subtask_id_yaml}}` in its own literal quotes (already-quoted output
    quoted a second time), silently corrupting any real id that actually
    needs quoting while this validation reports success.
    """
    probe_id = "orchestune-template-probe: needs-quoting #1"
    probe = SubTask(
        id=probe_id,
        description="",
        footprint=(),
        symbols=(),
        depends_on=(),
        risk=False,
        risk_reasons=(),
    )
    rendered = _render_issue_body(probe, template)
    if _subtask_id_from_body(rendered) != probe_id:
        raise ValueError(
            f"{template_path} から subtask_id を再照合できません"
            "（'{{subtask_id_yaml}}' がFootprint YAMLフェンス内の"
            "'subtask_id:' として描画されていません）。冪等性が壊れます"
        )


def _index_sub_issues_by_subtask_id(
    forge: IssueForge, parent_issue_number: int
) -> dict[str, int]:
    index: dict[str, int] = {}
    for record in forge.list_sub_issues(parent_issue_number):
        subtask_id = _subtask_id_from_body(record.body)
        if subtask_id:
            index.setdefault(subtask_id, record.number)
    return index


def _preview_only(
    subtasks: list[SubTask], dag_order: list[str], template: str, repo_root: Path
) -> ProvisionResult:
    by_id = {subtask.id: subtask for subtask in subtasks}
    previews = tuple(
        IssuePreview(
            subtask_id=subtask_id,
            title=_issue_title(by_id[subtask_id]),
            body=_append_symbol_warning(
                _render_issue_body(by_id[subtask_id], template),
                by_id[subtask_id],
                repo_root,
            ),
            labels=_derive_labels(by_id[subtask_id], dependencies_done=False),
            already_has_issue=by_id[subtask_id].issue_number is not None,
        )
        for subtask_id in dag_order
    )
    return ProvisionResult(
        parent_issue_number=None,
        applied=False,
        created={},
        reused={},
        previews=previews,
    )


def provision_issues(
    plan_path: str | Path,
    forge: IssueForge | None = None,
    apply: bool = True,
    template_path: str | Path = ".github/issue_template.md",
    repo_root: str | Path | None = None,
) -> ProvisionResult:
    """Provision GitHub Issues for every subtask in an approved decomposition plan.

    Idempotent and resumable: each subtask's `issue_number` (or a matching
    `subtask_id` found in an existing sub-issue's body) short-circuits
    re-creation, and every resolved number is written back to `plan_path`
    immediately, before moving on to the next subtask.

    `repo_root` (default: cwd) is where each subtask's `footprint` paths are
    resolved from when checking whether its `symbols` still exist in the
    codebase (#359); a mismatch appends a warning to the rendered Issue body
    rather than blocking provisioning, since the check can't tell a stale
    `symbols` list apart from a footprint file that legitimately doesn't
    exist yet. It's also where `orchestune.toml`/`pyproject.toml`'s
    `dag_ignore_patterns` is read from (#398/#404): without it, a
    similarity edge the setting was meant to suppress could still form
    here, changing the Issue-creation order (`dag.topological_order`) from
    what `orchestune-dag --plan ...` validated, or — if that edge only
    closes a cycle together with an explicit dependency — raise an
    unresolvable `DagCycleError` that validation didn't predict.
    """
    resolved_repo_root = Path(repo_root) if repo_root is not None else Path.cwd()
    subtasks, metadata = _load_plan(plan_path)
    if not metadata.title:
        raise ValueError(
            "decomposition_plan.md に必須の 'title' フィールドがありません"
        )

    orchestune_config = load_orchestune_config(resolved_repo_root)
    ignore_patterns = compile_extra_ignore_patterns(
        extract_dag_ignore_patterns(orchestune_config)
    )
    # #407: `orchestune-dag --threshold`で永続化された`dag_similarity_threshold`を
    # 尊重する。そうしないと、`orchestune-dag`が意図的に消したエッジが既定閾値で
    # 再計算されて復活し、Issue作成順（topological_order）が検証済みのプランと
    # 食い違う、あるいは明示的な依存関係と組み合わさって偽のDagCycleErrorを
    # 誘発しうる。
    config_threshold = extract_dag_similarity_threshold(orchestune_config)
    threshold = (
        config_threshold
        if config_threshold is not None
        else DEFAULT_SIMILARITY_THRESHOLD
    )
    dag = build_dag(subtasks, ignore_patterns=ignore_patterns, threshold=threshold)
    template = Path(template_path).read_text(encoding="utf-8")
    _validate_template_identity_marker(template, template_path)

    if not apply:
        return _preview_only(
            subtasks, dag.topological_order, template, resolved_repo_root
        )

    resolved_forge = forge or GitHubForge()

    parent_issue_number = metadata.parent_issue_number
    parent_title = f"[EPIC] {metadata.title}"
    if parent_issue_number is not None:
        # A persisted parent number is verified the same way a persisted
        # subtask number is below: it could be stale (e.g. the plan was
        # copied to another repo and that number now belongs to an
        # unrelated issue there), so it's trusted only after confirming it.
        # `PARENT_MARKER` alone isn't enough proof: it's a single constant
        # shared by every EPIC this module ever creates, so it can't tell
        # this plan's own parent apart from an unrelated EPIC created for a
        # *different* plan (e.g. a colliding issue number in another
        # Orchestune-managed repo) — the title must match too, the same
        # requirement the orphan-recovery search below already applies.
        candidate = resolved_forge.get_issue(parent_issue_number)
        if (
            candidate is None
            or candidate.title != parent_title
            or PARENT_MARKER not in candidate.body
        ):
            parent_issue_number = None
    if parent_issue_number is None:
        # Recover an orphan from a prior run that created the parent issue
        # but crashed (or failed to write) before persisting its number,
        # rather than unconditionally creating a duplicate EPIC. An exact
        # title match alone isn't enough proof of provenance (an unrelated
        # issue could coincidentally share the title), so also require our
        # own marker in the body before adopting it — and check every
        # exact-title match, not just the first, in case an unrelated
        # same-titled issue and our own orphaned parent both exist.
        candidates = resolved_forge.find_open_issues_by_exact_title(parent_title)
        marked_candidate = next(
            (c for c in candidates if PARENT_MARKER in c.body), None
        )
        if marked_candidate is not None:
            parent_issue_number = marked_candidate.number
        else:
            parent_issue_number = resolved_forge.create_issue(
                parent_title, _parent_body(metadata.title, metadata.description)
            )
        write_issue_numbers(plan_path, parent_issue_number=parent_issue_number)

    existing_by_subtask_id = _index_sub_issues_by_subtask_id(
        resolved_forge, parent_issue_number
    )

    resolved_numbers: dict[str, int] = {}
    dependencies_done: dict[str, bool] = {}
    created: dict[str, int] = {}
    reused: dict[str, int] = {}

    for subtask_id in dag.topological_order:
        subtask = dag.subtasks[subtask_id]
        # A persisted issue_number could be stale (e.g. the plan was copied
        # to another repo and that number now belongs to an unrelated
        # issue), so it isn't trusted outright: fetch it and check its body
        # actually carries this subtask's marker before reusing it — the
        # same test used for the subtask_id-search fallback below. This
        # check works even if the issue hasn't been linked to the parent
        # yet (a crash between create_issue and add_sub_issue), since the
        # marker is written at creation time regardless of linkage.
        number = None
        if subtask.issue_number is not None:
            candidate = resolved_forge.get_issue(subtask.issue_number)
            if (
                candidate is not None
                and _subtask_id_from_body(candidate.body) == subtask_id
            ):
                number = subtask.issue_number
        if number is None:
            number = existing_by_subtask_id.get(subtask_id)

        if number is not None:
            reused[subtask_id] = number
            labels = resolved_forge.get_issue_labels(number)
            dependencies_done[subtask_id] = "status:done" in labels
        else:
            all_deps_done = all(
                dependencies_done.get(dep, False) for dep in subtask.depends_on
            )
            labels = _derive_labels(subtask, dependencies_done=all_deps_done)
            body = _append_symbol_warning(
                _render_issue_body(subtask, template), subtask, resolved_repo_root
            )
            number = resolved_forge.create_issue(
                _issue_title(subtask), body, labels=labels
            )
            # Persist before the fallible relationship calls below: if
            # add_sub_issue/set_blocked_by then fails, a retry must find this
            # issue via `subtask.issue_number` rather than orphan-create a
            # duplicate (it isn't linked as a sub-issue yet, so the
            # subtask_id search over the parent's children can't find it).
            write_issue_numbers(plan_path, {subtask_id: number})
            created[subtask_id] = number
            dependencies_done[subtask_id] = False

        # Reconcile parent/blocked-by relationships unconditionally, not just
        # on creation: a prior run may have created this issue (or an earlier
        # dependency's set_blocked_by call) and then failed before finishing
        # all of them, in which case a reused issue can still be missing some.
        # Both operations are idempotent (`--set-parent` / `--add-blocked-by`).
        resolved_forge.add_sub_issue(parent_issue_number, number)
        for dependency_id in subtask.depends_on:
            resolved_forge.set_blocked_by(number, resolved_numbers[dependency_id])

        resolved_numbers[subtask_id] = number
        write_issue_numbers(plan_path, {subtask_id: number})

    return ProvisionResult(
        parent_issue_number=parent_issue_number,
        applied=True,
        created=created,
        reused=reused,
    )


def _print_result(result: ProvisionResult) -> None:
    if not result.applied:
        print("Dry run (--no-apply): no Issues were created.")
        for preview in result.previews:
            status = "reuse expected" if preview.already_has_issue else "would create"
            print(f"\n=== {preview.subtask_id} ({status}) ===")
            print(f"Title: {preview.title}")
            print(f"Labels: {', '.join(preview.labels)}")
            print(preview.body)
        return

    print(f"Parent issue: #{result.parent_issue_number}")
    print(f"Created: {len(result.created)}")
    for subtask_id, number in result.created.items():
        print(f"  + {subtask_id} -> #{number}")
    print(f"Reused: {len(result.reused)}")
    for subtask_id, number in result.reused.items():
        print(f"  = {subtask_id} -> #{number}")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="orchestune provision: decomposition_plan.mdからIssueを起票する"
    )
    parser.add_argument(
        "--plan",
        default="decomposition_plan.md",
        help="Path to the decomposition plan markdown file (default: decomposition_plan.md)",
    )
    parser.add_argument(
        "--template",
        default=".github/issue_template.md",
        help="Path to the issue body template (default: .github/issue_template.md)",
    )
    parser.add_argument("--apply", dest="apply", action="store_true", default=True)
    parser.add_argument("--no-apply", dest="apply", action="store_false")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _build_arg_parser().parse_args(argv)

    try:
        # footprintおよびorchestune.toml/[tool.orchestune]はリポジトリルート
        # からの相対パスとして定義されているため、呼び出し元のcwdではなく
        # --planファイル自身の位置を基点にする（dag_cli.pyと同じ規約。#404）。
        # --planがリポジトリルートより下のネストしたパスを指す場合でも、
        # dag_cli.pyと同じく.gitを上位探索して真のリポジトリルートを特定する
        # （#410, #418）。これにより、`orchestune-dag --plan ...`が検証した
        # 設定（dag_ignore_patterns等）と同じファイルを`orchestune provision`
        # も一貫して読む。
        repo_root = resolve_repo_root(args.plan)
        result = provision_issues(
            args.plan,
            apply=args.apply,
            template_path=args.template,
            repo_root=repo_root,
        )
    except ConfigError as error:
        # #428: config-derived errors (orchestune.toml / pyproject.toml
        # values) exit 2, matching orchestune-dispatch's `_config_error`
        # convention and orchestune-dag's own exit-2 mapping, distinct from
        # exit 1 for other failures (missing plan file, DagCycleError, etc.).
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    _print_result(result)
    raise SystemExit(0)

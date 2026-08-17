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
    DagResult,
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
from orchestune.forge import GitHubForge, IssueForge, RelationshipUnavailableError
from orchestune.issue_parsing import (
    FOOTPRINT_BLOCK_PATTERN,
    PARENT_MARKER,
    backfill_parent_issue_number,
    effective_parent_number,
    find_children_by_parent,
    parent_issue_number_from_body,
)
from orchestune.models import IssueRecord
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
    "parent_issue_number",
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
    # #485: ネイティブSub-issue/blocked_by関係のリンクに失敗し、本文metadata
    # フォールバックのみで縮退したsubtask_idの一覧（起動時/完了時に報告する）。
    degraded_subtask_ids: tuple[str, ...] = ()


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


def _render_issue_body(
    subtask: SubTask, template: str, parent_issue_number: int | None = None
) -> str:
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
        # #485: ネイティブSub-issue関係が使えない環境でも`--parent-issue`
        # モードが子Issueを発見できるよう、親番号を本文metadataにも
        # 永続化する（`add_sub_issue`が成功したかどうかに関わらず常に書く）。
        "parent_issue_number": (
            "null" if parent_issue_number is None else str(parent_issue_number)
        ),
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

    # #485 review (P1): a custom `--template` missing `{{parent_issue_number}}`
    # would silently render bodies without it. Provisioning would then report
    # success (possibly even "full", not degraded) while the parent-metadata
    # fallback — the only way to discover the issue when native sub-issue
    # linking is unavailable — has nothing to find. Probe with a concrete
    # number (not None/null) so a template that drops the placeholder text
    # entirely, or one that only ever emits the `parent_issue_number: null`
    # literal, both fail this check instead of only the latter.
    probe_parent_number = 999999
    probe_rendered = _render_issue_body(probe, template, probe_parent_number)
    if parent_issue_number_from_body(probe_rendered) != probe_parent_number:
        raise ValueError(
            f"{template_path} から parent_issue_number を再照合できません"
            "（'{{parent_issue_number}}' がFootprint YAMLフェンス内の"
            "'parent_issue_number:' として描画されていません）。ネイティブ"
            "Sub-issue関係が使えない環境でDispatcherが子Issueを発見できなく"
            "なります"
        )


def _index_sub_issues_by_subtask_id(
    forge: IssueForge, parent_issue_number: int
) -> dict[str, IssueRecord]:
    """Returns the full `IssueRecord` (not just the number) so a reuse can
    inspect/backfill its body without an extra `get_issue` round trip."""
    index: dict[str, IssueRecord] = {}
    # #485: ネイティブSub-issue関係を作れなかった過去実行のIssueも本文
    # metadataのparent_issue_numberから見つけて再利用対象に含める。
    for record in find_children_by_parent(forge, parent_issue_number):
        subtask_id = _subtask_id_from_body(record.body)
        if subtask_id:
            index.setdefault(subtask_id, record)
    return index


def _build_provisioning_dag(subtasks: list[SubTask], repo_root: Path) -> DagResult:
    orchestune_config = load_orchestune_config(repo_root)
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
    return build_dag(subtasks, ignore_patterns=ignore_patterns, threshold=threshold)


def _build_subtask_issue_body(
    subtask: SubTask,
    template: str,
    repo_root: Path,
    parent_issue_number: int | None = None,
) -> str:
    return _append_symbol_warning(
        _render_issue_body(subtask, template, parent_issue_number), subtask, repo_root
    )


def _resolve_parent_issue(
    forge: IssueForge, metadata: PlanMetadata, plan_path: str | Path
) -> int:
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
        candidate = forge.get_issue(parent_issue_number)
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
        candidates = forge.find_open_issues_by_exact_title(parent_title)
        marked_candidate = next(
            (c for c in candidates if PARENT_MARKER in c.body), None
        )
        if marked_candidate is not None:
            parent_issue_number = marked_candidate.number
        else:
            parent_issue_number = forge.create_issue(
                parent_title, _parent_body(metadata.title, metadata.description)
            )
        write_issue_numbers(plan_path, parent_issue_number=parent_issue_number)
    return parent_issue_number


def _ensure_reused_issue_is_discoverable(
    forge: IssueForge, candidate: IssueRecord, parent_issue_number: int
) -> bool:
    """#485 review (P2 x2): a reused Issue can predate `parent_issue_number`
    (created by an older template, or by a run before this field existed).
    Left unpatched, that Issue has neither a native parent (if
    `add_sub_issue` is now unavailable) nor a metadata fallback the
    parent-scoped Dispatcher can find it by.

    Before attempting any write, check whether `candidate` is *already*
    discoverable via `effective_parent_number` — most importantly, an
    already-established native `parent` from a prior run. Skipping
    straight to a body backfill attempt (and failing it) would otherwise
    wrongly report "no discovery mechanism" for an issue that's already a
    real native child; the current forge merely being unable to *re-write*
    a relationship that already exists on GitHub doesn't mean it's gone
    (#485 review round 5, P2).

    Returns whether the issue is (now) discoverable at all: already
    correct, successfully backfilled, or `False` if it wasn't already
    correct and the backfill write failed — the caller
    (`provision_issues`) must then confirm native `add_sub_issue` linking
    actually succeeds this run, or this subtask has no discovery mechanism
    left (#485 review round 4, P2).
    """
    if effective_parent_number(candidate) == parent_issue_number:
        return True

    new_body = backfill_parent_issue_number(candidate.body, parent_issue_number)
    if new_body is None:
        # `effective_parent_number` above already ruled out "already
        # correct" for this candidate, so a `None` here means the
        # Footprint fence itself couldn't be re-parsed — which can't
        # actually happen given `candidate` only reaches this call after
        # `_subtask_id_from_body` already parsed the same fence
        # successfully. Kept purely as a defensive fallback.
        return True
    try:
        forge.update_issue_body(candidate.number, new_body)
        return True
    except (RelationshipUnavailableError, AttributeError, NotImplementedError) as e:
        print(
            f"Warning: could not backfill parent_issue_number into "
            f"#{candidate.number}'s body: {e}",
            file=sys.stderr,
        )
        return False


def _provision_subtask(
    forge: IssueForge,
    subtask: SubTask,
    template: str,
    repo_root: Path,
    plan_path: str | Path,
    existing_by_subtask_id: dict[str, IssueRecord],
    dependencies_done: dict[str, bool],
    parent_issue_number: int,
) -> tuple[int, bool, bool, bool]:
    """Resolve an existing issue or create a new one for a subtask.

    Returns a tuple of `(issue_number, is_reused, is_done,
    has_parent_metadata)`. `has_parent_metadata` is `True` for a newly
    created issue (the body is always rendered with it) and for a reused
    issue whose body already carries or was successfully backfilled with
    it; `False` only for a reused issue where it's missing/stale *and* the
    backfill write failed — the caller must then verify native
    `add_sub_issue` linking succeeds, since neither discovery mechanism is
    otherwise available for this subtask.
    """
    # A persisted issue_number could be stale (e.g. the plan was copied
    # to another repo and that number now belongs to an unrelated
    # issue), so it isn't trusted outright: fetch it and check its body
    # actually carries this subtask's marker before reusing it — the
    # same test used for the subtask_id-search fallback below. This
    # check works even if the issue hasn't been linked to the parent
    # yet (a crash between create_issue and add_sub_issue), since the
    # marker is written at creation time regardless of linkage.
    number = None
    candidate: IssueRecord | None = None
    if subtask.issue_number is not None:
        fetched = forge.get_issue(subtask.issue_number)
        if fetched is not None and _subtask_id_from_body(fetched.body) == subtask.id:
            number = subtask.issue_number
            candidate = fetched
    if number is None:
        existing = existing_by_subtask_id.get(subtask.id)
        if existing is not None:
            number = existing.number
            candidate = existing

    if number is not None:
        labels = forge.get_issue_labels(number)
        has_parent_metadata = True
        if candidate is not None:
            has_parent_metadata = _ensure_reused_issue_is_discoverable(
                forge, candidate, parent_issue_number
            )
        return number, True, "status:done" in labels, has_parent_metadata

    all_deps_done = all(dependencies_done.get(dep, False) for dep in subtask.depends_on)
    labels = _derive_labels(subtask, dependencies_done=all_deps_done)
    body = _build_subtask_issue_body(subtask, template, repo_root, parent_issue_number)
    number = forge.create_issue(_issue_title(subtask), body, labels=labels)
    # Persist before the fallible relationship calls below: if
    # add_sub_issue/set_blocked_by then fails, a retry must find this
    # issue via `subtask.issue_number` rather than orphan-create a
    # duplicate (it isn't linked as a sub-issue yet, so the
    # subtask_id search over the parent's children can't find it).
    write_issue_numbers(plan_path, {subtask.id: number})
    return number, False, False, True


@dataclass(frozen=True)
class RelationshipLinkResult:
    parent_linked: bool
    unresolved_dependencies: tuple[str, ...]

    @property
    def degraded(self) -> bool:
        return not self.parent_linked or bool(self.unresolved_dependencies)


def _link_subtask_relationships(
    forge: IssueForge,
    parent_issue_number: int,
    issue_number: int,
    depends_on: Sequence[str],
    resolved_numbers: dict[str, int],
) -> RelationshipLinkResult:
    """Reconcile parent/blocked-by relationships unconditionally, not just
    on creation: a prior run may have created this issue (or an earlier
    dependency's set_blocked_by call) and then failed before finishing
    all of them, in which case a reused issue can still be missing some.
    Both operations are idempotent (`--set-parent` / `--add-blocked-by`).

    #485: `forge`がネイティブ関係操作を構造的にサポートしない場合
    （`RelationshipUnavailableError`、またはそもそもメソッドを実装して
    いない）は、provisioning全体を中断せず、既に本文metadataへ永続化済み
    (`_render_issue_body`)のparent_issue_number/depends_onへフォールバック
    したまま処理を続行する。それ以外の失敗（ネットワーク瞬断・権限不足・
    レート制限などの一時的なgh/API呼び出しエラー）は、今まで通り呼び出し
    元に伝播させ、再実行時の再試行に委ねる（このモジュールの冪等性は
    その前提の上に成り立っている。#323参照）。
    """
    unavailable_errors = (
        RelationshipUnavailableError,
        AttributeError,
        NotImplementedError,
    )

    parent_linked = True
    try:
        forge.add_sub_issue(parent_issue_number, issue_number)
    except unavailable_errors as e:
        parent_linked = False
        print(
            f"Warning: this forge does not support native sub-issue linking; "
            f"#{issue_number} relies on body metadata for parent #{parent_issue_number} "
            f"instead: {e}",
            file=sys.stderr,
        )

    unresolved: list[str] = []
    for dependency_id in depends_on:
        try:
            forge.set_blocked_by(issue_number, resolved_numbers[dependency_id])
        except unavailable_errors as e:
            unresolved.append(dependency_id)
            print(
                f"Warning: this forge does not support native blocked_by linking; "
                f"#{issue_number} relies on body depends_on for {dependency_id} "
                f"(#{resolved_numbers[dependency_id]}) instead: {e}",
                file=sys.stderr,
            )
    return RelationshipLinkResult(
        parent_linked=parent_linked, unresolved_dependencies=tuple(unresolved)
    )


def _preview_only(
    subtasks: list[SubTask],
    dag_order: list[str],
    template: str,
    repo_root: Path,
    parent_issue_number: int | None = None,
) -> ProvisionResult:
    by_id = {subtask.id: subtask for subtask in subtasks}
    previews = tuple(
        IssuePreview(
            subtask_id=subtask_id,
            title=_issue_title(by_id[subtask_id]),
            body=_build_subtask_issue_body(
                by_id[subtask_id], template, repo_root, parent_issue_number
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

    dag = _build_provisioning_dag(subtasks, resolved_repo_root)
    template = Path(template_path).read_text(encoding="utf-8")
    _validate_template_identity_marker(template, template_path)

    if not apply:
        return _preview_only(
            subtasks,
            dag.topological_order,
            template,
            resolved_repo_root,
            metadata.parent_issue_number,
        )

    resolved_forge = forge or GitHubForge()
    parent_issue_number = _resolve_parent_issue(resolved_forge, metadata, plan_path)
    existing_by_subtask_id = _index_sub_issues_by_subtask_id(
        resolved_forge, parent_issue_number
    )

    resolved_numbers: dict[str, int] = {}
    dependencies_done: dict[str, bool] = {}
    created: dict[str, int] = {}
    reused: dict[str, int] = {}
    degraded_subtask_ids: list[str] = []

    for subtask_id in dag.topological_order:
        subtask = dag.subtasks[subtask_id]
        number, is_reused, is_done, has_parent_metadata = _provision_subtask(
            resolved_forge,
            subtask,
            template,
            resolved_repo_root,
            plan_path,
            existing_by_subtask_id,
            dependencies_done,
            parent_issue_number,
        )
        if is_reused:
            reused[subtask_id] = number
        else:
            created[subtask_id] = number
        dependencies_done[subtask_id] = is_done

        link_result = _link_subtask_relationships(
            resolved_forge,
            parent_issue_number,
            number,
            subtask.depends_on,
            resolved_numbers,
        )
        if not has_parent_metadata and not link_result.parent_linked:
            # #485 review round 4 (P2): a reused issue whose body metadata
            # couldn't be backfilled AND whose native `add_sub_issue` also
            # failed has no discovery mechanism left at all — reporting
            # this as merely "degraded" would be misleading (that implies
            # the fallback works). Fail loudly instead of letting
            # `--parent-issue` Dispatcher and `process_parent_completion`
            # silently never see this subtask.
            raise RelationshipUnavailableError(
                f"#{number} ({subtask_id}) has neither a native parent link "
                f"nor discoverable parent_issue_number body metadata; "
                "this forge cannot reliably link it to its parent Issue"
            )
        if link_result.degraded:
            degraded_subtask_ids.append(subtask_id)
        resolved_numbers[subtask_id] = number
        write_issue_numbers(plan_path, {subtask_id: number})

    return ProvisionResult(
        parent_issue_number=parent_issue_number,
        applied=True,
        created=created,
        reused=reused,
        degraded_subtask_ids=tuple(degraded_subtask_ids),
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

    if result.degraded_subtask_ids:
        print(
            f"\nOperating mode: degraded/parent-metadata for "
            f"{len(result.degraded_subtask_ids)} subtask(s) "
            "(native sub-issue/blocked_by relationship writes failed; "
            "parent_issue_number/depends_on body metadata is authoritative "
            "for these instead):"
        )
        for subtask_id in result.degraded_subtask_ids:
            print(f"  ! {subtask_id}")
    else:
        print(
            "\nOperating mode: full (native sub-issue/blocked_by relationships linked)"
        )


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

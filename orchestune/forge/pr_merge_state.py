"""GitHub PR and branch merge status inspection mixed into :class:`GitHubForge`."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from urllib.parse import quote

from orchestune.validation import validate_ref_name

from .graphql_fallback import GhRunner
from .graphql_fallback import error_detail as _error_detail
from .issues_rest import is_graphql_unavailable

_BRANCH_PRESENCE_CHUNK = 50


def _partition_branch_names(
    names: Iterable[str],
) -> tuple[list[str], dict[str, bool | None]]:
    valid: list[str] = []
    invalid: dict[str, bool | None] = {}
    seen: set[str] = set()
    for name in names:
        if not isinstance(name, str) or name in seen:
            continue
        seen.add(name)
        try:
            validate_ref_name(name)
        except ValueError:
            invalid[name] = None
        else:
            valid.append(name)
    return valid, invalid


def _chunks(names: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(names), size):
        yield names[start : start + size]


def _branch_presence_query(count: int) -> str:
    variables = ", ".join(f"$q{index}: String!" for index in range(count))
    fields = "\n".join(
        f"    b{index}: ref(qualifiedName: $q{index}) {{ name prefix }}"
        for index in range(count)
    )
    return (
        f"query($owner: String!, $name: String!, {variables}) {{\n"
        "  repository(owner: $owner, name: $name) {\n"
        f"{fields}\n"
        "  }\n"
        "}"
    )


def _branch_presence_args(names: Sequence[str]) -> list[str]:
    args = [
        "gh",
        "api",
        "graphql",
        "-F",
        "owner={owner}",
        "-F",
        "name={repo}",
        "-f",
        f"query={_branch_presence_query(len(names))}",
    ]
    for index, name in enumerate(names):
        args.extend(["-f", f"q{index}=refs/heads/{name}"])
    return args


def _confirmed_ref(ref: object, name: str) -> bool:
    return (
        isinstance(ref, Mapping)
        and ref.get("name") == name
        and ref.get("prefix") == "refs/heads/"
    )


def _parse_branch_presence(
    payload: str, names: Sequence[str]
) -> dict[str, bool | None] | None:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or parsed.get("errors"):
        return None
    data = parsed.get("data")
    repository = data.get("repository") if isinstance(data, dict) else None
    if not isinstance(repository, dict):
        return None
    result: dict[str, bool | None] = {}
    for index, name in enumerate(names):
        alias = f"b{index}"
        if alias not in repository:
            return None
        ref = repository[alias]
        if ref is None:
            result[name] = False
        elif _confirmed_ref(ref, name):
            result[name] = True
        else:
            return None
    return result


def _rest_branch_presence(
    forge: GitHubPullRequestMergeStateMixin, names: Sequence[str]
) -> dict[str, bool | None]:
    found: dict[str, bool | None] = {}
    for name in names:
        try:
            found[name] = forge.branch_exists(name)
        except Exception:  # noqa: BLE001 - boundary: one REST miss stays indeterminate
            found[name] = None
    return found


class GitHubPullRequestMergeStateMixin:
    """PR and branch merge state inspection mixed into :class:`GitHubForge`."""

    _run: GhRunner

    def is_branch_merged_into(self, head: str, base: str) -> bool:
        validate_ref_name(head)
        validate_ref_name(base)
        stdout = self._run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "merged",
                "--head",
                head,
                "--base",
                base,
                "--json",
                "number",
                "--limit",
                "1",
            ]
        )
        return bool(json.loads(stdout))

    def get_merged_pr_timestamp(self, head: str, base: str) -> str | None:
        validate_ref_name(head)
        validate_ref_name(base)
        stdout = self._run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "merged",
                "--head",
                head,
                "--base",
                base,
                "--json",
                "mergedAt",
                "--limit",
                "1000",
            ]
        )
        timestamps = [
            result.get("mergedAt")
            for result in json.loads(stdout)
            if result.get("mergedAt")
        ]
        return str(max(timestamps)) if timestamps else None

    def branch_exists(self, branch: str) -> bool:
        validate_ref_name(branch)
        encoded_branch = quote(branch, safe="")
        try:
            self._run(
                ["gh", "api", f"repos/{{owner}}/{{repo}}/branches/{encoded_branch}"]
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").lower()
            if "404" in detail or "not found" in detail:
                return False
            raise
        return True

    def _query_branch_chunk(
        self, names: Sequence[str]
    ) -> tuple[str, dict[str, bool | None]]:
        try:
            stdout = self._run(_branch_presence_args(names))
        except subprocess.CalledProcessError as exc:
            if is_graphql_unavailable(_error_detail(exc)):
                return "down", {}
            return "unknown", {}
        except Exception:  # noqa: BLE001 - boundary: one chunk stays indeterminate
            return "unknown", {}
        parsed = _parse_branch_presence(stdout, names)
        if parsed is None:
            return "unknown", {}
        return "ok", parsed

    def _record_branch_chunk(
        self,
        result: dict[str, bool | None],
        names: Sequence[str],
        *,
        graphql_down: bool,
    ) -> bool:
        if graphql_down:
            result.update(_rest_branch_presence(self, names))
            return True
        status, parsed = self._query_branch_chunk(names)
        if status == "down":
            result.update(_rest_branch_presence(self, names))
            return True
        if status != "ok":
            result.update(dict.fromkeys(names, None))
            return False
        result.update(parsed)
        return False

    def branches_present(self, names: Iterable[str]) -> dict[str, bool | None]:
        """Return presence for each branch name. ``None`` means indeterminate."""
        valid, result = _partition_branch_names(names)
        graphql_down = False
        for chunk in _chunks(valid, _BRANCH_PRESENCE_CHUNK):
            graphql_down = self._record_branch_chunk(
                result, chunk, graphql_down=graphql_down
            )
        return result

    def is_current_branch_tip_merged_into(self, head: str, base: str) -> bool:
        return self.get_current_branch_tip_sha_if_merged_into(head, base) is not None

    def get_current_branch_tip_sha_if_merged_into(
        self, head: str, base: str
    ) -> str | None:
        """Return the observed tip only when that exact commit is in ``base``."""
        validate_ref_name(head)
        validate_ref_name(base)
        encoded_head = quote(head, safe="")
        tip_sha = self._run(
            [
                "gh",
                "api",
                f"repos/{{owner}}/{{repo}}/branches/{encoded_head}",
                "--jq",
                ".commit.sha",
            ]
        ).strip()
        if not re.fullmatch(r"[0-9a-fA-F]{40}", tip_sha):
            raise ValueError(f"GitHub returned an invalid branch tip SHA: {tip_sha!r}")
        status = self._run(
            [
                "gh",
                "api",
                f"repos/{{owner}}/{{repo}}/compare/{tip_sha}...{base}",
                "--jq",
                ".status",
            ]
        ).strip()
        return tip_sha if status in {"ahead", "identical"} else None

    def is_merge_commit_reachable_from(self, commit_oid: str, base: str) -> bool:
        """Whether a historical PR merge commit remains in the current base tip."""
        if not re.fullmatch(r"[0-9a-fA-F]{40}", commit_oid):
            raise ValueError(f"invalid merge commit SHA: {commit_oid!r}")
        validate_ref_name(base)
        status = self._run(
            [
                "gh",
                "api",
                f"repos/{{owner}}/{{repo}}/compare/{commit_oid}...{base}",
                "--jq",
                ".status",
            ]
        ).strip()
        return status in {"ahead", "identical"}

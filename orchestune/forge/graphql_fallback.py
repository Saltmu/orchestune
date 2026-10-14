"""Switch GraphQL-backed `gh` calls to their REST fallback once GraphQL is unusable.

Claude Code cloud sessions reject GitHub GraphQL with HTTP 403 (#1334). Issue and
pull request operations share one switch per :class:`GitHubForge` instance, so
the first rejection sends every later fallback-capable call straight to REST
(#1336).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import TypeVar

from .issues_rest import is_graphql_unavailable, is_unsupported_issue_field

_T = TypeVar("_T")


def error_detail(exc: subprocess.CalledProcessError) -> str:
    """The lower-cased output and error text of a failed `gh` call."""
    return " ".join(
        value.decode("utf-8", errors="replace")
        if isinstance(value, bytes)
        else str(value or "")
        for value in (exc.output, exc.stderr)
    ).lower()


class GraphQLFallbackMixin:
    """Holds the per-Forge GraphQL switch used by the issue and pull request mixins."""

    #: Set once a GraphQL-backed `gh` call proved GraphQL unusable here.
    _graphql_unavailable: bool = False

    def _graphql_or_rest(
        self,
        graphql: Callable[[], _T],
        rest: Callable[[], _T],
        requested_fields: str = "",
    ) -> _T:
        """Run the usual `gh` subcommand, or the REST fallback once GraphQL is unusable."""
        if not self._graphql_unavailable:
            try:
                return graphql()
            except subprocess.CalledProcessError as exc:
                detail = error_detail(exc)
                if is_graphql_unavailable(detail):
                    self._graphql_unavailable = True
                elif not is_unsupported_issue_field(detail, requested_fields):
                    raise
        return rest()

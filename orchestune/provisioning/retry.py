"""Bounded, provision-only retries with write reconciliation."""

from __future__ import annotations

import errno
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from email.utils import parsedate_to_datetime
from typing import Any, cast

from orchestune.forge import IssueForge
from orchestune.issue_parsing import (
    PARENT_MARKER,
    find_children_by_parent,
    parent_issue_number_from_body,
)
from orchestune.provisioning.rendering import _subtask_id_from_body

_TRANSIENT_STATUS = re.compile(
    r"(?:HTTP[/ ]|status(?: code)?[=: ]+)(429|502|503|504)\b", re.I
)
_RETRY_AFTER = re.compile(r"(?im)^retry-after:\s*(.+?)\s*$")
_RATE_RESET = re.compile(r"(?im)^x-ratelimit-reset:\s*(\d+)\s*$")
_CONNECTION_ERRORS = (
    "connection reset",
    "connection refused",
    "connection timed out",
    "temporary failure in name resolution",
    "unexpected eof",
    "stream error",
    "context deadline exceeded",
    "i/o timeout",
    "tls handshake timeout",
)


def _detail(error: BaseException) -> str:
    if isinstance(error, subprocess.CalledProcessError):
        parts = (error.stderr, error.output)
        return "\n".join(
            part.decode("utf-8", errors="replace")
            if isinstance(part, bytes)
            else str(part or "")
            for part in parts
        )
    return str(error)


def _is_transient(error: BaseException) -> bool:
    if isinstance(error, ConnectionError | TimeoutError):
        return True
    if isinstance(error, OSError) and error.errno in {
        errno.ECONNRESET,
        errno.ECONNREFUSED,
        errno.ETIMEDOUT,
        errno.EHOSTUNREACH,
    }:
        return True
    if not isinstance(error, subprocess.CalledProcessError):
        return False
    detail = _detail(error).lower()
    return (
        bool(_TRANSIENT_STATUS.search(detail))
        or any(marker in detail for marker in _CONNECTION_ERRORS)
        or _is_rate_limit_rejection(error)
    )


def _is_rate_limit_rejection(error: BaseException) -> bool:
    detail = _detail(error).lower()
    return "429" in detail or "rate limit exceeded" in detail


def _retry_after(error: BaseException, now: float) -> float | None:
    detail = _detail(error)
    match = _RETRY_AFTER.search(detail)
    if match:
        value = match.group(1)
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(value).timestamp() - now)
            except (TypeError, ValueError, OverflowError):
                pass
    reset = _RATE_RESET.search(detail)
    if reset:
        return max(0.0, float(reset.group(1)) - now)
    return None


class ProvisionRetryForge:
    """Wrap only provision operations; never retry an uncertain POST blindly."""

    def __init__(
        self,
        forge: IssueForge,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        min_interval: float = 0.2,
        max_attempts: int = 4,
        max_wait: float = 30.0,
    ) -> None:
        self._forge = forge
        self._sleep = sleep
        self._clock = clock
        self._min_interval = min_interval
        self._max_attempts = max_attempts
        self._max_wait = max_wait
        self._last_call: float | None = None

    def _paced(self, action: Callable[[], Any]) -> Any:
        if self._last_call is not None:
            delay = self._min_interval - (self._clock() - self._last_call)
            if delay > 0:
                self._sleep(delay)
        self._last_call = self._clock()
        return action()

    def _call(
        self,
        action: Callable[[], Any],
        probe: Callable[[], tuple[bool, Any]] | None = None,
        *,
        replay_on_miss: bool = True,
    ) -> Any:
        waited = 0.0
        uncertain = False
        uncertain_error: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                if uncertain and probe is not None:
                    found, value = self._paced(probe)
                    if found:
                        return value
                    if (
                        not replay_on_miss
                        and uncertain_error is not None
                        and not _is_rate_limit_rejection(uncertain_error)
                    ):
                        probe_delay = min(2.0**attempt, 8.0)
                        if (
                            attempt + 1 >= self._max_attempts
                            or waited + probe_delay > self._max_wait
                        ):
                            raise RuntimeError(
                                "Issue creation outcome is uncertain; inspect GitHub for the "
                                "subtask_id or parent marker, record any Issue number in the plan, "
                                "then rerun provision"
                            ) from uncertain_error
                        self._sleep(probe_delay)
                        waited += probe_delay
                        continue
                return self._paced(action)
            except Exception as error:
                if not _is_transient(error):
                    raise
                uncertain = True
                uncertain_error = error
                delay = _retry_after(error, self._clock())
                delay = min(2.0**attempt, 8.0) if delay is None else delay
                if attempt + 1 >= self._max_attempts or waited + delay > self._max_wait:
                    raise RuntimeError(
                        f"Provision retry limit reached after {attempt + 1} attempts "
                        f"and {waited:g}s waiting: {_detail(error)}"
                    ) from error
                self._sleep(delay)
                waited += delay
        raise AssertionError("unreachable")

    def _find_created(self, title: str, body: str) -> tuple[bool, int | None]:
        subtask_id = _subtask_id_from_body(body)
        parent_number = parent_issue_number_from_body(body)
        if subtask_id and parent_number is not None:
            result = find_children_by_parent(self._forge, parent_number)
            matching = [
                issue.number
                for issue in result.issues
                if _subtask_id_from_body(issue.body) == subtask_id
            ]
            if not matching and not result.metadata_search_supported:
                raise RuntimeError(
                    f"Cannot safely retry creation of {title!r}: parent metadata search is unavailable; "
                    "inspect GitHub and rerun provision after recording its Issue number"
                )
        elif PARENT_MARKER in body:
            matching = [
                issue.number
                for issue in self._forge.find_open_issues_by_exact_title(title)
                if PARENT_MARKER in issue.body
            ]
        else:
            matching = []
        if len(matching) > 1:
            raise RuntimeError(
                f"Multiple Issues match uncertain create for {title!r}: {matching}"
            )
        return (bool(matching), matching[0] if matching else None)

    def create_issue(self, title: str, body: str, labels: Sequence[str] = ()) -> int:
        return cast(
            int,
            self._call(
                lambda: self._forge.create_issue(title, body, labels),
                lambda: self._find_created(title, body),
                replay_on_miss=False,
            ),
        )

    def add_sub_issue(
        self, parent_issue_number: int | str, child_issue_number: int | str
    ) -> None:
        def probe() -> tuple[bool, None]:
            child = self._forge.get_issue(child_issue_number)
            return (
                child is not None
                and child.parent is not None
                and child.parent.get("number") == int(parent_issue_number),
                None,
            )

        self._call(
            lambda: self._forge.add_sub_issue(parent_issue_number, child_issue_number),
            probe,
        )

    def set_blocked_by(
        self, issue_number: int | str, blocking_issue_number: int | str
    ) -> None:
        def probe() -> tuple[bool, None]:
            issue = self._forge.get_issue(issue_number)
            return (
                issue is not None and int(blocking_issue_number) in issue.blocked_by,
                None,
            )

        self._call(
            lambda: self._forge.set_blocked_by(issue_number, blocking_issue_number),
            probe,
        )

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._forge, name)
        if not callable(attribute):
            return attribute
        return lambda *args, **kwargs: self._call(lambda: attribute(*args, **kwargs))

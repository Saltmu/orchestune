from __future__ import annotations

import subprocess

import pytest

from orchestune.issue_parsing import PARENT_MARKER
from orchestune.plan_writer import write_issue_numbers
from orchestune.provisioning.cli import _print_resume_hint
from orchestune.provisioning.retry import ProvisionRetryForge
from tests.test_provisioning_support import FakeForge


def _api_error(detail: str) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(1, ["gh", "api"], stderr=detail)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def time(self) -> float:
        return self.now


def test_retry_after_controls_wait_for_429() -> None:
    class FlakyForge(FakeForge):
        calls = 0

        def get_issue(self, issue_number):
            self.calls += 1
            if self.calls == 1:
                raise _api_error("HTTP 429\nRetry-After: 7")
            return super().get_issue(issue_number)

    clock = Clock()
    forge = FlakyForge()
    number = forge.create_issue("a", "b")
    wrapped = ProvisionRetryForge(
        forge, sleep=clock.sleep, clock=clock.time, min_interval=0
    )
    assert wrapped.get_issue(number) is not None
    assert clock.sleeps == [7.0]
    assert forge.calls == 2


def test_permanent_4xx_does_not_wait_or_retry() -> None:
    class DeniedForge(FakeForge):
        calls = 0

        def get_issue(self, issue_number):
            self.calls += 1
            raise _api_error("HTTP 403: permission denied")

    clock = Clock()
    forge = DeniedForge()
    wrapped = ProvisionRetryForge(forge, sleep=clock.sleep, clock=clock.time)
    with pytest.raises(subprocess.CalledProcessError):
        wrapped.get_issue(1)
    assert forge.calls == 1
    assert clock.sleeps == []


def test_retry_limit_reports_failure() -> None:
    class UnavailableForge(FakeForge):
        calls = 0

        def get_issue(self, issue_number):
            self.calls += 1
            raise _api_error("HTTP 503")

    clock = Clock()
    forge = UnavailableForge()
    wrapped = ProvisionRetryForge(
        forge, sleep=clock.sleep, clock=clock.time, min_interval=0, max_attempts=3
    )
    with pytest.raises(RuntimeError, match="retry limit reached"):
        wrapped.get_issue(1)
    assert forge.calls == 3
    assert clock.sleeps == [1.0, 2.0]


def test_lost_relationship_response_does_not_repeat_write() -> None:
    class LostResponseForge(FakeForge):
        writes = 0

        def add_sub_issue(self, parent_issue_number, child_issue_number):
            self.writes += 1
            super().add_sub_issue(parent_issue_number, child_issue_number)
            raise _api_error("HTTP 502")

    clock = Clock()
    forge = LostResponseForge()
    parent = forge.create_issue("parent", "body")
    child = forge.create_issue("child", "body")
    wrapped = ProvisionRetryForge(
        forge, sleep=clock.sleep, clock=clock.time, min_interval=0
    )
    wrapped.add_sub_issue(parent, child)
    assert forge.writes == 1


def test_temporary_connection_failure_retries() -> None:
    class FlakyForge(FakeForge):
        calls = 0

        def get_issue(self, issue_number):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionResetError("connection reset")
            return super().get_issue(issue_number)

    clock = Clock()
    forge = FlakyForge()
    number = forge.create_issue("a", "b")
    wrapped = ProvisionRetryForge(
        forge, sleep=clock.sleep, clock=clock.time, min_interval=0
    )
    assert wrapped.get_issue(number) is not None
    assert forge.calls == 2


def test_uncertain_create_with_delayed_search_never_reposts() -> None:
    class DelayedSearchForge(FakeForge):
        writes = 0

        def create_issue(self, title, body, labels=()):
            self.writes += 1
            super().create_issue(title, body, labels)
            raise _api_error("HTTP 503")

        def find_open_issues_by_exact_title(self, title):
            return []

    clock = Clock()
    forge = DelayedSearchForge()
    wrapped = ProvisionRetryForge(
        forge, sleep=clock.sleep, clock=clock.time, min_interval=0
    )
    with pytest.raises(RuntimeError, match="outcome is uncertain"):
        wrapped.create_issue("[EPIC] delayed", PARENT_MARKER)
    assert forge.writes == 1


def test_probe_rate_limit_does_not_make_uncertain_create_replayable() -> None:
    class DelayedSearchForge(FakeForge):
        writes = 0
        probes = 0

        def create_issue(self, title, body, labels=()):
            self.writes += 1
            super().create_issue(title, body, labels)
            raise _api_error("HTTP 502")

        def find_open_issues_by_exact_title(self, title):
            self.probes += 1
            if self.probes == 1:
                raise _api_error("HTTP 429\nRetry-After: 1")
            return []

    clock = Clock()
    forge = DelayedSearchForge()
    wrapped = ProvisionRetryForge(
        forge, sleep=clock.sleep, clock=clock.time, min_interval=0
    )
    with pytest.raises(RuntimeError, match="outcome is uncertain"):
        wrapped.create_issue("[EPIC] delayed", PARENT_MARKER)
    assert forge.writes == 1


def test_rejected_429_create_retries() -> None:
    class RateLimitedForge(FakeForge):
        calls = 0

        def create_issue(self, title, body, labels=()):
            self.calls += 1
            if self.calls == 1:
                raise _api_error("HTTP 429\nRetry-After: 3")
            return super().create_issue(title, body, labels)

    clock = Clock()
    forge = RateLimitedForge()
    wrapped = ProvisionRetryForge(
        forge, sleep=clock.sleep, clock=clock.time, min_interval=0
    )
    number = wrapped.create_issue("[EPIC] limited", PARENT_MARKER)
    assert number == 100
    assert forge.calls == 2
    assert clock.sleeps == [3.0]


@pytest.mark.parametrize(
    "detail",
    ["API rate limit exceeded", "You have exceeded a secondary rate limit"],
)
def test_rejected_403_rate_limit_create_retries(detail: str) -> None:
    class RateLimitedForge(FakeForge):
        calls = 0

        def create_issue(self, title, body, labels=()):
            self.calls += 1
            if self.calls == 1:
                raise _api_error(f"HTTP 403: {detail}\nRetry-After: 3")
            return super().create_issue(title, body, labels)

    clock = Clock()
    forge = RateLimitedForge()
    wrapped = ProvisionRetryForge(
        forge, sleep=clock.sleep, clock=clock.time, min_interval=0
    )
    assert wrapped.create_issue("[EPIC] limited", PARENT_MARKER) == 100
    assert forge.calls == 2
    assert clock.sleeps == [3.0]


@pytest.mark.parametrize(
    "message",
    ["context deadline exceeded", "i/o timeout", "TLS handshake timeout"],
)
def test_gh_transport_timeout_retries(message: str) -> None:
    class TimeoutForge(FakeForge):
        calls = 0

        def get_issue(self, issue_number):
            self.calls += 1
            if self.calls == 1:
                raise _api_error(message)
            return super().get_issue(issue_number)

    clock = Clock()
    forge = TimeoutForge()
    number = forge.create_issue("a", "b")
    wrapped = ProvisionRetryForge(
        forge, sleep=clock.sleep, clock=clock.time, min_interval=0
    )
    assert wrapped.get_issue(number) is not None
    assert forge.calls == 2


def test_create_probe_reads_parent_number_from_yaml_fence() -> None:
    class LostResponseForge(FakeForge):
        writes = 0

        def create_issue(self, title, body, labels=()):
            self.writes += 1
            super().create_issue(title, body, labels)
            raise _api_error("HTTP 503")

        def find_issues_by_parent_metadata(self, parent_issue_number):
            return [
                self.get_issue(number)
                for number, issue in self.issues.items()
                if f"parent_issue_number: {parent_issue_number}" in issue["body"]
            ]

    clock = Clock()
    forge = LostResponseForge()
    FakeForge.create_issue(forge, "[EPIC] parent", PARENT_MARKER)
    body = (
        "Description mentions parent_issue_number: 999\n"
        "parent_issue_number: 999\n\n"
        "```yaml\nsubtask_id: task-a\nparent_issue_number: 100\n```\n"
    )
    wrapped = ProvisionRetryForge(
        forge, sleep=clock.sleep, clock=clock.time, min_interval=0
    )
    assert wrapped.create_issue("task-a", body) == 101
    assert forge.writes == 1


def test_resume_hint_lists_saved_issue_numbers(plan_path, capsys) -> None:
    write_issue_numbers(plan_path, {"task-a": 101}, parent_issue_number=100)
    _print_resume_hint(str(plan_path))
    detail = capsys.readouterr().err
    assert "parent=100" in detail
    assert "'task-a': 101" in detail
    assert "unfinished=1" in detail
    assert "Rerun" in detail

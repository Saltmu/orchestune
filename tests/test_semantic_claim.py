"""Real-git coverage for the layer-2 semantic review claim (#1370).

Every test runs ``git`` against a local bare repository acting as ``origin``;
nothing in the git layer is mocked. Only the moment a push result is lost is
simulated, by wrapping the real ``run_git`` so the push happens but its
outcome is reported as a transport failure.
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orchestune.infra import git_cli
from orchestune.integrator import semantic_claim
from orchestune.integrator.semantic_claim import (
    CLAIM_BACKEND_UNSUPPORTED,
    ClaimStatus,
    PushStatus,
    RefUpdate,
    SemanticClaimCorruptError,
    SemanticClaimError,
    acquire_semantic_claim,
    delete_attempt_fix_branches,
    probe_claim_backend,
    promote_fix_output,
    push_refs_under_claim,
    read_semantic_claim,
    release_semantic_claim,
    renew_semantic_claim,
    semantic_claim_ref,
)

PARENT = 1033
NOW = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
TTL = 600
ATTEMPT = "a1b2c3d4e5f6a7b8c9d0"
OTHER_ATTEMPT = "ffffeeeeddddccccbbbb"
TEMP = "integration/parent-issue-1033/attempt"


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


def _commit(
    repository: Path, filename: str, contents: str, env: dict[str, str] | None = None
) -> str:
    (repository / filename).write_text(contents, encoding="utf-8")
    _git(repository, "add", filename, env=env)
    _git(repository, "commit", "-q", "-m", filename, env=env)
    return _git(repository, "rev-parse", "HEAD", env=env)


def _remote_oid(origin: Path, ref: str) -> str | None:
    out = _git(origin, "for-each-ref", "--format=%(objectname)", ref)
    return out or None


@dataclasses.dataclass(frozen=True)
class _RemoteTemplate:
    origin: Path
    author: Path
    worker: Path
    base: str


@pytest.fixture(scope="session")
def _remote_template(
    tmp_path_factory: pytest.TempPathFactory,
    _isolated_gitconfig: Path,
) -> _RemoteTemplate:
    root = tmp_path_factory.mktemp("semantic-claim-template")
    origin = root / "origin.git"
    author = root / "author"
    worker = root / "worker"
    env = git_cli.get_clean_git_env({"GIT_CONFIG_GLOBAL": str(_isolated_gitconfig)})

    _git(root, "init", "-q", "--bare", str(origin), env=env)
    _git(root, "init", "-q", "-b", "main", str(author), env=env)
    _git(author, "config", "user.name", "Author", env=env)
    _git(author, "config", "user.email", "author@example.com", env=env)
    base = _commit(author, "base.txt", "base\n", env=env)
    _git(author, "remote", "add", "origin", origin.as_posix(), env=env)
    _git(author, "push", "-q", "origin", "main", f"main:refs/heads/{TEMP}", env=env)
    _git(root, "clone", "-q", origin.as_posix(), str(worker), env=env)
    return _RemoteTemplate(origin=origin, author=author, worker=worker, base=base)


def _rewrite_git_config_url(config_path: Path, new_url: str) -> None:
    text = config_path.read_text(encoding="utf-8")
    updated = re.sub(
        r"(?m)^(\s*url\s*=\s*).*$",
        lambda m: m.group(1) + new_url,
        text,
    )
    config_path.write_text(updated, encoding="utf-8")


class Remote:
    """A bare origin, an author clone that seeds branches, and worker clones."""

    def __init__(self, root: Path, template: _RemoteTemplate) -> None:
        self.root = root
        self._template = template
        self.origin = root / "origin.git"
        self.author = root / "author"
        self.base = template.base
        shutil.copytree(template.origin, self.origin)
        shutil.copytree(template.author, self.author)
        _rewrite_git_config_url(self.author / ".git" / "config", self.origin.as_posix())

    def worker(self, name: str) -> Path:
        path = self.root / name
        shutil.copytree(self._template.worker, path)
        _rewrite_git_config_url(path / ".git" / "config", self.origin.as_posix())
        return path

    def push_branch(self, branch: str, filename: str, parent: str) -> str:
        _git(self.author, "checkout", "-q", "-B", branch, parent)
        sha = _commit(self.author, filename, f"{filename}\n")
        _git(self.author, "push", "-q", "-f", "origin", f"{branch}:refs/heads/{branch}")
        return sha

    def oid(self, ref: str) -> str | None:
        return _remote_oid(self.origin, ref)


@pytest.fixture
def remote(tmp_path: Path, _remote_template: _RemoteTemplate) -> Remote:
    return Remote(tmp_path, _remote_template)


def _acquire(worker: Path, *, holder: str, attempt: str = ATTEMPT, now=NOW, ttl=TTL):
    return acquire_semantic_claim(
        worker,
        parent_issue_number=PARENT,
        attempt_id=attempt,
        holder=holder,
        now=now,
        ttl_seconds=ttl,
    )


CLAIM_REF = semantic_claim_ref(PARENT)


class TestAcquire:
    def test_first_acquire_creates_generation_one(self, remote: Remote) -> None:
        worker = remote.worker("w1")
        result = _acquire(worker, holder="worker-1")

        assert result.status is ClaimStatus.ACQUIRED
        claim = result.claim
        assert claim is not None
        assert (claim.generation, claim.holder, claim.attempt_id) == (
            1,
            "worker-1",
            ATTEMPT,
        )
        assert claim.expires_at == NOW + timedelta(seconds=TTL)
        assert remote.oid(CLAIM_REF) == claim.ref_oid
        assert read_semantic_claim(remote.worker("reader"), PARENT) == claim

    def test_claim_commit_carries_the_versioned_json_on_an_empty_tree(
        self, remote: Remote
    ) -> None:
        worker = remote.worker("w1")
        claim = _acquire(worker, holder="worker-1").claim
        assert claim is not None

        message = _git(remote.origin, "log", "-1", "--format=%s", claim.ref_oid)
        assert json.loads(message) == {
            "version": 1,
            "parent_issue_number": PARENT,
            "attempt_id": ATTEMPT,
            "holder": "worker-1",
            "generation": 1,
            "expires_at": "2026-10-18T09:10:00+00:00",
        }
        tree = _git(remote.origin, "rev-parse", f"{claim.ref_oid}^{{tree}}")
        assert _git(remote.origin, "ls-tree", tree) == ""

    def test_live_claim_of_another_holder_is_not_taken(self, remote: Remote) -> None:
        first = _acquire(remote.worker("w1"), holder="worker-1").claim
        result = _acquire(remote.worker("w2"), holder="worker-2", attempt=OTHER_ATTEMPT)

        assert result.status is ClaimStatus.HELD_BY_OTHER
        assert result.claim == first
        assert first is not None and remote.oid(CLAIM_REF) == first.ref_oid

    def test_same_holder_and_attempt_reacquires_without_new_generation(
        self, remote: Remote
    ) -> None:
        worker = remote.worker("w1")
        first = _acquire(worker, holder="worker-1").claim
        later = NOW + timedelta(seconds=60)
        again = _acquire(worker, holder="worker-1", now=later)

        assert again.status is ClaimStatus.ACQUIRED
        assert first is not None and again.claim is not None
        assert again.claim.generation == first.generation
        assert again.claim.expires_at == later + timedelta(seconds=TTL)
        parent = _git(remote.origin, "rev-parse", f"{again.claim.ref_oid}^")
        assert parent == first.ref_oid

    def test_same_holder_with_another_attempt_is_held_by_other(
        self, remote: Remote
    ) -> None:
        worker = remote.worker("w1")
        _acquire(worker, holder="worker-1")
        result = _acquire(worker, holder="worker-1", attempt=OTHER_ATTEMPT)
        assert result.status is ClaimStatus.HELD_BY_OTHER

    def test_expired_claim_is_taken_with_the_next_generation(
        self, remote: Remote
    ) -> None:
        first = _acquire(remote.worker("w1"), holder="worker-1").claim
        assert first is not None
        result = _acquire(
            remote.worker("w2"),
            holder="worker-2",
            attempt=OTHER_ATTEMPT,
            now=first.expires_at,
        )

        assert result.status is ClaimStatus.ACQUIRED
        assert result.claim is not None
        assert result.claim.generation == first.generation + 1
        assert result.claim.holder == "worker-2"

    def test_stale_view_loses_the_compare_and_swap(
        self, remote: Remote, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A worker that read "no claim" just before another worker acquired."""
        winner = _acquire(remote.worker("w1"), holder="worker-1").claim
        loser = remote.worker("w2")
        real_read = semantic_claim.read_semantic_claim
        calls: list[int] = []

        def stale_first_read(root, parent_issue_number):
            calls.append(parent_issue_number)
            if len(calls) == 1:
                return None
            return real_read(root, parent_issue_number)

        monkeypatch.setattr(semantic_claim, "read_semantic_claim", stale_first_read)
        result = _acquire(loser, holder="worker-2", attempt=OTHER_ATTEMPT)

        assert result.status is ClaimStatus.HELD_BY_OTHER
        assert result.claim == winner
        assert winner is not None and remote.oid(CLAIM_REF) == winner.ref_oid

    def test_concurrent_acquire_has_exactly_one_winner(self, remote: Remote) -> None:
        workers = [remote.worker(f"w{index}") for index in range(4)]
        barrier = threading.Barrier(len(workers))
        results: dict[int, object] = {}

        def run(index: int) -> None:
            barrier.wait()
            results[index] = _acquire(
                workers[index], holder=f"worker-{index}", attempt=f"{index:020d}"
            )

        threads = [threading.Thread(target=run, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        statuses = [results[i].status for i in range(4)]  # type: ignore[attr-defined]
        assert statuses.count(ClaimStatus.ACQUIRED) == 1
        assert ClaimStatus.UNKNOWN not in statuses
        winner = next(
            results[i].claim  # type: ignore[attr-defined]
            for i in range(4)
            if statuses[i] is ClaimStatus.ACQUIRED
        )
        assert remote.oid(CLAIM_REF) == winner.ref_oid

    def test_naive_time_is_rejected(self, remote: Remote) -> None:
        with pytest.raises(ValueError):
            _acquire(remote.worker("w1"), holder="w", now=datetime(2026, 1, 1))

    @pytest.mark.parametrize("ttl", [0, -5, True])
    def test_non_positive_ttl_is_rejected(self, remote: Remote, ttl) -> None:
        with pytest.raises(ValueError):
            _acquire(remote.worker("w1"), holder="w", ttl=ttl)


class TestRead:
    def test_absent_ref_reads_as_none(self, remote: Remote) -> None:
        assert read_semantic_claim(remote.worker("w1"), PARENT) is None

    def test_a_deleted_remote_claim_also_drops_the_local_copy(
        self, remote: Remote
    ) -> None:
        worker = remote.worker("w1")
        _acquire(worker, holder="worker-1")
        _git(remote.origin, "update-ref", "-d", CLAIM_REF)

        assert read_semantic_claim(worker, PARENT) is None
        assert _remote_oid(worker, CLAIM_REF) is None

    def test_fetch_failure_is_not_absence(self, remote: Remote) -> None:
        worker = remote.worker("w1")
        _git(worker, "remote", "set-url", "origin", str(remote.root / "missing.git"))
        with pytest.raises(SemanticClaimError):
            read_semantic_claim(worker, PARENT)
        with pytest.raises(SemanticClaimError):
            _acquire(worker, holder="worker-1")

    @pytest.mark.parametrize(
        "message",
        [
            "not json",
            json.dumps({"version": 2}),
            json.dumps(
                {
                    "version": 1,
                    "parent_issue_number": 999,
                    "attempt_id": ATTEMPT,
                    "holder": "h",
                    "generation": 1,
                    "expires_at": "2026-10-18T09:10:00+00:00",
                }
            ),
            json.dumps(
                {
                    "version": 1,
                    "parent_issue_number": PARENT,
                    "attempt_id": ATTEMPT,
                    "holder": "h",
                    "generation": 1,
                    "expires_at": "2026-10-18T09:10:00",
                }
            ),
            json.dumps(
                {
                    "version": 1,
                    "parent_issue_number": PARENT,
                    "attempt_id": ATTEMPT,
                    "holder": "h",
                    "generation": 0,
                    "expires_at": "2026-10-18T09:10:00+00:00",
                }
            ),
        ],
    )
    def test_corrupt_content_raises(self, remote: Remote, message: str) -> None:
        tree = _git(remote.author, "rev-parse", "HEAD^{tree}")
        oid = _git(remote.author, "commit-tree", "-m", message, tree)
        _git(remote.author, "push", "-q", "origin", f"{oid}:{CLAIM_REF}")

        worker = remote.worker("w1")
        with pytest.raises(SemanticClaimCorruptError):
            read_semantic_claim(worker, PARENT)
        with pytest.raises(SemanticClaimCorruptError):
            _acquire(worker, holder="worker-1")

    def test_a_non_commit_object_is_corrupt(self, remote: Remote) -> None:
        tree = _git(remote.author, "rev-parse", "HEAD^{tree}")
        _git(remote.author, "push", "-q", "origin", f"{tree}:{CLAIM_REF}")
        with pytest.raises(SemanticClaimCorruptError):
            read_semantic_claim(remote.worker("w1"), PARENT)


class TestUnknownPushOutcome:
    @staticmethod
    def _lose_push_result(monkeypatch: pytest.MonkeyPatch, *, apply: bool) -> None:
        real_run_git = git_cli.run_git

        def flaky(args, **kwargs):
            if args and args[0] == "push":
                if apply:
                    real_run_git(args, **kwargs)
                raise OSError("connection reset by peer")
            return real_run_git(args, **kwargs)

        monkeypatch.setattr(semantic_claim, "run_git", flaky)

    def test_applied_push_is_recognised_by_reading_back(
        self, remote: Remote, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worker = remote.worker("w1")
        self._lose_push_result(monkeypatch, apply=True)
        result = _acquire(worker, holder="worker-1")

        assert result.status is ClaimStatus.ACQUIRED
        assert result.claim is not None
        assert remote.oid(CLAIM_REF) == result.claim.ref_oid

    def test_unapplied_push_is_unknown_not_acquired(
        self, remote: Remote, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worker = remote.worker("w1")
        self._lose_push_result(monkeypatch, apply=False)
        result = _acquire(worker, holder="worker-1")

        assert result.status is ClaimStatus.UNKNOWN
        assert result.claim is None
        assert remote.oid(CLAIM_REF) is None

    def test_unreadable_remote_after_a_lost_acquire_is_unknown(
        self, remote: Remote, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worker = remote.worker("w1")
        real_run_git = git_cli.run_git
        pushed: list[bool] = []

        def broken_after_push(args, **kwargs):
            if args and args[0] == "push":
                pushed.append(True)
                raise OSError("connection reset by peer")
            if pushed and args and args[0] in {"ls-remote", "fetch"}:
                raise OSError("network is unreachable")
            return real_run_git(args, **kwargs)

        monkeypatch.setattr(semantic_claim, "run_git", broken_after_push)
        result = _acquire(worker, holder="worker-1")
        assert result.status is ClaimStatus.UNKNOWN
        assert result.claim is None

    def test_push_under_claim_reconciles_an_applied_push(
        self, remote: Remote, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worker = remote.worker("w1")
        claim = _acquire(worker, holder="worker-1").claim
        assert claim is not None
        new = remote.push_branch("feature", "f.txt", remote.base)
        self._lose_push_result(monkeypatch, apply=True)
        _git(worker, "fetch", "-q", "origin", "feature")

        result = push_refs_under_claim(
            worker, claim, [RefUpdate(f"refs/heads/{TEMP}", new, remote.base)], now=NOW
        )
        assert result.status is PushStatus.PUSHED
        assert result.claim is not None
        assert remote.oid(CLAIM_REF) == result.claim.ref_oid

    def test_push_under_claim_reports_an_unapplied_push_as_failed(
        self, remote: Remote, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worker = remote.worker("w1")
        claim = _acquire(worker, holder="worker-1").claim
        assert claim is not None
        self._lose_push_result(monkeypatch, apply=False)

        result = push_refs_under_claim(worker, claim, [], now=NOW)
        assert result.status is PushStatus.FAILED
        assert result.claim == claim

    def test_unreadable_remote_after_a_lost_push_is_unknown(
        self, remote: Remote, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worker = remote.worker("w1")
        claim = _acquire(worker, holder="worker-1").claim
        assert claim is not None
        real_run_git = git_cli.run_git

        def broken(args, **kwargs):
            if args and args[0] in {"push", "ls-remote", "fetch"}:
                raise OSError("network is unreachable")
            return real_run_git(args, **kwargs)

        monkeypatch.setattr(semantic_claim, "run_git", broken)
        result = push_refs_under_claim(worker, claim, [], now=NOW)
        assert result.status is PushStatus.UNKNOWN
        assert result.claim is None


class TestPushUnderClaim:
    def test_updates_refs_and_advances_the_claim_in_one_push(
        self, remote: Remote
    ) -> None:
        worker = remote.worker("w1")
        claim = _acquire(worker, holder="worker-1").claim
        assert claim is not None
        new = remote.push_branch("feature", "f.txt", remote.base)
        _git(worker, "fetch", "-q", "origin", "feature")

        result = push_refs_under_claim(
            worker,
            claim,
            [
                RefUpdate(f"refs/heads/{TEMP}", new, remote.base),
                RefUpdate("refs/heads/created", new, None),
            ],
            now=NOW,
        )

        assert result.status is PushStatus.PUSHED
        advanced = result.claim
        assert advanced is not None
        assert advanced.generation == claim.generation
        assert advanced.expires_at == claim.expires_at
        assert advanced.ref_oid != claim.ref_oid
        assert remote.oid(CLAIM_REF) == advanced.ref_oid
        assert remote.oid(f"refs/heads/{TEMP}") == new
        assert remote.oid("refs/heads/created") == new
        assert read_semantic_claim(worker, PARENT) == advanced

    def test_a_reused_claim_cannot_push_twice_in_the_same_second(
        self, remote: Remote
    ) -> None:
        """Identical claim commits would make the second lease a no-op (#1376)."""
        worker = remote.worker("w1")
        claim = _acquire(worker, holder="worker-1").claim
        assert claim is not None
        first = push_refs_under_claim(
            worker, claim, [RefUpdate("refs/heads/one", remote.base, None)], now=NOW
        )
        second = push_refs_under_claim(
            worker, claim, [RefUpdate("refs/heads/two", remote.base, None)], now=NOW
        )

        assert first.status is PushStatus.PUSHED
        assert second.status is PushStatus.REJECTED
        assert first.claim is not None
        assert remote.oid(CLAIM_REF) == first.claim.ref_oid
        assert remote.oid("refs/heads/two") is None

    def test_same_holder_racing_itself_has_one_winner(self, remote: Remote) -> None:
        worker = remote.worker("w1")
        claim = _acquire(worker, holder="worker-1").claim
        assert claim is not None
        renewed = renew_semantic_claim(worker, claim, now=NOW, ttl_seconds=TTL)
        stale = renew_semantic_claim(worker, claim, now=NOW, ttl_seconds=TTL)
        assert renewed.status is PushStatus.PUSHED
        assert stale.status is PushStatus.REJECTED

    def test_deletes_a_ref_under_claim(self, remote: Remote) -> None:
        worker = remote.worker("w1")
        claim = _acquire(worker, holder="worker-1").claim
        assert claim is not None
        result = push_refs_under_claim(
            worker, claim, [RefUpdate(f"refs/heads/{TEMP}", None, remote.base)], now=NOW
        )
        assert result.status is PushStatus.PUSHED
        assert remote.oid(f"refs/heads/{TEMP}") is None

    def test_former_holder_is_rejected_and_nothing_changes(
        self, remote: Remote
    ) -> None:
        old_worker = remote.worker("w1")
        stale = _acquire(old_worker, holder="worker-1").claim
        assert stale is not None
        taken = _acquire(
            remote.worker("w2"),
            holder="worker-2",
            attempt=OTHER_ATTEMPT,
            now=stale.expires_at,
        ).claim
        assert taken is not None
        new = remote.push_branch("feature", "f.txt", remote.base)
        _git(old_worker, "fetch", "-q", "origin", "feature")

        result = push_refs_under_claim(
            old_worker,
            stale,
            [RefUpdate(f"refs/heads/{TEMP}", new, remote.base)],
            now=NOW,
        )

        assert result.status is PushStatus.REJECTED
        assert result.claim is None
        assert remote.oid(CLAIM_REF) == taken.ref_oid
        assert remote.oid(f"refs/heads/{TEMP}") == remote.base

    def test_one_failed_lease_rejects_every_update(self, remote: Remote) -> None:
        worker = remote.worker("w1")
        claim = _acquire(worker, holder="worker-1").claim
        assert claim is not None
        new = remote.push_branch("feature", "f.txt", remote.base)
        moved = remote.push_branch("other", "o.txt", remote.base)
        _git(worker, "fetch", "-q", "origin", "feature", "other")

        result = push_refs_under_claim(
            worker,
            claim,
            [
                RefUpdate(f"refs/heads/{TEMP}", new, remote.base),
                RefUpdate("refs/heads/other", new, remote.base),
            ],
            now=NOW,
        )

        assert result.status is PushStatus.REJECTED
        assert result.claim == claim
        assert remote.oid(CLAIM_REF) == claim.ref_oid
        assert remote.oid(f"refs/heads/{TEMP}") == remote.base
        assert remote.oid("refs/heads/other") == moved

    def test_creating_an_existing_ref_is_rejected(self, remote: Remote) -> None:
        worker = remote.worker("w1")
        claim = _acquire(worker, holder="worker-1").claim
        assert claim is not None
        new = remote.push_branch("feature", "f.txt", remote.base)
        _git(worker, "fetch", "-q", "origin", "feature")
        result = push_refs_under_claim(
            worker, claim, [RefUpdate(f"refs/heads/{TEMP}", new, None)], now=NOW
        )
        assert result.status is PushStatus.REJECTED
        assert result.claim == claim
        assert remote.oid(CLAIM_REF) == claim.ref_oid
        assert remote.oid(f"refs/heads/{TEMP}") == remote.base

    def test_a_ref_already_at_its_new_value_counts_as_applied(
        self, remote: Remote
    ) -> None:
        """Git skips the lease of an up-to-date ref, so a retry is idempotent."""
        worker = remote.worker("w1")
        claim = _acquire(worker, holder="worker-1").claim
        assert claim is not None
        result = push_refs_under_claim(
            worker, claim, [RefUpdate(f"refs/heads/{TEMP}", remote.base, None)], now=NOW
        )
        assert result.status is PushStatus.PUSHED
        assert result.claim is not None
        assert remote.oid(CLAIM_REF) == result.claim.ref_oid

    def test_expired_claim_is_refused_without_pushing(self, remote: Remote) -> None:
        worker = remote.worker("w1")
        claim = _acquire(worker, holder="worker-1").claim
        assert claim is not None
        result = push_refs_under_claim(worker, claim, [], now=claim.expires_at)
        assert result.status is PushStatus.REJECTED
        assert result.detail == "claim_expired"
        assert remote.oid(CLAIM_REF) == claim.ref_oid

    def test_policy_rejection_is_denied(self, remote: Remote) -> None:
        worker = remote.worker("w1")
        claim = _acquire(worker, holder="worker-1").claim
        assert claim is not None
        new = remote.push_branch("feature", "f.txt", remote.base)
        _git(worker, "fetch", "-q", "origin", "feature")
        _install_pre_receive(remote.origin, "refs/heads/")

        result = push_refs_under_claim(
            worker, claim, [RefUpdate(f"refs/heads/{TEMP}", new, remote.base)], now=NOW
        )
        assert result.status is PushStatus.DENIED
        assert result.claim == claim
        assert remote.oid(CLAIM_REF) == claim.ref_oid

    @pytest.mark.parametrize(
        "update",
        [
            RefUpdate("heads/no-prefix", None, "0" * 40),
            RefUpdate("refs/heads/x", None, None),
            RefUpdate("refs/heads/x", "not-a-sha", None),
            RefUpdate("refs/heads/../x", "a" * 40, None),
            RefUpdate(CLAIM_REF, "a" * 40, None),
        ],
    )
    def test_invalid_updates_are_rejected_before_git(
        self, remote: Remote, update: RefUpdate
    ) -> None:
        worker = remote.worker("w1")
        claim = _acquire(worker, holder="worker-1").claim
        assert claim is not None
        with pytest.raises(ValueError):
            push_refs_under_claim(worker, claim, [update], now=NOW)

    def test_a_claim_without_a_valid_ref_oid_is_rejected(self, remote: Remote) -> None:
        worker = remote.worker("w1")
        claim = _acquire(worker, holder="worker-1").claim
        assert claim is not None
        forged = dataclasses.replace(claim, ref_oid="--upload-pack=x")
        with pytest.raises(ValueError):
            push_refs_under_claim(worker, forged, [], now=NOW)
        assert remote.oid(CLAIM_REF) == claim.ref_oid

    def test_duplicate_refs_are_rejected(self, remote: Remote) -> None:
        worker = remote.worker("w1")
        claim = _acquire(worker, holder="worker-1").claim
        assert claim is not None
        update = RefUpdate(f"refs/heads/{TEMP}", None, remote.base)
        with pytest.raises(ValueError):
            push_refs_under_claim(worker, claim, [update, update], now=NOW)


class TestRenewAndRelease:
    def test_renew_extends_expiry_with_the_same_generation(
        self, remote: Remote
    ) -> None:
        worker = remote.worker("w1")
        claim = _acquire(worker, holder="worker-1").claim
        assert claim is not None
        later = NOW + timedelta(seconds=300)

        result = renew_semantic_claim(worker, claim, now=later, ttl_seconds=TTL)

        assert result.status is PushStatus.PUSHED
        assert result.claim is not None
        assert result.claim.generation == claim.generation
        assert result.claim.expires_at == later + timedelta(seconds=TTL)
        assert read_semantic_claim(worker, PARENT) == result.claim

    def test_renew_by_a_former_holder_is_rejected(self, remote: Remote) -> None:
        old_worker = remote.worker("w1")
        stale = _acquire(old_worker, holder="worker-1").claim
        assert stale is not None
        taken = _acquire(
            remote.worker("w2"),
            holder="worker-2",
            attempt=OTHER_ATTEMPT,
            now=stale.expires_at,
        ).claim
        assert taken is not None

        result = renew_semantic_claim(old_worker, stale, now=NOW, ttl_seconds=TTL)
        assert result.status is PushStatus.REJECTED
        assert remote.oid(CLAIM_REF) == taken.ref_oid

    def test_release_lets_another_holder_take_the_next_generation(
        self, remote: Remote
    ) -> None:
        worker = remote.worker("w1")
        claim = _acquire(worker, holder="worker-1").claim
        assert claim is not None
        released = release_semantic_claim(worker, claim, now=NOW)
        assert released.status is PushStatus.PUSHED
        assert released.claim is not None
        assert released.claim.expires_at == NOW

        result = _acquire(remote.worker("w2"), holder="worker-2", attempt=OTHER_ATTEMPT)
        assert result.status is ClaimStatus.ACQUIRED
        assert result.claim is not None
        assert result.claim.generation == claim.generation + 1


class TestPromoteFixOutput:
    def test_promotes_a_descendant_fix_branch(self, remote: Remote) -> None:
        worker = remote.worker("w1")
        claim = _acquire(worker, holder="worker-1").claim
        assert claim is not None
        fix_branch = f"integration/semantic-fix/{ATTEMPT[:12]}-g0-r1-fix-0"
        fix = remote.push_branch(fix_branch, "fix.txt", remote.base)

        result = promote_fix_output(
            worker,
            claim,
            temp_branch=TEMP,
            expected_head_sha=remote.base,
            fix_branch=fix_branch,
            now=NOW,
        )

        assert result.status is PushStatus.PUSHED
        assert remote.oid(f"refs/heads/{TEMP}") == fix
        assert result.claim is not None
        assert remote.oid(CLAIM_REF) == result.claim.ref_oid
        assert result.ref_updates == (
            RefUpdate(f"refs/heads/{TEMP}", fix, remote.base),
        )

    def test_rejects_a_fix_branch_that_does_not_descend_from_the_head(
        self, remote: Remote
    ) -> None:
        worker = remote.worker("w1")
        claim = _acquire(worker, holder="worker-1").claim
        assert claim is not None
        unrelated_root = remote.push_branch("side", "side.txt", remote.base)
        fix_branch = f"integration/semantic-fix/{ATTEMPT[:12]}-g0-r1-fix-0"
        remote.push_branch(fix_branch, "fix.txt", remote.base)

        result = promote_fix_output(
            worker,
            claim,
            temp_branch=TEMP,
            expected_head_sha=unrelated_root,
            fix_branch=fix_branch,
            now=NOW,
        )

        assert result.status is PushStatus.REJECTED
        assert result.detail == "fix_not_descendant"
        assert result.claim == claim
        assert remote.oid(f"refs/heads/{TEMP}") == remote.base
        assert remote.oid(CLAIM_REF) == claim.ref_oid

    def test_former_holder_cannot_promote(self, remote: Remote) -> None:
        old_worker = remote.worker("w1")
        stale = _acquire(old_worker, holder="worker-1").claim
        assert stale is not None
        taken = _acquire(
            remote.worker("w2"),
            holder="worker-2",
            attempt=OTHER_ATTEMPT,
            now=stale.expires_at,
        ).claim
        assert taken is not None
        fix_branch = f"integration/semantic-fix/{ATTEMPT[:12]}-g0-r1-fix-0"
        remote.push_branch(fix_branch, "fix.txt", remote.base)

        result = promote_fix_output(
            old_worker,
            stale,
            temp_branch=TEMP,
            expected_head_sha=remote.base,
            fix_branch=fix_branch,
            now=NOW,
        )

        assert result.status is PushStatus.REJECTED
        assert remote.oid(f"refs/heads/{TEMP}") == remote.base
        assert remote.oid(CLAIM_REF) == taken.ref_oid

    def test_moved_temp_branch_is_rejected(self, remote: Remote) -> None:
        worker = remote.worker("w1")
        claim = _acquire(worker, holder="worker-1").claim
        assert claim is not None
        fix_branch = f"integration/semantic-fix/{ATTEMPT[:12]}-g0-r1-fix-0"
        fix = remote.push_branch(fix_branch, "fix.txt", remote.base)
        moved = remote.push_branch(TEMP, "moved.txt", fix)

        result = promote_fix_output(
            worker,
            claim,
            temp_branch=TEMP,
            expected_head_sha=remote.base,
            fix_branch=fix_branch,
            now=NOW,
        )

        assert result.status is PushStatus.REJECTED
        assert remote.oid(f"refs/heads/{TEMP}") == moved
        assert remote.oid(CLAIM_REF) == claim.ref_oid

    def test_missing_fix_branch_fails_without_pushing(self, remote: Remote) -> None:
        worker = remote.worker("w1")
        claim = _acquire(worker, holder="worker-1").claim
        assert claim is not None

        result = promote_fix_output(
            worker,
            claim,
            temp_branch=TEMP,
            expected_head_sha=remote.base,
            fix_branch="integration/semantic-fix/absent",
            now=NOW,
        )

        assert result.status is PushStatus.FAILED
        assert result.claim == claim
        assert remote.oid(CLAIM_REF) == claim.ref_oid


class TestDeleteAttemptFixBranches:
    def test_deletes_only_the_attempts_fix_branches(self, remote: Remote) -> None:
        mine = [
            f"integration/semantic-fix/{ATTEMPT[:12]}-g0-r1-fix-0",
            f"integration/semantic-fix/{ATTEMPT[:12]}-g0-r2-fix-1",
        ]
        others = [
            f"integration/semantic-fix/{OTHER_ATTEMPT[:12]}-g0-r1-fix-0",
            f"integration/semantic-fix/{ATTEMPT[:12]}x-g0-r1-fix-0",
            f"feature/{ATTEMPT[:12]}-g0-r1-fix-0",
        ]
        for index, branch in enumerate(mine + others):
            remote.push_branch(branch, f"b{index}.txt", remote.base)

        delete_attempt_fix_branches(remote.worker("w1"), ATTEMPT)

        for branch in mine:
            assert remote.oid(f"refs/heads/{branch}") is None
        for branch in others:
            assert remote.oid(f"refs/heads/{branch}") is not None

    def test_nothing_to_delete_is_a_no_op(self, remote: Remote) -> None:
        delete_attempt_fix_branches(remote.worker("w1"), ATTEMPT)

    def test_listing_failure_raises(self, remote: Remote) -> None:
        worker = remote.worker("w1")
        _git(worker, "remote", "set-url", "origin", str(remote.root / "missing.git"))
        with pytest.raises(SemanticClaimError):
            delete_attempt_fix_branches(worker, ATTEMPT)

    def test_unsafe_attempt_id_is_rejected(self, remote: Remote) -> None:
        with pytest.raises(ValueError):
            delete_attempt_fix_branches(remote.worker("w1"), "../*")


def _install_pre_receive(origin: Path, rejected_prefix: str) -> None:
    hook = origin / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        "while read old new ref; do\n"
        f'  case "$ref" in {rejected_prefix}*) echo "denied $ref" >&2; exit 1;; esac\n'
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)


class TestProbeClaimBackend:
    def test_supported_backend_returns_none_and_leaves_no_ref(
        self, remote: Remote
    ) -> None:
        assert probe_claim_backend(remote.worker("w1")) is None
        refs = _git(remote.origin, "for-each-ref", "refs/orchestune/")
        assert refs == ""

    def test_hook_rejecting_the_claim_namespace_is_unsupported(
        self, remote: Remote
    ) -> None:
        _install_pre_receive(remote.origin, "refs/orchestune/")
        assert probe_claim_backend(remote.worker("w1")) == CLAIM_BACKEND_UNSUPPORTED

    def test_remote_without_atomic_push_is_unsupported(self, remote: Remote) -> None:
        _git(remote.origin, "config", "receive.advertiseAtomic", "false")
        assert probe_claim_backend(remote.worker("w1")) == CLAIM_BACKEND_UNSUPPORTED

    def test_denied_deletion_is_unsupported(self, remote: Remote) -> None:
        hook = remote.origin / "hooks" / "pre-receive"
        hook.write_text(
            "#!/bin/sh\n"
            "while read old new ref; do\n"
            '  case "$new" in *[!0]*) ;; *) echo "no deletes" >&2; exit 1;; esac\n'
            "done\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)
        assert probe_claim_backend(remote.worker("w1")) == CLAIM_BACKEND_UNSUPPORTED

    def test_unreachable_remote_raises_instead_of_unsupported(
        self, remote: Remote
    ) -> None:
        worker = remote.worker("w1")
        _git(worker, "remote", "set-url", "origin", str(remote.root / "missing.git"))
        with pytest.raises(SemanticClaimError):
            probe_claim_backend(worker)


class TestRemoteTemplateOptimization:
    def test_remote_and_worker_do_not_spawn_git(
        self, tmp_path: Path, _remote_template, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_git(*args, **kwargs):
            raise AssertionError(f"git invoked unexpectedly: {args} {kwargs}")

        monkeypatch.setattr(subprocess, "run", no_git)
        rem = Remote(tmp_path, _remote_template)
        worker = rem.worker("worker1")
        assert (rem.origin / "HEAD").exists()
        assert (rem.author / ".git").exists()
        assert (worker / ".git").exists()

"""`orchestune review status|retry|override` (#1436)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from orchestune.dispatch import review_cli
from orchestune.models import PrRecord
from orchestune.review.semantic_gate import (
    parse_semantic_review_override_marker,
    parse_semantic_review_retry_marker,
)
from tests.dispatch.test_postcycle_semantic_review import (
    ATTEMPT,
    FIXED,
    HEAD,
    IDENTITY,
    PARENT,
    PR,
    World,
)

ALICE = "alice"


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def _forge(world: World) -> MagicMock:
    forge = cast(MagicMock, world.config.resolved_forge)
    forge.get_pull_request.side_effect = lambda n: PrRecord(
        number=int(n),
        head_ref=IDENTITY.temp_branch if int(n) == PR else "feature/other",
        changed_files=(),
        base_ref=f"parent/issue-{PARENT}",
        is_cross_repository=False,
        head_sha=world.pr_head,
    )
    forge.get_authenticated_user.return_value = ALICE
    return forge


def _run(
    world: World, *argv: str, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    code = review_cli.main(
        list(argv),
        forge=_forge(world),
        config=world.config,
        now=world.clock,
        repository_root=Path("."),
    )
    out = capsys.readouterr()
    return code, out.out, out.err


def _stopped(world: World) -> World:
    """An attempt that stopped because the reviewer never answered."""
    world.seed()
    world.cycle()
    world.cycle(minutes=11)
    assert world.state.escalation_reason == "review_not_acknowledged"
    return world


def _markers(world: World, parse: Any) -> list[Any]:
    found = []
    for comment in world.comments[PR]:
        try:
            parsed = parse(comment["body"])
        except ValueError:
            continue
        if parsed is not None:
            found.append(parsed)
    return found


class TestStatus:
    def test_reports_the_facts_without_writing(
        self, world: World, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _stopped(world)
        written = {n: len(c) for n, c in world.comments.items()}
        code, out, _ = _run(world, "status", "--pr", str(PR), capsys=capsys)
        assert code == 0
        for expected in (
            f"#{PARENT}",
            f"#{PR}",
            ATTEMPT,
            HEAD,
            "escalated",
            "codex",
            "auto",
            "claude-cli",
            "review_not_acknowledged",
            "reviewer activity: no",
            f"orchestune review retry --pr {PR}",
        ):
            assert expected in out
        assert {n: len(c) for n, c in world.comments.items()} == written

    def test_json_is_machine_readable(
        self, world: World, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _stopped(world)
        code, out, _ = _run(world, "status", "--pr", str(PR), "--json", capsys=capsys)
        data = json.loads(out)
        assert code == 0
        assert data["attempt_id"] == ATTEMPT and data["reviewer"] == "codex"
        assert data["acknowledged"] is False and data["requests_on_record"] == 1

    def test_explicit_provider_is_named_as_such(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        world = _stopped(World(tmp_path, semantic_review_provider="claude"))
        _, out, _ = _run(world, "status", "--pr", str(PR), capsys=capsys)
        assert "explicit" in out and "claude" in out

    def test_rejects_a_pr_that_is_not_an_integration_attempt(
        self, world: World, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _stopped(world)
        code, _, err = _run(world, "status", "--pr", "77", capsys=capsys)
        assert code == 1 and "integration" in err


class TestRetryAndOverride:
    def test_retry_previews_by_default(
        self, world: World, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _stopped(world)
        before = len(world.comments[PR])
        code, out, _ = _run(
            world, "retry", "--pr", str(PR), "--reason", "app fixed", capsys=capsys
        )
        assert code == 0 and "--apply" in out and "semantic-review-retry" in out
        assert len(world.comments[PR]) == before

    def test_retry_apply_posts_one_marker_and_the_gate_resumes(
        self, world: World, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _stopped(world)
        args = ("retry", "--pr", str(PR), "--reason", "app fixed", "--apply")
        code, _, _ = _run(world, *args, capsys=capsys)
        assert code == 0
        requests = _markers(world, parse_semantic_review_retry_marker)
        assert len(requests) == 1
        assert (requests[0].attempt_id, requests[0].head_sha) == (ATTEMPT, HEAD)
        assert requests[0].kind == "retry" and requests[0].actor == ALICE
        world.post(PR, world.comments[PR].pop()["body"], ALICE)  # as a person
        report = world.cycle()
        assert report["next_phase"] == "review_pending"
        assert len(world.triggers()) == 2

    def test_applying_twice_posts_once(
        self, world: World, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _stopped(world)
        args = ("retry", "--pr", str(PR), "--reason", "app fixed", "--apply")
        assert _run(world, *args, capsys=capsys)[0] == 0
        code, out, _ = _run(world, *args, capsys=capsys)
        assert code == 0 and "already posted" in out
        assert len(_markers(world, parse_semantic_review_retry_marker)) == 1

    def test_override_apply_posts_the_bound_marker_and_the_gate_skips(
        self, world: World, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _stopped(world)
        args = ("override", "--pr", str(PR), "--reason", "owner reviewed")
        assert _run(world, *args, capsys=capsys)[0] == 0
        assert _markers(world, parse_semantic_review_override_marker) == []
        assert _run(world, *args, "--apply", capsys=capsys)[0] == 0
        [override] = _markers(world, parse_semantic_review_override_marker)
        assert (override.attempt_id, override.head_sha) == (ATTEMPT, HEAD)
        assert override.reason == "owner reviewed"
        world.post(PR, world.comments[PR].pop()["body"], ALICE)
        assert world.cycle()["next_phase"] == "skipped_by_human"

    @pytest.mark.parametrize("command", ["retry", "override"])
    @pytest.mark.parametrize("reason", ["", "   "])
    def test_empty_reason_is_refused(
        self,
        world: World,
        capsys: pytest.CaptureFixture[str],
        command: str,
        reason: str,
    ) -> None:
        _stopped(world)
        code, _, err = _run(
            world,
            command,
            "--pr",
            str(PR),
            "--reason",
            reason,
            "--apply",
            capsys=capsys,
        )
        assert code == 1 and "reason" in err
        assert len(_markers(world, parse_semantic_review_retry_marker)) == 0

    @pytest.mark.parametrize("actor", ["bob", "orchestune[bot]"])
    def test_actor_without_write_access_or_a_bot_is_refused(
        self, world: World, capsys: pytest.CaptureFixture[str], actor: str
    ) -> None:
        _stopped(world)
        before = len(world.comments[PR])
        forge = _forge(world)
        forge.get_authenticated_user.return_value = actor
        code = review_cli.main(
            ["override", "--pr", str(PR), "--reason", "x", "--apply"],
            forge=forge,
            config=world.config,
            now=world.clock,
            repository_root=Path("."),
        )
        assert code == 1 and "write access" in capsys.readouterr().err
        assert len(world.comments[PR]) == before

    def test_retry_needs_an_escalated_attempt(
        self, world: World, capsys: pytest.CaptureFixture[str]
    ) -> None:
        world.seed()
        world.cycle()
        code, _, err = _run(
            world, "retry", "--pr", str(PR), "--reason", "x", "--apply", capsys=capsys
        )
        assert code == 1 and "escalated" in err

    def test_invalidated_attempt_is_refused(
        self, world: World, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _stopped(world)
        world.refs[f"parent/issue-{PARENT}"] = "e" * 40  # the base moved on
        world.cycle()
        assert world.state.phase.value == "invalidated"
        for command in ("retry", "override"):
            code, _, err = _run(
                world,
                command,
                "--pr",
                str(PR),
                "--reason",
                "x",
                "--apply",
                capsys=capsys,
            )
            assert code == 1 and "invalidated" in err

    def test_state_that_cannot_be_read_is_refused(
        self, world: World, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _stopped(world)
        world.comments[PR].clear()
        code, _, err = _run(
            world, "retry", "--pr", str(PR), "--reason", "x", "--apply", capsys=capsys
        )
        assert code == 1 and "state" in err

    def test_head_that_moved_is_refused(
        self, world: World, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _stopped(world)
        world.pr_head = FIXED
        code, _, err = _run(
            world,
            "override",
            "--pr",
            str(PR),
            "--reason",
            "x",
            "--apply",
            capsys=capsys,
        )
        assert code == 1 and "head" in err
        assert _markers(world, parse_semantic_review_override_marker) == []

    def test_two_active_attempts_are_ambiguous(
        self, world: World, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _stopped(world)
        forge = _forge(world)
        record = forge.list_open_prs.side_effect()[0]
        other = PrRecord(
            number=PR + 1,
            head_ref=IDENTITY.temp_branch,
            changed_files=(),
            base_ref=record.base_ref,
            is_cross_repository=False,
            head_sha=record.head_sha,
        )
        forge.list_open_prs.side_effect = lambda *a, **k: [record, other]
        world.comments[PR + 1] = [dict(c) for c in world.comments[PR]]
        code, _, err = _run(
            world, "retry", "--pr", str(PR), "--reason", "x", "--apply", capsys=capsys
        )
        assert code == 1 and "multiple" in err

    def test_apply_revalidates_right_before_posting(
        self, world: World, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _stopped(world)
        forge = _forge(world)
        calls = {"n": 0}

        def permission(user: str) -> str:
            calls["n"] += 1
            if calls["n"] > 1:  # the head moves after the first validation
                world.pr_head = FIXED
            return "write"

        forge.get_actor_permission.side_effect = permission
        before = len(world.comments[PR])
        code = review_cli.main(
            ["override", "--pr", str(PR), "--reason", "x", "--apply"],
            forge=forge,
            config=world.config,
            now=world.clock,
            repository_root=Path("."),
        )
        assert code == 1 and "head" in capsys.readouterr().err
        assert len(world.comments[PR]) == before


def test_root_cli_lists_and_routes_the_review_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from orchestune import cli

    seen: list[list[str]] = []

    def fake_main(argv: list[str] | None = None, **_: object) -> int:
        seen.append(sys.argv[1:])
        return 0

    monkeypatch.setattr(review_cli, "main", fake_main)
    monkeypatch.setattr("sys.argv", ["orchestune", "review", "status", "--pr", "3"])
    with pytest.raises(SystemExit) as stop:
        cli.main()
    assert stop.value.code == 0 and seen == [["status", "--pr", "3"]]
    monkeypatch.setattr("sys.argv", ["orchestune"])
    with pytest.raises(SystemExit):
        cli.main()
    assert "review" in capsys.readouterr().out

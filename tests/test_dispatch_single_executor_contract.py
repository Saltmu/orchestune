"""#821 §5: ローカルロックの保証範囲と起動予約の競合を決定的に固定する契約テスト。

本Issueで追加する競合テストは起動予約だけである。独立状態からの二重選出の
再現は #821 §6 の後続設計で扱う。sleep・実GitHub・実ランナーは使わない。
"""

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestune.dispatch import cycle_execution, dispatcher
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.launch import _persist_launch_history
from orchestune.dispatch.result import PhaseResult, PhaseStatus
from orchestune.infra.process_utils import is_run_state_lock_held, run_state_lock
from orchestune.issue_parsing import launch_history_from_body
from tests.conftest import FakeForge

TIMEOUT = 5


def _join_all(threads: list[threading.Thread]) -> None:
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(TIMEOUT)
        assert not thread.is_alive()


def test_distinct_run_state_paths_do_not_exclude_each_other(tmp_path):
    """ローカルロックはrun-state pathごとであり、別clone同士は排他しない。"""
    barrier = threading.Barrier(2, timeout=TIMEOUT)
    errors: list[BaseException] = []

    def hold(lock: Path) -> None:
        try:
            with run_state_lock(lock):
                barrier.wait()  # 両者がロック内にいなければ BrokenBarrierError
        except BaseException as exc:  # noqa: BLE001 - スレッド外へ集約する
            errors.append(exc)

    locks = []
    for name in ("clone_a", "clone_b"):
        (tmp_path / name).mkdir()
        locks.append(tmp_path / name / "run_state.lock")
    _join_all([threading.Thread(target=hold, args=(lock,)) for lock in locks])
    assert errors == []


def test_same_run_state_path_excludes_other_thread(tmp_path):
    """対照: 同じpathでは保持中に別スレッドの取得が拒否される。"""
    lock = tmp_path / "run_state.lock"
    held, release = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def holder() -> None:
        with run_state_lock(lock):
            held.set()
            release.wait(TIMEOUT)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert held.wait(TIMEOUT)
        with pytest.raises(RuntimeError), run_state_lock(lock, timeout=0):
            pass
    finally:
        release.set()
        thread.join(TIMEOUT)
    assert errors == [] and not thread.is_alive()


class _ReadThenWaitForge(FakeForge):
    """get_issue の後、両者が読み終わるまで書込へ進ませない。"""

    def __init__(self, barrier: threading.Barrier) -> None:
        super().__init__()
        self._barrier = barrier
        self.gated = False

    def get_issue(self, issue_number):
        issue = super().get_issue(issue_number)
        if self.gated:
            self._barrier.wait()
        return issue


def test_independent_launch_reservations_can_lose_one(tmp_path):
    """独立状態が同じ親本文を読んでから書くと、2予約の一方が失われる。"""
    barrier = threading.Barrier(2, timeout=TIMEOUT)
    forge = _ReadThenWaitForge(barrier)
    parent = forge.create_issue("[EPIC] p", "body\n", ())
    forge.gated = True
    errors: list[BaseException] = []

    def reserve(now: float, name: str) -> None:
        config = DispatcherConfig(
            parent_issue_number=parent,
            forge=forge,
            run_state_path=tmp_path / name / "run_state.json",
            events_log_path=tmp_path / name / "events.jsonl",
        )
        try:
            _persist_launch_history(now, config)
        except BaseException as exc:  # noqa: BLE001 - スレッド外へ集約する
            errors.append(exc)

    _join_all(
        [
            threading.Thread(target=reserve, args=(1000.0, "a")),
            threading.Thread(target=reserve, args=(1001.0, "b")),
        ]
    )
    assert errors == []
    history = launch_history_from_body(forge.issues[parent].body)
    assert len(history) == 1
    assert history[0] in {1000.0, 1001.0}


def test_execute_cycle_holds_run_state_lock_only_during_locked_cycle(
    tmp_path, monkeypatch
):
    """ロックはサイクル本体だけを覆い、execute_cycle から戻れば解放される。"""
    lock = tmp_path / "run_state.lock"
    seen: list[bool] = []
    sentinel = object()

    def fake_locked_cycle(api, config, collector=None):
        seen.append(is_run_state_lock_held(lock))
        return sentinel

    monkeypatch.setattr(cycle_execution, "execute_locked_cycle", fake_locked_cycle)
    config = DispatcherConfig(
        parent_issue_number=1,
        forge=FakeForge(),
        run_state_path=tmp_path / "run_state.json",
        events_log_path=tmp_path / "events.jsonl",
    )
    api = SimpleNamespace(run_state_lock=run_state_lock)
    assert cycle_execution.execute_cycle(api, config) is sentinel
    assert seen == [True]
    assert not is_run_state_lock_held(lock)


def test_run_state_lock_does_not_cover_post_cycle_steps(tmp_path, monkeypatch):
    """CLI全体はローカルロックで守られない: 後処理はロックの外で動く。"""
    lock = tmp_path / "run_state.lock"
    config = DispatcherConfig(
        parent_issue_number=1,
        forge=FakeForge(),
        run_state_path=tmp_path / "run_state.json",
        events_log_path=tmp_path / "events.jsonl",
        apply=True,
    )
    observed: dict[str, object] = {}

    def fake_cycle(cfg):
        with run_state_lock(lock):
            pass
        return SimpleNamespace(skips=[], scheduling_decisions=[], forge_warnings=[])

    def probe() -> PhaseResult:
        observed["held"] = is_run_state_lock_held(lock)
        outcome: list[bool] = []

        def other_thread() -> None:
            try:
                with run_state_lock(lock, timeout=0):
                    outcome.append(True)
            except RuntimeError:
                outcome.append(False)

        _join_all([threading.Thread(target=other_thread)])
        observed["other_thread_acquired"] = outcome
        return PhaseResult("probe", PhaseStatus.SUCCESS)

    monkeypatch.setattr(dispatcher, "run_dispatch_cycle", fake_cycle)
    monkeypatch.setattr(dispatcher, "_decide_semantic_review_enabled", lambda: False)
    monkeypatch.setattr(
        dispatcher, "_post_cycle_steps", lambda *a, **k: [("probe", probe, True)]
    )
    dispatcher._run_dispatcher(config)
    assert observed == {"held": False, "other_thread_acquired": [True]}

"""State storage, serialization, and locking for local trial (#1734, #1736)."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from orchestune.infra.json_state import write_json_atomic
from orchestune.infra.process_utils import FileLock, FileLockContentionError
from orchestune.local_trial.contracts import (
    TrialError,
    TrialStore,
    canonical_digest,
    decode_attempt,
    decode_state,
    encode_attempt,
    encode_state,
)
from orchestune.local_trial.models import (
    AttemptResult,
    Checkpoint,
    TrialPhase,
    TrialState,
)

_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class LocalTrialStore(TrialStore):
    """FileLock-based atomic storage for local trials."""

    def __init__(
        self,
        common_dir: str | Path,
        primary_root: str | Path,
    ) -> None:
        self.common_dir = Path(common_dir).resolve()
        self.primary_root = Path(primary_root).resolve()
        self.lock_path = self.common_dir / "orchestune" / "trial.lock"
        self.pointer_file = self.common_dir / "orchestune" / "trial-active.json"
        self._is_locked = False
        self._owner_thread: int | None = None
        self._thread_lock = threading.Lock()

    def _validate_id(self, name: str, value: str) -> str:
        if not isinstance(value, str) or not _ID_RE.fullmatch(value):
            raise TrialError(
                "invalid_input", f"{name} must be a 32-character lowercase hex string"
            )
        return value

    def _state_path(self, trial_id: str) -> Path:
        self._validate_id("trial_id", trial_id)
        trials_dir = (self.primary_root / ".orchestune" / "trials").resolve()
        trial_dir = (trials_dir / trial_id).resolve()
        try:
            trial_dir.relative_to(trials_dir)
            trial_dir.relative_to(self.primary_root)
        except ValueError:
            raise TrialError(
                "invalid_input", "trial_id escapes root directory"
            ) from None
        return trial_dir / "state.json"

    def _attempt_path(self, trial_id: str, attempt_id: str) -> Path:
        self._validate_id("trial_id", trial_id)
        self._validate_id("attempt_id", attempt_id)
        state_file = self._state_path(trial_id)
        attempts_dir = (state_file.parent / "attempts").resolve()
        target = (attempts_dir / f"{attempt_id}.json").resolve()
        try:
            target.relative_to(attempts_dir)
            target.relative_to(self.primary_root)
        except ValueError:
            raise TrialError(
                "invalid_input", "attempt_id escapes root directory"
            ) from None
        return target

    def _require_writer_lock(self) -> None:
        current_thread = threading.get_ident()
        if not self._is_locked or self._owner_thread != current_thread:
            raise TrialError(
                "already_active", "Lock must be held by the current thread for writing"
            )

    @contextmanager
    def lock(self) -> Iterator[None]:
        current_thread = threading.get_ident()
        with self._thread_lock:
            if self._is_locked:
                raise TrialError("already_active", "Trial lock is already held")
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        file_lock = FileLock(self.lock_path, timeout=0)
        try:
            file_lock.acquire()
        except FileLockContentionError as exc:
            raise TrialError("already_active", "Trial file lock contention") from exc
        with self._thread_lock:
            self._is_locked = True
            self._owner_thread = current_thread
        try:
            yield
        finally:
            with self._thread_lock:
                self._is_locked = False
                self._owner_thread = None
            file_lock.release()

    def active_trial_id(self) -> str | None:
        if not self.pointer_file.exists():
            return None
        try:
            raw = json.loads(self.pointer_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise TrialError(
                "state_invalid", f"Failed to parse active pointer JSON: {exc}"
            ) from exc
        if not isinstance(raw, dict) or set(raw.keys()) != {
            "schema_version",
            "trial_id",
            "seed",
        }:
            raise TrialError("state_invalid", "Invalid schema keys in active pointer")
        if raw["schema_version"] != 1:
            raise TrialError(
                "schema_unsupported",
                f"Unsupported schema version: {raw.get('schema_version')}",
            )
        tid = raw.get("trial_id")
        if not isinstance(tid, str) or not _ID_RE.fullmatch(tid):
            raise TrialError("state_invalid", "Invalid trial_id format in pointer")
        try:
            decode_state(raw.get("seed"))
        except Exception as exc:
            raise TrialError(
                "state_invalid", f"Invalid seed in pointer: {exc}"
            ) from exc
        return tid

    def reserve(self, state: TrialState) -> None:
        self._require_writer_lock()
        self._validate_id("trial_id", state.trial_id)
        if (
            state.phase != TrialPhase.PREPARING
            or state.checkpoint != Checkpoint.PREPARE
            or state.revision != 0
            or state.pending_attempt is not None
        ):
            raise TrialError(
                "invalid_input",
                "Reserve requires initial preparing/prepare state with revision 0",
            )
        current_active = self.active_trial_id()
        if current_active is not None:
            raise TrialError(
                "already_active", f"Trial {current_active} is already active"
            )
        pointer_data = {
            "schema_version": 1,
            "trial_id": state.trial_id,
            "seed": encode_state(state),
        }
        write_json_atomic(self.pointer_file, pointer_data)

    def load(self, trial_id: str) -> TrialState:
        self._validate_id("trial_id", trial_id)
        state_path = self._state_path(trial_id)
        if state_path.exists():
            try:
                raw = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise TrialError(
                    "state_invalid", f"Failed to parse state JSON: {exc}"
                ) from exc
            loaded = decode_state(raw)
            if loaded.trial_id != trial_id:
                raise TrialError(
                    "identity_mismatch",
                    f"State trial_id {loaded.trial_id} != requested {trial_id}",
                )
            return loaded
        active_id = self.active_trial_id()
        if active_id == trial_id:
            raw = json.loads(self.pointer_file.read_text(encoding="utf-8"))
            seed = decode_state(raw["seed"])
            if (
                seed.trial_id != trial_id
                or seed.phase != TrialPhase.PREPARING
                or seed.checkpoint != Checkpoint.PREPARE
                or seed.pending_attempt is not None
            ):
                raise TrialError(
                    "identity_mismatch",
                    "Pointer seed does not match valid preparing state",
                )
            return seed
        raise TrialError("not_found", f"Trial {trial_id} not found")

    def save(self, state: TrialState, *, expected_revision: int | None) -> None:
        self._require_writer_lock()
        self._validate_id("trial_id", state.trial_id)
        active_id = self.active_trial_id()
        if active_id != state.trial_id:
            raise TrialError(
                "state_invalid",
                f"Trial {state.trial_id} does not match active {active_id}",
            )
        state_path = self._state_path(state.trial_id)
        if expected_revision is None:
            self._save_initial(state, state_path)
        else:
            self._save_revision(state, state_path, expected_revision)

    def _save_initial(self, state: TrialState, state_path: Path) -> None:
        if state.revision != 0:
            raise TrialError(
                "stale_revision",
                f"Initial save requires revision=0, got {state.revision}",
            )
        if state_path.exists():
            existing = self.load(state.trial_id)
            if canonical_digest(encode_state(existing)) == canonical_digest(
                encode_state(state)
            ):
                return
            raise TrialError(
                "stale_revision", "Initial state exists with differing content"
            )
        write_json_atomic(state_path, encode_state(state))

    def _save_revision(
        self, state: TrialState, state_path: Path, expected_rev: int
    ) -> None:
        if not state_path.exists():
            raise TrialError(
                "not_found", f"State file not found for trial {state.trial_id}"
            )
        existing = self.load(state.trial_id)
        if existing.revision != expected_rev or state.revision != expected_rev + 1:
            raise TrialError(
                "stale_revision",
                f"Revision mismatch: expected {expected_rev}, current {existing.revision}, new {state.revision}",
            )
        write_json_atomic(state_path, encode_state(state))

    def record_attempt(self, result: AttemptResult) -> None:
        self._require_writer_lock()
        trial_id = result.input.trial_id
        attempt_id = result.input.attempt_id
        active_id = self.active_trial_id()
        if active_id != trial_id:
            raise TrialError(
                "state_invalid", f"Trial {trial_id} does not match active {active_id}"
            )
        current_state = self.load(trial_id)
        if (
            not current_state.pending_attempt
            or current_state.pending_attempt.attempt_id != attempt_id
        ):
            raise TrialError(
                "state_invalid", f"No matching pending attempt for {attempt_id}"
            )
        attempt_path = self._attempt_path(trial_id, attempt_id)
        if attempt_path.exists():
            existing = self.load_attempt(trial_id, attempt_id)
            if existing and canonical_digest(
                encode_attempt(existing)
            ) == canonical_digest(encode_attempt(result)):
                return
            raise TrialError(
                "state_invalid",
                f"Attempt {attempt_id} already exists with different content",
            )
        write_json_atomic(attempt_path, encode_attempt(result))

    def load_attempt(self, trial_id: str, attempt_id: str) -> AttemptResult | None:
        self._validate_id("trial_id", trial_id)
        self._validate_id("attempt_id", attempt_id)
        target = self._attempt_path(trial_id, attempt_id)
        if not target.exists():
            return None
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except Exception as exc:
            raise TrialError(
                "state_invalid", f"Failed to parse attempt JSON: {exc}"
            ) from exc
        return decode_attempt(raw)

    def close(self, state: TrialState) -> None:
        self._require_writer_lock()
        self._validate_id("trial_id", state.trial_id)
        if (
            state.decision is None
            or state.pending_attempt is not None
            or state.phase != TrialPhase.CLOSED
            or state.checkpoint != Checkpoint.CLOSED
        ):
            raise TrialError(
                "invalid_input",
                "State must be closed with decision and without pending attempts",
            )
        state_path = self._state_path(state.trial_id)
        if self.pointer_file.exists():
            active_id = self.active_trial_id()
            if active_id != state.trial_id:
                raise TrialError(
                    "identity_mismatch",
                    f"Active pointer {active_id} != closing {state.trial_id}",
                )
            self._persist_final_state(state, state_path)
            try:
                self.pointer_file.unlink()
            except FileNotFoundError:
                pass
        else:
            if state_path.exists():
                existing = self.load(state.trial_id)
                if canonical_digest(encode_state(existing)) == canonical_digest(
                    encode_state(state)
                ):
                    return
            raise TrialError("not_found", "No active trial to close")

    def _persist_final_state(self, state: TrialState, state_path: Path) -> None:
        if state_path.exists():
            existing = self.load(state.trial_id)
            if canonical_digest(encode_state(existing)) != canonical_digest(
                encode_state(state)
            ):
                if state.revision != existing.revision + 1:
                    raise TrialError(
                        "stale_revision",
                        f"Final state revision mismatch: expected {existing.revision + 1}, got {state.revision}",
                    )
                write_json_atomic(state_path, encode_state(state))
        else:
            write_json_atomic(state_path, encode_state(state))

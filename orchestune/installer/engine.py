from __future__ import annotations

import datetime
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestune.infra.process_utils import FileLock
from orchestune.installer.contracts import (
    BundlePayload,
    BundleState,
    ConflictError,
    PhysicalRoot,
)
from orchestune.installer.migration import migrate_legacy_skills
from orchestune.installer.state import (
    LOCK_FILENAME,
    InstallerManifest,
    get_installer_dir,
    inspect_bundle_state,
    load_manifest,
    save_manifest,
)
from orchestune.installer.transaction import (
    SkillTransaction,
    recover_pending_transactions,
)


@dataclass
class OperationResult:
    physical_root: Path
    bundle_name: str
    operation: str
    state_before: BundleState
    state_after: BundleState
    actions: list[str] = field(default_factory=list)
    success: bool = True
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "physical_root": str(self.physical_root),
            "bundle_name": self.bundle_name,
            "operation": self.operation,
            "state_before": self.state_before.value,
            "state_after": self.state_after.value,
            "actions": self.actions,
            "success": self.success,
            "error": self.error,
        }


def inspect_status(root: PhysicalRoot, payload: BundlePayload) -> OperationResult:
    state = inspect_bundle_state(root.path, payload)
    manifest = load_manifest(root.path)
    consumers = []
    if manifest and payload.bundle_name in manifest.bundles:
        consumers = manifest.bundles[payload.bundle_name].consumers

    return OperationResult(
        physical_root=root.path,
        bundle_name=payload.bundle_name,
        operation="status",
        state_before=state,
        state_after=state,
        actions=[f"status: {state.value} (consumers: {consumers})"],
        success=True,
    )


def _install_absent_bundle(
    root: PhysicalRoot,
    payload: BundlePayload,
    requested_consumers: list[str],
    state: BundleState,
) -> OperationResult:
    tx = SkillTransaction(root.path)
    with tx:
        current_state = inspect_bundle_state(root.path, payload)
        if current_state not in (BundleState.ABSENT, BundleState.RECOVERY_REQUIRED):
            raise ConflictError(
                f"Root state changed concurrently to '{current_state.value}'"
            )
        tx.prepare_stage(payload)
        manifest = load_manifest(root.path) or InstallerManifest()
        tx.commit(payload, manifest, consumers=requested_consumers)
    new_state = inspect_bundle_state(root.path, payload)
    return OperationResult(
        physical_root=root.path,
        bundle_name=payload.bundle_name,
        operation="install",
        state_before=state,
        state_after=new_state,
        actions=[f"installed {payload.bundle_name} bundle"],
        success=True,
    )


def _install_managed_current(
    root: PhysicalRoot,
    payload: BundlePayload,
    requested_consumers: list[str],
    state: BundleState,
) -> OperationResult:
    curr_manifest = load_manifest(root.path)
    assert curr_manifest is not None
    entry = curr_manifest.bundles[payload.bundle_name]
    combined = sorted(list(set(entry.consumers + requested_consumers)))
    actions = []
    if combined != entry.consumers:
        entry.consumers = combined
        curr_manifest.generation += 1
        curr_manifest.updated_at = datetime.datetime.now(datetime.UTC).isoformat()
        save_manifest(root.path, curr_manifest)
        actions.append(f"added consumers {requested_consumers}")
    else:
        actions.append("already up-to-date (no-op)")
    return OperationResult(
        physical_root=root.path,
        bundle_name=payload.bundle_name,
        operation="install",
        state_before=state,
        state_after=state,
        actions=actions,
        success=True,
    )


def _install_legacy_migration(
    root: PhysicalRoot,
    payload: BundlePayload,
    requested_consumers: list[str],
    state: BundleState,
    migrate_legacy: bool,
) -> OperationResult:
    migrate_legacy_skills(
        root.path,
        payload,
        consumers=requested_consumers,
        migrate_legacy=migrate_legacy,
    )
    new_state = inspect_bundle_state(root.path, payload)
    return OperationResult(
        physical_root=root.path,
        bundle_name=payload.bundle_name,
        operation="install",
        state_before=state,
        state_after=new_state,
        actions=[f"migrated legacy skills to {payload.bundle_name} bundle"],
        success=True,
    )


def _recover_if_needed(
    root: PhysicalRoot, payload: BundlePayload, state: BundleState
) -> BundleState:
    if state == BundleState.RECOVERY_REQUIRED:
        installer_dir = get_installer_dir(root.path)
        installer_dir.mkdir(parents=True, exist_ok=True)
        with FileLock(installer_dir / LOCK_FILENAME, timeout=10.0):
            recover_pending_transactions(root.path)
        return inspect_bundle_state(root.path, payload)
    return state


def install_skills(
    root: PhysicalRoot,
    payload: BundlePayload,
    dry_run: bool = False,
    migrate_legacy: bool = False,
) -> OperationResult:
    state = inspect_bundle_state(root.path, payload)
    requested_consumers = [t.value for t in root.target_types]

    if dry_run:
        return OperationResult(
            physical_root=root.path,
            bundle_name=payload.bundle_name,
            operation="install",
            state_before=state,
            state_after=state,
            actions=["dry_run_preview"],
            success=True,
        )

    state = _recover_if_needed(root, payload, state)

    if state == BundleState.ABSENT:
        return _install_absent_bundle(root, payload, requested_consumers, state)

    if state == BundleState.MANAGED_CURRENT:
        return _install_managed_current(root, payload, requested_consumers, state)

    if state == BundleState.MANAGED_OUTDATED:
        raise ConflictError(
            f"Bundle '{payload.bundle_name}' at {root.path} is managed but outdated. "
            "Use 'orchestune skills update' to upgrade."
        )

    if state in (BundleState.LEGACY_LINK, BundleState.LEGACY_COPY):
        return _install_legacy_migration(
            root, payload, requested_consumers, state, migrate_legacy
        )

    raise ConflictError(
        f"Cannot install '{payload.bundle_name}' to {root.path}: root is in conflict state '{state.value}'."
    )


def _perform_update(
    root: PhysicalRoot, payload: BundlePayload, state: BundleState
) -> OperationResult:
    tx = SkillTransaction(root.path)
    with tx:
        current_state = inspect_bundle_state(root.path, payload)
        if current_state not in (
            BundleState.MANAGED_OUTDATED,
            BundleState.RECOVERY_REQUIRED,
        ):
            raise ConflictError(
                f"Root state changed concurrently to '{current_state.value}'"
            )
        manifest = load_manifest(root.path)
        assert manifest is not None
        existing_consumers = manifest.bundles[payload.bundle_name].consumers
        tx.prepare_stage(payload)
        tx.commit(payload, manifest, consumers=existing_consumers)
    new_state = inspect_bundle_state(root.path, payload)
    return OperationResult(
        physical_root=root.path,
        bundle_name=payload.bundle_name,
        operation="update",
        state_before=state,
        state_after=new_state,
        actions=[f"updated {payload.bundle_name} bundle to {payload.package_version}"],
        success=True,
    )


def update_skills(
    root: PhysicalRoot,
    payload: BundlePayload,
    dry_run: bool = False,
) -> OperationResult:
    state = inspect_bundle_state(root.path, payload)

    if dry_run:
        return OperationResult(
            physical_root=root.path,
            bundle_name=payload.bundle_name,
            operation="update",
            state_before=state,
            state_after=state,
            actions=["dry_run_preview"],
            success=True,
        )

    state = _recover_if_needed(root, payload, state)

    if state == BundleState.ABSENT:
        raise ConflictError(
            f"Bundle '{payload.bundle_name}' is not installed at {root.path}. "
            "Use 'orchestune skills install' first."
        )

    if state == BundleState.MODIFIED:
        raise ConflictError(
            f"Skills in '{payload.bundle_name}' at {root.path} have been modified by user. "
            "Refusing to overwrite."
        )

    if state == BundleState.MANAGED_CURRENT:
        return OperationResult(
            physical_root=root.path,
            bundle_name=payload.bundle_name,
            operation="update",
            state_before=state,
            state_after=state,
            actions=["already current (no-op)"],
            success=True,
        )

    if state == BundleState.MANAGED_OUTDATED:
        return _perform_update(root, payload, state)

    raise ConflictError(f"Cannot update: root is in conflict state '{state.value}'")


def _uninstall_consumer_or_bundle(
    root: PhysicalRoot,
    payload: BundlePayload,
    manifest: InstallerManifest,
    target_consumers: list[str],
) -> list[str]:
    entry = manifest.bundles[payload.bundle_name]
    remaining_consumers = [c for c in entry.consumers if c not in target_consumers]

    actions: list[str] = []
    if remaining_consumers:
        entry.consumers = remaining_consumers
        manifest.generation += 1
        manifest.updated_at = datetime.datetime.now(datetime.UTC).isoformat()
        save_manifest(root.path, manifest)
        actions.append(
            f"removed consumers {target_consumers}, remaining: {remaining_consumers}"
        )
    else:
        # Deregister bundle from manifest first to avoid leaving manifest in MODIFIED state if crash
        del manifest.bundles[payload.bundle_name]
        manifest.generation += 1
        manifest.updated_at = datetime.datetime.now(datetime.UTC).isoformat()
        save_manifest(root.path, manifest)

        for skill_name in entry.skills.keys():
            s_path = root.path / skill_name
            if s_path.is_dir() and not s_path.is_symlink():
                shutil.rmtree(s_path)
            elif s_path.exists() or s_path.is_symlink():
                s_path.unlink()

        actions.append(f"removed {payload.bundle_name} bundle files and deregistered")
    return actions


def _noop_uninstall(
    root: PhysicalRoot, payload: BundlePayload, state: BundleState, action: str
) -> OperationResult:
    return OperationResult(
        physical_root=root.path,
        bundle_name=payload.bundle_name,
        operation="uninstall",
        state_before=state,
        state_after=state,
        actions=[action],
        success=True,
    )


def uninstall_skills(
    root: PhysicalRoot,
    payload: BundlePayload,
    dry_run: bool = False,
) -> OperationResult:
    state = inspect_bundle_state(root.path, payload)
    target_consumers = [t.value for t in root.target_types]

    if dry_run:
        return _noop_uninstall(root, payload, state, "dry_run_preview")

    if state == BundleState.ABSENT:
        return _noop_uninstall(root, payload, state, "not installed (no-op)")

    if state == BundleState.MODIFIED:
        raise ConflictError(
            f"Skills in '{payload.bundle_name}' at {root.path} have been modified by user. "
            "Refusing to uninstall automatically."
        )

    installer_dir = get_installer_dir(root.path)
    installer_dir.mkdir(parents=True, exist_ok=True)
    with FileLock(installer_dir / LOCK_FILENAME, timeout=10.0):
        recover_pending_transactions(root.path)
        manifest = load_manifest(root.path)
        if not manifest or payload.bundle_name not in manifest.bundles:
            return _noop_uninstall(root, payload, state, "unmanaged (no-op)")

        actions = _uninstall_consumer_or_bundle(
            root, payload, manifest, target_consumers
        )

    new_state = inspect_bundle_state(root.path, payload)
    return OperationResult(
        physical_root=root.path,
        bundle_name=payload.bundle_name,
        operation="uninstall",
        state_before=state,
        state_after=new_state,
        actions=actions,
        success=True,
    )

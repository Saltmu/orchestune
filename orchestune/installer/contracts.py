from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class TargetType(str, Enum):
    CODEX = "codex"
    CLAUDE = "claude"
    ANTIGRAVITY = "antigravity"
    ANTIGRAVITY_CLI = "antigravity-cli"
    ALL = "all"


class ScopeType(str, Enum):
    PROJECT = "project"
    USER = "user"


class OperationType(str, Enum):
    INSTALL = "install"
    UPDATE = "update"
    UNINSTALL = "uninstall"
    STATUS = "status"
    DOCTOR = "doctor"


class BundleState(str, Enum):
    ABSENT = "absent"
    MANAGED_CURRENT = "managed-current"
    MANAGED_OUTDATED = "managed-outdated"
    MODIFIED = "modified"
    UNMANAGED = "unmanaged"
    LEGACY_LINK = "legacy-link"
    LEGACY_COPY = "legacy-copy"
    BROKEN_LINK = "broken-link"
    STATE_INVALID = "state-invalid"
    RECOVERY_REQUIRED = "recovery-required"


class InstallerExitCode(int, Enum):
    OK = 0
    ERROR = 1
    CONTRACT_ERROR = 2
    CONFLICT = 3


class InstallerError(Exception):
    """Base class for installer errors."""

    exit_code: InstallerExitCode = InstallerExitCode.ERROR


class TargetResolutionError(InstallerError):
    exit_code = InstallerExitCode.CONTRACT_ERROR


class PayloadError(InstallerError):
    exit_code = InstallerExitCode.CONTRACT_ERROR


class ManifestError(InstallerError):
    exit_code = InstallerExitCode.CONTRACT_ERROR


class TransactionError(InstallerError):
    exit_code = InstallerExitCode.ERROR


class ConflictError(InstallerError):
    exit_code = InstallerExitCode.CONFLICT


@dataclass(frozen=True)
class PhysicalRoot:
    path: Path
    target_types: list[TargetType]
    scope: ScopeType
    is_custom_skills_dir: bool = False


@dataclass(frozen=True)
class FileRecord:
    relative_path: str
    sha256: str
    mode: str = "regular"


@dataclass(frozen=True)
class SkillPayload:
    name: str
    files: dict[str, FileRecord]
    directories: list[str]


@dataclass(frozen=True)
class BundlePayload:
    bundle_name: str
    package_version: str
    source_kind: str
    bundle_digest: str
    skills: dict[str, SkillPayload]
    source_path: Path


@dataclass
class DiagnosticItem:
    check_name: str
    status: str  # ok, warning, error, not_checked
    message: str
    details: dict[str, Any] = field(default_factory=dict)

"""TOML document manipulation, comments preservation, and snapshot management."""

from __future__ import annotations

import difflib
import hashlib
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import tomlkit

from orchestune.dag.models import ConfigError

PROHIBITED_KEYS = frozenset(
    {
        "routine_token",
        "routine-token",
        "parent_issue",
        "parent-issue",
        "parent_issue_number",
        "parent-issue-number",
    }
)


@dataclass(frozen=True)
class ConfigSnapshot:
    path: Path
    exists: bool
    is_symlink: bool
    sha256: str | None = None
    mtime_ns: int | None = None
    size: int | None = None
    inode: int | None = None
    mode: int | None = None
    raw_bytes: bytes | None = None
    source_snapshot: ConfigSnapshot | None = None


def _snapshot_non_regular(
    path: Path,
    st: os.stat_result,
    is_symlink: bool,
    source_snapshot: ConfigSnapshot | None,
) -> ConfigSnapshot:
    return ConfigSnapshot(
        path=path,
        exists=True,
        is_symlink=is_symlink,
        mtime_ns=st.st_mtime_ns,
        size=st.st_size if not is_symlink else None,
        inode=st.st_ino,
        mode=st.st_mode,
        source_snapshot=source_snapshot,
    )


def create_snapshot(
    path: Path, source_snapshot: ConfigSnapshot | None = None
) -> ConfigSnapshot:
    """Create a point-in-time snapshot of the given file path."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return ConfigSnapshot(
            path=path,
            exists=False,
            is_symlink=False,
            source_snapshot=source_snapshot,
        )
    except OSError as exc:
        raise ConfigError(f"failed to inspect {path}: {exc}") from exc

    is_symlink = os.path.islink(path)
    if is_symlink or not path.is_file():
        return _snapshot_non_regular(path, st, is_symlink, source_snapshot)

    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"failed to read {path}: {exc}") from exc

    return ConfigSnapshot(
        path=path,
        exists=True,
        is_symlink=False,
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        mtime_ns=st.st_mtime_ns,
        size=st.st_size,
        inode=st.st_ino,
        mode=st.st_mode,
        raw_bytes=raw_bytes,
        source_snapshot=source_snapshot,
    )


def _verify_current_file(
    snap: ConfigSnapshot, st: os.stat_result, is_link: bool
) -> tuple[bool, str]:
    if snap.is_symlink != is_link:
        return False, f"File '{snap.path}' link type changed after reading"
    if is_link:
        if st.st_mtime_ns != snap.mtime_ns or st.st_ino != snap.inode:
            return False, f"Symlink '{snap.path}' was modified after reading"
        return True, ""

    if (
        st.st_size != snap.size
        or st.st_ino != snap.inode
        or st.st_mtime_ns != snap.mtime_ns
    ):
        return False, f"File '{snap.path}' metadata changed after reading"

    try:
        current_bytes = snap.path.read_bytes()
    except OSError as exc:
        return False, f"Failed to read '{snap.path}' for verification: {exc}"

    if hashlib.sha256(current_bytes).hexdigest() != snap.sha256:
        return False, f"File '{snap.path}' content changed after reading"
    return True, ""


def verify_snapshot_consistency(snap: ConfigSnapshot) -> tuple[bool, str]:
    """Check if the filesystem state matches the given snapshot."""
    if snap.source_snapshot is not None:
        source_ok, source_reason = verify_snapshot_consistency(snap.source_snapshot)
        if not source_ok:
            return (
                False,
                f"Source configuration file '{snap.source_snapshot.path}' changed: {source_reason}",
            )

    try:
        st = os.lstat(snap.path)
        current_exists = True
        current_is_link = os.path.islink(snap.path)
    except FileNotFoundError:
        current_exists = False
        current_is_link = False
        st = None
    except OSError as exc:
        return False, f"Failed to inspect '{snap.path}': {exc}"

    if not snap.exists:
        if current_exists:
            return (
                False,
                f"File '{snap.path}' was created by another process after reading",
            )
        return True, ""

    if not current_exists or st is None:
        return (
            False,
            f"File '{snap.path}' was deleted by another process after reading",
        )

    return _verify_current_file(snap, st, current_is_link)


class ConfigDocument:
    """Wrapper around tomlkit document for safe comment-preserving edits."""

    def __init__(
        self,
        doc: tomlkit.TOMLDocument,
        source_type: Literal["orchestune.toml", "pyproject.toml", "empty"],
        source_path: Path | None,
        target_path: Path,
        snapshot: ConfigSnapshot,
    ):
        self.doc = doc
        self.source_type = source_type
        self.source_path = source_path
        self.target_path = target_path
        self.snapshot = snapshot

    @classmethod
    def _load_existing_target(
        cls,
        target_path: Path,
        target_snap: ConfigSnapshot,
    ) -> ConfigDocument:
        if not target_path.is_file():
            raise ConfigError(f"'{target_path}' is not a regular file")

        assert target_snap.raw_bytes is not None
        try:
            text = target_snap.raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigError(f"'{target_path}' is not valid UTF-8: {exc}") from exc

        try:
            doc = tomlkit.parse(text)
        except Exception as exc:
            raise ConfigError(f"'{target_path}' has TOML syntax errors: {exc}") from exc

        try:
            ref_dict = tomllib.loads(text)
        except Exception as exc:
            raise ConfigError(
                f"'{target_path}' cannot be parsed by tomllib: {exc}"
            ) from exc

        if doc.unwrap() != ref_dict:
            raise ConfigError(
                f"'{target_path}' semantic structure mismatch between tomlkit and tomllib"
            )

        return cls(
            doc=doc,
            source_type="orchestune.toml",
            source_path=target_path,
            target_path=target_path,
            snapshot=target_snap,
        )

    @classmethod
    def _extract_pyproject_subtree(
        cls,
        pyproject_path: Path,
        pyproject_text: str,
        target_path: Path,
        pyproject_snap: ConfigSnapshot,
    ) -> ConfigDocument | None:
        try:
            parsed_pyproject = tomlkit.parse(pyproject_text)
        except Exception as exc:
            raise ConfigError(
                f"'{pyproject_path}' has TOML syntax errors: {exc}"
            ) from exc

        tool_table = parsed_pyproject.get("tool")
        if not (
            tool_table is not None
            and isinstance(tool_table, dict)
            and "orchestune" in tool_table
        ):
            return None

        subtree = tool_table["orchestune"]
        new_doc = tomlkit.document()
        if hasattr(subtree, "value") and hasattr(subtree.value, "body"):
            for k, v in subtree.value.body:
                new_doc.append(k, v)
        else:
            for key, item in subtree.items():
                new_doc.add(key, item)

        ref_subtree = (
            tomllib.loads(pyproject_text).get("tool", {}).get("orchestune", {})
        )
        if new_doc.unwrap() != ref_subtree:
            raise ConfigError(
                f"Failed to migrate subtree from '{pyproject_path}': semantic mismatch"
            )

        full_snap = create_snapshot(target_path, source_snapshot=pyproject_snap)
        return cls(
            doc=new_doc,
            source_type="pyproject.toml",
            source_path=pyproject_path,
            target_path=target_path,
            snapshot=full_snap,
        )

    @classmethod
    def _load_migrated_pyproject(
        cls,
        pyproject_path: Path,
        target_path: Path,
    ) -> ConfigDocument | None:
        if not (pyproject_path.exists() or os.path.islink(pyproject_path)):
            return None

        pyproject_snap = create_snapshot(pyproject_path)
        if pyproject_snap.is_symlink:
            raise ConfigError(
                f"'{pyproject_path}' is a symlink; please specify actual project directory"
            )
        if not pyproject_path.is_file():
            raise ConfigError(f"'{pyproject_path}' is not a regular file")

        assert pyproject_snap.raw_bytes is not None
        try:
            pyproject_text = pyproject_snap.raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigError(f"'{pyproject_path}' is not valid UTF-8: {exc}") from exc

        return cls._extract_pyproject_subtree(
            pyproject_path, pyproject_text, target_path, pyproject_snap
        )

    @classmethod
    def load(
        cls,
        project_dir: Path,
        mode: Literal["init", "edit"],
    ) -> ConfigDocument:
        target_path = project_dir / "orchestune.toml"
        pyproject_path = project_dir / "pyproject.toml"

        target_snap = create_snapshot(target_path)
        if target_snap.is_symlink:
            raise ConfigError(
                f"'{target_path}' is a symlink; symlinks are not supported for configuration"
            )

        if target_snap.exists:
            if mode == "init":
                raise ConfigError(
                    f"'{target_path}' already exists; use 'config edit' to edit existing configuration"
                )
            return cls._load_existing_target(target_path, target_snap)

        if mode == "edit":
            raise ConfigError(
                f"'{target_path}' does not exist; use 'config init' to create a new configuration"
            )

        migrated = cls._load_migrated_pyproject(pyproject_path, target_path)
        if migrated is not None:
            return migrated

        return cls(
            doc=tomlkit.document(),
            source_type="empty",
            source_path=None,
            target_path=target_path,
            snapshot=target_snap,
        )

    def _find_key(self, key: str) -> str | None:
        """Find matching key in document respecting hyphen vs underscore variants."""
        if key in self.doc:
            return key
        alt_key = key.replace("_", "-") if "_" in key else key.replace("-", "_")
        if alt_key in self.doc:
            return alt_key
        return None

    def get_value(self, key: str) -> Any:
        """Retrieve existing value unwrapped, or None."""
        matched = self._find_key(key)
        if matched is None:
            return None
        val = self.doc[matched]
        if hasattr(val, "unwrap"):
            return val.unwrap()
        return val

    def set_value(self, key: str, value: Any) -> None:
        """Set value preserving existing hyphen/underscore key format."""
        matched = self._find_key(key)
        target_key = matched if matched is not None else key

        if isinstance(value, dict):
            # table
            tbl = tomlkit.table()
            for k, v in value.items():
                tbl[k] = v
            self.doc[target_key] = tbl
        elif isinstance(value, list):
            arr = tomlkit.array()
            for item in value:
                arr.append(item)
            self.doc[target_key] = arr
        else:
            self.doc[target_key] = value

    def delete_value(self, key: str) -> bool:
        """Delete key if present. Returns True if deleted."""
        matched = self._find_key(key)
        if matched is not None:
            del self.doc[matched]
            return True
        return False

    def to_toml_string(self) -> str:
        """Serialize document to TOML string."""
        return tomlkit.dumps(self.doc)

    def generate_diff(self, new_toml_string: str) -> str:
        """Generate unified diff against original target file content with secret sanitization."""
        orig_text = ""
        orig_label = "original"
        if self.snapshot.raw_bytes is not None:
            orig_text = self.snapshot.raw_bytes.decode("utf-8", errors="replace")
            orig_label = str(self.target_path)
        elif self.source_type == "pyproject.toml":
            orig_label = f"{self.source_path} [tool.orchestune]"

        orig_lines = [
            self._sanitize_line(line) for line in orig_text.splitlines(keepends=True)
        ]
        new_lines = [
            self._sanitize_line(line)
            for line in new_toml_string.splitlines(keepends=True)
        ]

        diff = difflib.unified_diff(
            orig_lines,
            new_lines,
            fromfile=orig_label,
            tofile=str(self.target_path),
        )
        return "".join(diff)

    @staticmethod
    def _sanitize_line(line: str) -> str:
        stripped = line.strip()
        for secret_key in PROHIBITED_KEYS:
            if stripped.startswith(secret_key) and ("=" in stripped or ":" in stripped):
                prefix, _, _ = line.partition("=")
                return f"{prefix}= '[REDACTED]'\n"
        return line

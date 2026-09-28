"""Protected, atomic local credential storage shared by claim and completion."""

from __future__ import annotations

import os
import re
from pathlib import Path

_SAFE_CLAIM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _token_record_path(token_dir: Path, claim_id: str) -> Path:
    if not _SAFE_CLAIM_ID.fullmatch(claim_id):
        raise ValueError("claim ID contains unsafe characters")
    return token_dir / f"{claim_id}.token"


def _write_owner_token(token_dir: Path, claim_id: str, owner_token: str) -> None:
    """Atomically store a resume token with owner-only directory and file modes."""
    token_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(token_dir, 0o700)
    target = _token_record_path(token_dir, claim_id)
    temporary = target.with_suffix(".token.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{owner_token}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_owner_token(token_dir: Path, claim_id: str) -> str | None:
    path = _token_record_path(token_dir, claim_id)
    try:
        # Windows file modes do not represent the ACL that protects this token.
        if os.name != "nt" and path.stat().st_mode & 0o077:
            return None
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None

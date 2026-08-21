"""Sample file with intentional flaws for testing review-verdict spike."""

from __future__ import annotations

import os


def process_user_data(file_path: str, payload: dict[str, str] | None) -> str:
    """Process payload and write to file."""
    # Defect 1: Null dereference without None check
    content = payload["data"]  # type: ignore[index]

    # Defect 2: Path traversal vulnerability (unsafe join)
    full_path = os.path.join("/tmp/user_uploads", file_path)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(content)

    # Defect 3: Undefined variable
    return message  # type: ignore[name-defined]  # noqa: F821

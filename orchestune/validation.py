"""Input validation helpers shared by Forge and GitHub adapters."""

from __future__ import annotations

import re

_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_:.-]*$")
_REF_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]*$")
_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\\[\\]-]*$")


def validate_issue_number(value: int | str) -> int:
    text = str(value)
    if not re.fullmatch(r"[0-9]+", text) or int(text) <= 0:
        raise ValueError(f"issue番号が不正です: {value!r}")
    return int(text)


def validate_label(label: str) -> str:
    if not label or not _LABEL_PATTERN.match(label):
        raise ValueError(f"ラベル名が不正です: {label!r}")
    return label


def validate_ref_name(ref: str) -> str:
    if (
        not ref
        or not _REF_NAME_PATTERN.match(ref)
        or ref.startswith("-")
        or ".." in ref
    ):
        raise ValueError(f"ブランチ名が不正です: {ref!r}")
    return ref


def validate_username(username: str) -> str:
    if not username or not _USERNAME_PATTERN.match(username):
        raise ValueError(f"ユーザー名が不正です: {username!r}")
    return username

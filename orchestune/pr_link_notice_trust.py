"""Trust evaluation for PR link notice markers (#686).

Separates marker presence from author trustworthiness.
Markers are evaluated to one of three states:
- PRESENT: Marker posted by the authenticated user or a collaborator with triage+ permissions.
- ABSENT: No markers, or all marker authors confirmed untrusted.
- UNKNOWN: Marker present but authenticity cannot be verified (identity unresolved,
  collaborators API unavailable, or author unknown).
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum
from typing import Any

REASON_IDENTITY_UNRESOLVED = "identity_unresolved"
REASON_COLLABORATORS_UNAVAILABLE = "collaborators_unavailable"
REASON_AUTHOR_UNKNOWN = "author_unknown"


class NoticePresence(Enum):
    """Presence state of a verified PR link notice."""

    ABSENT = "ABSENT"
    PRESENT = "PRESENT"
    UNKNOWN = "UNKNOWN"


class TrustContext:
    """Session/batch scoped context caching identity and collaborators for notice verification."""

    def __init__(self, forge: Any) -> None:
        self._forge = forge
        self._self_identity: str | None = None
        self._self_identity_resolved: bool = False
        self._collaborators: frozenset[str] | None = None
        self._collaborators_attempted: bool = False
        self._last_deferred_reason: str | None = None

    @property
    def last_deferred_reason(self) -> str | None:
        return self._last_deferred_reason

    def get_self_identity(self) -> str | None:
        """Resolve and cache the authenticated user login (GET /user)."""
        if not self._self_identity_resolved:
            try:
                self._self_identity = self._forge.get_authenticated_user()
            except Exception:
                self._self_identity = None
            self._self_identity_resolved = True
        return self._self_identity

    def get_trusted_collaborators(self) -> frozenset[str] | None:
        """Resolve and cache the trusted collaborator logins lazily."""
        if not self._collaborators_attempted:
            self._collaborators_attempted = True
            try:
                self._collaborators = self._forge.list_notice_trusted_authors()
            except Exception:
                self._collaborators = None
        return self._collaborators

    @staticmethod
    def extract_author(comment: dict[str, Any]) -> str | None:
        """Extract author login from REST comment object or legacy comment dict."""
        user = comment.get("user")
        if isinstance(user, dict):
            login = user.get("login")
            if isinstance(login, str) and login.strip():
                return login.strip()
        author = comment.get("author")
        if isinstance(author, str) and author.strip():
            return author.strip()
        if isinstance(author, dict):
            login = author.get("login")
            if isinstance(login, str) and login.strip():
                return login.strip()
        return None

    def evaluate_candidate_markers(
        self, candidate_comments: Sequence[dict[str, Any]]
    ) -> tuple[NoticePresence, str | None]:
        """Evaluate a sequence of comments containing candidate notice markers.

        Returns (NoticePresence, deferred_reason).
        """
        if not candidate_comments:
            return NoticePresence.ABSENT, None

        self_login = self.get_self_identity()
        has_unknown = False
        last_reason: str | None = None

        for comment in candidate_comments:
            author = self.extract_author(comment)
            if not author:
                has_unknown = True
                last_reason = REASON_AUTHOR_UNKNOWN
                continue

            # Exact match with self identity (casefold)
            if self_login and author.casefold() == self_login.casefold():
                self._last_deferred_reason = None
                return NoticePresence.PRESENT, None

            # Not self (or self unknown): lazy-fetch collaborators
            collaborators = self.get_trusted_collaborators()
            if collaborators is None:
                has_unknown = True
                last_reason = REASON_COLLABORATORS_UNAVAILABLE
                continue

            # Check if collaborator (casefold)
            is_collab = any(author.casefold() == c.casefold() for c in collaborators)
            if is_collab:
                self._last_deferred_reason = None
                return NoticePresence.PRESENT, None

            # Author is not in trusted collaborators
            if not self_login:
                # Without self identity, we cannot prove this untrusted author isn't us
                has_unknown = True
                last_reason = REASON_IDENTITY_UNRESOLVED
            else:
                # Definitely not us, and definitely not a trusted collaborator: UNTRUSTED
                pass

        if has_unknown:
            self._last_deferred_reason = last_reason
            return NoticePresence.UNKNOWN, last_reason

        self._last_deferred_reason = None
        return NoticePresence.ABSENT, None


__all__ = [
    "NoticePresence",
    "REASON_AUTHOR_UNKNOWN",
    "REASON_COLLABORATORS_UNAVAILABLE",
    "REASON_IDENTITY_UNRESOLVED",
    "TrustContext",
]

"""Issue-comment API for Forge doubles that hold the integration retry budget (#820).

The Integrator applies only with a Forge that declares bounded execution and keeps its
retry-budget events as parent Issue comments, so every Forge double used with
``Integrator.run()`` needs both.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock


def install_event_comment_store(forge: MagicMock) -> list[dict[str, Any]]:
    """Back ``forge`` with an in-memory Issue-comment list; return that list."""
    forge.supports_bounded_execution = True
    event_comments: list[dict[str, Any]] = []

    def list_all_issue_comments(issue_number: int | str) -> list[dict[str, Any]]:
        return [
            dict(comment)
            for comment in event_comments
            if comment["issue_number"] == int(issue_number)
        ]

    def create_issue_comment(
        issue_number: int | str,
        body: str,
        author: str | None = None,
        user_type: str | None = None,
    ) -> dict[str, Any]:
        user_dict: dict[str, Any] = {
            "login": author or getattr(forge, "_authenticated_user", "bot")
        }
        if user_type is not None:
            user_dict["type"] = user_type
        comment = {
            "id": len(event_comments) + 1,
            "issue_number": int(issue_number),
            "body": body,
            "html_url": f"https://example.test/{issue_number}#c{len(event_comments) + 1}",
            "user": user_dict,
        }
        event_comments.append(comment)
        return dict(comment)

    forge.list_all_issue_comments = MagicMock(side_effect=list_all_issue_comments)
    forge.create_issue_comment = MagicMock(side_effect=create_issue_comment)
    forge.event_comments = event_comments
    if not hasattr(forge, "list_notice_trusted_authors"):
        forge.trusted_authors = frozenset({"bot"})
        forge.list_notice_trusted_authors = MagicMock(
            side_effect=lambda: forge.trusted_authors
        )
    return event_comments


class EventCommentForgeMixin:
    """Issue-comment API for in-memory Forge doubles; declares bounded execution."""

    supports_bounded_execution = True

    @property
    def event_comments(self) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = self.__dict__.setdefault("_event_comments", [])
        return comments

    def get_authenticated_user(self) -> str:
        return getattr(self, "_authenticated_user", "bot")

    def set_authenticated_user(self, user: str) -> None:
        self._authenticated_user = user

    @property
    def trusted_authors(self) -> frozenset[str]:
        return getattr(self, "_trusted_authors", frozenset({"bot"}))

    @trusted_authors.setter
    def trusted_authors(self, value: frozenset[str] | set[str]) -> None:
        self._trusted_authors = frozenset(value)

    def list_notice_trusted_authors(self) -> frozenset[str]:
        return self.trusted_authors

    def list_all_issue_comments(self, issue_number: int | str) -> list[dict[str, Any]]:
        return [
            dict(comment)
            for comment in self.event_comments
            if comment["issue_number"] == int(issue_number)
        ]

    def create_issue_comment(
        self,
        issue_number: int | str,
        body: str,
        author: str | None = None,
        user_type: str | None = None,
    ) -> dict[str, Any]:
        user_dict: dict[str, Any] = {"login": author or self.get_authenticated_user()}
        if user_type is not None:
            user_dict["type"] = user_type
        comment = {
            "id": len(self.event_comments) + 1,
            "issue_number": int(issue_number),
            "body": body,
            "user": user_dict,
        }
        self.event_comments.append(comment)
        return dict(comment)

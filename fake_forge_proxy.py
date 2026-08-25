"""Per-test holder used while migrating dispatch tests to the shared fake forge."""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock


class ActiveFakeForge:
    """Expose the current fake's methods to legacy, scoped test configurations."""

    forge: MagicMock | None = None

    def __getattr__(self, name: str) -> MagicMock:
        assert self.forge is not None, "fake_forge fixture has not been initialized"
        return cast(MagicMock, getattr(self.forge, name))

    def __setattr__(self, name: str, value: MagicMock | None) -> None:
        if name == "forge":
            object.__setattr__(self, name, value)
            return
        assert self.forge is not None, "fake_forge fixture has not been initialized"
        setattr(self.forge, name, value)

    def __delattr__(self, name: str) -> None:
        assert self.forge is not None, "fake_forge fixture has not been initialized"
        delattr(self.forge, name)


active_fake_forge = ActiveFakeForge()

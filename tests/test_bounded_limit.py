"""Tests for shared bounded-limit decisions."""

import pytest

from orchestune.bounded_limit import exceeds_limit


@pytest.mark.parametrize(
    ("value", "limit", "expected"),
    [
        (1, 2, False),
        (2, 2, False),
        (3, 2, True),
    ],
)
def test_exceeds_limit_only_after_the_configured_limit(value, limit, expected):
    assert exceeds_limit(value, limit) is expected

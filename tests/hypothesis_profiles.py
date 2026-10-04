"""Hypothesis profile for the stateful status-machine tests (#1217).

Loaded through `pytest_plugins` in `tests/conftest.py`. `print_blob` makes a
failing run print the `@reproduce_failure` blob; `HYPOTHESIS_PROFILE` may select
another registered profile (for example to try more examples).
"""

from __future__ import annotations

import os

from hypothesis import settings

settings.register_profile(
    "ci",
    max_examples=100,
    stateful_step_count=30,
    deadline=None,
    print_blob=True,
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))

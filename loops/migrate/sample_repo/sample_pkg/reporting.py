"""A second caller of the deprecated helper, to show multi-file migration.

The migrate loop emits one Candidate per file that matches the deprecated
pattern, so this file is migrated independently of ``currency.py``. Behavior
(the running total) is pinned by the tests.
"""
from __future__ import annotations

from .helpers import old_api


def summarize(values: list[int]) -> int:
    """Return the sum of each value doubled via the deprecated helper."""
    total = 0
    for v in values:
        total += old_api(v)
    return total

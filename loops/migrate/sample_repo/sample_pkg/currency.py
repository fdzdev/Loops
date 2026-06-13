"""A caller that uses the deprecated helper.

This module computes a doubled amount via the deprecated ``old_api`` and formats
it. After migration every ``old_api`` call here must become ``new_api`` while the
formatting behavior stays byte-for-byte identical (the tests pin it).
"""
from __future__ import annotations

from .helpers import old_api


def double_cents(cents: int) -> int:
    """Double a cent amount using the (deprecated) helper."""
    return old_api(cents)


def format_amount(cents: int) -> str:
    """Return a doubled amount formatted as dollars, e.g. ``$2.00`` for 100."""
    doubled = old_api(cents)
    return f"${doubled / 100:.2f}"

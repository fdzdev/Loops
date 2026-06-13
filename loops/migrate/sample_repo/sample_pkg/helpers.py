"""The deprecated helper and its replacement.

`new_api` is the real implementation. `old_api` is the deprecated shim kept only
for backward compatibility — it forwards to `new_api` and emits a warning. The
migration's job is to move every *caller* off `old_api` and onto `new_api`; this
module itself is the definition site, not a caller, so the loop should leave it
alone (it's excluded from the scan).
"""
from __future__ import annotations

import warnings


def new_api(value: int) -> int:
    """Double the input. The supported, non-deprecated entry point."""
    return value * 2


def old_api(value: int) -> int:
    """Deprecated alias for :func:`new_api`. Do not use in new code."""
    warnings.warn(
        "old_api() is deprecated; use new_api() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return new_api(value)

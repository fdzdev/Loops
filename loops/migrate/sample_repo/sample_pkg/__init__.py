"""A tiny sample package with a deprecated helper that should be migrated.

`old_api(x)` is the deprecated entry point. The maintainers added `new_api(x)`
with identical behavior and want every call site moved over so `old_api` can be
deleted in the next major version. The migrate loop does that rewrite.
"""
from .currency import format_amount
from .helpers import new_api, old_api

__all__ = ["new_api", "old_api", "format_amount"]

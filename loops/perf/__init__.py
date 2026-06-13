"""perf — a self-verifying performance-optimization loop.

The verifier (loops/perf/verifier.py) confirms an optimization only when a fresh
subprocess proves it is both correct and measurably faster. See README.md.
"""
from .loop import build, main

__all__ = ["build", "main"]

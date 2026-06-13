"""Behavior tests that must keep passing through the migration.

These pin the *observable behavior* of the callers, independent of whether they
go through ``old_api`` or ``new_api``. The verifier runs this suite against the
rewritten copy; if a rewrite changes behavior (or someone deletes a test to make
the count hit zero), the suite or the deterministic checks catch it.
"""
import sample_pkg
from sample_pkg.currency import double_cents, format_amount
from sample_pkg.reporting import summarize


def test_double_cents():
    assert double_cents(100) == 200
    assert double_cents(0) == 0
    assert double_cents(7) == 14


def test_format_amount():
    assert format_amount(100) == "$2.00"
    assert format_amount(50) == "$1.00"
    assert format_amount(1) == "$0.02"


def test_summarize():
    assert summarize([1, 2, 3]) == 12  # (1+2+3) doubled
    assert summarize([]) == 0
    assert summarize([10]) == 20


def test_new_api_equivalent_to_old_api():
    # Both helpers must agree — the migration relies on this equivalence.
    for x in (-5, 0, 1, 42, 1000):
        assert sample_pkg.new_api(x) == sample_pkg.old_api(x)

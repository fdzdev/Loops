"""Repro test for issue PRICE-1. FAILS against the buggy code, PASSES once the
per-unit discount is applied correctly. This is the test the verifier checks
went from red to green."""

from buggy_lib.pricing import line_total


def test_multi_quantity_discount_is_per_unit():
    # 3 units at $10, 10% off -> 3 * (10 * 0.9) = 27.0
    assert line_total(10.0, 3, 0.1) == 27.0

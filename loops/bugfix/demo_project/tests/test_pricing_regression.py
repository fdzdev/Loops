"""Regression fence for pricing. These already PASS against the buggy code and
must STAY green after any fix. They pin behavior a lazy "fix" (e.g. hardcoding
the repro's expected number) would break, since they exercise several distinct
inputs and edge cases."""

import pytest

from buggy_lib.pricing import line_total, cart_total


def test_no_discount_is_plain_multiplication():
    assert line_total(10.0, 3, 0.0) == 30.0
    assert line_total(2.5, 4) == 10.0


def test_single_unit_with_discount():
    # With quantity 1 the buggy and correct formulas happen to agree, so this
    # passes before AND after the fix — it pins the single-unit case.
    assert line_total(10.0, 1, 0.1) == 9.0


def test_zero_quantity_is_zero():
    assert line_total(99.0, 0, 0.5) == 0.0


def test_full_discount_single_unit_is_free():
    assert line_total(50.0, 1, 1.0) == 0.0


def test_invalid_discount_rejected():
    with pytest.raises(ValueError):
        line_total(10.0, 1, 1.5)


def test_negative_quantity_rejected():
    with pytest.raises(ValueError):
        line_total(10.0, -1, 0.0)


def test_cart_total_sums_lines():
    lines = [
        {"unit_price": 10.0, "quantity": 2, "discount_rate": 0.0},
        {"unit_price": 5.0, "quantity": 1},
    ]
    assert cart_total(lines) == 25.0

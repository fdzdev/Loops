"""Cart pricing.

BUG (issue PRICE-1): `line_total` discounts only the FIRST unit instead of
every unit on the line. It computes the discount on `unit_price` once and
subtracts it from the gross, rather than applying the rate to the whole line.
Multi-quantity discounted lines are therefore overcharged. The repro buys 3
units at $10 with 10% off and expects 3 * (10 * 0.9) = 27.0, but the buggy
code discounts a single unit and returns 30.0 - 1.0 = 29.0.

Edge cases (zero quantity, full discount, single unit) happen to come out
right under the buggy formula, which is exactly why the bug slipped through —
the regression tests pin those so a fix can't regress them.
"""

from __future__ import annotations

from typing import Iterable, Mapping


def line_total(unit_price: float, quantity: int, discount_rate: float = 0.0) -> float:
    """Total price for one cart line after a percentage discount.

    discount_rate is a fraction in [0, 1]; 0.1 means 10% off.
    """
    if quantity < 0:
        raise ValueError("quantity must be non-negative")
    if not (0.0 <= discount_rate <= 1.0):
        raise ValueError("discount_rate must be in [0, 1]")

    # BUG: the discounted unit price is only applied to ONE unit; the remaining
    # units are charged at full price. Correct: unit_price * quantity * (1 - rate).
    if quantity == 0:
        return 0.0
    discounted_unit = unit_price * (1.0 - discount_rate)
    return discounted_unit + unit_price * (quantity - 1)


def cart_total(lines: Iterable[Mapping[str, float]]) -> float:
    """Sum of line totals. Each line is a mapping with unit_price, quantity,
    and optional discount_rate."""
    total = 0.0
    for line in lines:
        total += line_total(
            unit_price=float(line["unit_price"]),
            quantity=int(line["quantity"]),
            discount_rate=float(line.get("discount_rate", 0.0)),
        )
    return total

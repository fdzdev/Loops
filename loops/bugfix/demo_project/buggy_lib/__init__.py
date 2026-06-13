"""buggy_lib — a tiny demo library shipped with two planted bugs.

This package is the *demo target* for the bugfix loop. Two of its modules
contain real, reproducible bugs; each has a pytest repro test that fails until
the bug is fixed. The remaining tests already pass and act as a regression
fence: a "fix" that breaks them does not count.
"""

from .pricing import line_total, cart_total
from .text import slugify

__all__ = ["line_total", "cart_total", "slugify"]

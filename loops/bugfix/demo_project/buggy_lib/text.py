"""Text helpers.

BUG (issue TEXT-1): `slugify` collapses spaces to single hyphens but does not
collapse *runs* of separators, and it leaves a trailing hyphen when the input
ends with punctuation. "Hello,  World!" should slugify to "hello-world", but
the buggy version yields "hello--world-".
"""

from __future__ import annotations

import re


def slugify(text: str) -> str:
    """Turn arbitrary text into a URL-safe slug.

    Lowercase, ASCII letters/digits only, words separated by single hyphens,
    no leading or trailing hyphen.
    """
    lowered = text.lower()
    # Replace any non-alphanumeric character with a hyphen.
    # BUG: this can emit multiple consecutive hyphens and a trailing hyphen,
    # because runs of separators are not collapsed and edges are not trimmed.
    out = re.sub(r"[^a-z0-9]", "-", lowered)
    return out

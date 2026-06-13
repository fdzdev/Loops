"""Repro test for issue TEXT-1. FAILS against the buggy code (which emits
double hyphens and a trailing hyphen), PASSES once separator runs are collapsed
and edges trimmed."""

from buggy_lib.text import slugify


def test_collapses_runs_and_trims_edges():
    assert slugify("Hello,  World!") == "hello-world"

"""The optimization registry: deliberately slow functions, their correctness
tests, and their benchmark inputs.

Each target is a self-contained `Target`: the baseline source (as a *string*,
because the verifier ships it to a subprocess), a set of correctness tests, and
an input generator. The loop asks llm.strong to produce a drop-in faster
replacement with the SAME name and signature; the verifier proves it.

Why the baseline is stored as source text rather than a live function: the
verifier runs both the baseline and the candidate in a fresh subprocess so the
candidate cannot monkeypatch the timer, the tests, or the baseline. Source text
is the unit of work that crosses that boundary.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Target:
    """One function we want to speed up.

    - `name`: the function name the candidate must define (drop-in replacement).
    - `baseline_src`: source of the slow reference implementation. Defines a
      function called `name`. This is the correctness oracle AND the timing
      baseline.
    - `tests_src`: source defining `TEST_CASES`, a list of (args_tuple, expected)
      pairs, plus optionally a `check(fn)` callable for property-style tests. The
      verifier imports this in the subprocess and runs every case against both
      baseline and candidate. Owned by the loop, never editable by the candidate.
    - `inputs_src`: source defining `make_inputs() -> list[tuple]`, the argument
      tuples used for benchmarking. Large enough that an asymptotic win shows up
      well above timer noise.
    - `signature_hint`: human-readable signature shown to the executor.
    """

    name: str
    baseline_src: str
    tests_src: str
    inputs_src: str
    signature_hint: str

    def baseline_hash(self) -> str:
        """A stable hash of the current baseline implementation. Part of the
        dedupe key so that if the registered baseline ever changes, the target is
        treated as a brand-new problem rather than silently reusing an old
        verdict."""
        return hashlib.sha256(self.baseline_src.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------
# Demo target 1: O(n^2) order-preserving dedupe.
# The naive version rescans the whole output list for every element. A dict /
# set gets it to O(n). Correctness: output must be the input with later
# duplicates removed, original order preserved.
# --------------------------------------------------------------------------
_DEDUPE_BASELINE = '''
def dedupe(items):
    """Return items with duplicates removed, preserving first-seen order.

    Deliberately O(n^2): membership-tests against a growing list.
    """
    out = []
    for x in items:
        found = False
        for y in out:          # linear scan of output for every input element
            if x == y:
                found = True
                break
        if not found:
            out.append(x)
    return out
'''

_DEDUPE_TESTS = '''
# Owned by the loop. The candidate cannot edit this file; weakening a test is
# impossible from inside the candidate.
TEST_CASES = [
    (([],),                               []),
    (((1,),),                             [1]),
    (((1, 1, 1),),                        [1]),
    (((3, 1, 2, 1, 3, 2),),               [3, 1, 2]),
    ((("a", "b", "a", "c", "b"),),        ["a", "b", "c"]),
    (((0, 0, 1, 0, 2, 1),),               [0, 1, 2]),
    ((tuple(range(50)) + tuple(range(50)),), list(range(50))),
]

def check(fn):
    """Property tests that don't fit the table form. Raise on failure."""
    import random
    rnd = random.Random(1234)
    for _ in range(200):
        n = rnd.randint(0, 40)
        data = [rnd.randint(0, 10) for _ in range(n)]
        got = list(fn(data))
        # Property 1: output is a subsequence with no repeats.
        assert len(set(got)) == len(got), f"output has duplicates: {got}"
        # Property 2: same set of distinct elements as the input.
        assert set(got) == set(data), f"changed the value set: {got} vs {data}"
        # Property 3: first-seen order preserved.
        seen, expected = set(), []
        for x in data:
            if x not in seen:
                seen.add(x)
                expected.append(x)
        assert got == expected, f"order not preserved: {got} vs {expected}"
'''

_DEDUPE_INPUTS = '''
def make_inputs():
    """Benchmark inputs: a big list with ~50% duplicates, where O(n^2) hurts."""
    import random
    rnd = random.Random(7)
    data = [rnd.randint(0, 1500) for _ in range(3000)]
    return [(data,)]
'''


# --------------------------------------------------------------------------
# Demo target 2: count pairs that sum to a target (classic O(n^2) double loop).
# A hash set makes it O(n). Correctness: number of *index pairs* (i < j) with
# a[i] + a[j] == target.
# --------------------------------------------------------------------------
_PAIRSUM_BASELINE = '''
def count_pairs(nums, target):
    """Count index pairs i < j with nums[i] + nums[j] == target.

    Deliberately O(n^2): nested loops over all pairs.
    """
    count = 0
    n = len(nums)
    for i in range(n):
        for j in range(i + 1, n):
            if nums[i] + nums[j] == target:
                count += 1
    return count
'''

_PAIRSUM_TESTS = '''
TEST_CASES = [
    (([], 0),                       0),
    (([1], 2),                      0),
    (([1, 1], 2),                   1),
    (([1, 1, 1], 2),                3),    # all C(3,2) pairs
    (([2, 4, 3, 1, 5], 6),          2),    # (2,4) and (1,5)
    (([0, 0, 0, 0], 0),             6),    # C(4,2)
    (([-2, 2, 0, 0], 0),            2),    # (-2,2) and (0,0)
]

def check(fn):
    import random
    rnd = random.Random(99)
    for _ in range(150):
        n = rnd.randint(0, 30)
        nums = [rnd.randint(-5, 5) for _ in range(n)]
        target = rnd.randint(-6, 6)
        # Reference brute force computed here, in loop-owned code.
        ref = 0
        for i in range(n):
            for j in range(i + 1, n):
                if nums[i] + nums[j] == target:
                    ref += 1
        got = fn(nums, target)
        assert got == ref, f"count_pairs({nums!r}, {target}) = {got}, expected {ref}"
'''

_PAIRSUM_INPUTS = '''
def make_inputs():
    """A list big enough that the O(n^2) double loop is clearly slow."""
    import random
    rnd = random.Random(13)
    nums = [rnd.randint(-50, 50) for _ in range(2500)]
    return [(nums, 0)]
'''


def registry() -> list[Target]:
    """The targets this loop tries to optimize. Add a Target here to expand the
    crop; the generator emits one Candidate per entry every round."""
    return [
        Target(
            name="dedupe",
            baseline_src=_DEDUPE_BASELINE,
            tests_src=_DEDUPE_TESTS,
            inputs_src=_DEDUPE_INPUTS,
            signature_hint="dedupe(items) -> list  # remove duplicates, keep first-seen order",
        ),
        Target(
            name="count_pairs",
            baseline_src=_PAIRSUM_BASELINE,
            tests_src=_PAIRSUM_TESTS,
            inputs_src=_PAIRSUM_INPUTS,
            signature_hint="count_pairs(nums, target) -> int  # count index pairs i<j summing to target",
        ),
    ]


__all__ = ["Target", "registry"]

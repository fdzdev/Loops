"""A genuinely flaky pytest module — the demo target for the `flaky` loop.

Each test below depends on a real non-deterministic source (the wall clock or an
unseeded RNG) and passes only *most* of the time. Run it a few times and watch
it flip:

    pytest loops/flaky/demo/test_flaky_demo.py   # passes ~70% of runs

The loop's job is to make these deterministic WITHOUT weakening them: the same
assertions must remain, and the test may not be skipped/xfailed/deleted. The
fixes a good executor finds:

  * test_recent_event_timestamp  -> freeze the clock (pin `now` to a fixed
    instant whose millisecond offset is < 700, or monkeypatch `time.time`).
  * test_shuffle_picks_low_card  -> seed the RNG so the draw is reproducible.

These are the canonical "freeze the clock" / "seed the RNG" flaky-test repairs.
The point is that the *intent* of each assertion is correct and provable; only
the nondeterministic input makes it flap.
"""
from __future__ import annotations

import random
import time


# ---------------------------------------------------------------------------
# Flaky source #1: depends on the wall clock.
#
# `event_ms_in_second` returns the millisecond offset (0..999) of the current
# instant within its second. The test asserts an event is logged "promptly" —
# within the first 700ms of its second. That's true ~70% of the time and depends
# entirely on *when* the wall clock happens to be read, so it flaps run to run.
# This is portable flakiness (no reliance on machine timing jitter): the fix is
# the canonical "freeze the clock" — pin `now` to a fixed instant.
# ---------------------------------------------------------------------------
def event_ms_in_second(now: float | None = None) -> int:
    """Millisecond offset (0..999) of `now` within its second (defaults to clock)."""
    t = time.time() if now is None else now
    return int((t % 1.0) * 1000)


def test_recent_event_timestamp() -> None:
    ms = event_ms_in_second()
    # Intent: the event is logged within the first 700ms of its second ("prompt").
    # Correct ~70% of the time; flaps whenever the clock is read late in a second.
    # Freezing the clock to a fixed instant makes this deterministic without
    # touching the assertion.
    assert ms < 700


# ---------------------------------------------------------------------------
# Flaky source #2: depends on an unseeded RNG.
#
# We draw 3 cards from a 10-card deck and assert the minimum is in the lower
# third. True ~71% of draws but not all -> ~70% pass.
# ---------------------------------------------------------------------------
def draw_hand(deck: list[int], k: int = 3) -> list[int]:
    """Draw `k` cards from `deck` without replacement."""
    return random.sample(deck, k)


def test_shuffle_picks_low_card() -> None:
    deck = list(range(1, 11))  # cards 1..10
    hand = draw_hand(deck, 3)
    # Intent: with 3 of 10 cards drawn, the lowest should land in the bottom
    # third (<= 3). True ~71% of draws (exact: 0.708); flaps when all three
    # drawn cards happen to be high. This is the ~70% pass rate the loop targets.
    assert min(hand) <= 3

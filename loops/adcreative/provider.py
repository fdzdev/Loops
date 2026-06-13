"""The metrics signal the verifier reads from.

The verifier is only as trustworthy as its signal. We isolate that signal behind
a small `MetricsProvider` Protocol so the loop can run two ways:

  - SIM mode (default, ships here): a seeded multi-armed-bandit simulator. Each
    creative has a hidden "true" CTR; the provider serves impressions and returns
    NOISY observed metrics. This is what makes the loop runnable out-of-the-box.
  - REAL mode (you plug in): a `MetaAdsProvider` / `GoogleAdsProvider` that reads
    live performance from an ad platform API. We ship the Protocol + a stub so the
    hook is obvious; we do NOT ship credentials or a network client.

The anti-gaming gate lives in the data contract, not just the verifier: a
`CreativeMetrics` carries an `impressions` count, and the verifier refuses to
rule on any creative with fewer than `min_impressions`. Short windows overfit —
a creative that "wins" on 12 impressions has told you nothing.
"""
from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


def _stable_seed(*parts: object) -> int:
    """Deterministic 32-bit seed from arbitrary parts.

    We must NOT use the builtin ``hash()`` here: CPython randomizes string and
    tuple hashing per process (PYTHONHASHSEED), so ``hash(key)`` returns a
    different value on every run. That would silently break the simulator's
    central promise — that a given creative key always maps to the same hidden
    truth, so a seeded run is reproducible and the printed ``true_ctr`` is the
    same number a human can re-check. hashlib is stable across processes.
    """
    h = hashlib.sha256("|".join(repr(p) for p in parts).encode("utf-8")).hexdigest()
    return int(h[:8], 16)


@dataclass(frozen=True)
class CreativeMetrics:
    """One observation window for one creative.

    `ctr` and `roas` are OBSERVED (noisy), not the hidden truth. `impressions` is
    the load-bearing field for the anti-gaming gate: no verdict below the floor.
    """

    impressions: int
    clicks: int
    conversions: int
    spend_usd: float
    revenue_usd: float

    @property
    def ctr(self) -> float:
        """Observed click-through rate. 0.0 when no impressions served yet."""
        return self.clicks / self.impressions if self.impressions else 0.0

    @property
    def roas(self) -> float:
        """Observed return on ad spend (revenue / spend). 0.0 when no spend."""
        return self.revenue_usd / self.spend_usd if self.spend_usd > 0 else 0.0


@runtime_checkable
class MetricsProvider(Protocol):
    """The signal the verifier trusts. Implement this to plug in real ad metrics.

    `serve` advances the experiment for one creative (real platforms do this for
    you as the campaign runs; the sim does it on demand). `metrics` returns the
    cumulative observed metrics so far. Both are keyed by the creative's
    `dedupe_key` so the verifier never needs the creative text to read a result.
    """

    def serve(self, key: str, impressions: int) -> None:
        """Accumulate `impressions` more impressions of creative `key`."""
        ...

    def metrics(self, key: str) -> CreativeMetrics:
        """Return cumulative observed metrics for creative `key`."""
        ...


# --------------------------------------------------------------------------- #
# SIM mode — runs out-of-the-box, no network, fully seeded.
# --------------------------------------------------------------------------- #


@dataclass
class _Arm:
    """Hidden ground truth for one creative in the bandit sim."""

    true_ctr: float
    true_cvr: float  # conversion rate given a click
    revenue_per_conv: float
    cpc_usd: float  # cost per click (what we "pay" the platform)
    impressions: int = 0
    clicks: int = 0
    conversions: int = 0
    spend_usd: float = 0.0
    revenue_usd: float = 0.0


class SimulatedMetricsProvider:
    """Seeded multi-armed-bandit simulator.

    Each creative gets a hidden true CTR assigned the first time it is served,
    drawn deterministically from the creative key + master seed (so a given key
    always gets the same truth, which makes runs reproducible AND means the
    generator can't influence its own grade by re-wording around the hash). Each
    served impression is a Bernoulli draw on the true CTR; clicks roll for a
    conversion; conversions add revenue. Observed metrics are therefore noisy
    estimates that converge on the truth only as impressions accumulate — exactly
    the regime where a min-impressions gate matters.
    """

    def __init__(
        self,
        *,
        seed: int = 1234,
        base_ctr_range: tuple[float, float] = (0.005, 0.060),
        base_cvr_range: tuple[float, float] = (0.02, 0.20),
        revenue_per_conv: float = 40.0,
        cpc_usd: float = 0.80,
    ):
        self._master_seed = seed
        self._base_ctr_range = base_ctr_range
        self._base_cvr_range = base_cvr_range
        self._revenue_per_conv = revenue_per_conv
        self._cpc_usd = cpc_usd
        self._arms: dict[str, _Arm] = {}

    def _arm(self, key: str) -> _Arm:
        if key not in self._arms:
            # Deterministic truth per key: fold the key into the master stream
            # with a STABLE hash so the same creative always has the same hidden
            # CTR across runs (builtin hash() is per-process randomized).
            key_seed = (self._master_seed ^ _stable_seed(key)) & 0xFFFFFFFF
            rng = random.Random(key_seed)
            lo, hi = self._base_ctr_range
            clo, chi = self._base_cvr_range
            self._arms[key] = _Arm(
                true_ctr=rng.uniform(lo, hi),
                true_cvr=rng.uniform(clo, chi),
                revenue_per_conv=self._revenue_per_conv,
                cpc_usd=self._cpc_usd,
            )
        return self._arms[key]

    def serve(self, key: str, impressions: int) -> None:
        arm = self._arm(key)
        # Per-serve RNG seeded by key + impressions-so-far so the draws are
        # reproducible and resumable (re-serving picks up a fresh, stable stream).
        stream_seed = (self._master_seed * 31 + _stable_seed(key, arm.impressions)) & 0xFFFFFFFF
        rng = random.Random(stream_seed)
        for _ in range(impressions):
            arm.impressions += 1
            if rng.random() < arm.true_ctr:
                arm.clicks += 1
                arm.spend_usd += arm.cpc_usd
                if rng.random() < arm.true_cvr:
                    arm.conversions += 1
                    arm.revenue_usd += arm.revenue_per_conv

    def metrics(self, key: str) -> CreativeMetrics:
        arm = self._arm(key)
        return CreativeMetrics(
            impressions=arm.impressions,
            clicks=arm.clicks,
            conversions=arm.conversions,
            spend_usd=round(arm.spend_usd, 4),
            revenue_usd=round(arm.revenue_usd, 4),
        )

    def true_ctr(self, key: str) -> float:
        """Sim-only: the hidden truth, for printing 'did the verifier get it right'."""
        return self._arm(key).true_ctr


def wilson_lower_bound(clicks: int, impressions: int, z: float = 1.96) -> float:
    """Wilson score lower bound on the CTR at confidence `z` (default ~95%).

    Why not just observed CTR? Observed CTR on a thin window is the gaming trap in
    numeric form: 1 click in 10 impressions reads as 10% CTR. The Wilson lower
    bound discounts for sample size, so a creative only clears the bar once the
    EVIDENCE — not the luck — is strong enough. This is the statistical twin of
    the min-impressions gate.
    """
    if impressions <= 0:
        return 0.0
    p = clicks / impressions
    n = impressions
    denom = 1.0 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (center - margin) / denom)


# --------------------------------------------------------------------------- #
# REAL mode — the hook. Ship the shape, not the network client.
# --------------------------------------------------------------------------- #


class MetaAdsProvider:
    """STUB. Real-use hook for a live ad platform (Meta Marketing API, Google Ads,
    TikTok, etc.). Implements the same `MetricsProvider` Protocol.

    To wire this up for production:
      1. In `__init__`, build your authenticated platform client (read the token
         from the environment — NEVER hardcode it, NEVER at import time).
      2. `serve(key, impressions)` maps to launching/topping-up the ad whose
         platform id you stored for `key`. On most platforms serving is automatic
         once the ad is live, so this may be a no-op that just ensures the ad
         exists with enough budget to reach `impressions`.
      3. `metrics(key)` calls the platform's insights endpoint and maps the
         response into `CreativeMetrics(impressions, clicks, conversions,
         spend_usd, revenue_usd)`.

    The verifier does not change between sim and real: the min-impressions gate
    and the Wilson lower bound are exactly the protections you want against a
    cold-start ad that got lucky in its first hour of real traffic.
    """

    def __init__(self, *, account_id: str | None = None, access_token: str | None = None):
        # Deliberately not constructing a network client here. Raise loudly if
        # someone runs the loop in real mode without finishing the wiring.
        self.account_id = account_id
        self.access_token = access_token

    def serve(self, key: str, impressions: int) -> None:  # pragma: no cover - stub
        raise NotImplementedError(
            "MetaAdsProvider.serve: wire this to your ad platform's launch/insights "
            "API. See the docstring. Use SimulatedMetricsProvider for the demo."
        )

    def metrics(self, key: str) -> CreativeMetrics:  # pragma: no cover - stub
        raise NotImplementedError(
            "MetaAdsProvider.metrics: map the platform insights response into "
            "CreativeMetrics. See the docstring. Use SimulatedMetricsProvider for the demo."
        )

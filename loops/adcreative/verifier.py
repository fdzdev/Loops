"""The verifier — DETERMINISTIC against a metrics signal.

A creative is CONFIRMED only when its live (or simulated) performance clears a
threshold AFTER a minimum number of impressions. The generator model never gets
a vote: the verdict is arithmetic over numbers the provider reports.

Two gates, both anti-gaming:

  1. MIN-IMPRESSIONS GATE (the trap in the brief). A short window overfits: a
     creative that scores 1 click on 8 impressions reads as a 12.5% CTR and would
     "win" against any sane threshold. We refuse to rule until the creative has
     accumulated `min_impressions`. Below the floor the verdict is REJECT with
     reason "insufficient impressions" — not a confirm, and not a silent pass.

  2. CONFIDENCE GATE. Even at the floor, observed CTR is noisy. We compare the
     Wilson score LOWER BOUND of the CTR against the threshold, so a creative
     clears the bar on evidence, not on a lucky run. (ROAS uses the observed
     value directly but is also gated on conversions so a single fluke sale can't
     carry it.)

Same code runs in sim and in production: only the injected `MetricsProvider`
changes. That is the whole point of reading the signal behind a Protocol.
"""
from __future__ import annotations

from agentloops import Verdict

from .provider import CreativeMetrics, MetricsProvider, wilson_lower_bound


def verify_creative(
    *,
    key: str,
    provider: MetricsProvider,
    min_impressions: int,
    ctr_threshold: float,
    roas_threshold: float | None = None,
    min_conversions_for_roas: int = 3,
    z: float = 1.96,
) -> Verdict:
    """Return a Verdict for one creative, reading performance from `provider`.

    Confirm IFF, after at least `min_impressions` impressions, the Wilson lower
    bound of the CTR clears `ctr_threshold` AND (if a `roas_threshold` is set) the
    observed ROAS clears it on enough conversions to be real.

    The proof in `evidence` is everything a human needs to re-check the call by
    hand in under a minute: the raw counts, both thresholds, and the gate that
    fired.
    """
    m: CreativeMetrics = provider.metrics(key)

    base_evidence = {
        "impressions": m.impressions,
        "clicks": m.clicks,
        "conversions": m.conversions,
        "spend_usd": m.spend_usd,
        "revenue_usd": m.revenue_usd,
        "observed_ctr": round(m.ctr, 5),
        "observed_roas": round(m.roas, 4),
        "ctr_threshold": ctr_threshold,
        "roas_threshold": roas_threshold,
        "min_impressions": min_impressions,
    }

    # --- Gate 1: the min-impressions floor. The anti-gaming heart of the loop. ---
    if m.impressions < min_impressions:
        return Verdict(
            confirmed=False,
            evidence={**base_evidence, "gate": "min_impressions"},
            reason=(
                f"insufficient impressions: {m.impressions} < {min_impressions} "
                f"(short windows overfit; no verdict yet)"
            ),
        )

    # --- Gate 2: confidence-discounted CTR. Beat the bar on evidence, not luck. ---
    ctr_lb = wilson_lower_bound(m.clicks, m.impressions, z=z)
    base_evidence["ctr_wilson_lower_bound"] = round(ctr_lb, 5)
    if ctr_lb < ctr_threshold:
        return Verdict(
            confirmed=False,
            evidence={**base_evidence, "gate": "ctr_confidence"},
            reason=(
                f"CTR lower bound {ctr_lb:.4f} < threshold {ctr_threshold:.4f} "
                f"(observed {m.ctr:.4f} on {m.impressions} impressions)"
            ),
        )

    # --- Optional gate 3: ROAS, also protected against a single fluke sale. ---
    if roas_threshold is not None:
        if m.conversions < min_conversions_for_roas:
            return Verdict(
                confirmed=False,
                evidence={**base_evidence, "gate": "roas_conversions"},
                reason=(
                    f"only {m.conversions} conversions (< {min_conversions_for_roas}); "
                    f"ROAS {m.roas:.2f} not yet trustworthy"
                ),
            )
        if m.roas < roas_threshold:
            return Verdict(
                confirmed=False,
                evidence={**base_evidence, "gate": "roas"},
                reason=f"ROAS {m.roas:.2f} < threshold {roas_threshold:.2f}",
            )

    parts = [f"CTR lower bound {ctr_lb:.4f} >= {ctr_threshold:.4f}"]
    if roas_threshold is not None:
        parts.append(f"ROAS {m.roas:.2f} >= {roas_threshold:.2f} on {m.conversions} conv")
    return Verdict(
        confirmed=True,
        evidence={**base_evidence, "gate": "passed"},
        reason="; ".join(parts) + f" after {m.impressions} impressions",
    )

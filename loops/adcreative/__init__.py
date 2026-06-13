"""adcreative — ROAS-graded ad-creative generation loop.

Generate ad-creative variants with llm.strong, grade each one DETERMINISTICALLY
against a metrics signal (CTR/ROAS) read from a MetricsProvider, and confirm only
the variants that beat threshold AFTER a minimum number of impressions.
"""
from .loop import AdCreative, build, main
from .provider import (
    CreativeMetrics,
    MetaAdsProvider,
    MetricsProvider,
    SimulatedMetricsProvider,
)
from .verifier import verify_creative

__all__ = [
    "AdCreative",
    "build",
    "main",
    "verify_creative",
    "CreativeMetrics",
    "MetricsProvider",
    "SimulatedMetricsProvider",
    "MetaAdsProvider",
]

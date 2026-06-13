"""Shared data shapes for the finmodel loop.

These Pydantic models are the *contract* between three parties who must never
collude:

  - the task generator (defines what model to build, and the rubric to grade it),
  - the executor (llm.strong proposes assumptions, then plain code builds the xlsx),
  - the verifier (independently recomputes every figure and checks the rubric).

Keeping the shapes here — not inside verifier.py — lets the verifier import only
what it grades, and keeps the generator from reaching into verifier internals.
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

from pydantic import BaseModel, Field


class ModelTask(BaseModel):
    """A model-building assignment plus the rubric it will be graded against.

    The rubric is a list of *named, code-checkable* criteria (see verifier.py for
    the exact boolean each name maps to). A criterion that isn't backed by a
    concrete boolean is rejected by the verifier as un-gradeable — that is the
    anti-gaming clause: you cannot pass a vague rubric.
    """

    title: str
    currency: str = "USD"
    start_year: int = Field(ge=1900, le=2200)
    years: int = Field(ge=2, le=10, description="number of contiguous projection years")
    rubric: list[str] = Field(
        description="named criteria; each must map to a concrete boolean in verifier.RUBRIC_CHECKS",
    )


class Assumptions(BaseModel):
    """The model structure llm.strong proposes. Deliberately *minimal numbers*:
    base figures and rates only. The xlsx (and the independent recompute) DERIVE
    every projected figure from these — the model never gets to hand us a total
    and ask us to trust it.
    """

    base_revenue: float = Field(gt=0, description="revenue in the first projected year")
    revenue_growth: float = Field(
        description="year-over-year revenue growth rate, e.g. 0.15 for 15%"
    )
    cogs_pct: float = Field(
        ge=0, le=1, description="cost of goods sold as a fraction of revenue"
    )
    opex_base: float = Field(ge=0, description="operating expense in the first year")
    opex_growth: float = Field(description="year-over-year opex growth rate")
    tax_rate: float = Field(ge=0, le=1, description="tax rate applied to positive pre-tax income")

    def dedupe_key(self) -> str:
        """Stable hash of the assumption set. Two proposals with identical
        assumptions are the same model build and should not be re-verified."""
        payload = json.dumps(self.model_dump(), sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


class ModelCandidate(BaseModel):
    """One proposed model build: the task, the proposed assumptions, and the path
    to the .xlsx the loop built from those assumptions. `dedupe_key` is the hash of
    the assumptions, so re-proposing the same numbers is a no-op."""

    task: ModelTask
    assumptions: Assumptions
    xlsx_path: Optional[str] = None

    @property
    def dedupe_key(self) -> str:
        return self.assumptions.dedupe_key()

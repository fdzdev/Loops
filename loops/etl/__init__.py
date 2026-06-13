"""ETL / data-pipeline self-healing loop.

Detects an input the current transform can't ingest, has llm.strong propose a
repaired transform, and confirms it ONLY via deterministic invariants
(schema + row reconciliation + non-null + a reconciling total). See README.md.
"""
from .loop import BrokenInput, ProposedTransform, build, main
from .verifier import verify_transform

__all__ = ["build", "main", "verify_transform", "BrokenInput", "ProposedTransform"]

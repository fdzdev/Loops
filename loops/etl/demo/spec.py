"""The pipeline contract the ETL loop heals toward.

This module is the *demo target*: a clean reference table, the canonical output
schema and invariants, and the naive `current_transform` that the pipeline ships
today. The loop's job is to repair inputs that the current transform can't
ingest WITHOUT silently corrupting the numbers.

Everything here is plain stdlib (csv, hashlib) so the demo runs out of the box.
The verifier in ``verifier.py`` imports the constants and helpers below; keeping
them in one place means the goal an LLM heals toward and the goal the verifier
enforces are literally the same object — an adversary can't satisfy a looser
copy of the spec.
"""
from __future__ import annotations

import csv
import io
from typing import Iterable

# --- Canonical output contract -------------------------------------------------

# The exact columns (and order) every transform MUST emit.
OUTPUT_SCHEMA: tuple[str, ...] = ("order_id", "region", "units", "unit_price", "revenue")

# Columns that may never contain a null/empty value in the output.
REQUIRED_NON_NULL: tuple[str, ...] = ("order_id", "region", "units", "revenue")

# The reconciling total. This is the load-bearing invariant: a transform that
# "heals" by dropping rows, zeroing fields, or mangling types will move this
# number. We pin it to the clean reference so corruption can't pass.
RECONCILE_COLUMN: str = "revenue"

# Absolute tolerance on the reconciled total, in the same units as RECONCILE_COLUMN.
# Small but non-zero to absorb float formatting, NOT to absorb dropped rows.
RECONCILE_TOLERANCE: float = 0.01

# Rows the input legitimately contains that the transform is EXPECTED to drop
# (e.g. void / cancelled orders). Row-count reconciliation is:
#     input_rows == output_rows + documented_drops
# so an undocumented drop (silent data loss) fails verification.
DOCUMENTED_DROPS: int = 1  # the "VOID" order in the messy demo input


def reference_path() -> str:
    import os

    return os.path.join(os.path.dirname(__file__), "reference.csv")


def load_reference() -> list[dict[str, str]]:
    """The clean, trusted table. Source of truth for the reconciled total."""
    with open(reference_path(), newline="") as f:
        return list(csv.DictReader(f))


def reference_total() -> float:
    """Sum of the reconcile column over the clean reference."""
    return round(sum(float(r[RECONCILE_COLUMN]) for r in load_reference()), 2)


# --- The transform the pipeline ships TODAY ------------------------------------


def count_input_rows(raw_text: str) -> int:
    """How many data rows the raw input claims to have.

    Used by row-count reconciliation. Deliberately tolerant about the delimiter
    so a messy ';'-delimited file still reports its true row count; this is the
    *denominator* the transform must account for, not part of the transform.
    """
    sample = raw_text.splitlines()
    if not sample:
        return 0
    delimiter = ";" if sample[0].count(";") >= sample[0].count(",") else ","
    reader = csv.reader(io.StringIO(raw_text), delimiter=delimiter)
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    return max(0, len(rows) - 1)  # minus header


def current_transform(raw_text: str) -> list[dict[str, str]]:
    """The naive transform in production. Assumes clean, comma-delimited input
    with the canonical headers and bare numerics.

    It works on ``reference.csv`` and BREAKS on the messy variant (wrong
    delimiter, renamed headers, '$' and whitespace, a VOID row). The generator
    detects that breakage; the executor proposes a replacement.
    """
    reader = csv.DictReader(io.StringIO(raw_text))
    out: list[dict[str, str]] = []
    for row in reader:
        # Hard-fails on renamed/missing columns and non-numeric '$' values.
        out.append(
            {
                "order_id": row["order_id"],
                "region": row["region"],
                "units": str(int(row["units"])),
                "unit_price": f"{float(row['unit_price']):.2f}",
                "revenue": f"{float(row['revenue']):.2f}",
            }
        )
    return out


def transform_fails(transform, raw_text: str) -> tuple[bool, str]:
    """Run ``transform`` on ``raw_text`` and report whether it is broken on this
    input. "Broken" means: it raised, produced an empty result, or produced the
    wrong schema. This is the cheap pre-check the generator uses to decide an
    input is worth healing — the real proof is the deterministic verifier.
    """
    try:
        out = transform(raw_text)
    except Exception as exc:  # noqa: BLE001 - any failure means "needs healing"
        return True, f"{type(exc).__name__}: {exc}"
    if not out:
        return True, "transform produced zero rows"
    cols = tuple(out[0].keys())
    if cols != OUTPUT_SCHEMA:
        return True, f"schema mismatch: got {cols}, want {OUTPUT_SCHEMA}"
    return False, "ok"


def schema_of(rows: Iterable[dict]) -> tuple[str, ...]:
    rows = list(rows)
    return tuple(rows[0].keys()) if rows else ()

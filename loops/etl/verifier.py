"""The verifier — deterministic invariants, no model opinion.

The executor hands us a *repaired transform* (a Python function body). We run it
on the messy input and decide CONFIRMED iff ALL of these hold:

  1. SCHEMA       output columns == the canonical OUTPUT_SCHEMA, exactly.
  2. ROW RECONCILE  input_rows == output_rows + documented_drops.
                  (an undocumented drop is silent data loss -> fail)
  3. NON-NULL     every REQUIRED_NON_NULL column is non-empty in every row.
  4. TOTAL RECONCILE  sum(revenue) over the output == reference_total within
                  RECONCILE_TOLERANCE.

Invariant 4 is the anti-gaming clause with teeth. A transform can satisfy
"didn't crash" and "right schema" while quietly dropping rows or zeroing
fields — but the moment it does, the reconciled total drifts off the trusted
reference and verification fails. "It ran" is never the bar; "the money still
adds up" is.

The verdict comes ONLY from running this code. The model that wrote the
transform never gets to grade it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from agentloops import Verdict

from .demo import spec


@dataclass
class _Check:
    name: str
    passed: bool
    detail: str


def compile_transform(body: str) -> Callable[[str], list[dict]]:
    """Turn a proposed function body into a callable ``transform(raw_text)``.

    The body runs in a namespace whose globals expose stdlib parsing tools
    (``csv``, ``io``, ``re``) and the normal builtins. This is NOT a security
    sandbox: ``__builtins__`` is present, so a body could ``__import__`` other
    modules. We don't rely on the namespace for trust — the verdict comes from
    the *deterministic local reconciliation* in ``verify_transform``. A
    transform that reached the network still couldn't fetch a value that makes a
    corrupted output reconcile to our trusted reference total, so the namespace
    being permissive does not weaken the verifier. (If you point this loop at
    untrusted transform bodies, run ``compile_transform`` in a real sandbox.)

    The body is expected to define a function ``transform(raw_text) -> list[dict]``.
    """
    import csv as _csv
    import io as _io
    import re as _re

    sandbox: dict[str, Any] = {
        "csv": _csv,
        "io": _io,
        "re": _re,
        "__builtins__": __builtins__,
    }
    exec(body, sandbox)  # noqa: S102 - intentional: this IS the candidate under test
    fn = sandbox.get("transform")
    if not callable(fn):
        raise ValueError("proposed body did not define a callable `transform(raw_text)`")
    return fn  # type: ignore[return-value]


def verify_transform(*, raw_text: str, transform_body: str) -> Verdict:
    """Run the proposed transform against the demo input and check all invariants.

    Pure and deterministic: same inputs -> same Verdict, no LLM involved.
    """
    checks: list[_Check] = []
    reference_total = spec.reference_total()
    input_rows = spec.count_input_rows(raw_text)

    # Compile + run the candidate. A crash here is an automatic, evidenced reject.
    try:
        transform = compile_transform(transform_body)
        out = transform(raw_text)
    except Exception as exc:  # noqa: BLE001
        return Verdict(
            confirmed=False,
            reason=f"transform raised: {type(exc).__name__}: {exc}",
            evidence={
                "stage": "execution",
                "error": f"{type(exc).__name__}: {exc}",
                "reference_total": reference_total,
                "input_rows": input_rows,
            },
        )

    if not isinstance(out, list) or (out and not isinstance(out[0], dict)):
        return Verdict(
            confirmed=False,
            reason="transform did not return list[dict]",
            evidence={"stage": "shape", "type": type(out).__name__},
        )

    # 1) SCHEMA --------------------------------------------------------------
    got_schema = spec.schema_of(out)
    schema_ok = got_schema == spec.OUTPUT_SCHEMA
    checks.append(
        _Check(
            "schema",
            schema_ok,
            f"got {list(got_schema)}, want {list(spec.OUTPUT_SCHEMA)}",
        )
    )

    # 2) ROW RECONCILE -------------------------------------------------------
    output_rows = len(out)
    expected_rows = input_rows - spec.DOCUMENTED_DROPS
    row_ok = output_rows == expected_rows
    checks.append(
        _Check(
            "row_reconcile",
            row_ok,
            f"input_rows({input_rows}) - documented_drops({spec.DOCUMENTED_DROPS}) "
            f"= {expected_rows}, output_rows = {output_rows}",
        )
    )

    # 3) NON-NULL on required columns ---------------------------------------
    null_offenders: list[str] = []
    for i, row in enumerate(out):
        for col in spec.REQUIRED_NON_NULL:
            val = row.get(col)
            if val is None or str(val).strip() == "":
                null_offenders.append(f"row {i}: {col} is empty")
    nonnull_ok = not null_offenders
    checks.append(
        _Check(
            "required_non_null",
            nonnull_ok,
            "all required columns populated" if nonnull_ok else "; ".join(null_offenders[:5]),
        )
    )

    # 4) TOTAL RECONCILE (the anti-gaming invariant) ------------------------
    try:
        output_total = round(
            sum(float(row[spec.RECONCILE_COLUMN]) for row in out), 2
        )
        total_delta = round(abs(output_total - reference_total), 4)
        total_ok = total_delta <= spec.RECONCILE_TOLERANCE
        total_detail = (
            f"output_total={output_total} vs reference_total={reference_total} "
            f"(|delta|={total_delta} <= tol={spec.RECONCILE_TOLERANCE})"
        )
    except (KeyError, ValueError, TypeError) as exc:
        output_total = None  # type: ignore[assignment]
        total_delta = None  # type: ignore[assignment]
        total_ok = False
        total_detail = f"could not total {spec.RECONCILE_COLUMN}: {type(exc).__name__}: {exc}"
    checks.append(_Check("total_reconcile", total_ok, total_detail))

    confirmed = all(c.passed for c in checks)
    failed = [c.name for c in checks if not c.passed]

    evidence = {
        "invariants": {c.name: {"passed": c.passed, "detail": c.detail} for c in checks},
        "reference_total": reference_total,
        "output_total": output_total,
        "total_delta": total_delta,
        "input_rows": input_rows,
        "output_rows": output_rows,
        "documented_drops": spec.DOCUMENTED_DROPS,
        "output_schema": list(got_schema),
        "sample_output": out[:3],
    }
    reason = (
        "all 4 invariants pass; total reconciles to reference"
        if confirmed
        else f"failed invariants: {', '.join(failed)}"
    )
    return Verdict(confirmed=confirmed, evidence=evidence, reason=reason)

"""Mutation harness for the measured-spend fix.

Each mutation is a faithful, plausible way to get this wrong -- the shape a
future edit would actually take, not a scrambled character. A mutation that
survives means the tests certify a fix they do not exercise.

Two families here, because this change has two halves that fail differently:

* the measurement being WRONG (fal's figure discarded, the estimate substituted,
  a header misread);
* the measurement being UNREACHED (obtained at the call site and never wired
  into the ledger, which a mutation table over generation.py alone cannot see).

Run:  python scratch/mutate_measured_spend.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# test_measured_spend proves measured and estimated stay distinct; the other two
# are the suites that would notice a measurement breaking something it should
# not touch -- the quote/ledger agreement and the lineage guarantees.
TESTS = ["tests/test_measured_spend.py",
         "tests/test_cost_quote_matches_ledger.py",
         "tests/test_generation_lineage.py"]

# (label, file, find, replace, what breaking it would mean)
MUTATIONS = [
    # --- an estimate wearing fal's name -----------------------------------------
    (
        "cost_source is inferred from the number being present",
        "backend/generation.py",
        "    return a.cost_source == MEASURED",
        "    return bool(a.cost) or a.cost_source == MEASURED",
        "any bare cost= figure reads as measured against fal — the exact "
        "conflation this change removes, restored one level down",
    ),
    (
        "_amount falls through a measured zero to the estimate",
        "backend/generation.py",
        "    if measured(a):\n        return a.cost\n"
        "    return a.cost if a.cost else a.estimated_cost",
        "    return a.cost if a.cost else a.estimated_cost",
        "fal saying it billed nothing is overwritten by a number we invented, "
        "on the one row where we actually know",
    ),
    (
        "a measurement is trusted without validating its figures",
        "backend/generation.py",
        "    if not isinstance(measurement, dict):\n        return {}",
        "    if not isinstance(measurement, dict):\n        return {}\n"
        "    if True:\n"
        "        return {'cost': measurement.get('cost') or 0.0,\n"
        "                'cost_source': MEASURED,\n"
        "                'billable_units': measurement.get('units') or 0.0,\n"
        "                'billing_unit': str(measurement.get('unit') or ''),\n"
        "                'provider_request_id': str(measurement.get('request_id') or '')}",
        "a payload that crossed a network boundary and did not parse is "
        "recorded as a measured cost",
    ),
    (
        "succeed ignores the measurement it was handed",
        "backend/generation.py",
        "    if measurement is not None:\n        changes.update(_measured_changes(measurement))",
        "    if False:\n        changes.update(_measured_changes(measurement))",
        "fal's figure is obtained and then dropped; every attempt stays "
        "estimated while the code reads as though it measures",
    ),
    (
        "spend counts every paid attempt as measured",
        "backend/generation.py",
        "    from_fal = [a for a in paid_for if measured(a)]\n"
        "    inferred = [a for a in paid_for if not measured(a)]",
        "    from_fal = list(paid_for)\n    inferred = []",
        "an all-estimated ledger reports its whole total as measured against fal",
    ),
    (
        "spend_is_measured is aliased to spend_is_certain",
        "backend/generation.py",
        '        "spend_is_measured": not inferred,',
        '        "spend_is_measured": not unsettled,',
        "one key carries both questions again — certain about what happened is "
        "reported as certain about what it cost",
    ),
    (
        "a legacy row is backfilled as measured on read",
        "backend/generation.py",
        "    for a in rows:\n        if a.error.startswith(ABANDONED):",
        "    for a in rows:\n        if a.estimated_cost and not a.cost_source:\n"
        "            a.cost_source = MEASURED\n"
        "            a.cost = a.estimated_cost\n"
        "        if a.error.startswith(ABANDONED):",
        "every historical estimate is promoted into a field that means actual",
    ),
    (
        "an unrecognised cost_source is read as estimated instead of refused",
        "backend/generation.py",
        '    if raw.get("cost_source", "") not in _COST_SOURCES:',
        "    if False:",
        "a ledger whose provenance field nobody understands is silently "
        "classified rather than refused",
    ),

    # --- the measurement itself ---------------------------------------------------
    (
        "billable_units falls back to a number when the header is absent",
        "backend/fal_billing.py",
        "    raw = resp.headers.get(BILLABLE_UNITS_HEADER)\n    if raw is None:\n        return None",
        "    raw = resp.headers.get(BILLABLE_UNITS_HEADER)\n    if raw is None:\n        return 1.0",
        "an endpoint that reports nothing is priced at one unit and called "
        "measured",
    ),
    (
        "a failed result fetch is treated as zero units",
        "backend/fal_billing.py",
        '    if getattr(resp, "status_code", 0) != 200:\n        return None\n'
        "    raw = resp.headers.get(BILLABLE_UNITS_HEADER)",
        '    if getattr(resp, "status_code", 0) != 200:\n        return 0.0\n'
        "    raw = resp.headers.get(BILLABLE_UNITS_HEADER)",
        "a network fault reports a measured $0.00 over money that did go",
    ),
    (
        "a negative or unparseable header is coerced rather than refused",
        "backend/fal_billing.py",
        "    try:\n        units = float(raw)\n    except (TypeError, ValueError):\n        return None",
        "    try:\n        units = float(raw)\n    except (TypeError, ValueError):\n        units = 0.0",
        "a header this code does not understand becomes a bill of $0.00",
    ),
    (
        "the queue URL keeps the variant path and 404s",
        "backend/fal_billing.py",
        '    return f"{QUEUE_HOST}/{parts[0]}/{parts[1]}/requests/{request_id}"',
        '    return f"{QUEUE_HOST}/{endpoint}/requests/{request_id}"',
        "every measurement 404s, silently, and every attempt stays estimated "
        "while the code reads as though it measures",
    ),
    (
        "measure_quietly stops being quiet",
        "backend/fal_billing.py",
        "    try:\n        return measure(endpoint, request_id, get=get)\n"
        "    except Exception as exc:  # noqa: BLE001",
        "    if True:\n        return measure(endpoint, request_id, get=get)\n"
        "    try:\n        pass\n    except Exception as exc:  # noqa: BLE001",
        "a fault while asking what a clip cost is recorded as a generation that "
        "may not have billed — the opposite of the truth, since the clip is in "
        "hand",
    ),

    # --- reachability: obtained, then dropped ---------------------------------------
    (
        "the request id is never captured",
        "backend/director.py",
        "    result = fal_client.subscribe(endpoint, arguments=arguments, with_logs=False,\n"
        "                                  on_enqueue=request_ids.append)",
        "    result = fal_client.subscribe(endpoint, arguments=arguments, with_logs=False)",
        "fal cannot be asked what it billed, and no measurement is ever possible",
    ),
    (
        "generate_paid_clip stops calling back with the measurement",
        "backend/director.py",
        "    if on_billed is not None and request_ids:",
        "    if False:",
        "the producing half goes dark; the ledger silently reverts to estimates",
    ),
    (
        "the compile drops the measurement on the floor",
        "backend/director.py",
        "                                generation.succeed(plan.beat_id, att.id, ds.clip,\n"
        "                                                   measurement=(billed or [None])[0])",
        "                                generation.succeed(plan.beat_id, att.id, ds.clip)",
        "THE REACHABILITY MUTATION. Every unit test over generation.py still "
        "passes: the ledger can hold a measured cost and nothing puts one there",
    ),
    (
        "the scene total drops the measured/estimated split",
        "backend/planner.py",
        '        "spend_is_measured": not estimated_attempts,',
        '        "spend_is_measured": True,',
        "one measured beat and one estimated beat sum into a confident number",
    ),
]


def run(work: Path) -> tuple[int, str]:
    p = subprocess.run(
        [sys.executable, "-m", "pytest", *TESTS, "-x", "-q", "-p", "no:cacheprovider"],
        cwd=work, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr)[-1500:]


def main() -> int:
    survivors = []
    for label, rel, find, repl, why in MUTATIONS:
        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / "repo"
            shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(
                ".git", "__pycache__", "node_modules", ".pytest_cache", "*.mp4",
                "*.png", "*.wav", "*.mp3", "render", "scratch"))
            target = work / rel
            src = target.read_text(encoding="utf-8")
            if find not in src:
                print(f"[ SKIP ] {label}\n         anchor not found in {rel}")
                survivors.append((label, "anchor not found — harness is stale"))
                continue
            target.write_text(src.replace(find, repl, 1), encoding="utf-8")
            code, tail = run(work)
            if code == 0:
                print(f"[SURVIVES] {label}")
                print(f"           would mean: {why}")
                survivors.append((label, why))
            else:
                print(f"[ KILLED ] {label}")

    print()
    if survivors:
        print(f"{len(survivors)} mutation(s) survived:")
        for label, why in survivors:
            print(f"  - {label}: {why}")
        return 1
    print(f"all {len(MUTATIONS)} mutations killed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

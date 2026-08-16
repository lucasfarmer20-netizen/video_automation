"""One entry point for every mutation harness in this directory.

    python scratch/mutate_all.py            # run them all, in order
    python scratch/mutate_all.py --list     # what exists, and how many mutations
    python scratch/mutate_all.py --only gate1,slice8_spend

There are eight harnesses now. Eight commands, eight exit codes and eight
summaries read to a human as "some mutation testing happened"; nobody
reconstructs the total, and a harness that stopped being run is invisible
because nothing was expecting it. This file is the list, so a harness that is
not in it does not exist and one that fails cannot be lost in scrollback.

It **runs** the harnesses; it does not reimplement them. Each one keeps its own
mutations, probes, anchors and restore logic, and is executed as its own process
exactly as it would be by hand. Consolidating the entry point is the point;
rewriting the mutations would lose the reasoning attached to each one, which is
most of their value.

Three things it is careful about, each of which has burned this project before:

  * **Serial, always.** Six of these harnesses mutate ``backend/main.py`` and
    three mutate ``frontend/src/app/page.tsx``. Two running at once would
    interleave edits and restores on the same file, and the survivors and the
    tree hash would both be fiction. There is deliberately no --jobs flag.
  * **Exit codes are read from the process, not from a pipe.** A previous
    hand-off reported ``HARNESS_EXIT=0`` that was ``tail``'s exit code while the
    harness had returned 1 with four unproven signatures. ``proc.returncode`` is
    the only thing here that decides pass or fail, and every harness's code is
    printed in the summary whether it passed or not.
  * **A harness that could not run is not a harness that passed.** The two
    frontend harnesses need ``frontend/node_modules``; without it they are
    reported NOT RUN and the overall exit is non-zero, because "we did not check"
    and "we checked and it was fine" must not print the same way.
"""
from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FRONTEND = ROOT / "frontend"


@dataclass
class Harness:
    key: str                 # what --only matches
    module: str              # file stem in scratch/
    suite: str               # "python" | "frontend" | "both"
    covers: str              # the safeguard, in one line


# Order is oldest-slice-first, and within a slice cheapest-first. A harness that
# fails early tells you more per minute than one that fails after the two hour
# frontend sweep.
HARNESSES = [
    Harness("gate1", "mutate_gate1", "python",
            "Gate 1: gate_cleared() and the four doors that consult it"),
    Harness("paid_path", "mutate_paid_path", "python",
            "the paid path: begin()'s dispositions, the dispatch boundary, "
            "terminal facts, which tier a beat lands in"),
    Harness("isolation", "mutate_isolation", "python",
            "the three irreversible outcomes: a manifest overwritten by another "
            "film, an unknown project served the active one, a project deleted "
            "after a short copy"),
    Harness("slice7", "mutate_slice7", "python",
            "§11.7 export equivalence, §9.1 no-overwrite, §9.2 append-only history"),
    Harness("slice8_spend", "mutate_slice8_spend", "python",
            "§6.1 spend accounting: spend/billed/at_risk/unknown_spend + consumers"),
    Harness("slice8_tg_s4", "mutate_slice8_tg_s4", "python",
            "TG-S4-04/05/06: compile dispatch, ledger identity, the schema gate"),
    Harness("slice8_storage_gate", "mutate_slice8_storage_gate", "python",
            "the storage gate: refuse rather than serve the disk copy"),
    Harness("slice8_storage_gate_ui", "mutate_slice8_storage_gate_ui", "frontend",
            "the storage gate on screen: state the block, never offer to create over it"),
    Harness("slice8", "mutate_slice8", "both",
            "project identity on create and delete (#7, #8)"),
    Harness("5b", "mutate_5b", "frontend",
            "the timeline UI bound to slots, not to file paths"),
]


def count_mutations(h: Harness) -> int | None:
    """How many mutations a harness defines, without running it.

    Imported rather than parsed: every harness declares MUTATIONS at module
    scope and does its work under ``if __name__ == "__main__"``, so importing is
    cheap and cannot start a run. A count that came from a regex would drift the
    first time someone built a mutation in a loop -- which mutate_gate1 already
    does for the five prefix lengths.
    """
    path = HERE / f"{h.module}.py"
    if not path.is_file():
        print(f"  ! {h.module}.py does not exist")
        return None
    name = f"_harness_{h.key}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec. Every harness builds its Mutation type with
    # @dataclass, and dataclasses resolves field types through
    # sys.modules[cls.__module__].__dict__ -- which is None for a module that is
    # being executed but not yet registered, and the import dies with a bare
    # AttributeError that looks like a harness bug rather than a loader one.
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # noqa: BLE001 -- reported, not swallowed
        print(f"  ! could not import {h.module}: {type(exc).__name__}: {exc}")
        return None
    finally:
        sys.modules.pop(name, None)
    return len(getattr(mod, "MUTATIONS", []))


def runnable(h: Harness) -> str:
    """"" if it can run here, otherwise why not."""
    if h.suite in ("frontend", "both") and not (FRONTEND / "node_modules").is_dir():
        return "frontend/node_modules is absent — run `npm ci` in frontend/"
    return ""


def run(h: Harness) -> tuple[int, float]:
    """Run one harness as its own process. Returns (exit code, seconds).

    Output is inherited rather than captured: these runs are long, and a harness
    whose progress is invisible for forty minutes gets killed by whoever is
    watching. Nothing is piped, so the returncode below is the harness's own.
    """
    started = time.monotonic()
    proc = subprocess.run([sys.executable, str(HERE / f"{h.module}.py")], cwd=ROOT)
    return proc.returncode, time.monotonic() - started


def main() -> int:
    # The harness descriptions carry section marks; on Windows the console
    # defaults to cp1252 and a summary line that cannot be encoded is a crash in
    # the reporter, not in anything it was reporting.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", action="store_true",
                    help="print the harnesses and their mutation counts, run nothing")
    ap.add_argument("--only", default="",
                    help="comma-separated harness keys to run")
    args = ap.parse_args()

    wanted = [k.strip() for k in args.only.split(",") if k.strip()]
    unknown = [k for k in wanted if k not in {h.key for h in HARNESSES}]
    if unknown:
        print(f"unknown harness key(s): {', '.join(unknown)}")
        print(f"known: {', '.join(h.key for h in HARNESSES)}")
        return 2
    selected = [h for h in HARNESSES if not wanted or h.key in wanted]

    counts = {h.key: count_mutations(h) for h in selected}
    total = sum(c for c in counts.values() if c)
    missing = [h.key for h in selected if counts[h.key] is None]

    print(f"{len(selected)} harness(es), {total} mutations")
    for h in selected:
        why = runnable(h)
        n = counts[h.key]
        state = "MISSING" if n is None else (f"{n:>3} mutations")
        print(f"  {h.key:<24} {h.suite:<8} {state}   {h.covers}")
        if why:
            print(f"  {'':<24} {'':<8} NOT RUNNABLE HERE: {why}")
    if missing:
        print(f"\nharness file(s) not found: {', '.join(missing)}")

    if args.list:
        return 1 if missing else 0

    results: list[tuple[Harness, int | None, float]] = []
    for h in selected:
        why = runnable(h)
        if counts[h.key] is None:
            results.append((h, None, 0.0))
            continue
        if why:
            print(f"\n{'=' * 78}\n== SKIPPING {h.key}: {why}\n{'=' * 78}")
            results.append((h, None, 0.0))
            continue
        print(f"\n{'=' * 78}\n== {h.key}  ({counts[h.key]} mutations, {h.suite} suite)"
              f"\n== {h.covers}\n{'=' * 78}", flush=True)
        code, secs = run(h)
        results.append((h, code, secs))

    print(f"\n{'=' * 78}\n== summary\n{'=' * 78}")
    ran = failed = 0
    for h, code, secs in results:
        n = counts[h.key]
        if code is None:
            verdict, shown = "NOT RUN", "  -"
        else:
            verdict = "clean" if code == 0 else "FAILED"
            shown = f"{code:>3}"
            ran += n or 0
            failed += 0 if code == 0 else 1
        print(f"  {h.key:<24} exit {shown}  {verdict:<8} "
              f"{(n or 0):>3} mutations  {secs / 60:5.1f} min")
    not_run = [h.key for h, code, _ in results if code is None]
    print(f"\n  {ran} of {total} mutations exercised"
          + (f"; NOT RUN: {', '.join(not_run)}" if not_run else ""))
    if failed:
        print(f"  {failed} harness(es) reported a survivor, an unproven signature, "
              f"or a tree it could not restore — read their sections above.")
    return 1 if failed or not_run or missing else 0


if __name__ == "__main__":
    raise SystemExit(main())

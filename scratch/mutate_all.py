"""One entry point for every mutation harness in this directory.

    python scratch/mutate_all.py            # run them all, in order
    python scratch/mutate_all.py --list     # what exists, and how many mutations
    python scratch/mutate_all.py --only gate1,slice8_spend

One command per harness means one exit code and one summary each, and a set of
them reads to a human as "some mutation testing happened": nobody reconstructs
the total by hand, and a harness that stopped being run is invisible because
nothing was expecting it. This file is the list, so a harness that is not in it
does not exist and one that fails cannot be lost in scrollback.

**No count is written down here.** An earlier version of this docstring said
"eight harnesses" and "six of these mutate backend/main.py"; both were stale
within one commit, in a file whose whole claim is to be the authoritative list.
Every number this module states is derived at run time from ``HARNESSES`` and
from each harness's own ``MUTATIONS`` -- the header prints the harness count and
the mutation total, and ``shared_targets()`` prints which files more than one
harness edits. A number nobody maintains is worse than no number, and this file
is the last place that should assert one from memory.

It **runs** the harnesses; it does not reimplement them. Each one keeps its own
mutations, probes, anchors and restore logic, and is executed as its own process
exactly as it would be by hand. Consolidating the entry point is the point;
rewriting the mutations would lose the reasoning attached to each one, which is
most of their value.

Four things it is careful about, each of which has burned this project before:

  * **Serial, always.** Several of these edit the same production files -- run
    ``--list`` for the derived contention report. Two running at once would
    interleave edits and restores on one file, and the survivors and the tree
    hash would both be fiction. There is deliberately no --jobs flag.
  * **The tree is checked against git BEFORE anything runs.** The hashes below
    are taken from what is on disk, so they cannot see a mutation a *previous*
    killed run left behind -- that leftover simply becomes this run's baseline,
    and every result is computed against it while the drift check reports
    nothing. A harness restores in a ``finally`` and a kill does not reach it, so
    this is the ordinary failure, not an exotic one. Refusing beats warning: a
    warning at the top of a fifteen-minute run is a warning nobody reads.
  * **The tree is verified BETWEEN harnesses, not only at the end.** Each
    harness returns non-zero if it could not restore what it edited, but a
    summary line is read after the fact, and by then the next harness has
    already run against a dirty tree. Worse, the leftover is by construction a
    mutation that was mid-run, so it may be a SURVIVOR -- in which case the next
    harness's baseline suite passes and every result from there on is computed
    against mutated production code and reported with full confidence. So the
    union of every selected harness's target files is hashed before the run and
    re-checked after each one, and a mismatch STOPS the run rather than being
    noted. Restoring is deliberately not attempted: the correct content is
    whatever git has, and guessing it here would be a third opinion.
  * **Exit codes are read from the process, not from a pipe.** A previous
    hand-off reported ``HARNESS_EXIT=0`` that was ``tail``'s exit code while the
    harness had returned 1 with four unproven signatures. ``proc.returncode`` is
    the only thing here that decides pass or fail, and every harness's code is
    printed in the summary whether it passed or not.
  * **A harness that could not run is not a harness that passed.** The frontend
    harnesses need ``frontend/node_modules``; without it they are reported NOT
    RUN and the overall exit is non-zero, because "we did not check" and "we
    checked and it was fine" must not print the same way. A harness skipped
    because the run stopped early is reported the same way.
"""
from __future__ import annotations

import argparse
import hashlib
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
    Harness("pass_a", "mutate_pass_a", "python",
            "Audit Pass A: the Director approval boundary — the force bypass, "
            "undecided Critic findings, derived warning identity, durable dispositions"),
    Harness("isolation", "mutate_isolation", "python",
            "the four irreversible outcomes: a manifest overwritten by another "
            "film, an unknown project served the active one on a WRITE, a "
            "project deleted after a short copy, a delete escaping the workspace"),
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


@dataclass
class Loaded:
    """What one harness declares, read without running it."""
    count: int | None                       # None means it could not be loaded
    targets: frozenset                      # the files its mutations edit


def inspect(h: Harness) -> Loaded:
    """Read a harness's mutation count and target files, without running it.

    Imported rather than parsed: every harness declares MUTATIONS at module
    scope and does its work under ``if __name__ == "__main__"``, so importing is
    cheap and cannot start a run. A count that came from a regex would drift the
    first time someone built a mutation in a loop -- which mutate_gate1 already
    does for the five prefix lengths -- and the target list would drift the first
    time an anchor moved to another module.
    """
    path = HERE / f"{h.module}.py"
    if not path.is_file():
        print(f"  ! {h.module}.py does not exist")
        return Loaded(None, frozenset())
    name = f"_harness_{h.key}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return Loaded(None, frozenset())
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
        return Loaded(None, frozenset())
    finally:
        sys.modules.pop(name, None)
    mutations = getattr(mod, "MUTATIONS", [])
    targets = {Path(p) for m in mutations for p, _, _ in getattr(m, "edits", [])}
    return Loaded(len(mutations), frozenset(targets))


def shared_targets(loaded: dict) -> dict:
    """Files more than one selected harness edits, keyed by path.

    This is the derived form of the claim the docstring makes about running
    serially. Stating it as a number in prose is how it went stale; computing it
    means the justification is re-checked on every run and a new harness that
    starts editing a shared file shows up here without anyone remembering to say
    so.
    """
    owners: dict = {}
    for key, info in loaded.items():
        for p in info.targets:
            owners.setdefault(p, []).append(key)
    return {p: sorted(keys) for p, keys in owners.items() if len(keys) > 1}


def dirty_targets(paths) -> list[str]:
    """Which of ``paths`` differ from what git has. Repo-relative, sorted.

    ``git status --porcelain`` over the exact paths rather than the whole tree:
    an untracked scratch file or an in-progress doc edit is not a reason to
    refuse a mutation run, and refusing on those would train people to reach
    straight for --allow-dirty.

    A git that cannot answer -- not a checkout, not installed -- returns nothing
    rather than raising. This is a precondition on top of the hash check, not a
    replacement for it, and a harness run is more useful than a refusal to run
    somewhere git is absent.
    """
    if not paths:
        return []
    rel = sorted(str(Path(p).relative_to(ROOT)).replace("\\", "/")
                 for p in paths if str(p).startswith(str(ROOT)))
    if not rel:
        return []
    try:
        proc = subprocess.run(["git", "status", "--porcelain", "--", *rel],
                              cwd=ROOT, capture_output=True, text=True)
    except (OSError, ValueError):  # pragma: no cover - no git on this machine
        return []
    if proc.returncode != 0:
        return []
    out = []
    for line in (proc.stdout or "").splitlines():
        name = line[3:].strip().strip('"')
        # A rename prints "old -> new"; the one that matters is where it is now.
        if " -> " in name:
            name = name.split(" -> ", 1)[1]
        if name:
            out.append(name)
    return sorted(set(out))


def hash_targets(paths) -> dict:
    """sha256 of every target file that exists. Missing files map to None."""
    out = {}
    for p in sorted(paths, key=str):
        try:
            out[p] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            out[p] = None
    return out


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
    ap.add_argument("--allow-dirty", action="store_true",
                    help="run even though a target file differs from git; the "
                         "run then says so rather than implying a clean tree")
    args = ap.parse_args()

    wanted = [k.strip() for k in args.only.split(",") if k.strip()]
    unknown = [k for k in wanted if k not in {h.key for h in HARNESSES}]
    if unknown:
        print(f"unknown harness key(s): {', '.join(unknown)}")
        print(f"known: {', '.join(h.key for h in HARNESSES)}")
        return 2
    selected = [h for h in HARNESSES if not wanted or h.key in wanted]

    loaded = {h.key: inspect(h) for h in selected}
    counts = {k: info.count for k, info in loaded.items()}
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

    # Why this runs serially, derived rather than asserted. If this report is
    # empty the harnesses are independent and a --jobs flag would be defensible;
    # while it is not, one is not.
    contended = shared_targets(loaded)
    if contended:
        print(f"\nfiles more than one selected harness edits "
              f"({len(contended)} — why this runs serially):")
        for p, keys in sorted(contended.items(), key=lambda kv: str(kv[0])):
            try:
                shown = p.relative_to(ROOT)
            except ValueError:                      # pragma: no cover
                shown = p
            print(f"  {str(shown):<40} {len(keys)}x  {', '.join(keys)}")

    if args.list:
        return 1 if missing else 0

    # Every file any selected harness will edit, hashed before anything runs.
    # This is the between-harness check: a harness that could not restore what
    # it touched leaves the NEXT one measuring mutated production code, and if
    # the leftover happens to be a survivor its baseline suite passes and
    # everything downstream is wrong with full confidence.
    watched = set().union(*(info.targets for info in loaded.values())) \
        if loaded else set()

    # ...and the half the hashes CANNOT cover, which is where they are taken.
    #
    # `before` is whatever is on disk right now. A run killed mid-mutation -- a
    # Ctrl-C, a reaped container, a worker torn down -- leaves an edit behind,
    # because the harness restores in a `finally` and a kill does not reach it.
    # The next invocation then hashes that leftover AS the baseline, every drift
    # check compares against it, and this runner reports a clean tree while every
    # harness measures mutated production code.
    #
    # The only thing between that and a wrong answer is each harness's own
    # "baseline suite: PASS" -- and a leftover is by construction a mutation that
    # was mid-run, so it may be a SURVIVOR, in which case the baseline passes and
    # the numbers are wrong with full confidence. This is not hypothetical: five
    # harness kills in one day across this project's workers, each one restored
    # by hand from a habit rather than by anything the tool required.
    #
    # So git is asked first. Refusing beats warning, because a warning at the top
    # of a fifteen-minute run is a warning nobody reads; and git is the reference
    # rather than this runner, for the same reason the drift check does not
    # repair what it finds -- the correct content is whatever was committed, and
    # a guess here would be a third opinion about the file.
    dirty = dirty_targets(watched)
    if dirty and not args.allow_dirty:
        print(f"\n{'!' * 78}")
        print(f"REFUSING TO START: {len(dirty)} target file(s) already differ from git.")
        for shown in dirty:
            print(f"  - {shown}")
        print("A mutation left behind by a killed run would be hashed as this "
              "run's baseline, and every result computed against it.")
        print(f"  git checkout -- {' '.join(dirty)}")
        print("Or --allow-dirty if these are edits you meant to measure against.")
        print(f"{'!' * 78}")
        return 2
    if dirty:
        print(f"\n--allow-dirty: measuring against {len(dirty)} uncommitted "
              f"target file(s), so these results are NOT about the committed tree:")
        for shown in dirty:
            print(f"  - {shown}")

    before = hash_targets(watched)
    print(f"\nwatching {len(watched)} target file(s) for drift between harnesses")

    results: list[tuple[Harness, int | None, float]] = []
    stopped = ""
    for h in selected:
        why = runnable(h)
        if stopped:
            results.append((h, None, 0.0))
            continue
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

        now = hash_targets(watched)
        drift = [p for p in watched if before[p] != now.get(p)]
        if drift:
            # Deliberately NOT repaired here. The correct content is whatever
            # git has; writing a guess would make this a third opinion about
            # the file, and the harnesses already restore from content they
            # captured at their own entry.
            stopped = h.key
            print(f"\n{'!' * 78}")
            print(f"STOPPING: {h.key} left {len(drift)} target file(s) changed.")
            for p in sorted(drift, key=str):
                try:
                    shown = p.relative_to(ROOT)
                except ValueError:                  # pragma: no cover
                    shown = p
                print(f"  - {shown}")
            print("Every later harness would run against mutated production "
                  "code, and a leftover that SURVIVED its own suite would let "
                  "their baselines pass. Nothing after this point is trustworthy.")
            print("  git status && git checkout -- <file>   then re-run.")
            print(f"{'!' * 78}", flush=True)

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

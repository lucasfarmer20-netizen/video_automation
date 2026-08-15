"""Mutation harness for V1 slice 8 item 1 — spend under-reporting (§6.1).

    python scratch/mutate_slice8_spend.py

Same shape as ``mutate_slice7.py``, for the same two reasons the guardrails
give, and one more that is specific to money:

  * **a test that fails under a faithful mutation of the fix** — every mutation
    below must be killed. A survivor is a safeguard no test protects;
  * **a mutation that genuinely reproduces the defect** — the known failure mode
    is a mutation that "passes" because it accidentally hid the bug it was
    restoring. So the ``DEFECT`` mutations run a **probe**: a real spend report,
    read under the mutation, checked for the defect's own signature. The
    signature must appear under the mutation and must NOT appear pristine.

The money-specific reason: this defect is silent by construction. It does not
raise, it does not corrupt anything, and the number it prints is a perfectly
well-formed $0.00. Nothing but a probe that reads the reported figure can tell
the fixed code from the broken code, because both look completely healthy.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GEN = ROOT / "backend" / "generation.py"
DIRECTOR = ROOT / "backend" / "director.py"
MAIN = ROOT / "backend" / "main.py"


# --------------------------------------------------------------------------- #
# probes: a real spend report, read under the mutation
# --------------------------------------------------------------------------- #

_PRELUDE = """
import json, sys, tempfile, types
from pathlib import Path
sys.path.insert(0, r"__ROOT__")
for _m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(_m, types.ModuleType(_m))
from backend import config, generation

tmp = Path(tempfile.mkdtemp())
config.MANIFEST_PATH = tmp / "storyboard_manifest.json"

def report(s):
    # Older shapes have no at_risk key at all, which is itself the defect.
    return {"spent": s.get("spent"), "at_risk": s.get("at_risk"),
            "paid_attempts": s.get("paid_attempts"),
            "at_risk_attempts": s.get("at_risk_attempts")}
"""

# The reported defect, in the roadmap's own words: a clip bought and used
# reports $0. The record carries the provider's success -- output AND cost --
# and the attempt was closed as abandoned afterwards.
PROBE_BOUGHT = _PRELUDE + """
generation._save_attempts("s001", [generation.GenerationAttempt(
    id="s001.01.a1", shot_id="s001.01", beat_id="s001", attempt=1,
    status=generation.FAILED, paid=True, cost=0.60,
    output="render/s001/s001.01.mp4",
    error="abandoned: wrote it off after the timeout")])
s = generation.spend("s001")
print("PROBE_BOUGHT_REPORT=" + json.dumps(report(s)))
print("PROBE_BOUGHT_CLIP_REPORTS_ZERO=" + ("true" if s.get("spent") == 0.0 else "false"))
"""

# The reachable half, through the real compile path: the provider is called, it
# never answers, a human abandons the attempt. Nobody knows whether it billed,
# and the defect is that the report says nothing at all about it.
PROBE_STRANDED = _PRELUDE + """
from backend import director
from backend.manifest import Camera, Shot, Storyboard

plan = director.CoveragePlan(beat_id="s011", beat_duration=5.0, status="locked")
ds = director.DirectorShot(id="s011.01", beat_id="s011", motion_type="ai_video",
                           camera=Camera(move="static", duration=5.0),
                           prompt="a hand on drill steel", subject="single jack",
                           shot_size="m", purpose="master")
ds.draft_variations = ["a.png"]
ds.chosen_variation = 0
plan.coverage = [ds]
director.approve(plan)
director.save_plan(plan)

sb = Storyboard(title="T", storyboard_approved=True,
                shots=[Shot(scene_id="s011", narration="n", prompt="p",
                            camera=Camera(move="static", duration=5.0))])
render_dir = tmp / "render"
render_dir.mkdir(parents=True, exist_ok=True)

def timeout(*a, **k):
    raise RuntimeError("provider timeout after submit")

director.generate_paid_clip = timeout
director.normalize_clip = lambda *a, **k: None
director.fit_clip = lambda *a, **k: None
try:
    director.compile_coverage(director.load_plan("s011"), sb, render_dir,
                              log=lambda m: None)
except Exception:
    pass

in_doubt = generation.spend("s011")
stuck = [a for a in generation.for_shot("s011", "s011.01") if a.status == "running"]
generation.abandon("s011", stuck[0].id, "checked the dashboard, no answer")
after = generation.spend("s011")

print("PROBE_IN_DOUBT_REPORT=" + json.dumps(report(in_doubt)))
print("PROBE_AFTER_ABANDON_REPORT=" + json.dumps(report(after)))
print("PROBE_ABANDONED_MONEY_INVISIBLE="
      + ("true" if (after.get("at_risk") or 0.0) == 0.0 else "false"))
print("PROBE_IN_DOUBT_MONEY_INVISIBLE="
      + ("true" if (in_doubt.get("at_risk") or 0.0) == 0.0 else "false"))
"""

PROBES = {"bought": PROBE_BOUGHT, "stranded": PROBE_STRANDED}


@dataclass
class Mutation:
    name: str
    clause: str
    removes: str                       # the behaviour this deletes, in one line
    edits: list[tuple[Path, str, str]]
    # Set for the mutations that restore a real defect: (probe, signature) pairs
    # where the signature must appear under the mutation and must NOT appear
    # pristine. That pair is what separates "the defect came back" from
    # "something broke".
    probes: list[tuple[str, str]] = field(default_factory=list)
    defect: bool = False
    expect: list[str] = field(default_factory=list)


# --- anchors ----------------------------------------------------------------------

BILLED = "    return a.paid and (a.status == SUCCEEDED or bool(a.output) or a.cost > 0)"
AT_RISK = (
    "    return a.paid and not billed(a) and (\n"
    "        a.status == RUNNING or a.outcome_unknown or a.error.startswith(ABANDONED))"
)
AMOUNT = "    return a.cost if a.cost else a.estimated_cost"
SPENT_KEY = '        "spent": spent,'
CERTAIN_KEY = '        "spend_is_certain": not unsettled,'
SUMMARY_RISK = "    if unsettled:"
RECORD_PRICE = "            estimated_cost=float(estimated_cost or 0.0),"
IN_DOUBT_FLAG = "        target.outcome_unknown = True"
ABANDON_FLAG = (
    "    return _finish(beat_id, attempt_id, status=FAILED,\n"
    "                   error=f\"{ABANDONED}{reason}\", outcome_unknown=True)"
)
DIRECTOR_PRICE = "                            estimated_cost=PAID_CLIP_COST,"
API_HOISTED = '            "at_risk": spend["at_risk"],'
ABANDON_ROUTE = '            "spend": spend, "at_risk": spend["at_risk"]}'
PLAN_PAYLOAD = '        d["at_risk"] = d["spend"]["at_risk"]'


MUTATIONS = [
    # ---- reproductions of the reported defect ----------------------------------
    Mutation(
        "DEFECT §6.1: spend() counts only paid AND succeeded", "§6.1",
        "the whole fix at once — the pre-fix predicate, so a bought clip that "
        "was abandoned contributes 0.00 and nothing reports the exposure",
        [(GEN, BILLED, "    return a.paid and a.status == SUCCEEDED  # MUTANT"),
         (GEN, AT_RISK, "    return False  # MUTANT")],
        probes=[("bought", "PROBE_BOUGHT_CLIP_REPORTS_ZERO=true"),
                ("stranded", "PROBE_ABANDONED_MONEY_INVISIBLE=true"),
                ("stranded", "PROBE_IN_DOUBT_MONEY_INVISIBLE=true")],
        defect=True,
        expect=["a_bought_clip_that_was_abandoned_still_reports_what_it_cost",
                "an_unrecorded_outcome_is_money_at_risk_not_money_ignored",
                "abandoning_the_attempt_does_not_settle_the_bill"],
    ),
    Mutation(
        "DEFECT §6.1: a bought clip stops counting once it is closed some other "
        "way", "§6.1",
        "rule 1 only — the exposure is still reported, but a clip the record "
        "says was produced reports $0.00 because the status was rewritten",
        [(GEN, BILLED, "    return a.paid and a.status == SUCCEEDED  # MUTANT")],
        probes=[("bought", "PROBE_BOUGHT_CLIP_REPORTS_ZERO=true")],
        defect=True,
        expect=["a_bought_clip_that_was_abandoned_still_reports_what_it_cost",
                "a_bought_clip_left_in_doubt_still_reports_what_it_cost",
                "recorded_media_alone_counts_as_bought",
                "a_recorded_cost_alone_counts_as_bought"],
    ),
    Mutation(
        "DEFECT §6.1: an unknown outcome is reported as nothing", "§6.1",
        "rule 2 only — money the provider may have taken is invisible again, "
        "which is the reachable half of the defect on today's code",
        [(GEN, AT_RISK, "    return False  # MUTANT")],
        probes=[("stranded", "PROBE_ABANDONED_MONEY_INVISIBLE=true"),
                ("stranded", "PROBE_IN_DOUBT_MONEY_INVISIBLE=true")],
        defect=True,
        expect=["an_unrecorded_outcome_is_money_at_risk_not_money_ignored",
                "abandoning_the_attempt_does_not_settle_the_bill",
                "a_running_attempt_is_money_at_risk"],
    ),
    Mutation(
        "DEFECT §6.1: the at-risk figure is always $0.00", "§6.1",
        "the price recorded before dispatch — the exposure is counted but has "
        "no amount, which is the original defect wearing a new key",
        [(GEN, AMOUNT, "    return a.cost  # MUTANT")],
        probes=[("stranded", "PROBE_ABANDONED_MONEY_INVISIBLE=true")],
        defect=True,
        expect=["an_unrecorded_outcome_is_money_at_risk_not_money_ignored",
                "abandoning_the_attempt_does_not_settle_the_bill",
                "recorded_media_alone_counts_as_bought"],
    ),
    Mutation(
        "DEFECT §6.1: the price is never recorded, so nothing can be quantified",
        "§6.1",
        "the caller's half of the same thing — director stops telling the ledger "
        "what the attempt was about to cost, and after a crash there is no "
        "second chance to record it",
        [(DIRECTOR, DIRECTOR_PRICE, "  # MUTANT: price not recorded")],
        probes=[("stranded", "PROBE_ABANDONED_MONEY_INVISIBLE=true")],
        defect=True,
        expect=["the_price_is_recorded_before_the_provider_is_called",
                "an_unrecorded_outcome_is_money_at_risk_not_money_ignored"],
    ),

    # ---- faithful mutations of the fix ------------------------------------------
    Mutation(
        "certain and uncertain money are added together", "§6.1",
        "the separation §6.1 rests on — `spent` stops meaning what has ACTUALLY "
        "been billed, and neither figure is answerable afterwards",
        [(GEN, SPENT_KEY, '        "spent": round(spent + risk, 4),  # MUTANT')],
        expect=["at_risk_money_is_never_folded_into_the_billed_total",
                "an_unrecorded_outcome_is_money_at_risk_not_money_ignored"],
    ),
    Mutation(
        "a killed-mid-generation attempt stops counting as exposure", "§6.1",
        "the shape nothing can distinguish from a live one — a process killed "
        "between the provider call and in_doubt() records no flag at all",
        [(GEN, AT_RISK,
          "    return a.paid and not billed(a) and (  # MUTANT\n"
          "        a.outcome_unknown or a.error.startswith(ABANDONED))")],
        expect=["a_running_attempt_is_money_at_risk"],
    ),
    Mutation(
        "closing the attempt settles the bill", "§6.1",
        "the exposure surviving abandon() — the money disappears from the "
        "report at exactly the moment a human writes the attempt off",
        [(GEN, AT_RISK,
          "    return a.paid and not billed(a) and a.status == RUNNING  # MUTANT")],
        expect=["abandoning_the_attempt_does_not_settle_the_bill",
                "a_legacy_abandoned_record_is_still_money_at_risk"],
    ),
    Mutation(
        "a ledger written before outcome_unknown loses its exposure", "§6.1",
        "the fallback for old records — every attempt abandoned before this "
        "slice silently drops to $0.00",
        [(GEN, AT_RISK,
          "    return a.paid and not billed(a) and (  # MUTANT\n"
          "        a.status == RUNNING or a.outcome_unknown)")],
        expect=["a_legacy_abandoned_record_is_still_money_at_risk"],
    ),
    Mutation(
        "recorded media stops being evidence the clip was bought", "§6.1",
        "one of the two things a record can say about a provider success — a "
        "row carrying the clip but not its price falls out of the billed total",
        [(GEN, BILLED,
          "    return a.paid and (a.status == SUCCEEDED or a.cost > 0)  # MUTANT")],
        expect=["recorded_media_alone_counts_as_bought"],
    ),
    Mutation(
        "a recorded cost stops being evidence the clip was bought", "§6.1",
        "the other one — a provider that billed and returned nothing usable "
        "reports no charge",
        [(GEN, BILLED,
          "    return a.paid and (a.status == SUCCEEDED or bool(a.output))  # MUTANT")],
        expect=["a_recorded_cost_alone_counts_as_bought"],
    ),
    Mutation(
        "an ordinary failure is reported as exposure", "§6.1",
        "the other direction — every pre-dispatch failure becomes money at "
        "risk, which is how a real figure turns into noise nobody reads",
        [(GEN, AT_RISK, "    return a.paid and not billed(a)  # MUTANT")],
        expect=["an_ordinary_failure_is_not_money_at_risk",
                "a_failure_before_dispatch_is_an_ordinary_retryable_failure"],
    ),
    Mutation(
        "the report always claims to be certain", "§6.1",
        "the one-line answer to 'is this number the whole story' — it now says "
        "yes while attempts are unresolved",
        [(GEN, CERTAIN_KEY, '        "spend_is_certain": True,  # MUTANT')],
        expect=["an_unrecorded_outcome_is_money_at_risk_not_money_ignored"],
    ),
    Mutation(
        "the summary states the billed total and stops there", "§6.1",
        "the one string a caller is most likely to render verbatim — it reads "
        "as a complete answer while money is unaccounted for",
        [(GEN, SUMMARY_RISK, "    if False:  # MUTANT")],
        expect=["an_unrecorded_outcome_is_money_at_risk_not_money_ignored",
                "the_lineage_api_reports_what_is_at_risk_beside_the_total"],
    ),
    Mutation(
        "the ledger route stops hoisting the at-risk figure", "§6.1",
        "the guarantee that a client reading the total alone cannot miss it",
        [(MAIN, API_HOISTED, "  # MUTANT: not hoisted")],
        expect=["the_lineage_api_reports_what_is_at_risk_beside_the_total"],
    ),
    Mutation(
        "abandoning answers with the billed total only", "§6.1",
        "the exposure at the exact moment it is created — the human who just "
        "wrote off a possible charge is told nothing happened",
        [(MAIN, ABANDON_ROUTE, '            "spend": spend}  # MUTANT')],
        expect=["abandoning_answers_with_the_money_it_put_at_risk"],
    ),
    Mutation(
        "the plan payload reports the block without the money", "§6.1",
        "the money on the screen that already reports the stuck attempt",
        [(MAIN, PLAN_PAYLOAD, "        pass  # MUTANT")],
        expect=["the_plan_payload_reports_the_money_at_risk"],
    ),
    Mutation(
        "in_doubt() and abandon() stop recording that the outcome is unknown",
        "§6.1",
        "the recorded fact, leaving only the error text — with BOTH writers "
        "removed, since either one alone is covered by the other's fallback",
        [(GEN, IN_DOUBT_FLAG, "        pass  # MUTANT"),
         (GEN, ABANDON_FLAG,
          "    return _finish(beat_id, attempt_id, status=FAILED,  # MUTANT\n"
          "                   error=f\"unresolved: {reason}\")")],
        expect=["abandoning_the_attempt_does_not_settle_the_bill"],
    ),
]


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #

def run_suite() -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def run_probe(name: str) -> tuple[bool, str]:
    script = PROBES[name].replace("__ROOT__", str(ROOT))
    proc = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def probe_lines(out: str) -> list[str]:
    return [ln.strip() for ln in out.splitlines() if ln.strip().startswith("PROBE_")]


def failing_tests(out: str) -> list[str]:
    return sorted({line.split(" ", 1)[1].strip()
                   for line in out.splitlines() if line.startswith("FAILED ")})


def _read(path: Path) -> str:
    """Read without translating line endings — restoration has to be byte-exact
    or the tree check is measuring the harness, not the mutations."""
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def apply(path: Path, find: str, replace: str) -> None:
    """One edit, refusing anything but an exactly-one-site anchor.

    ``str.replace`` silently takes the first match, so an ambiguous anchor
    mutates a site the run then reports results about with total confidence.
    """
    text = _read(path)
    if "\r\n" in text:
        find, replace = find.replace("\n", "\r\n"), replace.replace("\n", "\r\n")
    hits = text.count(find)
    if hits == 0:
        raise SystemExit(f"anchor not found in {path.name}:\n{find}")
    if hits > 1:
        raise SystemExit(
            f"anchor matches {hits} sites in {path.name} — narrow it:\n{find}")
    _write(path, text.replace(find, replace, 1))


def digest(paths) -> dict:
    return {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def main() -> int:
    touched = sorted({p for m in MUTATIONS for p, _, _ in m.edits})
    pristine = {p: _read(p) for p in touched}
    before = digest(touched)

    ok, out = run_suite()
    print(f"baseline suite: {'PASS' if ok else 'FAIL'}")
    if not ok:
        print(out[-4000:])
        return 1

    clean_probes: dict[str, str] = {}
    for name in sorted(PROBES):
        pok, pout = run_probe(name)
        clean_probes[name] = pout
        print(f"baseline probe {name}: {'ran' if pok else 'ERRORED'}")
        for ln in probe_lines(pout):
            print(f"    {ln}")
        if not pok:
            print(pout[-2500:])
            return 1

    survived, unproven = [], []
    for mut in MUTATIONS:
        snapshot = {p: _read(p) for p, _, _ in mut.edits}
        probe_out: dict[str, str] = {}
        try:
            for path, find, replace in mut.edits:
                apply(path, find, replace)
            suite_ok, suite_out = run_suite()
            for name in {n for n, _ in mut.probes}:
                probe_out[name] = run_probe(name)[1]
        finally:
            for path, text in snapshot.items():
                _write(path, text)

        killed = not suite_ok
        print(f"\n[{mut.clause}] {mut.name}")
        print(f"    removes: {mut.removes}")
        print(f"    suite: {'killed' if killed else 'SURVIVED'}")
        failed = failing_tests(suite_out)
        for t in failed[:8]:
            print(f"      x {t}")
        if len(failed) > 8:
            print(f"      ... and {len(failed) - 8} more")
        if not killed:
            survived.append(mut.name)

        # A mutation killed only by collateral damage elsewhere is not evidence
        # about this safeguard, so each names the test that must catch it.
        for want in mut.expect:
            if not any(want in t for t in failed):
                unproven.append(f"{mut.name}: expected {want} to fail")

        for name, sig in mut.probes:
            print(f"    probe {name}:")
            for ln in probe_lines(probe_out.get(name, "")):
                print(f"      {ln}")
            if not mut.defect:
                continue
            back = sig in probe_out.get(name, "")
            clean = sig in clean_probes[name]
            print(f"    defect restored: {back} (signature {sig!r}); "
                  f"present pristine: {clean}")
            if not back:
                unproven.append(
                    f"{mut.name}: probe {name} did NOT show the defect ({sig}) "
                    f"— the mutation may have hidden the bug it was restoring")
            if clean:
                unproven.append(
                    f"{mut.name}: the signature {sig} is present PRISTINE — the "
                    f"probe does not measure this defect")

    after = digest(touched)
    dirty = [p.name for p in touched if before[p] != after[p]]
    for p in touched:
        if before[p] != after[p]:
            _write(p, pristine[p])

    ok, _ = run_suite()
    print(f"\nrestored suite: {'PASS' if ok else 'FAIL'}")
    if dirty:
        print(f"  TREE NOT RESTORED — was still modified: {', '.join(dirty)}")
    if survived:
        print("\nmutations that SURVIVED (unprotected safeguards):")
        for name in survived:
            print(f"  - {name}")
    if unproven:
        print("\nnot proven:")
        for line in unproven:
            print(f"  - {line}")
    return 1 if survived or unproven or dirty or not ok else 0


if __name__ == "__main__":
    raise SystemExit(main())

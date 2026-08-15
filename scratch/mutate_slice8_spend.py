"""Mutation harness for V1 slice 8 item 1 — spend under-reporting (§6.1).

    python scratch/mutate_slice8_spend.py

Same shape as ``mutate_slice7.py``, for the same two reasons the guardrails
give, and one more that is specific to money:

  * **a test that fails under a faithful mutation of the fix** — every mutation
    below must be killed. A survivor is a safeguard no test protects;
  * **a mutation that genuinely reproduces the defect** — the known failure mode
    is a mutation that "passes" because it accidentally hid the bug it was
    restoring.

The money-specific reason: this defect is silent by construction. It does not
raise, it corrupts nothing, and the number it prints is a perfectly well-formed
$0.00. Both the fixed and the broken code look completely healthy.

So **every** mutation here declares a probe signature, not just the ones that
restore a historical defect. A mutation that reddens the suite without showing
the behaviour it claims to remove proves the tests are sensitive to *something*;
it does not prove they guard the thing named. Each signature must appear under
the mutation and must NOT appear pristine, and a mutation with no probe is a
hard error rather than a quiet omission.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GEN = ROOT / "backend" / "generation.py"
DIRECTOR = ROOT / "backend" / "director.py"
MAIN = ROOT / "backend" / "main.py"


# --------------------------------------------------------------------------- #
# probes: real spend reports, read under the mutation
# --------------------------------------------------------------------------- #

_PRELUDE = """
import json, sys, tempfile, types
from pathlib import Path
sys.path.insert(0, r"__ROOT__")
for _m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(_m, types.ModuleType(_m))
from backend import atomic, config, generation

tmp = Path(tempfile.mkdtemp())
config.MANIFEST_PATH = tmp / "storyboard_manifest.json"

def report(s):
    # Older shapes have no at_risk key at all, which is itself the defect.
    return {"spent": s.get("spent"), "at_risk": s.get("at_risk"),
            "paid_attempts": s.get("paid_attempts"),
            "at_risk_attempts": s.get("at_risk_attempts")}

def write_raw(beat_id, doc):
    p = generation.ledger_path(beat_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_json(p, doc)
"""

# A real coverage plan whose paid generation reaches the provider and dies. Used
# by every probe that has to exercise the live path rather than a hand-written
# ledger.
_SCENE = """
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

def strand():
    try:
        director.compile_coverage(director.load_plan("s011"), sb, render_dir,
                                  log=lambda m: None)
    except Exception:
        pass
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
# never answers, a human abandons the attempt. Nobody knows whether it billed.
PROBE_STRANDED = _PRELUDE + _SCENE + """
strand()
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

# Every shape a paid row can take, classified one by one. This is what lets a
# mutation of a single clause show the row it reclassifies, instead of only
# reddening a test name.
_ROW = ('{"id": "s001.01.%s", "shot_id": "s001.01", "beat_id": "s001", '
        '"attempt": %d, %s}')
PROBE_SHAPES = _PRELUDE + """
rows = [
  ("succeeded",        {"status": "succeeded", "paid": True, "cost": 0.60,
                        "output": "a.mp4"}),
  ("media_only",       {"status": "failed", "paid": True, "output": "a.mp4",
                        "estimated_cost": 0.60,
                        "error": "abandoned: the cost never came back"}),
  ("cost_only",        {"status": "failed", "paid": True, "cost": 0.60,
                        "error": "abandoned: billed, nothing usable"}),
  ("running",          {"status": "running", "paid": True,
                        "estimated_cost": 0.60}),
  ("in_doubt",         {"status": "running", "paid": True,
                        "estimated_cost": 0.60, "outcome_unknown": True,
                        "error": "provider timeout after submit"}),
  ("abandoned",        {"status": "failed", "paid": True,
                        "estimated_cost": 0.60, "outcome_unknown": True,
                        "error": "abandoned: no answer"}),
  ("legacy_abandoned", {"status": "failed", "paid": True,
                        "estimated_cost": 0.60,
                        "error": "abandoned: resolved by hand months ago"}),
  ("plain_fail",       {"status": "failed", "paid": True,
                        "estimated_cost": 0.60,
                        "error": "rejected before dispatch"}),
  ("free",             {"status": "running", "paid": False,
                        "estimated_cost": 0.60}),
]
doc = {"beat_id": "s001", "attempts": []}
for i, (name, over) in enumerate(rows):
    row = {"id": "s001.01." + name, "shot_id": "s001.01", "beat_id": "s001",
           "attempt": i + 1}
    row.update(over)
    doc["attempts"].append(row)
write_raw("s001", doc)

loaded = generation.load_attempts("s001")
for a in loaded:
    name = a.id.split(".")[-1]
    if generation.billed(a):
        how = "billed"
    elif generation.at_risk(a):
        how = "at_risk"
    else:
        how = "neither"
    print("PROBE_CLASS_" + name + "=" + how)

s = generation.spend("s001")
print("PROBE_SHAPES_SPENT=" + repr(s.get("spent")))
print("PROBE_SHAPES_AT_RISK=" + repr(s.get("at_risk")))
print("PROBE_SHAPES_CERTAIN=" + ("true" if s.get("spend_is_certain") else "false"))
print("PROBE_SHAPES_SUMMARY=" + str(s.get("summary")))
print("PROBE_SHAPES_SUMMARY_STATES_RISK="
      + ("true" if "at risk" in str(s.get("summary")) else "false"))
"""

# A row abandoned before outcome_unknown existed, abandoned again. It used to be
# a 200 no-op; the fix that made the row reportable made it unrecoverable.
PROBE_REPLAY = _PRELUDE + """
write_raw("s001", {"beat_id": "s001", "attempts": [{
    "id": "s001.01.a1", "shot_id": "s001.01", "beat_id": "s001", "attempt": 1,
    "status": "failed", "paid": True, "estimated_cost": 0.60,
    "error": "abandoned: checked the dashboard, no answer"}]})
s = generation.spend("s001")
print("PROBE_REPLAY_AT_RISK=" + repr(s.get("at_risk")))
try:
    again = generation.abandon("s001", "s001.01.a1",
                               "checked the dashboard, no answer")
    print("PROBE_REPLAY=ok")
except Exception as exc:
    print("PROBE_REPLAY=refused:" + type(exc).__name__)
"""

# Present, syntactically valid, and not a ledger. The HIGH finding: `{}` used to
# read as an empty lineage, which spend() then reported as a CERTAIN $0.00.
PROBE_MALFORMED = _PRELUDE + """
GOOD = {"id": "s001.01.a1", "shot_id": "s001.01", "beat_id": "s001",
        "attempt": 1, "status": "succeeded", "paid": True, "cost": 0.60,
        "output": "a.mp4"}

def without(key):
    return {k: v for k, v in GOOD.items() if k != key}

def over(**kw):
    row = dict(GOOD)
    row.update(kw)
    return row

CASES = {
    "empty_object":     {},
    "no_attempts":      {"beat_id": "s001"},
    "attempts_not_list": {"beat_id": "s001", "attempts": {"a": 1}},
    "doc_is_list":      [],
    "row_not_object":   {"beat_id": "s001", "attempts": ["s001.01.a1"]},
    "row_missing_id":   {"beat_id": "s001", "attempts": [without("id")]},
    "cost_is_string":   {"beat_id": "s001", "attempts": [over(cost="0.60")]},
    "status_unknown":   {"beat_id": "s001", "attempts": [
        {"id": "s001.01.a1", "shot_id": "s001.01", "beat_id": "s001",
         "attempt": 1, "status": "cancelled", "paid": True,
         "estimated_cost": 0.60}]},
    "foreign_ledger":   {"beat_id": "s999", "attempts": [dict(GOOD)]},
    "foreign_row":      {"beat_id": "s001", "attempts": [over(beat_id="s999")]},
}

any_certain_zero = False
for name in sorted(CASES):
    write_raw("s001", CASES[name])
    try:
        s = generation.spend("s001")
        outcome = ("reported:" + repr(s.get("spent")) + "/" + repr(s.get("at_risk"))
                   + "/" + ("true" if s.get("spend_is_certain") else "false"))
        if not s.get("spent") and not s.get("at_risk") and s.get("spend_is_certain"):
            any_certain_zero = True
    except generation.LedgerUnreadable:
        outcome = "unreadable"
    except Exception as exc:
        outcome = "raised:" + type(exc).__name__
    print("PROBE_MALFORMED_" + name + "=" + outcome)
print("PROBE_MALFORMED_CERTAIN_ZERO=" + ("true" if any_certain_zero else "false"))
"""

# What the three HTTP consumers actually put on the wire, readable and not.
PROBE_ROUTES = _PRELUDE + _SCENE + """
from fastapi.testclient import TestClient
from backend import main as M
M.get_current_project = lambda: sb
M.config.MANIFEST_PATH = config.MANIFEST_PATH
M.get_active_manifest_path = lambda: str(config.MANIFEST_PATH)
client = TestClient(M.app, raise_server_exceptions=False)

strand()

lineage = client.get("/api/generation/s011").json()
print("PROBE_API_AT_RISK=" + repr(lineage.get("at_risk", "absent")))
print("PROBE_API_AT_RISK_HOISTED="
      + ("true" if "at_risk" in lineage else "false"))

plan = client.get("/api/director/plan/s011").json()["plan"]
print("PROBE_PLAN_AT_RISK=" + repr(plan.get("at_risk", "absent")))

stuck = lineage["unresolved"][0]
reply = client.post("/api/generation/s011/" + stuck["id"] + "/abandon",
                    json={"reason": "no answer from the provider"}).json()
print("PROBE_ABANDON_REPLY_AT_RISK=" + repr(reply.get("at_risk", "absent")))

generation.ledger_path("s011").write_text("{not json", encoding="utf-8")
broken = client.get("/api/director/plan/s011").json()["plan"]
spend = broken.get("spend", "absent")
print("PROBE_PLAN_UNREADABLE_SPEND=" + json.dumps(spend))
print("PROBE_PLAN_UNREADABLE_AT_RISK=" + repr(broken.get("at_risk", "absent")))
print("PROBE_PLAN_UNREADABLE_SPENT="
      + (repr(spend.get("spent")) if isinstance(spend, dict) else "absent"))
# The distinction that matters, and the only one a client can act on: is
# "nothing at stake" tellable from "could not tell"? A `?? 0` renders null and
# 0 alike, so THAT is not the discriminator -- whether the payload still
# carries the difference is.
print("PROBE_PLAN_UNREADABLE_STATES_UNKNOWN="
      + ("true" if isinstance(spend, dict) and spend.get("spent") is None
         else "false"))
"""

PROBES = {
    "bought": PROBE_BOUGHT,
    "stranded": PROBE_STRANDED,
    "shapes": PROBE_SHAPES,
    "replay": PROBE_REPLAY,
    "malformed": PROBE_MALFORMED,
    "routes": PROBE_ROUTES,
}


@dataclass
class Mutation:
    name: str
    clause: str
    removes: str                       # the behaviour this deletes, in one line
    edits: list[tuple[Path, str, str]]
    # (probe, signature) pairs. The signature must appear under the mutation and
    # must NOT appear pristine -- that pair is what separates "the defect came
    # back" from "something broke". Required for every mutation, not only the
    # ones restoring a historical defect.
    probes: list[tuple[str, str]] = field(default_factory=list)
    defect: bool = False               # restores a defect that really shipped
    expect: list[str] = field(default_factory=list)


# --- anchors ----------------------------------------------------------------------

BILLED = "    return a.paid and (a.status == SUCCEEDED or bool(a.output) or a.cost > 0)"
AT_RISK = (
    "    return a.paid and not billed(a) and (a.status == RUNNING or a.outcome_unknown)"
)
AMOUNT = "    return a.cost if a.cost else a.estimated_cost"
SPENT_KEY = '        "spent": spent,'
CERTAIN_KEY = '        "spend_is_certain": not unsettled,'
SUMMARY_RISK = "    if unsettled:"
MIGRATE = (
    "    for a in rows:\n"
    "        if a.error.startswith(ABANDONED):\n"
    "            a.outcome_unknown = True"
)
UNKNOWN_SPENT = '        "spent": None,'
UNKNOWN_RISK = '        "at_risk": None,'

# The whole schema gate, so the mutation restores the exact pre-fix line.
VALIDATE_DOC = (
    "    if not isinstance(raw, dict):\n"
    "        raise LedgerUnreadable(\n"
    "            f\"the generation ledger for {beat_id} is a \"\n"
    "            f\"{type(raw).__name__}, not an object. It has NOT been \"\n"
    "            f\"overwritten; inspect it before generating again.\")"
)
VALIDATE_LIST = "    if not isinstance(listed, list):"
VALIDATE_BEAT = '    if raw.get("beat_id") != beat_id:'
ROW_TYPES = "        if key in _SHAPE and not _valid(_SHAPE[key], value):"
ROW_REQUIRED = "        if key not in raw:"
ROW_STATUS = "    if raw.get(\"status\", RUNNING) not in _STATUSES:"
ROW_BEAT = '    if raw["beat_id"] != beat_id:'

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
PLAN_UNREADABLE = (
    "        d[\"spend\"] = generation.unknown_spend(\"the generation ledger for this \"\n"
    "                                              \"beat could not be read\")\n"
    "        d[\"at_risk\"] = None"
)


MUTATIONS = [
    # ---- reproductions of a defect that really shipped -------------------------
    Mutation(
        "DEFECT §6.1: spend() counts only paid AND succeeded", "§6.1",
        "the whole fix at once — the pre-fix predicate, so a bought clip that "
        "was abandoned contributes 0.00 and nothing reports the exposure",
        [(GEN, BILLED, "    return a.paid and a.status == SUCCEEDED  # MUTANT"),
         (GEN, AT_RISK, "    return False  # MUTANT")],
        probes=[("bought", "PROBE_BOUGHT_CLIP_REPORTS_ZERO=true"),
                ("stranded", "PROBE_ABANDONED_MONEY_INVISIBLE=true"),
                ("stranded", "PROBE_IN_DOUBT_MONEY_INVISIBLE=true"),
                ("shapes", "PROBE_SHAPES_SPENT=0.6"),
                ("shapes", "PROBE_SHAPES_AT_RISK=0.0")],
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
        probes=[("bought", "PROBE_BOUGHT_CLIP_REPORTS_ZERO=true"),
                ("shapes", "PROBE_CLASS_media_only=at_risk"),
                ("shapes", "PROBE_CLASS_cost_only=at_risk")],
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
                ("stranded", "PROBE_IN_DOUBT_MONEY_INVISIBLE=true"),
                ("shapes", "PROBE_CLASS_running=neither"),
                ("shapes", "PROBE_CLASS_abandoned=neither")],
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
        probes=[("stranded", "PROBE_ABANDONED_MONEY_INVISIBLE=true"),
                ("shapes", "PROBE_SHAPES_AT_RISK=0.0")],
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
    Mutation(
        "DEFECT TG-S4-06: a malformed ledger is a CERTAIN zero", "§6.1",
        "the schema gate — `{}` reads as an empty lineage again, and spend() "
        "now stamps that $0.00 as settled fact rather than merely omitting it",
        [(GEN, VALIDATE_DOC + "\n" + VALIDATE_BEAT, "    if False:  # MUTANT"),
         (GEN, VALIDATE_LIST, "    if False:  # MUTANT"),
         (GEN, "    rows = [_row(beat_id, i, a) for i, a in enumerate(listed)]",
          "    known = set(GenerationAttempt.__dataclass_fields__)  # MUTANT\n"
          "    rows = [GenerationAttempt(**{k: v for k, v in a.items() if k in known})\n"
          "            for a in (raw.get(\"attempts\") or [])]")],
        probes=[("malformed", "PROBE_MALFORMED_CERTAIN_ZERO=true"),
                ("malformed", "PROBE_MALFORMED_empty_object=reported:0.0/0.0/true"),
                ("malformed", "PROBE_MALFORMED_attempts_not_list=raised:AttributeError")],
        defect=True,
        expect=["a_malformed_ledger_is_unreadable_not_empty",
                "a_malformed_ledger_never_reports_a_certain_zero"],
    ),
    Mutation(
        "DEFECT §6.1: abandon() stops marking the outcome unknown", "§6.1",
        "both writers of the recorded fact AND the marker the read migration "
        "keys on, so an abandoned attempt reports no exposure at all",
        [(GEN, IN_DOUBT_FLAG, "        pass  # MUTANT"),
         (GEN, ABANDON_FLAG,
          "    return _finish(beat_id, attempt_id, status=FAILED,  # MUTANT\n"
          "                   error=f\"unresolved: {reason}\")")],
        probes=[("stranded", "PROBE_ABANDONED_MONEY_INVISIBLE=true")],
        defect=True,
        expect=["abandoning_the_attempt_does_not_settle_the_bill"],
    ),

    # ---- faithful mutations of the fix ------------------------------------------
    Mutation(
        "certain and uncertain money are added together", "§6.1",
        "the separation §6.1 rests on — `spent` stops meaning what has ACTUALLY "
        "been billed, and neither figure is answerable afterwards",
        [(GEN, SPENT_KEY, '        "spent": round(spent + risk, 4),  # MUTANT')],
        probes=[("shapes", "PROBE_SHAPES_SPENT=4.2")],
        expect=["at_risk_money_is_never_folded_into_the_billed_total",
                "an_unrecorded_outcome_is_money_at_risk_not_money_ignored"],
    ),
    Mutation(
        "a killed-mid-generation attempt stops counting as exposure", "§6.1",
        "the shape nothing can distinguish from a live one — a process killed "
        "between the provider call and in_doubt() records no flag at all",
        [(GEN, AT_RISK,
          "    return a.paid and not billed(a) and a.outcome_unknown  # MUTANT")],
        probes=[("shapes", "PROBE_CLASS_running=neither")],
        expect=["a_running_attempt_is_money_at_risk"],
    ),
    Mutation(
        "closing the attempt settles the bill", "§6.1",
        "the exposure surviving abandon() — the money disappears from the "
        "report at exactly the moment a human writes the attempt off",
        [(GEN, AT_RISK,
          "    return a.paid and not billed(a) and a.status == RUNNING  # MUTANT")],
        probes=[("shapes", "PROBE_CLASS_abandoned=neither"),
                ("shapes", "PROBE_CLASS_legacy_abandoned=neither")],
        expect=["abandoning_the_attempt_does_not_settle_the_bill",
                "a_legacy_abandoned_record_is_still_money_at_risk"],
    ),
    Mutation(
        "a ledger written before outcome_unknown loses its exposure", "§6.1",
        "the read-time migration — every attempt abandoned before this slice "
        "silently drops to $0.00, AND stops being re-abandonable, because its "
        "shape no longer matches what abandon() now writes",
        [(GEN, MIGRATE, "    pass  # MUTANT: no migration")],
        probes=[("shapes", "PROBE_CLASS_legacy_abandoned=neither"),
                ("replay", "PROBE_REPLAY=refused:TerminalConflict")],
        expect=["a_legacy_abandoned_record_is_still_money_at_risk",
                "a_legacy_abandoned_record_can_still_be_abandoned_again"],
    ),
    Mutation(
        "recorded media stops being evidence the clip was bought", "§6.1",
        "one of the two things a record can say about a provider success — a "
        "row carrying the clip but not its price falls out of the billed total",
        [(GEN, BILLED,
          "    return a.paid and (a.status == SUCCEEDED or a.cost > 0)  # MUTANT")],
        probes=[("shapes", "PROBE_CLASS_media_only=at_risk")],
        expect=["recorded_media_alone_counts_as_bought"],
    ),
    Mutation(
        "a recorded cost stops being evidence the clip was bought", "§6.1",
        "the other one — a provider that billed and returned nothing usable "
        "reports no charge",
        [(GEN, BILLED,
          "    return a.paid and (a.status == SUCCEEDED or bool(a.output))  # MUTANT")],
        probes=[("shapes", "PROBE_CLASS_cost_only=at_risk")],
        expect=["a_recorded_cost_alone_counts_as_bought"],
    ),
    Mutation(
        "an ordinary failure is reported as exposure", "§6.1",
        "the other direction — every pre-dispatch failure becomes money at "
        "risk, which is how a real figure turns into noise nobody reads",
        [(GEN, AT_RISK, "    return a.paid and not billed(a)  # MUTANT")],
        probes=[("shapes", "PROBE_CLASS_plain_fail=at_risk")],
        expect=["an_ordinary_failure_is_not_money_at_risk",
                "a_failure_before_dispatch_is_an_ordinary_retryable_failure"],
    ),
    Mutation(
        "the report always claims to be certain", "§6.1",
        "the one-line answer to 'is this number the whole story' — it now says "
        "yes while attempts are unresolved",
        [(GEN, CERTAIN_KEY, '        "spend_is_certain": True,  # MUTANT')],
        probes=[("shapes", "PROBE_SHAPES_CERTAIN=true")],
        expect=["an_unrecorded_outcome_is_money_at_risk_not_money_ignored"],
    ),
    Mutation(
        "the summary states the billed total and stops there", "§6.1",
        "the one string a caller is most likely to render verbatim — it reads "
        "as a complete answer while money is unaccounted for",
        [(GEN, SUMMARY_RISK, "    if False:  # MUTANT")],
        probes=[("shapes", "PROBE_SHAPES_SUMMARY_STATES_RISK=false")],
        expect=["an_unrecorded_outcome_is_money_at_risk_not_money_ignored",
                "the_lineage_api_reports_what_is_at_risk_beside_the_total"],
    ),

    # ---- the schema gate, clause by clause --------------------------------------
    Mutation(
        "a row's field types are no longer checked", "§6.1",
        "the type gate — a cost of \"0.60\" reaches the total and fails as an "
        "incidental TypeError deep in a sum, which is TG-S4-06's shape",
        [(GEN, ROW_TYPES, "        if False:  # MUTANT")],
        probes=[("malformed", "PROBE_MALFORMED_cost_is_string=raised:TypeError")],
        expect=["a_malformed_ledger_is_unreadable_not_empty",
                "a_malformed_ledger_never_reports_a_certain_zero"],
    ),
    Mutation(
        "a row with no id is constructed anyway", "§6.1",
        "the identity gate — a row nobody can reconcile against a bill becomes "
        "a constructor TypeError instead of a stated unreadable ledger",
        [(GEN, ROW_REQUIRED, "        if False:  # MUTANT")],
        probes=[("malformed", "PROBE_MALFORMED_row_missing_id=raised:TypeError")],
        expect=["a_malformed_ledger_is_unreadable_not_empty"],
    ),
    Mutation(
        "an unrecognised status is accepted", "§6.1",
        "the status gate — a row in a state no guard knows counts as neither "
        "billed, nor at risk, nor in flight, and drops out of every check while "
        "the report calls itself certain",
        [(GEN, ROW_STATUS, "    if False:  # MUTANT")],
        probes=[("malformed", "PROBE_MALFORMED_status_unknown=reported:0.0/0.0/true")],
        expect=["a_malformed_ledger_never_reports_a_certain_zero"],
    ),
    Mutation(
        "a row belonging to another beat is counted here", "§6.1",
        "the identity gate at row level — one beat is billed for another's "
        "generation",
        [(GEN, ROW_BEAT, "    if False:  # MUTANT")],
        probes=[("malformed", "PROBE_MALFORMED_foreign_row=reported:0.6/0.0/true")],
        expect=["a_malformed_ledger_is_unreadable_not_empty"],
    ),

    # ---- the consumers ----------------------------------------------------------
    Mutation(
        "an unreadable ledger reports no exposure instead of an unknown one",
        "§6.1",
        "the difference between $0.00 and 'could not tell' on the one path "
        "where the exposure is least known",
        [(GEN, UNKNOWN_SPENT, '        "spent": 0.0,  # MUTANT'),
         (GEN, UNKNOWN_RISK, '        "at_risk": 0.0,  # MUTANT')],
        probes=[("routes", "PROBE_PLAN_UNREADABLE_SPENT=0.0"),
                ("routes", "PROBE_PLAN_UNREADABLE_STATES_UNKNOWN=false")],
        expect=["an_unreadable_ledger_states_the_exposure_is_unknown"],
    ),
    Mutation(
        "the plan payload omits the figures when the ledger cannot be read",
        "§6.1",
        "the stated answer on the error path — an absent key reads as $0.00 "
        "through the `?? 0` every client eventually writes",
        [(MAIN, PLAN_UNREADABLE, "        pass  # MUTANT")],
        probes=[("routes", "PROBE_PLAN_UNREADABLE_SPEND=\"absent\""),
                ("routes", "PROBE_PLAN_UNREADABLE_STATES_UNKNOWN=false")],
        expect=["an_unreadable_ledger_states_the_exposure_is_unknown"],
    ),
    Mutation(
        "the ledger route stops hoisting the at-risk figure", "§6.1",
        "the guarantee that a client reading the total alone cannot miss it",
        [(MAIN, API_HOISTED, "  # MUTANT: not hoisted")],
        probes=[("routes", "PROBE_API_AT_RISK_HOISTED=false")],
        expect=["the_lineage_api_reports_what_is_at_risk_beside_the_total"],
    ),
    Mutation(
        "abandoning answers with the billed total only", "§6.1",
        "the exposure at the exact moment it is created — the human who just "
        "wrote off a possible charge is told nothing happened",
        [(MAIN, ABANDON_ROUTE, '            "spend": spend}  # MUTANT')],
        probes=[("routes", "PROBE_ABANDON_REPLY_AT_RISK='absent'")],
        expect=["abandoning_answers_with_the_money_it_put_at_risk"],
    ),
    Mutation(
        "the plan payload reports the block without the money", "§6.1",
        "the money on the screen that already reports the stuck attempt",
        [(MAIN, PLAN_PAYLOAD, "        pass  # MUTANT")],
        probes=[("routes", "PROBE_PLAN_AT_RISK='absent'")],
        expect=["the_plan_payload_reports_the_money_at_risk"],
    ),
]


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #

# The reported summary contains a "•", and on Windows both ends of the pipe
# default to cp1252: the child encodes it as cp1252, we decode as utf-8 and get
# U+FFFD, and printing that back to a cp1252 console raises. A signature check
# reading mojibake is a harness fault reported as a mutation result, so pin the
# encoding on both sides rather than sanitising after the fact.
_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


def run_suite() -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q"], cwd=ROOT, env=_ENV,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def run_probe(name: str) -> tuple[bool, str]:
    script = PROBES[name].replace("__ROOT__", str(ROOT))
    proc = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, env=_ENV,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def probe_lines(out: str) -> list[str]:
    return [ln.strip() for ln in out.splitlines() if ln.strip().startswith("PROBE_")]


def shows(out: str, signature: str) -> bool:
    """Whether a probe printed exactly this line.

    Equality, not ``in``. A substring signature silently matches a DIFFERENT
    value -- ``PROBE_SHAPES_AT_RISK=0`` is inside ``...=0.60`` -- so a mutation
    could be reported as demonstrating a zero while the probe printed sixty
    cents. That is the accidental-hiding failure the guardrails name, arriving
    through the check rather than through the mutation.
    """
    return signature in probe_lines(out)


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
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - older/odd streams
        pass

    # A mutation with no declared signature can only ever prove the suite is
    # sensitive to something. Refuse to run rather than report that as evidence.
    naked = [m.name for m in MUTATIONS if not m.probes]
    if naked:
        print("mutations with no probe signature:")
        for n in naked:
            print(f"  - {n}")
        return 1

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

    # Every signature must be ABSENT pristine. Checked up front so a typo in a
    # signature is reported as a harness fault, not as a mutation result.
    stale = [f"{m.name}: {sig} is present PRISTINE in probe {probe}"
             for m in MUTATIONS for probe, sig in m.probes
             if shows(clean_probes[probe], sig)]

    survived, unproven = [], list(stale)
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
        for t in failed[:6]:
            print(f"      x {t}")
        if len(failed) > 6:
            print(f"      ... and {len(failed) - 6} more")
        if not killed:
            survived.append(mut.name)

        # A mutation killed only by collateral damage elsewhere is not evidence
        # about this safeguard, so each names the test that must catch it.
        for want in mut.expect:
            if not any(want in t for t in failed):
                unproven.append(f"{mut.name}: expected {want} to fail")

        for name in sorted({n for n, _ in mut.probes}):
            print(f"    probe {name}:")
            for ln in probe_lines(probe_out.get(name, "")):
                print(f"      {ln}")
            if not probe_lines(probe_out.get(name, "")):
                print(f"      (no PROBE_ output — probe crashed under the mutation)")
        for name, sig in mut.probes:
            shown = shows(probe_out.get(name, ""), sig)
            print(f"    signature {'SHOWN' if shown else 'MISSING'}: {sig}")
            if not shown:
                unproven.append(
                    f"{mut.name}: probe {name} did NOT show {sig} — killed, but "
                    f"the behaviour it names was not demonstrated")

    after = digest(touched)
    dirty = [p.name for p in touched if before[p] != after[p]]
    for p in touched:
        if before[p] != after[p]:
            _write(p, pristine[p])

    ok, _ = run_suite()
    print(f"\nrestored suite: {'PASS' if ok else 'FAIL'}")
    print(f"mutations: {len(MUTATIONS)} defined, {len(survived)} survived, "
          f"{len(MUTATIONS) - len(survived)} killed")
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

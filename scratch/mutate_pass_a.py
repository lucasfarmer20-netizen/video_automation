"""Mutation harness for Audit Pass A — the Director approval boundary.

    python scratch/mutate_pass_a.py
    python scratch/mutate_pass_a.py --discover   # probes only, no pytest

This one exists because the inventory left it out.

`docs/audits/slice8_deferred_safeguard_inventory.md` was written to make the
round's scoping defensible: every slice 0-4 safeguard the retrofit does not
cover, with what it guards and what failing costs. Pass A -- four Director
defects closed in ``b1e466e`` alongside slices 0 and 1 -- appeared in **neither**
list. Not deferred with a reason; absent. An inventory's failure mode is
omission rather than error, and that is one.

It is also not deferrable. The scope bar for this round was "costs money or
admits unapproved work", and this boundary is the second clause exactly:

  * ``force=true`` used to skip the draft check entirely, so any caller could
    send an unapproved plan into a compile that generates stills and, for
    ai_video shots, **buys paid video**. The flag is gone rather than fixed;
  * both lock routes -- ``/api/director/lock_scene`` and
    ``/api/director/lock/{beat}`` -- used to lock straight past every unresolved
    Critic finding, so "approved" said nothing about whether the review had been
    answered;
  * the **compile** route asserts it again (``backend/main.py``, the ``undecided``
    409). That is not redundancy: it is what stops a plan locked *before* the
    rule existed walking into generation;
  * a warning's identity is derived from **content**. Trusting a caller-supplied
    id let a changed finding inherit the decision recorded about the one it
    replaced, so a new problem arrived pre-approved;
  * a disposition is **durable**. Dismissing a warning used to change React
    state and nothing else.

Same obligations as the rest. Every mutation must be killed, every mutation
declares a probe signature that must appear under it and be absent pristine, and
the signatures are about **dispatch** -- whether generation was handed the plan
-- rather than about response text. That is the doctrine
``tests/test_director_approval_gate.py`` was written on, and for the same reason:
a guard that returns a polite error and starts the job anyway reads as passing.

One mutation here *inserts* code rather than removing it: the ``force`` bypass.
That is deliberate and it is not the hypothetical-defect trap the inventory
declines elsewhere -- the bypass is not invented, it is the code that shipped,
restored. A safeguard implemented by DELETING a parameter can only be mutated by
putting it back.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "backend" / "main.py"
DIRECTOR = ROOT / "backend" / "director.py"

# The repository root is itself a project, so a probe that saves a plan can file
# it here if a mutation loses the project binding. Same ownership rule as the
# other harnesses: refuse to start if occupied, remove only what this run wrote.
ROOT_ARTIFACTS = (ROOT / "director" / "s903.json",
                  ROOT / "generation" / "s903.json")


# --------------------------------------------------------------------------- #
# probes
# --------------------------------------------------------------------------- #

_PRELUDE = '''
import json, sys, tempfile, types
from pathlib import Path
sys.path.insert(0, r"__ROOT__")
for _m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

from fastapi.testclient import TestClient
from backend import config, director
from backend import main as M
from backend.manifest import Camera, Shot, Storyboard

BEAT = "s903"

STRAY = [Path(r"__ROOT__") / "director" / (BEAT + ".json"),
         Path(r"__ROOT__") / "generation" / (BEAT + ".json")]
OCCUPIED = [str(p) for p in STRAY if p.exists()]
if OCCUPIED:
    print("PROBE_REFUSED_TO_RUN=" + json.dumps(OCCUPIED))
    raise SystemExit(3)

tmp = Path(tempfile.mkdtemp())
config.MANIFEST_PATH = tmp / "storyboard_manifest.json"
director.config.MANIFEST_PATH = config.MANIFEST_PATH

WARNING = {"beat_id": BEAT, "shot_id": BEAT + ".01", "kind": "repetition",
           "detail": "Three consecutive medium shots on the same axis.",
           "suggestion": "Vary one of them."}

# Dispatch, not response text. A guard that returns a polite error and starts
# the job anyway reads as passing, which is the whole reason the Pass A tests
# assert on start_job rather than on the body.
dispatched = []
M.start_job = lambda name, fn, *a, **k: (dispatched.append(name), True)[1]

sb = Storyboard(title="T", script_locked=True, storyboard_approved=True,
                shots=[Shot(scene_id=BEAT, narration="n", prompt="p",
                            camera=Camera(move="static", duration=12.0))])
M.get_current_project = lambda: sb
M.get_active_manifest_path = lambda: str(config.MANIFEST_PATH)

client = TestClient(M.app, raise_server_exceptions=False)


def plan(status="draft", warnings=None):
    p = director.CoveragePlan(beat_id=BEAT, beat_duration=12.0, status=status)
    p.coverage = [
        director.DirectorShot(id=BEAT + ".01", beat_id=BEAT, motion_type="parallax",
                              prompt="a", camera=Camera(move="static", duration=6.0)),
        director.DirectorShot(id=BEAT + ".02", beat_id=BEAT, motion_type="parallax",
                              prompt="b", camera=Camera(move="static", duration=6.0)),
    ]
    p.warnings = director.normalize_warnings(warnings or [])
    p.warning_dispositions = {}
    if status == "locked":
        director.approve(p)
    director.save_plan(p)
    return p


def sweep():
    for stray in STRAY:
        if stray.is_file():
            stray.unlink()
'''

# The boundary itself, driven through the real routes.
PROBE_GATE = _PRELUDE + '''
# --- a draft plan, with and without the flag that used to step over it --------
plan("draft")
del dispatched[:]
r = client.post("/api/director/compile/" + BEAT)
print("PROBE_PASSA_DRAFT_STATUS=%d" % r.status_code)
print("PROBE_PASSA_DRAFT_DISPATCHED=" + ("true" if dispatched else "false"))

plan("draft")
del dispatched[:]
r = client.post("/api/director/compile/" + BEAT + "?force=true")
print("PROBE_PASSA_FORCE_STATUS=%d" % r.status_code)
# The whole of A-01 in one line: an unapproved plan handed to generation, which
# generates stills and, for an ai_video shot, buys paid video.
print("PROBE_PASSA_FORCE_DISPATCHED=" + ("true" if dispatched else "false"))

# --- a plan locked before the review rule existed -----------------------------
plan("locked", [WARNING])
del dispatched[:]
r = client.post("/api/director/compile/" + BEAT)
print("PROBE_PASSA_LOCKED_UNDECIDED_STATUS=%d" % r.status_code)
print("PROBE_PASSA_LOCKED_UNDECIDED_DISPATCHED="
      + ("true" if dispatched else "false"))

# --- both lock routes, against an undecided finding ---------------------------
plan("draft", [WARNING])
r = client.post("/api/director/lock_scene", json={"beats": [BEAT]})
print("PROBE_PASSA_SCENE_LOCK_STATUS=%d" % r.status_code)
print("PROBE_PASSA_SCENE_LOCKED_OVER_WARNING="
      + ("true" if director.load_plan(BEAT).status == "locked" else "false"))

plan("draft", [WARNING])
r = client.post("/api/director/lock/" + BEAT)
print("PROBE_PASSA_BEAT_LOCK_STATUS=%d" % r.status_code)
print("PROBE_PASSA_BEAT_LOCKED_OVER_WARNING="
      + ("true" if director.load_plan(BEAT).status == "locked" else "false"))

# --- the controls. A gate that refuses everything is not a working gate -------
plan("draft", [])
client.post("/api/director/lock_scene", json={"beats": [BEAT]})
print("PROBE_PASSA_CLEAN_PLAN_LOCKS="
      + ("true" if director.load_plan(BEAT).status == "locked" else "false"))

p = plan("draft", [WARNING])
wid = p.warnings[0]["id"]
client.post("/api/director/warning/" + BEAT + "/" + wid,
            json={"decision": "accepted", "note": "deliberate"})
client.post("/api/director/lock/" + BEAT)
print("PROBE_PASSA_DECIDED_PLAN_LOCKS="
      + ("true" if director.load_plan(BEAT).status == "locked" else "false"))

plan("locked", [WARNING])
client.post("/api/director/lock/" + BEAT + "?locked=false")
print("PROBE_PASSA_UNLOCK_ALLOWED="
      + ("true" if director.load_plan(BEAT).status == "draft" else "false"))

plan("locked", [])
del dispatched[:]
client.post("/api/director/compile/" + BEAT)
print("PROBE_PASSA_CLEAN_LOCKED_COMPILES="
      + ("true" if dispatched else "false"))
sweep()
'''

# Warning identity: derived from what the finding says, never from who sent it.
PROBE_IDENTITY = _PRELUDE + '''
p = plan("draft", [dict(WARNING, id="critic-fixed-id")])
wid = p.warnings[0]["id"]
print("PROBE_PASSA_SUPPLIED_ID_BECAME_KEY="
      + ("true" if wid == "critic-fixed-id" else "false"))
print("PROBE_PASSA_SOURCE_ID_KEPT="
      + ("true" if p.warnings[0].get("source_id") == "critic-fixed-id" else "false"))

client.post("/api/director/warning/" + BEAT + "/" + wid,
            json={"decision": "accepted"})
print("PROBE_PASSA_DECISION_TOOK="
      + ("true" if not director.unresolved_warnings(director.load_plan(BEAT))
         else "false"))

# The critic now reports something else entirely under the SAME supplied id.
changed = {"id": "critic-fixed-id", "beat_id": BEAT, "shot_id": BEAT + ".09",
           "kind": "identity_risk", "detail": "The protagonist's face is unreadable."}
p2 = director.load_plan(BEAT)
p2.warnings = director.normalize_warnings([changed])
director.save_plan(p2)
left = director.unresolved_warnings(director.load_plan(BEAT))
print("PROBE_PASSA_CHANGED_FINDING_INHERITED_A_DECISION="
      + ("true" if not left else "false"))
r = client.post("/api/director/lock/" + BEAT)
print("PROBE_PASSA_LOCKED_OVER_A_NEW_FINDING="
      + ("true" if director.load_plan(BEAT).status == "locked" else "false"))

# What the derivation must cover. Two findings differing only by target, and a
# finding whose text was rewritten, each have to be a DIFFERENT warning -- an id
# that collides is a decision silently transferred onto a problem nobody read.
a = director.warning_id(dict(WARNING, shot_id=BEAT + ".01"))
b = director.warning_id(dict(WARNING, shot_id=BEAT + ".02"))
print("PROBE_PASSA_DIFFERENT_TARGETS_SHARE_AN_ID="
      + ("true" if a == b else "false"))
print("PROBE_PASSA_CHANGED_DETAIL_KEEPS_ITS_ID="
      + ("true" if director.warning_id(dict(WARNING, detail="Something else."))
         == director.warning_id(WARNING) else "false"))
print("PROBE_PASSA_ID_IS_STABLE="
      + ("true" if director.warning_id(WARNING) == director.warning_id(dict(WARNING))
         else "false"))
sweep()
'''

# A decision is a durable fact about the plan, not a thing the browser forgot.
PROBE_DURABLE = _PRELUDE + '''
p = plan("draft", [WARNING])
wid = p.warnings[0]["id"]
client.post("/api/director/warning/" + BEAT + "/" + wid,
            json={"decision": "resolved", "note": "on purpose"})

raw = json.loads(director.plan_path(BEAT).read_text(encoding="utf-8"))
print("PROBE_PASSA_DISPOSITION_ON_DISK="
      + ("true" if (raw.get("warning_dispositions") or {}).get(wid) else "false"))

reloaded = director.load_plan(BEAT)
print("PROBE_PASSA_DECISION_SURVIVES_RELOAD="
      + ("true" if (reloaded.warning_dispositions or {}).get(wid, {}).get("decision")
         == "resolved" else "false"))
print("PROBE_PASSA_UNRESOLVED_AFTER_RELOAD=%d"
      % len(director.unresolved_warnings(reloaded)))
print("PROBE_PASSA_FINDING_ITSELF_RETAINED=%d" % len(reloaded.warnings))

# Clearing a decision must make the warning block again -- otherwise "cleared"
# is another word for approved.
client.post("/api/director/warning/" + BEAT + "/" + wid, json={"decision": ""})
r = client.post("/api/director/lock/" + BEAT)
print("PROBE_PASSA_CLEARED_DECISION_STILL_LOCKS="
      + ("true" if director.load_plan(BEAT).status == "locked" else "false"))
sweep()
'''

PROBES = {
    "gate": PROBE_GATE,
    "identity": PROBE_IDENTITY,
    "durable": PROBE_DURABLE,
}


@dataclass
class Mutation:
    name: str
    clause: str
    removes: str
    edits: list[tuple[Path, str, str]]
    probes: list[tuple[str, str]] = field(default_factory=list)
    defect: bool = False
    expect: list[str] = field(default_factory=list)
    # The reachability obligation, same as mutate_gate1.py. `expect` proves the
    # right TEST died; `proves` proves the right ASSERTION inside it ran, and
    # `not_proves` catches a test red on its fixture check instead. These tests
    # were written to assert on DISPATCH rather than on response text, so the
    # phrases below are the sentences that say work was handed to generation --
    # not the status codes.
    #
    # `proves_note` is required when `proves` is empty; main() refuses to run
    # without one, so "the probe carries this" is written down rather than
    # assumed.
    proves: list[str] = field(default_factory=list)
    not_proves: list[str] = field(default_factory=list)
    proves_note: str = ""


# --- anchors ------------------------------------------------------------------

COMPILE_SIGNATURE = "def compile_director_coverage(beat_id: str):"
COMPILE_DRAFT_CHECK = (
    '        if plan.status == "draft":\n'
    "            drifted = next((h for h in reversed(plan.approval_history or [])"
)
COMPILE_UNDECIDED = (
    "        undecided = director.unresolved_warnings(plan)\n"
    "        if undecided:\n"
    "            return JSONResponse(status_code=409, content={"
)
BEAT_LOCK_UNDECIDED = (
    "        undecided = director.unresolved_warnings(plan)\n"
    "        if undecided:\n"
    "            return JSONResponse(status_code=400, content={"
)
SCENE_LOCK_UNDECIDED = (
    "            undecided = director.unresolved_warnings(plan)\n"
    "            if undecided:"
)
WARNING_ID_DERIVED = '        d["id"] = warning_id(d)'
WARNING_ID_PREIMAGE = (
    '    parts = [str(w.get("beat_id") or ""), str(w.get("shot_id") or ""),\n'
    '             str(w.get("kind") or ""), str(w.get("detail") or "")]'
)
UNRESOLVED_PREDICATE = (
    "    return [w for w in normalize_warnings(plan.warnings)\n"
    '            if not (disp.get(w["id"]) or {}).get("decision")]'
)
DISPOSITIONS_LOADED = (
    '    raw["warning_dispositions"] = dict(raw.get("warning_dispositions") or {})'
)


MUTATIONS = [
    # ---- A-01: the flag that stepped over the gate ------------------------------
    Mutation(
        "DEFECT A-01: force=true skips the draft check", "§11.5 / CLAUDE.md Gate 1",
        "the bypass exactly as it shipped, restored — a query parameter sends an "
        "unapproved plan into a compile that generates stills and, for an "
        "ai_video shot, buys paid video. Approval is the whole boundary between "
        "a proposal and spending money on it",
        [(MAIN, COMPILE_SIGNATURE,
          "def compile_director_coverage(beat_id: str, force: bool = False):  # MUTANT"),
         (MAIN, COMPILE_DRAFT_CHECK,
          '        if plan.status == "draft" and not force:  # MUTANT\n'
          "            drifted = next((h for h in reversed(plan.approval_history or [])")],
        probes=[("gate", "PROBE_PASSA_FORCE_DISPATCHED=true"),
                ("gate", "PROBE_PASSA_FORCE_STATUS=200")],
        defect=True,
        expect=["test_the_force_flag_no_longer_bypasses_approval",
                "test_no_spelling_of_force_gets_a_draft_through"],
        proves=["force=true sent an unapproved plan into generation"],
        not_proves=["a draft plan was handed to generation"],
    ),
    Mutation(
        "the compile stops refusing a draft plan at all", "§11.5",
        "the draft check itself — the flag is not even needed, every unapproved "
        "plan compiles, and `status` stops meaning anything about whether a "
        "human allocated the budget",
        [(MAIN, COMPILE_DRAFT_CHECK,
          "        if False:  # MUTANT\n"
          "            drifted = next((h for h in reversed(plan.approval_history or [])")],
        probes=[("gate", "PROBE_PASSA_DRAFT_DISPATCHED=true"),
                ("gate", "PROBE_PASSA_FORCE_DISPATCHED=true")],
        expect=["test_a_draft_plan_is_refused_at_compile",
                "test_the_force_flag_no_longer_bypasses_approval"],
        proves=["a draft plan was handed to generation",
                "force=true sent an unapproved plan into generation"],
    ),

    # ---- A-02: unresolved findings cannot be bulk-approved ----------------------
    Mutation(
        "DEFECT A-02: the scene lock approves straight past every warning",
        "§5.4",
        "the bulk action's half — 'Approve Scene Plan' locks a plan carrying "
        "undecided critic findings, which turns 'approved' into a state that "
        "says nothing about whether the review was answered",
        [(MAIN, SCENE_LOCK_UNDECIDED,
          "            undecided = []  # MUTANT\n"
          "            if undecided:")],
        probes=[("gate", "PROBE_PASSA_SCENE_LOCKED_OVER_WARNING=true"),
                ("gate", "PROBE_PASSA_SCENE_LOCK_STATUS=200")],
        defect=True,
        expect=["test_scene_lock_refuses_a_plan_with_an_undecided_warning"],
        proves=["locked despite the warning"],
    ),
    Mutation(
        "the single-beat lock approves straight past every warning", "§5.4",
        "the second way in — the rule was attached to the bulk route and the "
        "per-beat route is the same approval by another name, which is the "
        "failure mode of gating routes instead of the decision",
        [(MAIN, BEAT_LOCK_UNDECIDED,
          "        undecided = []  # MUTANT\n"
          "        if undecided:\n"
          "            return JSONResponse(status_code=400, content={")],
        probes=[("gate", "PROBE_PASSA_BEAT_LOCKED_OVER_WARNING=true"),
                ("gate", "PROBE_PASSA_BEAT_LOCK_STATUS=200")],
        expect=["test_beat_lock_refuses_a_plan_with_an_undecided_warning"],
        proves=["the single-beat route locked despite the warning"],
    ),
    Mutation(
        "the compile stops re-asserting the review rule", "§5.4",
        "the defence in depth, which is not redundancy: it is the only thing "
        "stopping a plan locked BEFORE this rule existed from walking into "
        "generation, and every such plan is on disk right now",
        [(MAIN, COMPILE_UNDECIDED,
          "        undecided = []  # MUTANT\n"
          "        if undecided:\n"
          "            return JSONResponse(status_code=409, content={")],
        probes=[("gate", "PROBE_PASSA_LOCKED_UNDECIDED_DISPATCHED=true"),
                ("gate", "PROBE_PASSA_LOCKED_UNDECIDED_STATUS=200")],
        expect=["test_a_locked_plan_with_an_undecided_warning_still_cannot_compile"],
        proves=["a plan locked before the review rule was handed to generation"],
    ),
    Mutation(
        "nothing is ever unresolved", "§5.4",
        "the predicate every one of those three routes asks — one line, and the "
        "whole review boundary goes quiet at once while all three call sites "
        "still read as gated",
        [(DIRECTOR, UNRESOLVED_PREDICATE,
          "    return []  # MUTANT: nothing is ever unresolved")],
        probes=[("gate", "PROBE_PASSA_SCENE_LOCKED_OVER_WARNING=true"),
                ("gate", "PROBE_PASSA_BEAT_LOCKED_OVER_WARNING=true"),
                ("gate", "PROBE_PASSA_LOCKED_UNDECIDED_DISPATCHED=true")],
        expect=["test_scene_lock_refuses_a_plan_with_an_undecided_warning",
                "test_beat_lock_refuses_a_plan_with_an_undecided_warning",
                "test_a_locked_plan_with_an_undecided_warning_still_cannot_compile"],
        proves=["locked despite the warning"],
    ),
    Mutation(
        "everything is always unresolved", "§5.4",
        "the other direction, and the one that gets a gate removed rather than "
        "fixed — no plan can ever be locked, including one whose every finding "
        "has a recorded decision, so the review boundary becomes an obstacle to "
        "route around",
        [(DIRECTOR, UNRESOLVED_PREDICATE,
          "    return normalize_warnings(plan.warnings)  # MUTANT")],
        # A plan carrying NO warnings is unaffected -- normalize_warnings([]) is
        # empty however the predicate reads it -- so neither
        # test_a_clean_plan_locks_normally nor the clean-compile probe line can
        # see this, and declaring them was an over-claim the run corrected. What
        # this breaks is a plan whose findings have all been decided, which is
        # the case the gate exists to let through.
        probes=[("gate", "PROBE_PASSA_DECIDED_PLAN_LOCKS=false")],
        expect=["test_locking_is_allowed_once_every_warning_has_a_decision",
                "test_a_suggestionless_warning_can_still_be_decided"],
        proves_note=(
            "the over-strict direction has no money sentence to reach: the tests "
            "assert that a decided plan locks, with bare asserts. "
            "PROBE_PASSA_DECIDED_PLAN_LOCKS=false is the statement that the gate "
            "now refuses work it was built to let through"),
    ),

    # ---- A-04: identity is derived, never supplied -------------------------------
    Mutation(
        "DEFECT A-04: a caller-supplied warning id becomes the identity",
        "§5.4",
        "the derivation — a changed finding keeps the identity of the one it "
        "replaced and inherits the decision recorded about it, so a NEW problem "
        "arrives pre-approved and the scene locks straight over it",
        [(DIRECTOR, WARNING_ID_DERIVED,
          '        d["id"] = supplied or warning_id(d)  # MUTANT')],
        probes=[("identity", "PROBE_PASSA_SUPPLIED_ID_BECAME_KEY=true"),
                ("identity", "PROBE_PASSA_CHANGED_FINDING_INHERITED_A_DECISION=true"),
                ("identity", "PROBE_PASSA_LOCKED_OVER_A_NEW_FINDING=true")],
        defect=True,
        expect=["test_a_supplied_warning_id_cannot_carry_a_decision_onto_a_changed_finding",
                "test_a_supplied_id_is_kept_for_tracing_but_is_not_the_key"],
        proves=["a caller-supplied id became the identity"],
    ),
    # The preimage, not the call site. The first version of these two mutated
    # `d["id"] = warning_id(d)` inside normalize_warnings, and the probe measures
    # warning_id() DIRECTLY -- so the signature never appeared and the run
    # reported a mutation that had changed nothing the probe could see. What the
    # tests actually guard is which fields the digest covers, so that is what is
    # removed here.
    Mutation(
        "warning identity stops covering what the finding is about", "§5.4",
        "the target — two findings that differ only by which shot they are "
        "about collapse to one id, so deciding either silently decides both",
        [(DIRECTOR, WARNING_ID_PREIMAGE,
          '    parts = [str(w.get("beat_id") or ""),  # MUTANT: target dropped\n'
          '             str(w.get("kind") or ""), str(w.get("detail") or "")]')],
        probes=[("identity", "PROBE_PASSA_DIFFERENT_TARGETS_SHARE_AN_ID=true")],
        expect=["test_two_findings_that_differ_only_by_target_get_different_ids"],
        proves=["assert director.warning_id(a) != director.warning_id(b)"],
    ),
    Mutation(
        "warning identity stops covering what the finding SAYS", "§5.4",
        "the text — a finding rewritten into a different problem keeps the id of "
        "the one it replaced, so the decision recorded about the old one covers "
        "it. Same harm as A-04, reached through the derivation rather than "
        "through a supplied id, and invisible to any test that only checks the "
        "id is stable",
        [(DIRECTOR, WARNING_ID_PREIMAGE,
          '    parts = [str(w.get("beat_id") or ""), str(w.get("shot_id") or ""),  # MUTANT\n'
          '             str(w.get("kind") or "")]')],
        probes=[("identity", "PROBE_PASSA_CHANGED_DETAIL_KEEPS_ITS_ID=true")],
        expect=["test_a_changed_warning_gets_a_new_id"],
        proves=["assert director.warning_id(changed) != director.warning_id(WARNING)"],
    ),

    # ---- A-03: a decision is durable ---------------------------------------------
    Mutation(
        "DEFECT A-03: a decision is forgotten on the next read", "§5.4",
        "durability — the server-side shape of the original defect, where "
        "dismissing a warning changed React state and nothing else. Every "
        "decision is dropped when the plan is loaded, so the review has to be "
        "redone on every read and can never be completed",
        [(DIRECTOR, DISPOSITIONS_LOADED,
          '    raw["warning_dispositions"] = {}  # MUTANT')],
        # NOT test_dispositions_round_trip_through_the_plan_file: it sets one
        # decision and reads the raw file immediately, and the drop happens on
        # LOAD, so the bytes it inspects are correct. The probe agrees --
        # PROBE_PASSA_DISPOSITION_ON_DISK stays true under this mutation. What
        # dies is everything that reads the decision back.
        probes=[("durable", "PROBE_PASSA_DECISION_SURVIVES_RELOAD=false"),
                ("durable", "PROBE_PASSA_UNRESOLVED_AFTER_RELOAD=1"),
                ("gate", "PROBE_PASSA_DECIDED_PLAN_LOCKS=false")],
        defect=True,
        expect=["test_a_decision_survives_a_reload",
                "test_a_recritique_that_repeats_a_finding_keeps_its_decision",
                "test_locking_is_allowed_once_every_warning_has_a_decision"],
        proves=["the decision was lost on re-check"],
    ),
]


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #

_ENV = {k: v for k, v in os.environ.items()
        if k not in ("FAL_KEY", "ELEVENLABS_API_KEY", "ANTHROPIC_API_KEY")}
_ENV.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})

_RECLAIMED: dict[Path, int] = {}


def run_suite() -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q"], cwd=ROOT, env=_ENV,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def reclaim_root_artifacts() -> list[Path]:
    """Remove ROOT project files THIS RUN created. See the note in main()."""
    removed = [p for p in ROOT_ARTIFACTS if p.is_file()]
    for p in removed:
        p.unlink()
        _RECLAIMED[p] = _RECLAIMED.get(p, 0) + 1
    return removed


def run_probe(name: str) -> tuple[bool, str]:
    reclaim_root_artifacts()
    script = PROBES[name].replace("__ROOT__", str(ROOT))
    proc = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, env=_ENV,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def probe_lines(out: str) -> list[str]:
    return [ln.strip() for ln in out.splitlines() if ln.strip().startswith("PROBE_")]


def shows(out: str, signature: str) -> bool:
    """Whether a probe printed exactly this line. Equality, not ``in``."""
    return signature in probe_lines(out)


def failure_lines(out: str) -> str:
    """Only the lines pytest marks as the FAILING statement and its explanation.

    Matching `proves` / `not_proves` against the whole suite output does not
    work, and the first run with these fields enforced is what showed it.
    pytest's long traceback prints the failing test's source from the def down
    to the failure: the failing statement is prefixed ``>``, its explanation
    ``E``, and every earlier line -- including earlier assertions and their
    message strings -- is printed UNPREFIXED as context.

    So a substring search over the raw output cannot tell "this assertion
    failed" from "this assertion sits above the one that did". It reported three
    not_proves violations in mutate_paid_path.py that were nothing of the kind:
    the money assertion had passed, and its source was merely visible above the
    refusal check that failed. Restricting the search to ``>``/``E`` lines is
    what makes the distinction the field was added to draw.
    """
    return "\n".join(ln for ln in out.splitlines()
                      if ln.lstrip().startswith(("E ", "> ")))


def failing_tests(out: str) -> list[str]:
    return sorted({line.split(" ", 1)[1].strip()
                   for line in out.splitlines() if line.startswith("FAILED ")})


def _purge_pycache(path: Path) -> None:
    """Drop any cached bytecode for ``path``.

    CPython invalidates a ``.pyc`` on (mtime, size) at one-second mtime
    resolution, so two mutations of one file whose replacements are the same
    length, applied within the same second, let the second run import the
    first's bytecode. Two of the mutations here are exactly that shape -- the
    two ``undecided = []`` edits differ only in the lines that follow them. See
    the longer note in ``mutate_paid_path.py``.
    """
    cache = path.parent / "__pycache__"
    if cache.is_dir():
        for pyc in cache.glob(f"{path.stem}.*.pyc"):
            try:
                pyc.unlink()
            except OSError:
                pass


def _read(path: Path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    _purge_pycache(path)


def apply(path: Path, find: str, replace: str) -> None:
    """One edit, refusing anything but an exactly-one-site anchor."""
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


def discover() -> int:
    """Apply each mutation, run only its probes, print everything, restore."""
    touched = sorted({p for m in MUTATIONS for p, _, _ in m.edits})
    pristine = {p: _read(p) for p in touched}
    before = digest(touched)
    for name in sorted(PROBES):
        ok, out = run_probe(name)
        print(f"\n=== PRISTINE probe {name}: {'ran' if ok else 'ERRORED'}")
        for ln in probe_lines(out):
            print(f"    {ln}")
        if not ok:
            print(out[-2500:])
    for mut in MUTATIONS:
        snapshot = {p: _read(p) for p, _, _ in mut.edits}
        outs: dict[str, str] = {}
        try:
            for path, find, replace in mut.edits:
                apply(path, find, replace)
            for name in sorted({n for n, _ in mut.probes} or PROBES):
                outs[name] = run_probe(name)[1]
        finally:
            for path, text in snapshot.items():
                _write(path, text)
        print(f"\n=== MUTATED: {mut.name}")
        for name, out in outs.items():
            print(f"  probe {name}:")
            for ln in probe_lines(out):
                print(f"    {ln}")
            if not probe_lines(out):
                print(f"    (no PROBE_ output)\n{out[-1500:]}")
    reclaim_root_artifacts()
    after = digest(touched)
    dirty = [p.name for p in touched if before[p] != after[p]]
    for p in touched:
        if before[p] != after[p]:
            _write(p, pristine[p])
    if dirty:
        print(f"\nTREE NOT RESTORED — was still modified: {', '.join(dirty)}")
    return 1 if dirty else 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--discover", action="store_true",
                    help="apply each mutation and print its probe output; no pytest")
    args = ap.parse_args()

    naked = [m.name for m in MUTATIONS if not m.probes]
    if naked:
        print("mutations with no probe signature:")
        for n in naked:
            print(f"  - {n}")
        return 1

    silent = [m.name for m in MUTATIONS if not m.proves and not m.proves_note]
    if silent:
        print("mutations with neither `proves` nor a written `proves_note`:")
        for n in silent:
            print(f"  - {n}")
        return 1

    occupied = [p for p in ROOT_ARTIFACTS if p.exists()]
    if occupied:
        print("refusing to run: these exist and are not this harness's to touch:")
        for p in occupied:
            print(f"  - {p.relative_to(ROOT)}")
        return 1

    if args.discover:
        return discover()

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

        for want in mut.expect:
            if not any(want in t for t in failed):
                unproven.append(f"{mut.name}: expected {want} to fail")
        failed_asserts = failure_lines(suite_out)
        for phrase in mut.proves:
            shown = phrase in failed_asserts
            print(f"    assertion {'FIRED' if shown else 'NEVER RAN'}: {phrase!r}")
            if not shown:
                unproven.append(
                    f"{mut.name}: no failing assertion said {phrase!r} - the test "
                    f"died on a cheaper assertion first, so the defect was never "
                    f"demonstrated")
        for phrase in mut.not_proves:
            if phrase in failed_asserts:
                print(f"    assertion UNEXPECTEDLY FIRED: {phrase!r}")
                unproven.append(
                    f"{mut.name}: {phrase!r} also failed - the test may be red "
                    f"for a reason other than the one named")
        if mut.proves_note and not mut.proves:
            print(f"    no assertion pinned: {mut.proves_note}")

        for name in sorted({n for n, _ in mut.probes}):
            lines = probe_lines(probe_out.get(name, ""))
            print(f"    probe {name}:")
            for ln in lines:
                print(f"      {ln}")
            if not lines:
                print("      (no PROBE_ output — probe crashed under the mutation)")
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

    strays = reclaim_root_artifacts()
    if strays:
        dirty += [str(p.relative_to(ROOT)) + " (written by a mutated run, removed)"
                  for p in strays]

    ok, _ = run_suite()
    print(f"\nrestored suite: {'PASS' if ok else 'FAIL'}")
    print(f"mutations: {len(MUTATIONS)} defined, {len(survived)} survived, "
          f"{len(MUTATIONS) - len(survived)} killed")
    for p, n in sorted(_RECLAIMED.items(), key=lambda kv: str(kv[0])):
        print(f"  reclaimed {p.relative_to(ROOT)} {n}x — removed, and only "
              f"because this run created them")
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

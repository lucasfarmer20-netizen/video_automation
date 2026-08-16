"""Mutation harness for Gate 1's strict-boolean approval (contract §5.4).

    python scratch/mutate_gate1_strict.py

Same shape and the same two obligations as ``mutate_slice8_storage_gate.py``:

  * **a test that fails under a faithful mutation of the fix** — every mutation
    below must be killed. A survivor is a safeguard no test protects;
  * **a mutation that genuinely reproduces the defect** — the known failure mode
    is a mutation that "passes" because it accidentally hid the bug it was
    restoring, so each DEFECT mutation carries probe signatures that ARE the
    production symptom and must be absent pristine.

The signature that matters most is::

    PROBE_INMEM_GATE_no=True

which is the reported defect verbatim: a paid Tier-C beat carrying
``approved = 'no'``, on an approved storyboard, clearing Gate 1. Nothing raises,
nothing logs, and the next thing that happens is a call to a paid video API for
a beat whose own field says no.

The three enforcement points are mutated INDEPENDENTLY as well as together,
because the argument for defence in depth is only worth making if each layer is
separately proven to be load-bearing:

  * ``gate_cleared``   — catches producers that never pass a loader, and
    ``/api/approve`` (backend/main.py:4315) is exactly one of those;
  * ``from_dict``      — catches the value before it becomes a Shot, so nothing
    downstream of the loader ever sees it, and the degrade is what gets
    persisted;
  * ``main._scan_projects`` — reads the manifest JSON raw, so it is the one
    place ``from_dict`` does not cover, and it drives a UI badge (§11.4).

Mutating only one at a time is what distinguishes "both layers are needed" from
"one layer is doing all the work and the other is decoration".
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "backend" / "manifest.py"
MAIN = ROOT / "backend" / "main.py"
DIRECTOR = ROOT / "backend" / "director.py"


# --------------------------------------------------------------------------- #
# probes: the real predicate, the real loader, the real sidebar scan
# --------------------------------------------------------------------------- #

_PRELUDE = '''
import json, sys, tempfile, types
from pathlib import Path
sys.path.insert(0, r"__ROOT__")
for _m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

from backend import manifest as M
from backend.manifest import MotionType, Shot, Storyboard

# The loose-value space, and which half was truthy under the original predicate.
# The truthy ones are the defect; the falsy ones were shut by accident.
LOOSE = [("no", "no"), ("false", "false"), ("true", "true"), ("one", 1),
         ("list", ["vesper"]), ("dict", {"by": "vesper"}),
         ("zero", 0), ("none", None), ("emptylist", [])]


class EqAny:
    """Equal to everything, truthy. Stands in for every sentinel token at once.

    A probe over affirmative STRINGS can only demonstrate the tokens somebody
    thought to list. This one is accepted by any `== <token>` or `in (<tokens>)`
    sentinel whatever the token is, so PROBE_SENTINEL_eqany=True is evidence
    about the whole class rather than about one word.
    """
    def __eq__(self, other): return True
    def __ne__(self, other): return False
    def __hash__(self): return hash(True)
    def __bool__(self): return True
    def __repr__(self): return "<eqany>"


# Affirmative-looking values. `yes` is in the test file's table; `UNLISTED` is
# deliberately NOT -- see the SENTINEL mutations for why that distinction is the
# whole point.
UNLISTED = "sanctioned-by-vesper-2026"
SENTINELS = [("yes", "yes"), ("unlisted", UNLISTED), ("eqany", EqAny())]

tmp = Path(tempfile.mkdtemp())


def paid(scene_id="s001", approved=True):
    s = Shot(scene_id=scene_id, motion_type=MotionType.AI_VIDEO,
             video_model="seedance_2_0")
    s.approved = approved
    return s


def beat_json(scene_id="s001", approved=True):
    return {"scene_id": scene_id, "motion_type": "ai_video",
            "video_model": "seedance_2_0", "approved": approved}
'''

# The reported defect, in memory: nothing has been through a loader, which is
# the shape /api/approve produces when it assigns s.approved on a live object.
PROBE_INMEM = _PRELUDE + '''
for name, value in LOOSE:
    sb = Storyboard(title="T", storyboard_approved=True,
                    shots=[paid(approved=value)])
    print("PROBE_INMEM_GATE_%s=%s" % (name, sb.gate_cleared()))

# The project-level term, one line above: `if not "no"` is False, so the
# short-circuit never fires and a ready paid beat carries the gate open.
sb = Storyboard(title="T", shots=[paid()])
sb.storyboard_approved = "no"
print("PROBE_INMEM_SBGATE_no=%s" % sb.gate_cleared())

# The sentinel space: a paid Tier-C beat whose approval merely LOOKS like
# consent. All False pristine.
for name, value in SENTINELS:
    sb = Storyboard(title="T", storyboard_approved=True,
                    shots=[paid(approved=value)])
    print("PROBE_SENTINEL_%s=%s" % (name, sb.gate_cleared()))

# The floor: with a real boolean this same fixture must still open.
print("PROBE_INMEM_GATE_realtrue=%s"
      % Storyboard(title="T", storyboard_approved=True,
                   shots=[paid()]).gate_cleared())
'''

# The paid doors: require_paid_gate, which every paid render route calls, and
# the director's coverage route, which is the second way onto Tier C.
PROBE_PAID = _PRELUDE + '''
from backend import main as MAIN

def gate_admits(value):
    sb = Storyboard(title="T", shots=[paid()])
    sb.storyboard_approved = value
    try:
        MAIN.require_paid_gate(sb, "render")
        return True          # no refusal -- the paid call proceeds
    except Exception:
        return False

for name, value in SENTINELS:
    print("PROBE_PAIDGATE_%s=%s" % (name, gate_admits(value)))
print("PROBE_PAIDGATE_no=%s" % gate_admits("no"))
print("PROBE_PAIDGATE_realtrue=%s" % gate_admits(True))
'''

# The director's coverage route -- the second door onto the paid video tier.
PROBE_COVERAGE = _PRELUDE + '''
from backend import director
from backend.director import CoveragePlan, DirectorShot
from backend.manifest import Camera

director.config.MANIFEST_PATH = tmp / "m.json"


def coverage_admits(value, motion="ai_video"):
    """True when the approval refusal did NOT fire -- i.e. the paid route ran."""
    plan = CoveragePlan(
        beat_id="s003", beat_duration=27.0, status="locked",
        coverage=[DirectorShot(id="s003.01", beat_id="s003", prompt="a thing",
                               camera=Camera(duration=27.0), motion_type=motion)])
    director.save_plan(plan)
    sb = Storyboard(id="p", title="T",
                    shots=[Shot(scene_id="s003", narration="x",
                                camera=Camera(duration=27.0))])
    sb.storyboard_approved = value
    try:
        director.compile_coverage(plan, sb, tmp / "render", log=lambda m: None)
        return True
    except Exception as exc:
        return "not approved" not in str(exc)


for name, value in SENTINELS:
    print("PROBE_COVERAGE_%s=%s" % (name, coverage_admits(value)))
print("PROBE_COVERAGE_no=%s" % coverage_admits("no"))
# Free tiers stay open before the gate; tightening approval must not close them.
print("PROBE_COVERAGE_free_no=%s" % coverage_admits("no", motion="parallax"))
'''

# The boundary: a manifest on disk, loaded the way every project is loaded.
PROBE_LOAD = _PRELUDE + '''
for name, value in LOOSE:
    mf = tmp / ("m_%s.json" % name)
    mf.write_text(json.dumps({
        "title": "T", "storyboard_approved": True,
        "shots": [beat_json(approved=value)]}), encoding="utf-8")
    sb = M.load(mf)
    print("PROBE_LOAD_GATE_%s=%s" % (name, sb.gate_cleared()))
    print("PROBE_LOAD_FIELD_%s=%r" % (name, sb.shots[0].approved))
    # What the NEXT reader sees. A value left in the field serialises straight
    # back out, so the degrade has to be a real bool or it is not a degrade.
    M.save(sb, mf)
    print("PROBE_LOAD_WRITTEN_%s=%r"
          % (name, json.loads(mf.read_text(encoding="utf-8"))["shots"][0]["approved"]))

# The project-level field, through the same loader.
mf = tmp / "m_sb.json"
mf.write_text(json.dumps({
    "title": "T", "storyboard_approved": "no",
    "shots": [beat_json()]}), encoding="utf-8")
sb = M.load(mf)
print("PROBE_LOAD_SBGATE_no=%s" % sb.gate_cleared())
print("PROBE_LOAD_SBFIELD_no=%r" % sb.storyboard_approved)

# No over-correction: a project a human really approved must still clear.
mf = tmp / "m_ok.json"
mf.write_text(json.dumps({
    "title": "T", "storyboard_approved": True,
    "shots": [beat_json(), beat_json("s002")]}), encoding="utf-8")
print("PROBE_LOAD_GATE_realtrue=%s" % M.load(mf).gate_cleared())
'''

# The note. A beat that flips to unapproved must say why, and must not say it
# for the shapes that always meant "no decision recorded".
PROBE_NOTE = _PRELUDE + '''
import io, contextlib

def note_for(value):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        Storyboard.from_dict("p", {"title": "T", "storyboard_approved": True},
                             [beat_json(approved=value)])
    return "not a boolean" in buf.getvalue()

print("PROBE_NOTE_no=%s" % note_for("no"))
print("PROBE_NOTE_one=%s" % note_for(1))
print("PROBE_NOTE_true_bool=%s" % note_for(True))
print("PROBE_NOTE_false_bool=%s" % note_for(False))
print("PROBE_NOTE_null=%s" % note_for(None))
'''

# The sidebar badge: raw JSON, so from_dict never touches it.
PROBE_SIDEBAR = _PRELUDE + '''
from backend import main as MAIN

proj = tmp / "sidebar" / "bestiary" / "leshy"
proj.mkdir(parents=True, exist_ok=True)
mf = proj / "storyboard_manifest.json"
mf.write_text(json.dumps({
    "title": "Leshy", "channel": "bestiary", "storyboard_approved": "no",
    "shots": [beat_json()]}), encoding="utf-8")

MAIN.WORKSPACE_ROOT = tmp / "sidebar"
MAIN.get_active_manifest_path = lambda: str(mf)
listed = [p for p in MAIN._scan_projects() if p["name"] == "Leshy"]
print("PROBE_SIDEBAR_LISTED=%d" % len(listed))
badge = listed[0]["storyboard_approved"] if listed else "missing"
print("PROBE_SIDEBAR_BADGE_no=%r" % badge)
# The two answers as ONE signature, because the defect is the DISAGREEMENT and
# neither half states it alone: the gate reads False pristine and under the
# mutation, so "gate is False" is not evidence of anything on its own.
print("PROBE_SIDEBAR_AGREEMENT=badge:%r/gate:%s" % (badge, M.load(mf).gate_cleared()))

# The second raw read: /api/projects refines each scanned entry from the
# Firestore document, which is not a Storyboard and never passes from_dict. For
# a bootstrapped project -- i.e. every deployed one -- this arm decides.
MAIN.get_project_id_from_path = lambda rel: "leshy"
MAIN.manifest.list_projects = lambda channel=None: [
    {"id": "leshy", "title": "Leshy", "channel": "bestiary",
     "storyboard_approved": "no"}]
entries = [p for p in MAIN.get_projects()["projects"] if p["name"] == "Leshy"]
print("PROBE_SIDEBAR_DOC_BADGE_no=%r"
      % (entries[0]["storyboard_approved"] if entries else "missing"))
'''

PROBES = {
    "inmem": PROBE_INMEM,
    "load": PROBE_LOAD,
    "note": PROBE_NOTE,
    "sidebar": PROBE_SIDEBAR,
    "paid": PROBE_PAID,
    "coverage": PROBE_COVERAGE,
}


@dataclass
class Mutation:
    name: str
    clause: str
    removes: str                       # the behaviour this deletes, in one line
    edits: list[tuple[Path, str, str]]
    probes: list[tuple[str, str]] = field(default_factory=list)
    defect: bool = False               # restores a defect that really shipped
    expect: list[str] = field(default_factory=list)


# --- anchors ----------------------------------------------------------------------

EXPLICIT_RETURN = "    return value is True\n"

FLAG_BODY = (
    '    if approval_is_explicit(raw):\n'
    '        return True\n'
    '    if raw is False or raw is None:\n'
    '        return False\n'
    '    print(f"manifest: {field}={raw!r} on {where} is {type(raw).__name__}, not a "\n'
    '          f"boolean; reading it as NOT approved (§5.4). Re-approve to restore "\n'
    '          f"it -- Gate 1 will not spend on a value nobody checked.")\n'
    '    return False\n'
)

GATE_BODY = (
    "        if not approval_is_explicit(self.storyboard_approved):\n"
    "            return False\n"
    "        return all(\n"
    "            approval_is_explicit(s.approved) and bool(s.video_model)\n"
    "            for s in self.shots\n"
    "            if s.needs_paid_video()\n"
    "        )\n"
)

FROMDICT_SHOT = (
    '                fields["approved"] = _explicit_approval_flag(\n'
    '                    shot.get("approved"), "approved", f"beat {scene}")\n'
)

FROMDICT_SB = (
    "            storyboard_approved=_explicit_approval_flag(\n"
    '                data.get("storyboard_approved"), "storyboard_approved",\n'
    '                f"project {project_id!r}"),\n'
)

SIDEBAR = (
    "                    storyboard_approved = manifest.approval_is_explicit(\n"
    '                        manifest_data.get("storyboard_approved"))\n'
)

SIDEBAR_DOC = (
    '                p["storyboard_approved"] = manifest.approval_is_explicit(\n'
    '                    doc.get("storyboard_approved", p["storyboard_approved"]))\n'
)

# The pre-fix text, restored verbatim.
PRE_GATE = (
    "        if not self.storyboard_approved:  # MUTANT\n"
    "            return False\n"
    "        return all(\n"
    "            s.approved and bool(s.video_model)\n"
    "            for s in self.shots\n"
    "            if s.needs_paid_video()\n"
    "        )\n"
)
PRE_FROMDICT_SHOT = "                pass  # MUTANT: no boundary check on approved\n"
PRE_FROMDICT_SB = (
    '            storyboard_approved=data.get("storyboard_approved", False),  # MUTANT\n'
)
PRE_SIDEBAR = (
    "                    storyboard_approved = bool(  # MUTANT\n"
    '                        manifest_data.get("storyboard_approved", False))\n'
)
PRE_SIDEBAR_DOC = (
    '                p["storyboard_approved"] = doc.get(  # MUTANT\n'
    '                    "storyboard_approved", p["storyboard_approved"])\n'
)

# The two paid doors.
PAID_GATE = (
    '    if not manifest.approval_is_explicit(getattr(sb, "storyboard_approved", False)):\n'
)
DIRECTOR_GATE = (
    '    if paid and not approval_is_explicit(getattr(sb, "storyboard_approved", False)):\n'
)


MUTATIONS = [
    # ---- mutations that restore the defect that shipped -------------------------
    Mutation(
        "DEFECT: the whole fix at once — approval is truthy again everywhere",
        "contract §5.4",
        "every enforcement point in one edit: the pre-fix predicate, the "
        "unvalidated loader on both fields, and the sidebar's bool(). This is "
        "the code as it shipped, and `approved: \"no\"` on a paid Tier-C beat "
        "opens Gate 1 from memory, from disk, and lights the badge",
        [(MANIFEST, GATE_BODY, PRE_GATE),
         (MANIFEST, FROMDICT_SHOT, PRE_FROMDICT_SHOT),
         (MANIFEST, FROMDICT_SB, PRE_FROMDICT_SB),
         (MAIN, SIDEBAR, PRE_SIDEBAR),
         (MAIN, SIDEBAR_DOC, PRE_SIDEBAR_DOC)],
        probes=[("inmem", "PROBE_INMEM_GATE_no=True"),
                ("inmem", "PROBE_INMEM_GATE_false=True"),
                ("inmem", "PROBE_INMEM_GATE_true=True"),
                ("inmem", "PROBE_INMEM_GATE_one=True"),
                ("inmem", "PROBE_INMEM_GATE_list=True"),
                ("inmem", "PROBE_INMEM_GATE_dict=True"),
                ("inmem", "PROBE_INMEM_SBGATE_no=True"),
                ("load", "PROBE_LOAD_GATE_no=True"),
                ("load", "PROBE_LOAD_FIELD_no='no'"),
                ("load", "PROBE_LOAD_WRITTEN_no='no'"),
                ("load", "PROBE_LOAD_SBGATE_no=True"),
                ("sidebar", "PROBE_SIDEBAR_BADGE_no=True"),
                ("sidebar", "PROBE_SIDEBAR_DOC_BADGE_no='no'")],
        defect=True,
        expect=["test_gate_shut_when_any_paid_beats_approval_is_not_a_boolean",
                "test_gate_shut_when_the_storyboard_approval_is_not_a_boolean",
                "test_a_loose_approval_never_becomes_an_approved_shot",
                "test_a_hand_edited_manifest_on_disk_cannot_open_gate_1",
                "test_the_sidebar_does_not_badge_a_project_gate_1_refuses_to_open"],
    ),
    Mutation(
        "DEFECT: gate_cleared trusts its input again", "contract §5.4",
        "the last line of defence only — the loader still normalises, so a "
        "manifest read from disk is safe and ONLY beats built in memory are "
        "exposed. That is not a narrow case: /api/approve sets s.approved on a "
        "live Storyboard (backend/main.py:4315) and never passes from_dict",
        [(MANIFEST, GATE_BODY, PRE_GATE)],
        probes=[("inmem", "PROBE_INMEM_GATE_no=True"),
                ("inmem", "PROBE_INMEM_SBGATE_no=True")],
        defect=True,
        expect=["test_gate_shut_when_any_paid_beats_approval_is_not_a_boolean",
                "test_gate_shut_when_the_storyboard_approval_is_not_a_boolean"],
    ),
    Mutation(
        "DEFECT: the loader lets the value through again", "contract §5.4",
        "the boundary only — gate_cleared still refuses, so no money is spent, "
        "but `\"no\"` now lives in Shot.approved, gets written back by the next "
        "save, and reaches every other reader of the field (motion.py:490 "
        "renders on `not shot.approved`, exports.py:221 digests it)",
        [(MANIFEST, FROMDICT_SHOT, PRE_FROMDICT_SHOT),
         (MANIFEST, FROMDICT_SB, PRE_FROMDICT_SB)],
        probes=[("load", "PROBE_LOAD_FIELD_no='no'"),
                ("load", "PROBE_LOAD_WRITTEN_no='no'"),
                ("load", "PROBE_LOAD_SBFIELD_no='no'"),
                ("note", "PROBE_NOTE_no=False")],
        defect=True,
        expect=["test_a_loose_approval_never_becomes_an_approved_shot",
                "test_a_loose_storyboard_approval_never_becomes_an_approved_project",
                "test_the_loose_value_does_not_survive_into_what_gets_written_back",
                "test_a_wrong_type_is_said_out_loud"],
    ),
    Mutation(
        "DEFECT: the sidebar badges a project the gate refuses to open",
        "contract §11.4",
        "bool() on the raw JSON read — _scan_projects never passes from_dict, "
        "so this is the one place the boundary does not cover, and the result "
        "is the UI showing \"Approved ✓\" for a project that cannot render",
        [(MAIN, SIDEBAR, PRE_SIDEBAR)],
        probes=[("sidebar", "PROBE_SIDEBAR_BADGE_no=True"),
                ("sidebar", "PROBE_SIDEBAR_AGREEMENT=badge:True/gate:False")],
        defect=True,
        expect=["test_the_sidebar_does_not_badge_a_project_gate_1_refuses_to_open"],
    ),
    Mutation(
        "DEFECT: the durable store restores the badge the scan withheld",
        "contract §11.4",
        "the Firestore refinement on /api/projects -- a second raw read, of a "
        "document rather than a Storyboard, so from_dict never covers it either. "
        "It exists only for BOOTSTRAPPED projects, which is every deployed one, "
        "so it overwrites the scan and undoes the fix one line later",
        [(MAIN, SIDEBAR_DOC, PRE_SIDEBAR_DOC)],
        probes=[("sidebar", "PROBE_SIDEBAR_DOC_BADGE_no='no'")],
        defect=True,
        expect=["test_the_durable_store_cannot_restore_a_badge_the_scan_withheld"],
    ),

    # ---- faithful mutations of the fix ------------------------------------------
    Mutation(
        "the strict check is truthiness wearing a new name", "contract §5.4",
        "`value is True` -> `bool(value)` at the single point every layer calls. "
        "The three call sites are untouched and still look strict, which is why "
        "this is the mutation most likely to survive a careless review",
        [(MANIFEST, EXPLICIT_RETURN, "    return bool(value)  # MUTANT\n")],
        probes=[("inmem", "PROBE_INMEM_GATE_no=True"),
                ("load", "PROBE_LOAD_GATE_no=True"),
                ("load", "PROBE_LOAD_FIELD_no=True"),
                ("sidebar", "PROBE_SIDEBAR_BADGE_no=True"),
                ("sidebar", "PROBE_SIDEBAR_DOC_BADGE_no=True")],
        expect=["test_nothing_loose_counts_as_an_approval",
                "test_gate_shut_when_any_paid_beats_approval_is_not_a_boolean",
                "test_a_loose_approval_never_becomes_an_approved_shot",
                "test_the_durable_store_cannot_restore_a_badge_the_scan_withheld"],
    ),
    Mutation(
        "equality instead of identity", "contract §5.4",
        "`is True` -> `== True`, which is the change a reviewer would call "
        "equivalent. It is not: bool is an int in Python, so 1 and 1.0 now count "
        "as an approval — exactly the values a loosely-typed client sends, and "
        "the reason the ledger's _valid() spells the same distinction out",
        [(MANIFEST, EXPLICIT_RETURN, "    return value == True  # MUTANT # noqa: E712\n")],
        probes=[("inmem", "PROBE_INMEM_GATE_one=True")],
        expect=["test_nothing_loose_counts_as_an_approval",
                "test_gate_shut_when_any_paid_beats_approval_is_not_a_boolean"],
    ),
    Mutation(
        "the migration wearing a fix's clothes", "contract §5.4",
        "the loader coerces with bool() instead of refusing — the one thing the "
        "brief forbids by name. Nothing is asked to re-approve, every manifest "
        "keeps working, and `approved: \"no\"` is now durably stored as true",
        [(MANIFEST, FLAG_BODY, "    return bool(raw)  # MUTANT\n")],
        probes=[("load", "PROBE_LOAD_GATE_no=True"),
                ("load", "PROBE_LOAD_FIELD_no=True"),
                ("load", "PROBE_LOAD_WRITTEN_no=True")],
        expect=["test_a_loose_approval_never_becomes_an_approved_shot",
                "test_the_loose_value_does_not_survive_into_what_gets_written_back",
                "test_a_hand_edited_manifest_on_disk_cannot_open_gate_1"],
    ),
    Mutation(
        "the flip is silent", "contract §5.4",
        "the printed note — the degrade still runs the safe way, so nothing is "
        "overspent, but a beat the user approved yesterday is unapproved today "
        "with nothing anywhere saying why. That is the difference between a "
        "behaviour change and a mystery",
        [(MANIFEST, FLAG_BODY,
          "    if approval_is_explicit(raw):\n"
          "        return True\n"
          "    return False  # MUTANT: no note\n")],
        probes=[("note", "PROBE_NOTE_no=False"),
                ("note", "PROBE_NOTE_one=False")],
        expect=["test_a_wrong_type_is_said_out_loud"],
    ),
    Mutation(
        "the note fires on the shapes that never meant approved",
        "contract §5.4",
        "the null/boolean exemption — every ordinary load of every unapproved "
        "beat now prints a warning, so the note that matters is buried in notes "
        "that do not, which is how a real one comes to be ignored",
        [(MANIFEST, FLAG_BODY,
          "    if approval_is_explicit(raw):\n"
          "        return True\n"
          "    print(f\"manifest: {field}={raw!r} on {where} is \"\n"
          "          f\"{type(raw).__name__}, not a boolean\")  # MUTANT\n"
          "    return False\n")],
        probes=[("note", "PROBE_NOTE_false_bool=True"),
                ("note", "PROBE_NOTE_null=True")],
        expect=["test_a_boolean_or_a_null_is_not_worth_a_note"],
    ),
    Mutation(
        "the gate checks the project term but not the beats", "contract §5.4",
        "half the predicate — the storyboard flag is strict and the per-beat one "
        "is truthy again, which is the shape a partial fix takes and which reads "
        "as correct because the function does mention approval_is_explicit",
        [(MANIFEST, GATE_BODY,
          "        if not approval_is_explicit(self.storyboard_approved):\n"
          "            return False\n"
          "        return all(\n"
          "            s.approved and bool(s.video_model)  # MUTANT\n"
          "            for s in self.shots\n"
          "            if s.needs_paid_video()\n"
          "        )\n")],
        probes=[("inmem", "PROBE_INMEM_GATE_no=True")],
        expect=["test_gate_shut_when_any_paid_beats_approval_is_not_a_boolean"],
    ),
    Mutation(
        "the gate checks the beats but not the project", "contract §5.4",
        "the other half — every beat is strictly approved and the project flag "
        "is truthy, so `storyboard_approved: \"no\"` opens a gate whose entire "
        "purpose is that a human said yes",
        [(MANIFEST, GATE_BODY,
          "        if not self.storyboard_approved:  # MUTANT\n"
          "            return False\n"
          "        return all(\n"
          "            approval_is_explicit(s.approved) and bool(s.video_model)\n"
          "            for s in self.shots\n"
          "            if s.needs_paid_video()\n"
          "        )\n")],
        probes=[("inmem", "PROBE_INMEM_SBGATE_no=True")],
        expect=["test_gate_shut_when_the_storyboard_approval_is_not_a_boolean"],
    ),
    Mutation(
        "the strict check is applied to the model term too", "contract §5.4",
        "over-correction — video_model asks whether a model was CHOSEN, not "
        "whether anyone gave permission, so `is True` there closes Gate 1 on "
        "every legitimately approved project and the fix takes the product down",
        [(MANIFEST, GATE_BODY,
          "        if not approval_is_explicit(self.storyboard_approved):\n"
          "            return False\n"
          "        return all(\n"
          "            approval_is_explicit(s.approved)\n"
          "            and approval_is_explicit(s.video_model)  # MUTANT\n"
          "            for s in self.shots\n"
          "            if s.needs_paid_video()\n"
          "        )\n")],
        probes=[("inmem", "PROBE_INMEM_GATE_realtrue=False"),
                ("load", "PROBE_LOAD_GATE_realtrue=False")],
        expect=["test_the_paid_fixture_clears_the_gate_when_approval_is_a_real_boolean",
                "test_a_real_approval_still_opens_the_gate_through_from_dict"],
    ),
    # ---- sentinels: the class the token list alone cannot close -----------------
    #
    # These are the mutations that caught the first revision of this work. Every
    # loose value in the test table then read as a REFUSAL or a structure, so a
    # check weakened to accept one affirmative token passed all of them with the
    # entire suite green.
    #
    # SENTINEL-1 and SENTINEL-2 are the same weakening with a different word, and
    # the pair is the point. `yes` is in the test file's table, so the token list
    # kills it. `sanctioned-by-vesper-2026` appears NOWHERE in that file, so the
    # token list cannot kill it and only _EqualsAnything can -- which is what
    # makes the guard a closed class rather than a longer list of instances.
    # If SENTINEL-2 ever survives while SENTINEL-1 dies, the object has been
    # dropped and this file is back to whack-a-mole.
    Mutation(
        "SENTINEL-1: a listed affirmative token counts as approval",
        "contract §5.4",
        "strictness for one word -- `is True or == \"yes\"`, the shape somebody "
        "adds for a client that sends text. A paid Tier-C beat carrying "
        "`approved: \"yes\"` clears Gate 1 having been approved by nobody",
        [(MANIFEST, EXPLICIT_RETURN,
          '    return value is True or value == "yes"  # MUTANT\n')],
        probes=[("inmem", "PROBE_SENTINEL_yes=True"),
                ("inmem", "PROBE_SENTINEL_eqany=True"),
                ("paid", "PROBE_PAIDGATE_yes=True")],
        expect=["test_nothing_loose_counts_as_an_approval[str-yes]",
                "test_nothing_loose_counts_as_an_approval[equals-anything]",
                "test_gate_shut_when_any_paid_beats_approval_is_not_a_boolean",
                "test_the_paid_gate_refuses_an_approval_that_is_not_a_boolean"],
    ),
    Mutation(
        "SENTINEL-2: an UNLISTED token counts as approval", "contract §5.4",
        "the same weakening with a word the test table does not contain. No "
        "list of affirmative strings can kill this, however long -- the token "
        "space is every string and the dangerous one is by definition the one "
        "nobody listed. Only _EqualsAnything reaches it",
        [(MANIFEST, EXPLICIT_RETURN,
          '    return value is True or value == "sanctioned-by-vesper-2026"  # MUTANT\n')],
        probes=[("inmem", "PROBE_SENTINEL_unlisted=True"),
                ("inmem", "PROBE_SENTINEL_eqany=True"),
                ("paid", "PROBE_PAIDGATE_eqany=True")],
        expect=["test_nothing_loose_counts_as_an_approval[equals-anything]",
                "test_the_affirmative_half_of_the_table_is_not_token_bound"],
    ),
    Mutation(
        "SENTINEL-3: a membership whitelist, bypassing the helper entirely",
        "contract §5.4",
        "the sentinel added at the CALL SITE rather than at the definition, so "
        "approval_is_explicit is untouched and its unit tests all still pass. "
        "Only a test that drives gate_cleared itself can see this",
        [(MANIFEST,
          "            approval_is_explicit(s.approved) and bool(s.video_model)\n",
          "            (approval_is_explicit(s.approved)\n"
          '             or s.approved in ("yes", "on", "granted"))  # MUTANT\n'
          "            and bool(s.video_model)\n")],
        probes=[("inmem", "PROBE_SENTINEL_yes=True"),
                ("inmem", "PROBE_SENTINEL_eqany=True")],
        expect=["test_gate_shut_when_any_paid_beats_approval_is_not_a_boolean"],
    ),
    Mutation(
        "SENTINEL-4: the paid gate infers approval again", "contract §5.4",
        "require_paid_gate back to truthiness -- the helper every paid render "
        "route calls, and the function money actually passes through. Not "
        "reachable today, which is exactly why it needs a test rather than an "
        "argument",
        [(MAIN, PAID_GATE,
          '    if not getattr(sb, "storyboard_approved", False):  # MUTANT\n')],
        probes=[("paid", "PROBE_PAIDGATE_no=True"),
                ("paid", "PROBE_PAIDGATE_yes=True"),
                ("paid", "PROBE_PAIDGATE_eqany=True")],
        expect=["test_the_paid_gate_refuses_an_approval_that_is_not_a_boolean"],
    ),
    Mutation(
        "SENTINEL-5: the coverage door infers approval again", "contract §5.4",
        "the director's Tier-C route back to truthiness. It exists because "
        "coverage was a second door onto paid video that walked past the first "
        "gate; two doors that disagree about what approved means is how the "
        "third gets missed",
        [(DIRECTOR, DIRECTOR_GATE,
          '    if paid and not getattr(sb, "storyboard_approved", False):  # MUTANT\n')],
        probes=[("coverage", "PROBE_COVERAGE_no=True"),
                ("coverage", "PROBE_COVERAGE_yes=True"),
                ("coverage", "PROBE_COVERAGE_eqany=True")],
        expect=["test_paid_coverage_refuses_an_approval_that_is_not_a_boolean"],
    ),

    Mutation(
        "the loader resets approval on beats that cost nothing", "contract §5.4",
        "over-correction at the boundary — a free beat's approval is not a term "
        "in Gate 1, and dropping it means every local-tier beat reads as "
        "unapproved, which motion.py:490 uses to decide what to render",
        [(MANIFEST, FROMDICT_SHOT,
          '                fields["approved"] = False  # MUTANT\n')],
        probes=[("load", "PROBE_LOAD_GATE_realtrue=False")],
        expect=["test_a_real_approval_still_opens_the_gate_through_from_dict",
                "test_a_loose_approval_never_becomes_an_approved_shot"],
    ),
]


# --------------------------------------------------------------------------- #
# harness (same as mutate_slice8_storage_gate.py — see its notes)
# --------------------------------------------------------------------------- #

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
    """Whether a probe printed exactly this line. Equality, not ``in``."""
    return signature in probe_lines(out)


def failing_tests(out: str) -> list[str]:
    return sorted({line.split(" ", 1)[1].strip()
                   for line in out.splitlines() if line.startswith("FAILED ")})


def _read(path: Path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


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


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

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

        for name in sorted({n for n, _ in mut.probes}):
            print(f"    probe {name}:")
            for ln in probe_lines(probe_out.get(name, "")):
                print(f"      {ln}")
            if not probe_lines(probe_out.get(name, "")):
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

"""Mutation harness for Gate 1 — ``gate_cleared()`` and the four paid doors.

    python scratch/mutate_gate1.py

This is the one item in the slice 8 retrofit that had **no harness at all**. The
brief attributed the `shots[:k]` prefix sweep and the `all`/`any` quantifier to
``scratch/mutate_slice8.py``; that file is the project-identity harness for
issues #7 and #8 and never touches the gate. The sweep exists — it is the
parametrization in ``tests/test_manifest.py`` — but nothing had ever removed the
predicate and checked that the sweep notices. This file does that.

Gate 1 is not one function. It is a predicate plus four places that consult it,
and each of the four is a separate way for money to leave:

  * ``Storyboard.gate_cleared()``      backend/manifest.py — the predicate
  * ``require_paid_gate()``            backend/main.py     — the studio's paid routes
  * the coverage gate                  backend/director.py — Director's paid tier
  * ``stage_gate()``                   pipeline.py         — the CLI orchestrator

Same two obligations as every other harness here, and one that is specific to a
gate:

  * **a test that fails under a faithful mutation of the safeguard** — every
    mutation below must be killed. A survivor is a gate no test protects;
  * **a mutation that genuinely reproduces the defect it names** — so every
    mutation declares (probe, signature) pairs. The signature must appear under
    the mutation and must NOT appear pristine.
  * **the defect-proving assertion must be the one that fires.** A gate test
    typically asserts the verdict and then something cheaper about its fixture;
    if the cheap one dies first the table goes red having demonstrated nothing.
    ``proves`` names the assertion source that must appear in the failure output
    and ``not_proves`` names the ones that must not. This project has produced
    three failures of exactly that class.

What a gate mutation must show is not "a test went red" but "money moved". The
route, director and pipeline probes therefore count **calls to the paid
provider**, not status codes: under a mutation of ``require_paid_gate`` the
route probe reports that an unapproved storyboard bought a still and reached
fal, which is the defect in the only terms that matter.

Nothing here can reach a real provider. Every probe installs a recording stub in
``sys.modules`` before ``backend.main`` is imported, and the probe environment
has ``FAL_KEY``/``ELEVENLABS_API_KEY``/``ANTHROPIC_API_KEY`` stripped, so a stub
that missed something fails loudly rather than spending.
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
PIPELINE = ROOT / "pipeline.py"

# The repository root is itself a project, so a probe that saves a plan or opens
# a ledger can file it here if a mutation loses the project binding. Beat id
# s901 is outside the range any real episode uses (17 beats), but "unlikely" is
# not "nobody's", so the run refuses to start if either path already exists and
# only removes what it created. Same ownership rule as mutate_slice8_tg_s4.py.
ROOT_ARTIFACTS = (ROOT / "generation" / "s901.json",
                  ROOT / "director" / "s901.json")


# --------------------------------------------------------------------------- #
# probes
# --------------------------------------------------------------------------- #

_PRELUDE = '''
import json, sys, tempfile, types
from pathlib import Path
sys.path.insert(0, r"__ROOT__")


class _Recorder(types.ModuleType):
    """A stand-in for a paid client that records instead of spending.

    Installed in sys.modules BEFORE backend.main imports it, so the real
    fal_client can never be bound. Any attribute reached is counted; calling one
    records the name and returns a shape the caller can proceed with, because a
    probe that dies at the first stub cannot report what happened after it.
    """
    calls = []

    def __getattr__(self, name):
        def call(*a, **k):
            type(self).calls.append(name)
            if name == "upload_file":
                return "https://example.invalid/probe.png"
            if name == "subscribe":
                return {"video": {"url": "https://example.invalid/probe.mp4"}}
            return None
        return call


FAL = _Recorder("fal_client")
sys.modules["fal_client"] = FAL
for _m in ("anthropic", "elevenlabs"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

from backend import config, manifest
from backend.manifest import Camera, MotionType, Shot, Storyboard

tmp = Path(tempfile.mkdtemp())


def paid(scene_id, approved=True, video_model="seedance_2_0"):
    """A Tier-C beat, gate-clearing unless told otherwise."""
    return Shot(scene_id=scene_id, motion_type=MotionType.AI_VIDEO,
                approved=approved, video_model=video_model)


def free(scene_id, motion_type=MotionType.PARALLAX):
    return Shot(scene_id=scene_id, motion_type=motion_type)
'''

# The predicate itself, swept the way tests/test_manifest.py sweeps it: every
# position of every length, crossed with the three ways one beat can be unready.
# A `shots[:k]` gate is invisible to any fixture of fixed size, so the probe
# reports the FIRST (length, position, kind) the gate fails to see -- which is a
# different pair for every prefix length, and different again for the quantifier.
PROBE_GATE = _PRELUDE + '''
MAX_PAID = 6
KINDS = ("unapproved", "no_model", "blank_model")


def broken(scene_id, kind):
    if kind == "unapproved":
        return paid(scene_id, approved=False)
    if kind == "no_model":
        return paid(scene_id, video_model=None)
    return paid(scene_id, video_model="")


blind = []
for n in range(1, MAX_PAID + 1):
    for i in range(n):
        for kind in KINDS:
            shots = [paid("s%03d" % (j + 1)) for j in range(n)]
            shots[i] = broken(shots[i].scene_id, kind)
            sb = Storyboard(title="T", storyboard_approved=True,
                            shots=[*shots, free("s900")])
            if sb.gate_cleared():
                blind.append("n%di%d-%s" % (n, i, kind))

print("PROBE_GATE_BLIND_COUNT=%d" % len(blind))
print("PROBE_GATE_ANY_BLIND=" + ("true" if blind else "false"))
print("PROBE_GATE_FIRST_BLIND=" + (blind[0] if blind else "none"))

# The three single-term cases, stated separately so a mutation of one term is
# distinguishable from a mutation of the quantifier.
print("PROBE_GATE_ONE_PAID_UNAPPROVED=" + str(Storyboard(
    title="T", storyboard_approved=True,
    shots=[paid("s001", approved=False)]).gate_cleared()))
print("PROBE_GATE_ONE_PAID_NO_MODEL=" + str(Storyboard(
    title="T", storyboard_approved=True,
    shots=[paid("s001", video_model=None)]).gate_cleared()))
print("PROBE_GATE_ONE_PAID_BLANK_MODEL=" + str(Storyboard(
    title="T", storyboard_approved=True,
    shots=[paid("s001", video_model="")]).gate_cleared()))

# The storyboard-level flag, checked first and alone: every beat below is ready,
# so a True here is that flag having stopped being consulted.
print("PROBE_GATE_UNAPPROVED_BOARD=" + str(Storyboard(
    title="T", storyboard_approved=False,
    shots=[paid("s001"), free("s002")]).gate_cleared()))
print("PROBE_GATE_UNAPPROVED_EMPTY_BOARD=" + str(
    Storyboard(title="T", shots=[]).gate_cleared()))

# The other direction. An all-local episode costs nothing at the video API, so
# per-beat budget approval has nothing to gate and the gate must stay OPEN --
# a filter that stops selecting paid beats turns Gate 1 into a second approval
# gate on free work and blocks every episode that never buys a clip.
print("PROBE_GATE_ALL_LOCAL_UNAPPROVED=" + str(Storyboard(
    title="T", storyboard_approved=True,
    shots=[free("s001", MotionType.STATIC), free("s002")]).gate_cleared()))
print("PROBE_GATE_READY_BOARD=" + str(Storyboard(
    title="T", storyboard_approved=True,
    shots=[paid("s001"), paid("s002"), free("s003")]).gate_cleared()))
'''

# The studio's paid video route, driven for real. The signature is not the
# status code -- it is whether an unapproved storyboard reached the provider.
PROBE_ROUTE = _PRELUDE + '''
from fastapi.testclient import TestClient
import backend.main as M

root = tmp / "bestiary" / "leshy"
(root / "assets" / "s001").mkdir(parents=True)
MF = root / "storyboard_manifest.json"
config.MANIFEST_PATH = MF

shot = Shot(scene_id="s001", narration="n", prompt="p",
            camera=Camera(move="static", duration=6.0),
            motion_type=MotionType.AI_VIDEO, video_model="seedance_2_0")
sb = Storyboard(title="T", storyboard_approved=False, shots=[shot])
manifest.save(sb, MF)

M.get_current_project = lambda: sb
M.save_current_project = lambda _sb: None
M.get_active_manifest_path = lambda: str(MF)
M.config.require_for = lambda *a, **k: None

# Everything downstream of the gate is redirected into tmp and stubbed. Not
# tidiness: without this the route stops at "Missing starting still image
# draft" BEFORE it reaches fal, so a mutation that removed the gate would show
# the still being bought and nothing else -- half the spend, reported as though
# that were the whole of it.
still = root / "assets" / "s001" / "var_a.png"
still.write_bytes(b"\\x89PNG")
render_dir = root / "render"
M.config.episode_paths = lambda title: {"render": render_dir, "root": root,
                                        "narration": root, "sfx": root}
M.config.assets_dir = lambda: root / "assets"
# Resolves only once the shot HAS a draft, which is what makes the auto-draft
# branch reachable. Returning the file unconditionally made the route skip the
# draft purchase entirely, so the first run of this harness reported the gate
# mutation reaching fal while showing no still bought -- half the spend,
# presented as the whole of it.
M._resolve_local_image_file = lambda rel, scene_id=None: (still if rel else None)
M.assets._download = lambda url, dest: Path(dest).write_bytes(b"CLIP")

drafts = []
def fake_drafts(shot_, n=3, backend="", render=None, **kw):
    # $0.15 an image. Recorded, never called out.
    drafts.append(shot_.scene_id)
    shot_.draft_variations = ["assets/s001/var_a.png"]
    shot_.draft_image = shot_.draft_variations[0]
    return shot_.draft_variations
M.assets.generate_for_shot = fake_drafts

client = TestClient(M.app, raise_server_exceptions=False)
r = client.post("/api/shot/s001/generate_video", json={})

print("PROBE_ROUTE_STATUS=%d" % r.status_code)
print("PROBE_ROUTE_SAYS_APPROVE_FIRST="
      + ("true" if "Approve the storyboard first" in r.text else "false"))
# The two spends this route makes, in the order it makes them. The still is
# bought on the way down to the video call, which is why the gate has to sit
# above both rather than merely above fal.
print("PROBE_ROUTE_BOUGHT_A_STILL=" + ("true" if drafts else "false"))
print("PROBE_ROUTE_CALLED_FAL=" + ("true" if FAL.calls else "false"))
print("PROBE_ROUTE_FAL_CALLS=" + json.dumps(FAL.calls))

# The other direction, measured on the same route: an APPROVED storyboard must
# get through. Without this leg PROBE_ROUTE_STATUS=400 is the pristine reading
# too, so it cannot be the signature for a gate that refuses everything -- the
# first version of this harness declared exactly that and the run caught it.
sb.storyboard_approved = True
drafts.clear()
del FAL.calls[:]
approved = client.post("/api/shot/s001/generate_video", json={})
print("PROBE_ROUTE_APPROVED_STATUS=%d" % approved.status_code)
print("PROBE_ROUTE_APPROVED_REFUSED="
      + ("true" if "Approve the storyboard first" in approved.text else "false"))
print("PROBE_ROUTE_APPROVED_REACHED_FAL="
      + ("true" if FAL.calls else "false"))
'''

# Director coverage: the second route to Tier C, gated separately in
# director.py because the plan is compiled outside the storyboard's own routes.
PROBE_DIRECTOR = _PRELUDE + '''
from backend import director, projects

root = tmp / "bestiary" / "leshy"
(root / "assets").mkdir(parents=True)
MF = root / "storyboard_manifest.json"

STRAY = [Path(r"__ROOT__") / "generation" / "s901.json",
         Path(r"__ROOT__") / "director" / "s901.json"]
OCCUPIED = [str(p) for p in STRAY if p.exists()]
if OCCUPIED:
    # A generation ledger is a money record; refuse rather than clear.
    print("PROBE_DIRECTOR_REFUSED_TO_RUN=" + json.dumps(OCCUPIED))
    raise SystemExit(3)

sb = Storyboard(title="T", storyboard_approved=False,
                shots=[Shot(scene_id="s901", narration="n", prompt="p",
                            camera=Camera(move="static", duration=5.0))])
manifest.save(sb, MF)
config.MANIFEST_PATH = MF
projects.set_fallback(projects.ProjectContext.from_manifest(MF))
director.config.MANIFEST_PATH = MF

plan = director.CoveragePlan(beat_id="s901", beat_duration=5.0, status="locked")
ds = director.DirectorShot(id="s901.01", beat_id="s901", motion_type="ai_video",
                           camera=Camera(move="static", duration=5.0),
                           prompt="a hand on drill steel", subject="single jack",
                           shot_size="m", purpose="master")
ds.draft_variations = ["a.png"]
ds.chosen_variation = 0
plan.coverage = [ds]
director.approve(plan)
director.save_plan(plan)

bought = []
def fake_paid(ds_, synth, sb_, out_dir, log=print):
    bought.append(ds_.id)
    target = Path(out_dir) / (ds_.id + ".mp4")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"PAID-BYTES")
    return target

director.generate_paid_clip = fake_paid
director.normalize_clip = lambda *a, **k: None
director.fit_clip = lambda *a, **k: None
director.concat = lambda *a, **k: Path("beat.mp4")
director.probe_seconds = lambda *a, **k: 5.0

render_dir = tmp / "render"
render_dir.mkdir(parents=True, exist_ok=True)
refusal = ""
try:
    director.compile_coverage(director.load_plan("s901"), sb, render_dir,
                              log=lambda m: None)
    outcome = "compiled"
except Exception as exc:
    outcome = "raised:" + type(exc).__name__
    refusal = str(exc)

print("PROBE_DIRECTOR_OUTCOME=" + outcome)
print("PROBE_DIRECTOR_SAYS_NOT_APPROVED="
      + ("true" if "not approved" in refusal else "false"))
print("PROBE_DIRECTOR_PAID_CALLS=%d" % len(bought))
print("PROBE_DIRECTOR_BOUGHT_UNAPPROVED="
      + ("true" if bought else "false"))

# The free-tier leg. Static and parallax cost nothing and drafts are explicitly
# a pre-gate activity, so an unapproved beat must still assemble locally -- that
# review is what produces the approval. Without this leg
# PROBE_DIRECTOR_SAYS_NOT_APPROVED=true is the pristine reading too, and so
# cannot be the signature for a gate that has stopped distinguishing tiers.
plan2 = director.load_plan("s901")
plan2.coverage[0].motion_type = "parallax"
plan2.status = "locked"
director.approve(plan2)
director.save_plan(plan2)
free_refusal = ""
try:
    director.compile_coverage(director.load_plan("s901"), sb, render_dir,
                              log=lambda m: None, skip_existing=False)
    free_outcome = "compiled"
except Exception as exc:
    free_outcome = "raised:" + type(exc).__name__
    free_refusal = str(exc)

print("PROBE_DIRECTOR_FREE_OUTCOME=" + free_outcome)
print("PROBE_DIRECTOR_FREE_SAYS_NOT_APPROVED="
      + ("true" if "not approved" in free_refusal else "false"))

for stray in STRAY:
    if stray.is_file():
        stray.unlink()
'''

# pipeline.py, the CLI orchestrator CLAUDE.md names as the only entrypoint.
# stage_gate re-reads the manifest after the human has been sent to the studio
# and returns None if the gate is still shut; returning a storyboard is the
# pipeline continuing into the render stages on an unapproved cut.
PROBE_PIPELINE = _PRELUDE + '''
import io, contextlib
import pipeline

root = tmp / "bestiary" / "leshy"
root.mkdir(parents=True, exist_ok=True)
MF = root / "storyboard_manifest.json"
config.MANIFEST_PATH = MF

sb = Storyboard(title="T", storyboard_approved=False,
                shots=[paid("s001", approved=False, video_model=None)])
manifest.save(sb, MF)

hushed = io.StringIO()
with contextlib.redirect_stdout(hushed):
    shut = pipeline.stage_gate(sb, "127.0.0.1", 5000)

print("PROBE_PIPELINE_SHUT_RESULT="
      + ("None" if shut is None else type(shut).__name__))
print("PROBE_PIPELINE_ADVANCED_UNAPPROVED="
      + ("false" if shut is None else "true"))
print("PROBE_PIPELINE_TOLD_THE_HUMAN="
      + ("true" if "Gate NOT cleared" in hushed.getvalue() else "false"))
print("PROBE_PIPELINE_SKIPPED_THE_PROMPT="
      + ("true" if "HUMAN-APPROVAL GATE" not in hushed.getvalue() else "false"))

# The cleared side, so a mutation that simply jams the gate shut is visible too.
ok = Storyboard(title="T", storyboard_approved=True, shots=[paid("s001")])
manifest.save(ok, MF)
hushed2 = io.StringIO()
with contextlib.redirect_stdout(hushed2):
    opened = pipeline.stage_gate(ok, "127.0.0.1", 5000)
print("PROBE_PIPELINE_OPEN_RESULT="
      + ("None" if opened is None else type(opened).__name__))
'''

PROBES = {
    "gate": PROBE_GATE,
    "route": PROBE_ROUTE,
    "director": PROBE_DIRECTOR,
    "pipeline": PROBE_PIPELINE,
}


@dataclass
class Mutation:
    name: str
    clause: str
    removes: str                       # the behaviour this deletes, in one line
    edits: list[tuple[Path, str, str]]
    # (probe, signature) pairs. Required for every mutation: the signature must
    # appear under the mutation and must NOT appear pristine.
    probes: list[tuple[str, str]] = field(default_factory=list)
    defect: bool = False
    expect: list[str] = field(default_factory=list)
    # Reachability. `expect` proves the right TEST died; these two prove the
    # right ASSERTION inside it ran. `proves` must appear in the mutated suite's
    # output, `not_proves` must not -- the second is what catches a test that
    # went red on its fixture check rather than on the verdict.
    proves: list[str] = field(default_factory=list)
    not_proves: list[str] = field(default_factory=list)


# --- anchors ------------------------------------------------------------------
#
# The gate body is replaced whole rather than clause by clause. Its terms are
# short and appear elsewhere in manifest.py; a whole-body anchor cannot be
# ambiguous, and every mutation is then visibly a complete alternative predicate
# rather than a diff a reader has to apply in their head.

GATE_BODY = (
    "        return all(\n"
    "            s.approved and bool(s.video_model)\n"
    "            for s in self.shots\n"
    "            if s.needs_paid_video()\n"
    "        )"
)


def gate(terms: str = "s.approved and bool(s.video_model)",
         quantifier: str = "all", iterable: str = "self.shots",
         predicate: str = "\n            if s.needs_paid_video()") -> str:
    return (f"        return {quantifier}(  # MUTANT\n"
            f"            {terms}\n"
            f"            for s in {iterable}{predicate}\n"
            f"        )")


GATE_APPROVAL_CHECK = (
    "        if not self.storyboard_approved:\n"
    "            return False\n"
)

NEEDS_PAID = (
    "    def needs_paid_video(self) -> bool:\n"
    "        return self.motion_type == MotionType.AI_VIDEO"
)

REQUIRE_PAID_GATE = (
    '    if not getattr(sb, "storyboard_approved", False):\n'
    "        raise HTTPException(\n"
    "            status_code=400,\n"
    '            detail=f"Approve the storyboard first — that is where the {what} "\n'
    '                   f"budget is allocated.",\n'
    "        )"
)

DIRECTOR_GATE = '    if paid and not getattr(sb, "storyboard_approved", False):'
DIRECTOR_SELECTS_PAID = (
    '    paid = [s.id for s in plan.coverage if s.motion_type == "ai_video"]'
)

PIPELINE_RECHECK = "    if not sb.gate_cleared():"
PIPELINE_FIRST_CHECK = "    if sb.gate_cleared():"


def _prefix(k: int) -> Mutation:
    """A gate that reads a prefix of the shot list.

    Not hypothetical: `for s in self.shots[:3]` passed all fifteen gate tests as
    they stood three revisions ago, and opened Gate 1 on a fourth beat with no
    model and no approval. tests/test_manifest.py is parametrized over length x
    position precisely to close the whole class, and these are what check that
    it did. The first blind pair for [:k] is (k+1 beats, position k), which is
    why each mutation below declares a different one.
    """
    return Mutation(
        f"the gate reads only the first {k} shot(s)", "CLAUDE.md Gate 1",
        f"every beat past position {k} — a storyboard whose {k + 1}th paid beat "
        f"is unapproved or has no model clears Gate 1 and reaches the paid API",
        [(MANIFEST, GATE_BODY, gate(iterable=f"self.shots[:{k}]"))],
        probes=[("gate", "PROBE_GATE_ANY_BLIND=true"),
                ("gate", f"PROBE_GATE_FIRST_BLIND=n{k + 1}i{k}-unapproved")],
        expect=["test_gate_shut_when_any_paid_shot_is_unapproved",
                "test_gate_shut_when_any_one_of_several_paid_shots_has_no_video_model"],
        proves=["assert sb.gate_cleared() is False"],
        not_proves=["assert _siblings_are_ready(paid, broken_at)"],
    )


MUTATIONS = [
    # ---- the predicate: the quantifier ----------------------------------------
    Mutation(
        "one ready paid beat clears the gate for all of them", "CLAUDE.md Gate 1",
        "the quantifier — `all` becomes `any`, so a storyboard with one approved "
        "Tier-C beat clears Gate 1 for every other Tier-C beat in the episode, "
        "however unready. A single-shot fixture cannot see this: over one "
        "element `all` and `any` agree",
        [(MANIFEST, GATE_BODY, gate(quantifier="any"))],
        probes=[("gate", "PROBE_GATE_ANY_BLIND=true"),
                ("gate", "PROBE_GATE_FIRST_BLIND=n2i0-unapproved")],
        expect=["test_gate_shut_when_any_paid_shot_is_unapproved",
                "test_gate_shut_when_any_one_of_several_paid_shots_has_no_video_model"],
        proves=["assert sb.gate_cleared() is False"],
        # No not_proves here, unlike every prefix mutation below, and the reason
        # is worth recording because the first run of this harness reported it as
        # a fault. `_siblings_are_ready` DOES also fail under `any`, legitimately:
        # for the single-paid-beat cases the sibling list is empty, and `any([])`
        # is False where `all([])` is True. That is a real second consequence of
        # swapping the quantifier, not a cheaper assertion masking the first.
    ),

    # ---- the predicate: a prefix of the list -----------------------------------
    _prefix(1), _prefix(2), _prefix(3), _prefix(4), _prefix(5),

    # ---- the predicate: the per-beat terms -------------------------------------
    Mutation(
        "an approved Tier-C beat needs no model to render on", "CLAUDE.md Gate 1",
        "the model term — a beat ticked for spend with nothing selected to "
        "generate it sends the pipeline at a paid API with no model",
        [(MANIFEST, GATE_BODY, gate(terms="s.approved"))],
        probes=[("gate", "PROBE_GATE_ONE_PAID_NO_MODEL=True"),
                ("gate", "PROBE_GATE_ONE_PAID_BLANK_MODEL=True"),
                ("gate", "PROBE_GATE_FIRST_BLIND=n1i0-no_model")],
        expect=["test_gate_shut_when_any_one_of_several_paid_shots_has_no_video_model"],
        proves=["assert sb.gate_cleared() is False"],
        not_proves=["assert _siblings_are_ready(paid, broken_at)"],
    ),
    Mutation(
        "a Tier-C beat with a model needs no approval", "CLAUDE.md Gate 1",
        "the per-beat approval tick — approving the storyboard becomes approving "
        "the spend, and the line-item budget allocation Gate 1 exists for is gone",
        [(MANIFEST, GATE_BODY, gate(terms="bool(s.video_model)"))],
        probes=[("gate", "PROBE_GATE_ONE_PAID_UNAPPROVED=True"),
                ("gate", "PROBE_GATE_FIRST_BLIND=n1i0-unapproved")],
        expect=["test_gate_shut_when_any_paid_shot_is_unapproved"],
        proves=["assert sb.gate_cleared() is False"],
        not_proves=["assert _siblings_are_ready(paid, broken_at)"],
    ),
    Mutation(
        "a blank model counts as a model", "CLAUDE.md Gate 1",
        "`bool()` — the empty string is what a cleared dropdown writes, and it "
        "is not None, so the gate opens on a beat with nothing selected",
        [(MANIFEST, GATE_BODY, gate(terms="s.approved and s.video_model is not None"))],
        probes=[("gate", "PROBE_GATE_ONE_PAID_BLANK_MODEL=True"),
                ("gate", "PROBE_GATE_FIRST_BLIND=n1i0-blank_model")],
        expect=["test_gate_shut_when_any_one_of_several_paid_shots_has_no_video_model"],
        proves=["assert sb.gate_cleared() is False"],
        not_proves=["assert _siblings_are_ready(paid, broken_at)"],
    ),

    # ---- the predicate: what it is quantified over -----------------------------
    Mutation(
        "the storyboard-level approval flag is never consulted",
        "CLAUDE.md Gate 1",
        "the human in the loop — an episode nobody approved clears Gate 1 as "
        "long as its paid beats happen to carry models, and an empty storyboard "
        "clears it unconditionally",
        [(MANIFEST, GATE_APPROVAL_CHECK, "")],
        probes=[("gate", "PROBE_GATE_UNAPPROVED_BOARD=True"),
                ("gate", "PROBE_GATE_UNAPPROVED_EMPTY_BOARD=True")],
        expect=["test_gate_shut_when_the_storyboard_is_not_approved",
                "test_gate_open_on_an_empty_storyboard_is_approval_only"],
        proves=["assert sb.gate_cleared() is False"],
    ),
    Mutation(
        "no beat is a paid beat", "CLAUDE.md Gate 1",
        "the tier test itself — nothing is ever selected for the gate, so the "
        "`all(...)` is vacuous over every storyboard and Gate 1 degrades to the "
        "approval checkbox alone",
        [(MANIFEST, NEEDS_PAID,
          "    def needs_paid_video(self) -> bool:  # MUTANT\n"
          "        return False")],
        probes=[("gate", "PROBE_GATE_ANY_BLIND=true"),
                ("gate", "PROBE_GATE_ONE_PAID_UNAPPROVED=True"),
                ("gate", "PROBE_GATE_FIRST_BLIND=n1i0-unapproved")],
        expect=["test_needs_paid_video_covers_every_motion_type",
                "test_gate_shut_when_any_paid_shot_is_unapproved",
                "test_paid_shots_selects_only_ai_video_beats"],
        proves=["assert sb.gate_cleared() is False"],
    ),
    Mutation(
        "every beat is a paid beat", "CLAUDE.md Gate 1",
        "the other direction, and the reason the filter is not merely an "
        "optimisation — Gate 1 becomes a per-beat approval gate on free work, "
        "so an all-local episode that costs nothing can never clear it",
        [(MANIFEST, GATE_BODY, gate(predicate=""))],
        probes=[("gate", "PROBE_GATE_ALL_LOCAL_UNAPPROVED=False"),
                ("gate", "PROBE_GATE_READY_BOARD=False")],
        expect=["test_gate_open_with_no_paid_shots_even_though_no_shot_is_approved",
                "test_gate_open_when_approved_and_every_paid_shot_is_ready"],
        proves=["assert sb.gate_cleared() is True"],
    ),

    # ---- door 1: the studio's paid video route ---------------------------------
    Mutation(
        "the studio's paid route stops asking whether the gate is clear",
        "CLAUDE.md Gate 1",
        "require_paid_gate's whole body — this route reached fal with no "
        "approval check at all until it was added, and it buys a $0.15 still on "
        "the way down, so removing it spends twice before anyone is asked",
        [(MAIN, REQUIRE_PAID_GATE, "    if False:  # MUTANT\n        pass")],
        probes=[("route", "PROBE_ROUTE_BOUGHT_A_STILL=true"),
                ("route", "PROBE_ROUTE_CALLED_FAL=true"),
                ("route", "PROBE_ROUTE_SAYS_APPROVE_FIRST=false")],
        defect=True,
        expect=["test_paid_video_route_refuses_an_unapproved_storyboard"],
    ),
    Mutation(
        "the paid route refuses whatever the storyboard says",
        "CLAUDE.md Gate 1",
        "the other direction — an approved storyboard is refused too, which is "
        "the shape a test asserting only the refusal cannot tell from a working "
        "gate",
        [(MAIN, REQUIRE_PAID_GATE,
          "    if True:  # MUTANT\n"
          "        raise HTTPException(\n"
          "            status_code=400,\n"
          '            detail=f"Approve the storyboard first — that is where the {what} "\n'
          '                   f"budget is allocated.",\n'
          "        )")],
        probes=[("route", "PROBE_ROUTE_APPROVED_REFUSED=true"),
                ("route", "PROBE_ROUTE_APPROVED_REACHED_FAL=false")],
        expect=["test_paid_video_route_refuses_an_unapproved_storyboard"],
    ),

    # ---- door 2: Director coverage ---------------------------------------------
    Mutation(
        "Director coverage walks past the approval gate", "CLAUDE.md Gate 1",
        "the coverage gate exactly as it was missing — compile_coverage calls "
        "generate_paid_clip for any ai_video shot and consults nothing, which is "
        "the second route to Tier C that Pass A found open",
        [(DIRECTOR, DIRECTOR_GATE, "    if False:  # MUTANT")],
        probes=[("director", "PROBE_DIRECTOR_BOUGHT_UNAPPROVED=true"),
                ("director", "PROBE_DIRECTOR_PAID_CALLS=1"),
                ("director", "PROBE_DIRECTOR_SAYS_NOT_APPROVED=false")],
        defect=True,
        expect=["test_paid_coverage_refused_until_the_storyboard_is_approved"],
    ),
    Mutation(
        "coverage stops recognising which shots cost money", "CLAUDE.md Gate 1",
        "the tier selection the gate is quantified over — `paid` is always "
        "empty, so the check is dead and every ai_video shot compiles "
        "unapproved while the code still reads as gated",
        [(DIRECTOR, DIRECTOR_SELECTS_PAID, "    paid = []  # MUTANT")],
        probes=[("director", "PROBE_DIRECTOR_BOUGHT_UNAPPROVED=true"),
                ("director", "PROBE_DIRECTOR_PAID_CALLS=1")],
        expect=["test_paid_coverage_refused_until_the_storyboard_is_approved"],
    ),
    Mutation(
        "coverage refuses free tiers as well", "CLAUDE.md Gate 1",
        "the free-tier carve-out — static and parallax cost nothing and drafts "
        "are explicitly pre-gate, so refusing them makes an unapproved beat "
        "impossible to assemble for the review that produces the approval",
        [(DIRECTOR, DIRECTOR_SELECTS_PAID,
          "    paid = [s.id for s in plan.coverage]  # MUTANT")],
        probes=[("director", "PROBE_DIRECTOR_FREE_SAYS_NOT_APPROVED=true")],
        expect=["test_free_coverage_compiles_before_approval"],
    ),

    # ---- door 3: the CLI orchestrator ------------------------------------------
    Mutation(
        "the pipeline continues after an uncleared gate", "CLAUDE.md Gate 1",
        "stage_gate's re-read verdict — the human is sent to the studio, the "
        "manifest is reloaded, and the run advances into the render stages "
        "whatever came back",
        [(PIPELINE, PIPELINE_RECHECK, "    if False:  # MUTANT")],
        probes=[("pipeline", "PROBE_PIPELINE_ADVANCED_UNAPPROVED=true"),
                ("pipeline", "PROBE_PIPELINE_TOLD_THE_HUMAN=false")],
        expect=["test_an_uncleared_gate_stops_the_pipeline",
                "test_a_storyboard_approved_with_an_unready_paid_beat_still_stops_it"],
        proves=['assert pipeline.stage_gate(sb, "127.0.0.1", 5000) is None'],
        not_proves=['assert "Gate NOT cleared" in capsys.readouterr().out'],
    ),
    Mutation(
        "the pipeline treats every gate as already cleared", "CLAUDE.md Gate 1",
        "the fast path — stage_gate returns the storyboard it was handed without "
        "reloading or checking, so the gate is skipped rather than failed and "
        "the human is never even shown the instructions",
        [(PIPELINE, PIPELINE_FIRST_CHECK, "    if True:  # MUTANT")],
        probes=[("pipeline", "PROBE_PIPELINE_ADVANCED_UNAPPROVED=true"),
                ("pipeline", "PROBE_PIPELINE_SKIPPED_THE_PROMPT=true")],
        expect=["test_an_uncleared_gate_stops_the_pipeline",
                "test_a_storyboard_approved_with_an_unready_paid_beat_still_stops_it"],
        proves=['assert pipeline.stage_gate(sb, "127.0.0.1", 5000) is None'],
        not_proves=['assert "Gate NOT cleared" in capsys.readouterr().out'],
    ),
]


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #

# The gate's refusal contains an em dash, and on Windows both ends of the pipe
# default to cp1252; a signature check reading mojibake is a harness fault
# reported as a mutation result. Pin the encoding on both sides.
#
# The API keys are removed rather than left in place. Every probe stubs its
# provider, but a stub that missed something must fail, not spend: with no key
# present config.require_for() refuses and fal_client is the recorder anyway.
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
    """Remove ROOT project files THIS RUN created. Returns what it removed.

    Ownership is established once, by the check at the top of ``main()``: both
    paths were verified absent before anything ran, so anything at them now was
    written by this harness. MUST NOT be called before that check -- it is the
    only thing standing between this function and a real money record.
    """
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
    """Whether a probe printed exactly this line.

    Equality, not ``in``. ``PROBE_GATE_BLIND_COUNT=1`` is a substring of
    ``...=18``, and ``PROBE_DIRECTOR_PAID_CALLS=1`` of ``...=12``, so a
    substring check would report a mutation as demonstrating a number the probe
    never printed.
    """
    return signature in probe_lines(out)


def failing_tests(out: str) -> list[str]:
    return sorted({line.split(" ", 1)[1].strip()
                   for line in out.splitlines() if line.startswith("FAILED ")})


def _read(path: Path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _purge_pycache(path: Path) -> None:
    """Drop any cached bytecode for ``path``.

    CPython invalidates a ``.pyc`` on (mtime, size) of the source, at one-second
    mtime resolution. Two mutations of one file whose replacements happen to be
    the same length, applied within the same second, let the second run import
    the first one's bytecode — a result reported with total confidence about
    code that never executed. Found in ``mutate_paid_path.py --discover``; see
    the longer note there. The five prefix mutations below are exactly that
    shape: ``self.shots[:1]`` through ``self.shots[:5]`` differ by one digit and
    nothing else.
    """
    cache = path.parent / "__pycache__"
    if cache.is_dir():
        for pyc in cache.glob(f"{path.stem}.*.pyc"):
            try:
                pyc.unlink()
            except OSError:
                pass


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

    occupied = [p for p in ROOT_ARTIFACTS if p.exists()]
    if occupied:
        print("refusing to run: these exist and are not this harness's to touch:")
        for p in occupied:
            print(f"  - {p.relative_to(ROOT)}")
        print("A generation ledger is a money record. Move them aside, confirm "
              "they are not real, and re-run.")
        return 1

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

        for phrase in mut.proves:
            shown = phrase in suite_out
            print(f"    assertion {'FIRED' if shown else 'NEVER RAN'}: {phrase!r}")
            if not shown:
                unproven.append(
                    f"{mut.name}: no failing assertion said {phrase!r} — the test "
                    f"died on a cheaper assertion first, so the defect was never "
                    f"demonstrated")
        for phrase in mut.not_proves:
            if phrase in suite_out:
                print(f"    assertion UNEXPECTEDLY FIRED: {phrase!r}")
                unproven.append(
                    f"{mut.name}: the fixture assertion {phrase!r} also failed — "
                    f"the test may be red for a reason other than the gate")

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

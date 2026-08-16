"""Mutation harness for the paid path — the guards that decide whether to spend.

    python scratch/mutate_paid_path.py
    python scratch/mutate_paid_path.py --discover   # probes only, no pytest

``mutate_slice8_spend.py`` covers what the ledger *reports*: billed, at risk,
certain, unreadable. This covers what it *permits*. They are different
questions and the second is the expensive one — a reporting defect misstates a
number, a permission defect buys a clip twice.

Four safeguards, none of which had a mutation:

  * **begin()'s dispositions.** Only ``"created"`` permits a call to fal. The
    other three are each a distinct way of already having paid: ``duplicate``
    (same request), ``reused`` (same inputs, media still there), ``in_flight``
    (an attempt for these inputs is still running and may already have been
    billed). S4-01 is the last one; it is the whole crash window.
  * **The dispatch boundary** (S4-R02). Everything before the provider call is
    an ordinary retryable failure; everything after it is in doubt. Getting
    this backwards in either direction is expensive: mark a pre-dispatch failure
    in doubt and the beat is stranded behind a recovery step for a risk nobody
    took; mark a post-dispatch failure as failed and the retry buys the clip
    again.
  * **Terminal facts** (S4-R03). Identical means identical *facts*, not the same
    status. ``succeed(first.mp4, $0.60)`` followed by ``succeed(other.mp4, $99)``
    used to return successfully and keep the first, so a caller believed a cost
    was recorded that never was.
  * **What counts as media.** A truncated download leaves a zero-byte file that
    ``is_file()`` confirms, and reusing it ships an empty clip while reporting a
    hit — while the *other* direction refuses to replace footage that is
    genuinely gone, which is the legitimate re-buy S4-01's first fix broke.

Same obligations as the rest: every mutation must be killed, every mutation
declares a probe signature that must appear under it and must be absent
pristine, and the signatures are counts of paid calls and stored costs rather
than status codes.

``--discover`` exists because a declared signature that never appears is a
harness fault reported as a result. It applies each mutation, runs only its
probes, prints everything they emit and restores — so the signatures below were
read off a real run rather than predicted.
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
GEN = ROOT / "backend" / "generation.py"
DIRECTOR = ROOT / "backend" / "director.py"
MANIFEST = ROOT / "backend" / "manifest.py"

# Beat s902 is outside the range any real episode uses, but "unlikely" is not
# "nobody's": a generation ledger is the record of what was paid. The run
# refuses to start if either path exists and removes only what it created.
ROOT_ARTIFACTS = (ROOT / "generation" / "s902.json",
                  ROOT / "director" / "s902.json")


# --------------------------------------------------------------------------- #
# probes
# --------------------------------------------------------------------------- #

_PRELUDE = '''
import json, sys, tempfile, types
from pathlib import Path
sys.path.insert(0, r"__ROOT__")
for _m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(_m, types.ModuleType(_m))
from backend import config, generation

BEAT = "s902"
SHOT = "s902.01"

STRAY = [Path(r"__ROOT__") / "generation" / (BEAT + ".json"),
         Path(r"__ROOT__") / "director" / (BEAT + ".json")]
OCCUPIED = [str(p) for p in STRAY if p.exists()]
if OCCUPIED:
    print("PROBE_REFUSED_TO_RUN=" + json.dumps(OCCUPIED))
    raise SystemExit(3)


def fresh():
    """A brand new project directory, so each case starts with no lineage."""
    d = Path(tempfile.mkdtemp())
    config.MANIFEST_PATH = d / "storyboard_manifest.json"
    return d


def sweep():
    for stray in STRAY:
        if stray.is_file():
            stray.unlink()
'''

# begin()'s four dispositions, each reached by the state that should produce it.
# The number that matters is the last line: how many of the five calls came back
# "created", which is the only disposition that permits a call to fal.
PROBE_DISPOSITION = _PRELUDE + '''
fresh()

def start(**kw):
    kw.setdefault("beat_id", BEAT)
    kw.setdefault("shot_id", SHOT)
    kw.setdefault("paid", True)
    kw.setdefault("estimated_cost", 0.60)
    return generation.begin(**kw)

a1, d1 = start(signature="sig-a", idempotency_key="k1")
_a2, d2 = start(signature="sig-a", idempotency_key="k1")
_a3, d3 = start(signature="sig-a")
generation.succeed(BEAT, a1.id, "out.mp4", cost=0.60)
_a4, d4 = start(signature="sig-a", exists=lambda rel: True)
_a5, d5 = start(signature="sig-a", exists=lambda rel: False)

print("PROBE_DISP_FRESH=" + d1)
print("PROBE_DISP_SAME_REQUEST=" + d2)
print("PROBE_DISP_RUNNING_SAME_INPUTS=" + d3)
print("PROBE_DISP_BOUGHT_AND_PRESENT=" + d4)
print("PROBE_DISP_BOUGHT_BUT_GONE=" + d5)
# Two of the five are legitimate reasons to spend: the first request, and the
# re-buy of media that is genuinely gone. Any more is a second bill.
made = [d1, d2, d3, d4, d5].count("created")
print("PROBE_DISP_PERMITS_TO_SPEND=%d" % made)
print("PROBE_DISP_ATTEMPTS_OPENED=%d" % len(generation.for_shot(BEAT, SHOT)))
sweep()
'''

# The whole compile path, driven for real, counting calls to the provider.
PROBE_REBUY = _PRELUDE + '''
from backend import director
from backend.manifest import Camera, Shot, Storyboard


def scene():
    d = fresh()
    director.config.MANIFEST_PATH = config.MANIFEST_PATH
    render_dir = d / "render"
    render_dir.mkdir(parents=True, exist_ok=True)

    plan = director.CoveragePlan(beat_id=BEAT, beat_duration=5.0, status="locked")
    ds = director.DirectorShot(id=SHOT, beat_id=BEAT, motion_type="ai_video",
                               camera=Camera(move="static", duration=5.0),
                               prompt="a hand on drill steel", subject="single jack",
                               shot_size="m", purpose="master")
    ds.draft_variations = ["a.png"]
    ds.chosen_variation = 0
    plan.coverage = [ds]
    director.approve(plan)
    director.save_plan(plan)

    sb = Storyboard(title="T", storyboard_approved=True,
                    shots=[Shot(scene_id=BEAT, narration="n", prompt="p",
                                camera=Camera(move="static", duration=5.0))])
    calls = {"paid": 0}

    def fake_paid(ds_, synth, sb_, out_dir, log=print):
        calls["paid"] += 1
        target = Path(out_dir) / (ds_.id + ".mp4")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"PAID-BYTES")
        return target

    director.generate_paid_clip = fake_paid
    director.normalize_clip = lambda *a, **k: None
    director.fit_clip = lambda *a, **k: None
    director.concat = lambda *a, **k: Path("beat.mp4")
    return director, sb, render_dir, calls, fake_paid


def compile_once(d, sb, render_dir, skip_existing=True):
    try:
        d.compile_coverage(d.load_plan(BEAT), sb, render_dir,
                           log=lambda m: None, skip_existing=skip_existing)
        return "compiled"
    except BaseException as exc:
        return "raised:" + type(exc).__name__


# --- the crash window: charged, then the process died -------------------------
d, sb, render_dir, calls, fake_paid = scene()

def charge_then_die(ds_, synth, sb_, out_dir, log=print):
    calls["paid"] += 1
    target = Path(out_dir) / (ds_.id + ".mp4")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"PAID-BYTES")
    raise KeyboardInterrupt("container reclaimed mid-generation")

d.generate_paid_clip = charge_then_die
compile_once(d, sb, render_dir)
first = calls["paid"]
d.generate_paid_clip = fake_paid
replay = compile_once(d, sb, render_dir, skip_existing=False)
shot_error = d.load_plan(BEAT).coverage[0].error or ""

print("PROBE_REBUY_STUCK_FIRST_PAID=%d" % first)
print("PROBE_REBUY_STUCK_REPLAY=" + replay)
print("PROBE_REBUY_STUCK_TOTAL_PAID=%d" % calls["paid"])
print("PROBE_REBUY_STUCK_BOUGHT_TWICE="
      + ("true" if calls["paid"] > 1 else "false"))
print("PROBE_REBUY_STUCK_ERROR_NAMES_RUNNING="
      + ("true" if "still recorded as running" in shot_error else "false"))
print("PROBE_REBUY_STUCK_AT_RISK=" + repr(generation.spend(BEAT).get("at_risk")))
sweep()

# --- the marker is gone and the ledger is the only thing that remembers -------
d, sb, render_dir, calls, _ = scene()
compile_once(d, sb, render_dir)
plan = d.load_plan(BEAT)
plan.coverage[0].paid_clip = ""
plan.coverage[0].paid_signature = ""
d.save_plan(plan)
marker = compile_once(d, sb, render_dir, skip_existing=False)

print("PROBE_REBUY_MARKER_REPLAY=" + marker)
print("PROBE_REBUY_MARKER_TOTAL_PAID=%d" % calls["paid"])
print("PROBE_REBUY_MARKER_BOUGHT_TWICE="
      + ("true" if calls["paid"] > 1 else "false"))
print("PROBE_REBUY_MARKER_SPENT=" + repr(generation.spend(BEAT).get("spent")))
sweep()

# --- the legitimate re-buy: the clip on disk is a truncated download ----------
# The other direction, and the one a too-eager guard breaks. S4-01's first fix
# refused to replace media that was genuinely gone; an existing test caught it.
d, sb, render_dir, calls, _ = scene()
compile_once(d, sb, render_dir)
bought = render_dir / BEAT / (SHOT + ".mp4")
if bought.is_file():
    bought.write_bytes(b"")          # the download was truncated to nothing
plan = d.load_plan(BEAT)
plan.coverage[0].paid_clip = ""
plan.coverage[0].paid_signature = ""
d.save_plan(plan)
compile_once(d, sb, render_dir, skip_existing=False)

print("PROBE_REBUY_TRUNCATED_TOTAL_PAID=%d" % calls["paid"])
print("PROBE_REBUY_TRUNCATED_REPLACED="
      + ("true" if calls["paid"] > 1 else "false"))
sweep()
'''

# Terminal facts. Each case gets its own ledger so one conflict cannot mask the
# next, and every case reports the STORED bytes afterwards -- the guarantee is
# that the first result stands, not merely that something raised.
PROBE_TERMINAL = _PRELUDE + '''
def opened():
    fresh()
    att, _ = generation.begin(beat_id=BEAT, shot_id=SHOT, signature="sig-a",
                              idempotency_key="k1", paid=True,
                              estimated_cost=0.60)
    return att


def outcome(fn, *a, **kw):
    try:
        got = fn(*a, **kw)
        return "returned:" + (got.status if got is not None else "None")
    except generation.TerminalConflict:
        return "raised:TerminalConflict"
    except Exception as exc:
        return "raised:" + type(exc).__name__


att = opened()
generation.succeed(BEAT, att.id, "first.mp4", cost=0.60)
print("PROBE_TERM_DIFFERENT_OUTPUT="
      + outcome(generation.succeed, BEAT, att.id, "other.mp4", 0.60))
print("PROBE_TERM_STORED_OUTPUT=" + generation.for_shot(BEAT, SHOT)[0].output)

att = opened()
generation.succeed(BEAT, att.id, "first.mp4", cost=0.60)
print("PROBE_TERM_DIFFERENT_COST="
      + outcome(generation.succeed, BEAT, att.id, "first.mp4", 99.00))
print("PROBE_TERM_STORED_COST=" + repr(generation.for_shot(BEAT, SHOT)[0].cost))
print("PROBE_TERM_SPENT_AFTER_CONFLICT=" + repr(generation.spend(BEAT).get("spent")))

att = opened()
generation.succeed(BEAT, att.id, "first.mp4", cost=0.60)
print("PROBE_TERM_REWRITTEN_AS_FAILED="
      + outcome(generation.fail, BEAT, att.id, "a late callback"))
after = generation.for_shot(BEAT, SHOT)[0]
print("PROBE_TERM_STATUS_AFTER_LATE_FAIL=" + after.status)
print("PROBE_TERM_BILL_FELL_TO_ZERO="
      + ("true" if generation.spend(BEAT).get("spent") == 0.0 else "false"))

att = opened()
generation.fail(BEAT, att.id, "provider failed")
print("PROBE_TERM_ABANDON_OVER_A_FAILURE="
      + outcome(generation.abandon, BEAT, att.id, "human abandoned"))
print("PROBE_TERM_STORED_REASON=" + generation.for_shot(BEAT, SHOT)[0].error)

att = opened()
generation.succeed(BEAT, att.id, "first.mp4", cost=0.60)
print("PROBE_TERM_EXACT_REPLAY="
      + outcome(generation.succeed, BEAT, att.id, "first.mp4", 0.60))
print("PROBE_TERM_REPLAY_PAID_ATTEMPTS=%d"
      % generation.spend(BEAT).get("paid_attempts", -1))
sweep()
'''

# The dispatch boundary, both sides, through the real compile.
PROBE_BOUNDARY = _PRELUDE + '''
from backend import director
from backend.manifest import Camera, Shot, Storyboard


def scene():
    d = fresh()
    director.config.MANIFEST_PATH = config.MANIFEST_PATH
    render_dir = d / "render"
    render_dir.mkdir(parents=True, exist_ok=True)
    plan = director.CoveragePlan(beat_id=BEAT, beat_duration=5.0, status="locked")
    ds = director.DirectorShot(id=SHOT, beat_id=BEAT, motion_type="ai_video",
                               camera=Camera(move="static", duration=5.0),
                               prompt="a hand on drill steel", subject="single jack",
                               shot_size="m", purpose="master")
    ds.draft_variations = ["a.png"]
    ds.chosen_variation = 0
    plan.coverage = [ds]
    director.approve(plan)
    director.save_plan(plan)
    sb = Storyboard(title="T", storyboard_approved=True,
                    shots=[Shot(scene_id=BEAT, narration="n", prompt="p",
                                camera=Camera(move="static", duration=5.0))])
    calls = {"paid": 0}

    def fake_paid(ds_, synth, sb_, out_dir, log=print):
        calls["paid"] += 1
        target = Path(out_dir) / (ds_.id + ".mp4")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"PAID-BYTES")
        return target

    director.generate_paid_clip = fake_paid
    director.normalize_clip = lambda *a, **k: None
    director.fit_clip = lambda *a, **k: None
    director.concat = lambda *a, **k: Path("beat.mp4")
    return director, sb, render_dir, calls


def run(d, sb, render_dir, skip_existing=False):
    try:
        d.compile_coverage(d.load_plan(BEAT), sb, render_dir,
                           log=lambda m: None, skip_existing=skip_existing)
        return "compiled"
    except BaseException as exc:
        return "raised:" + type(exc).__name__


# --- BEFORE the provider: the storage denied a delete -------------------------
d, sb, render_dir, calls = scene()
target = render_dir / BEAT / (SHOT + ".mp4")
target.parent.mkdir(parents=True, exist_ok=True)
target.write_bytes(b"a stale free render")

real_unlink = Path.unlink
state = {"deny": True}

def deny(self, *a, **k):
    if self == target and state["deny"]:
        raise PermissionError(13, "storage denied the delete")
    return real_unlink(self, *a, **k)

Path.unlink = deny
run(d, sb, render_dir)
row = generation.for_shot(BEAT, SHOT)[0]
print("PROBE_BOUND_PRE_PAID_CALLS=%d" % calls["paid"])
print("PROBE_BOUND_PRE_STATUS=" + row.status)
print("PROBE_BOUND_PRE_IN_DOUBT=" + ("true" if row.outcome_unknown else "false"))
print("PROBE_BOUND_PRE_AT_RISK=" + repr(generation.spend(BEAT).get("at_risk")))

# The retry, once the denial lifts. No billing uncertainty was created, so it
# must simply work rather than needing a human to abandon something.
state["deny"] = False
retry = run(d, sb, render_dir)
Path.unlink = real_unlink
print("PROBE_BOUND_PRE_RETRY=" + retry)
print("PROBE_BOUND_PRE_RETRY_PAID=%d" % calls["paid"])
print("PROBE_BOUND_PRE_STRANDED="
      + ("false" if calls["paid"] == 1 else "true"))
sweep()

# --- AFTER the provider: it was called and never answered ---------------------
d, sb, render_dir, calls = scene()

def timeout(ds_, synth, sb_, out_dir, log=print):
    calls["paid"] += 1
    raise RuntimeError("provider timeout after submit")

d.generate_paid_clip = timeout
run(d, sb, render_dir)
row = generation.for_shot(BEAT, SHOT)[0]
print("PROBE_BOUND_POST_PAID_CALLS=%d" % calls["paid"])
print("PROBE_BOUND_POST_STATUS=" + row.status)
print("PROBE_BOUND_POST_IN_DOUBT=" + ("true" if row.outcome_unknown else "false"))
print("PROBE_BOUND_POST_AT_RISK=" + repr(generation.spend(BEAT).get("at_risk")))

# And the retry must be refused, because the money may already have gone.
d.generate_paid_clip = timeout
again = run(d, sb, render_dir)
print("PROBE_BOUND_POST_RETRY=" + again)
print("PROBE_BOUND_POST_RETRY_PAID=%d" % calls["paid"])
print("PROBE_BOUND_POST_BOUGHT_TWICE="
      + ("true" if calls["paid"] > 1 else "false"))
sweep()
'''

# Which tier a beat lands in when the manifest does not say. This is the
# cheapest way in the codebase to spend money by accident: the tier IS the
# budget, and the two places that choose one on the caller's behalf are a
# dataclass default and a fallback in from_dict.
PROBE_TIER = _PRELUDE + '''
from backend.manifest import MotionType, Shot, Storyboard

sb = Storyboard.from_dict("p", {}, [
    {"scene_id": "s001"},                                  # no tier recorded
    {"scene_id": "s002", "motion_type": "claymation"},     # a tier nothing knows
    {"scene_id": "s003", "motion_type": "parallax"},
    {"scene_id": "s004", "motion_type": "ai_video"},
])
for s in sb.shots:
    print("PROBE_TIER_" + s.scene_id + "=" + s.motion_type.value
          + "/" + ("paid" if s.needs_paid_video() else "free"))

print("PROBE_TIER_UNTIERED_IS_PAID="
      + ("true" if sb.shots[0].needs_paid_video() else "false"))
print("PROBE_TIER_UNKNOWN_IS_PAID="
      + ("true" if sb.shots[1].needs_paid_video() else "false"))
print("PROBE_TIER_PAID_COUNT=%d" % len(sb.paid_shots()))
# A Shot built in code with no tier stated -- the same question one layer up.
print("PROBE_TIER_BARE_SHOT_IS_PAID="
      + ("true" if Shot(scene_id="s001").needs_paid_video() else "false"))
'''

PROBES = {
    "disposition": PROBE_DISPOSITION,
    "rebuy": PROBE_REBUY,
    "terminal": PROBE_TERMINAL,
    "boundary": PROBE_BOUNDARY,
    "tier": PROBE_TIER,
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
    # The third obligation, and the one this file originally declared and never
    # used. `expect` proves the right TEST died; these prove the right ASSERTION
    # inside it ran. A test that dies on `pytest.raises(...)` not raising, or on
    # a status check placed above the money check, reddens the table having
    # demonstrated nothing -- and three tests in this repo were doing exactly
    # that until mutation testing found them.
    #
    # `proves`      -- text that MUST appear in the mutated suite's output. An
    #                  assertion message where one exists, the assertion source
    #                  otherwise; pytest prints both for the failing assert.
    # `not_proves`  -- text that must NOT appear: the cheaper neighbouring
    #                  assertion, so a test red for the wrong reason is caught.
    # `proves_note` -- required when `proves` is empty. Says in writing why no
    #                  assertion can be pinned and what carries the evidence
    #                  instead. main() refuses to run if a mutation has neither,
    #                  so "the probe covers it" is a recorded decision rather
    #                  than an omission that reads like one.
    proves: list[str] = field(default_factory=list)
    not_proves: list[str] = field(default_factory=list)
    proves_note: str = ""


# --- anchors ------------------------------------------------------------------

BEGIN_DUPLICATE = (
    "        if idempotency_key:\n"
    "            dup = next((a for a in attempts\n"
    "                        if a.idempotency_key == idempotency_key), None)\n"
    "            if dup is not None:\n"
    '                return dup, "duplicate"'
)

BEGIN_REUSE = (
    "        if signature:\n"
    "            for a in mine:\n"
    "                if (a.status == SUCCEEDED and a.signature == signature and a.output\n"
    "                        and (exists is None or exists(a.output))):\n"
    '                    return a, "reused"'
)

BEGIN_IN_FLIGHT = (
    "        if signature:\n"
    "            stuck = next((a for a in mine\n"
    "                          if a.status == RUNNING and a.signature == signature), None)\n"
    "            if stuck is not None:\n"
    '                return stuck, "in_flight"'
)

TERMINAL_GUARD = "        if target.terminal:"
TERMINAL_FACTS = (
    "            if target.status == status and not differing:\n"
    "                return target"
)

MEDIA_PRESENT = (
    "        return bool(path) and path.is_file() and path.stat().st_size > 0"
)

DIR_IN_FLIGHT_REFUSAL = '                        if how == "in_flight":'
DIR_ONLY_CREATED_SPENDS = '                        if how != "created":'
DIR_PRE_DISPATCH = (
    "                            except BaseException as exc:\n"
    "                                generation.fail(plan.beat_id, att.id,\n"
    '                                                f"before dispatch: {exc}")\n'
    "                                raise"
)
DIR_POST_DISPATCH = (
    "                                generation.in_doubt(plan.beat_id, att.id, str(exc))"
)

SHOT_DEFAULT_TIER = "    motion_type: MotionType = MotionType.PARALLAX"
FROM_DICT_MISSING_TIER = '                raw_tier = shot.get("motion_type", "parallax")'
FROM_DICT_UNKNOWN_TIER = '                    fields["motion_type"] = MotionType.PARALLAX'


MUTATIONS = [
    # ---- begin(): the four dispositions ----------------------------------------
    Mutation(
        "DEFECT S4-01: an attempt still running for these inputs is not noticed",
        "§11.1",
        "the crash window itself — the provider may already have been paid and "
        "nothing here can tell, so begin() hands back \"created\" and the compile "
        "buys the clip a second time",
        [(GEN, BEGIN_IN_FLIGHT, "        if False:  # MUTANT: no in_flight guard\n            pass")],
        probes=[("disposition", "PROBE_DISP_RUNNING_SAME_INPUTS=created"),
                ("disposition", "PROBE_DISP_PERMITS_TO_SPEND=3"),
                ("rebuy", "PROBE_REBUY_STUCK_BOUGHT_TWICE=true"),
                ("rebuy", "PROBE_REBUY_STUCK_TOTAL_PAID=2"),
                ("boundary", "PROBE_BOUND_POST_BOUGHT_TWICE=true")],
        defect=True,
        expect=["test_a_crash_after_charging_does_not_buy_the_clip_again"],
        proves=["the crash window bought the clip a second time"],
        not_proves=["the replay was not refused"],
    ),
    Mutation(
        "the same request opens a second attempt", "§11.1",
        "the idempotency arm — two clicks, a UI retry or a restarted worker each "
        "become a separate purchase of the same shot",
        [(GEN, BEGIN_DUPLICATE, "        if False:  # MUTANT\n            pass")],
        probes=[("disposition", "PROBE_DISP_SAME_REQUEST=in_flight")],
        # NOT test_concurrent_duplicate_requests_open_exactly_one_attempt, and
        # the reason is a fact about the guards rather than about the test. Two
        # simultaneous identical requests both carry a signature, so with the
        # idempotency arm gone the SECOND one meets the in_flight arm instead --
        # the first attempt is still running with the same signature -- and no
        # second attempt is opened. That test therefore cannot distinguish the
        # two guards, which is exactly what
        # test_the_two_guards_are_not_the_same_guard is for, and that one does
        # die here. Measured, not assumed: the first run of this harness
        # declared the concurrency test and was corrected by the result.
        expect=["test_a_duplicate_request_does_not_open_a_second_attempt",
                "test_a_worker_restart_does_not_re_bill",
                "test_the_two_guards_are_not_the_same_guard"],
        proves=["a repeated request was allowed to spend again"],
    ),
    Mutation(
        "media already bought for these inputs is bought again", "§11.1",
        "the reuse arm — a genuinely new request for an unchanged shot stops "
        "finding what it already owns, which is the guard the idempotency key "
        "cannot provide because the key is different by construction",
        [(GEN, BEGIN_REUSE, "        if False:  # MUTANT\n            pass")],
        probes=[("disposition", "PROBE_DISP_BOUGHT_AND_PRESENT=created"),
                ("rebuy", "PROBE_REBUY_MARKER_BOUGHT_TWICE=true")],
        expect=["test_unchanged_inputs_reuse_the_media_already_bought",
                "test_a_lost_paid_marker_does_not_re_buy_the_clip",
                "test_the_captured_project_still_blocks_a_second_charge_after_the_switch"],
        proves=["the clip was bought twice after the marker was lost"],
    ),
    Mutation(
        "a recorded success whose media is gone is reused anyway", "§11.1",
        "the existence check inside the reuse arm — the shot goes on referencing "
        "media that is not there, which reads as a render bug long after the "
        "deletion and cannot be repaired by retrying",
        [(GEN, BEGIN_REUSE,
          "        if signature:  # MUTANT\n"
          "            for a in mine:\n"
          "                if (a.status == SUCCEEDED and a.signature == signature\n"
          "                        and a.output):\n"
          '                    return a, "reused"')],
        probes=[("disposition", "PROBE_DISP_BOUGHT_BUT_GONE=reused"),
                ("rebuy", "PROBE_REBUY_TRUNCATED_REPLACED=false")],
        expect=["test_a_recorded_success_whose_media_is_gone_is_not_reusable",
                "test_a_recorded_paid_clip_that_got_truncated_is_regenerated"],
        proves=['assert how == "created"'],
    ),
    Mutation(
        "a zero-byte download counts as the clip", "§11.1",
        "the size test — a truncated download leaves a file is_file() confirms, "
        "so the reuse check ships an empty clip while reporting a hit, and the "
        "legitimate re-buy is refused",
        [(DIRECTOR, MEDIA_PRESENT,
          "        return bool(path) and path.is_file()  # MUTANT")],
        probes=[("rebuy", "PROBE_REBUY_TRUNCATED_REPLACED=false"),
                ("rebuy", "PROBE_REBUY_TRUNCATED_TOTAL_PAID=1")],
        # One test, deliberately. test_a_zero_byte_download_is_not_mistaken_for_a
        # _paid_clip guards the OTHER size check -- the re-bill marker's, which
        # has always tested size -- and survives this. That the two checks are
        # separate is the point of _media_present's docstring: the attempt-level
        # one had to be made exactly as strict or it becomes the weaker of the
        # two and decides first.
        expect=["test_a_recorded_paid_clip_that_got_truncated_is_regenerated"],
        proves_note=(
            "the test asserts a re-render count with a bare assert and no "
            "message, and the count IS the money; PROBE_REBUY_TRUNCATED_REPLACED"
            "=false carries the same fact independently"),
    ),

    # ---- the caller's half: only "created" may spend ----------------------------
    Mutation(
        "the compile spends on any disposition", "§11.1",
        "the rule begin() exists to centralise — duplicate, reused and in_flight "
        "all fall through to the provider, so every guard above becomes advisory",
        [(DIRECTOR, DIR_ONLY_CREATED_SPENDS, "                        if False:  # MUTANT")],
        probes=[("rebuy", "PROBE_REBUY_MARKER_BOUGHT_TWICE=true"),
                ("rebuy", "PROBE_REBUY_MARKER_TOTAL_PAID=2")],
        # NOT test_an_unchanged_shot_is_still_never_re_billed: that one leaves
        # DirectorShot.paid_clip in place, and the older marker guard
        # short-circuits above this branch, so the disposition check never runs.
        # The ledger is the INDEPENDENT guard and only the tests that clear the
        # marker can see it -- which is the whole reason the ledger exists.
        expect=["test_a_lost_paid_marker_does_not_re_buy_the_clip",
                "test_the_captured_project_still_blocks_a_second_charge_after_the_switch"],
        proves=["the clip was bought twice after the marker was lost"],
    ),
    Mutation(
        "an attempt that may have been billed is not surfaced to a human",
        "§11.1",
        "the refusal that names the risk — the compile carries on past a stuck "
        "attempt without telling anyone the provider may already have been paid, "
        "so the one state that requires a human decision is silently absorbed",
        [(DIRECTOR, DIR_IN_FLIGHT_REFUSAL,
          "                        if False:  # MUTANT")],
        probes=[("rebuy", "PROBE_REBUY_STUCK_ERROR_NAMES_RUNNING=false")],
        expect=["test_a_crash_after_charging_does_not_buy_the_clip_again"],
        proves=["the replay was not refused"],
        not_proves=["the crash window bought the clip a second time"],
    ),

    # ---- the dispatch boundary (S4-R02) -----------------------------------------
    Mutation(
        "DEFECT S4-R02: a failure before dispatch is recorded as possibly billed",
        "§11.6",
        "the boundary — a permission error deleting a stale file records an "
        "attempt whose outcome is unknown, so the beat is stranded behind a "
        "recovery step that exists for a risk nobody took, and the at-risk "
        "figure reports money that could not have been charged",
        [(DIRECTOR, DIR_PRE_DISPATCH,
          "                            except BaseException as exc:  # MUTANT\n"
          "                                generation.in_doubt(plan.beat_id, att.id,\n"
          '                                                    f"before dispatch: {exc}")\n'
          "                                raise")],
        probes=[("boundary", "PROBE_BOUND_PRE_IN_DOUBT=true"),
                ("boundary", "PROBE_BOUND_PRE_STATUS=running"),
                ("boundary", "PROBE_BOUND_PRE_AT_RISK=0.6"),
                ("boundary", "PROBE_BOUND_PRE_STRANDED=true")],
        defect=True,
        expect=["test_a_failure_before_dispatch_is_an_ordinary_retryable_failure",
                "test_a_pre_dispatch_failure_does_not_strand_the_beat"],
        proves=["a pre-dispatch failure was left in doubt"],
        not_proves=["the provider was called"],
    ),
    Mutation(
        "DEFECT S4-01: a failure after dispatch is recorded as an ordinary failure",
        "§11.6",
        "the other side of the same boundary, and the more expensive one — once "
        "the provider has been called, \"failed\" invites a retry that buys the "
        "clip again, and the money the provider may have taken stops being "
        "reported at all",
        [(DIRECTOR, DIR_POST_DISPATCH,
          "                                generation.fail(plan.beat_id, att.id, str(exc))  # MUTANT")],
        probes=[("boundary", "PROBE_BOUND_POST_IN_DOUBT=false"),
                ("boundary", "PROBE_BOUND_POST_STATUS=failed"),
                ("boundary", "PROBE_BOUND_POST_AT_RISK=0.0"),
                ("boundary", "PROBE_BOUND_POST_BOUGHT_TWICE=true")],
        defect=True,
        expect=["test_a_failed_paid_generation_stays_attached_and_in_doubt"],
        proves=["an unknown provider outcome recorded as failed"],
    ),

    # ---- terminal facts (S4-R03) -------------------------------------------------
    Mutation(
        "DEFECT S4-R03: a terminal attempt is compared by status alone", "§11.6",
        "the fact comparison — succeed(first.mp4, $0.60) followed by "
        "succeed(other.mp4, $99) returns successfully and keeps the first, so a "
        "caller believes a cost was recorded that never was",
        [(GEN, TERMINAL_FACTS,
          "            if target.status == status:  # MUTANT\n"
          "                return target")],
        probes=[("terminal", "PROBE_TERM_DIFFERENT_OUTPUT=returned:succeeded"),
                ("terminal", "PROBE_TERM_DIFFERENT_COST=returned:succeeded")],
        defect=True,
        expect=["test_the_same_status_with_a_different_output_is_a_conflict",
                "test_the_same_status_with_a_different_cost_is_a_conflict",
                "test_a_failure_with_a_different_reason_is_a_conflict"],
        proves=["the conflicting cost was accepted"],
        not_proves=["a second completion rewrote the bill"],
    ),
    Mutation(
        "a finished attempt can be rewritten by a late callback", "§11.6",
        "terminality itself — a succeeded, paid attempt is rewritten as failed "
        "by a late callback, and the record of what actually happened is gone. "
        "Note what this mutation does NOT do any more: the reported bill stays "
        "at $0.60, because §6.1's billed() counts a recorded output or cost "
        "independently of status. The two fixes overlap, so the assertion pinned "
        "below is the stored record rather than the total",
        [(GEN, TERMINAL_GUARD, "        if False:  # MUTANT")],
        probes=[("terminal", "PROBE_TERM_REWRITTEN_AS_FAILED=returned:failed"),
                ("terminal", "PROBE_TERM_STATUS_AFTER_LATE_FAIL=failed")],
        defect=True,
        expect=["test_a_succeeded_attempt_cannot_be_rewritten_as_failed",
                "test_a_failed_attempt_cannot_be_rewritten_as_succeeded",
                "test_abandon_cannot_overwrite_a_recorded_failure"],
        proves=["the stored record changed"],
    ),

    # ---- which tier a beat lands in when nobody said ----------------------------
    #
    # The cheapest accidental spend in the codebase. Tier B is ~70% of shots and
    # costs nothing, which is exactly why it is the safe default: a beat with no
    # tier recorded must never default into the paid one. Two places choose on
    # the caller's behalf, and both are one token away from Tier C.
    Mutation(
        "a beat with no tier recorded defaults into the paid tier",
        "CLAUDE.md motion tiers",
        "the safe default in from_dict — every untiered beat in every manifest "
        "written by an older or partial writer becomes an ai_video beat, and the "
        "budget line the human approves is computed from that",
        [(MANIFEST, FROM_DICT_MISSING_TIER,
          '                raw_tier = shot.get("motion_type", "ai_video")  # MUTANT')],
        probes=[("tier", "PROBE_TIER_UNTIERED_IS_PAID=true"),
                ("tier", "PROBE_TIER_PAID_COUNT=2"),
                ("tier", "PROBE_TIER_s001=ai_video/paid")],
        expect=["test_missing_motion_type_defaults_to_parallax"],
        proves=["assert back.shots[0].needs_paid_video() is False"],
    ),
    Mutation(
        "a tier nothing recognises falls back into the paid tier",
        "CLAUDE.md motion tiers",
        "the fallback arm — and note that the log line one statement above says "
        "\"falling back to parallax\" as a LITERAL, so a fallback rewritten to "
        "Tier C keeps printing the right word while putting the beat in the paid "
        "tier. A test asserting the sentence cannot see this; only one asserting "
        "the tier can",
        [(MANIFEST, FROM_DICT_UNKNOWN_TIER,
          '                    fields["motion_type"] = MotionType.AI_VIDEO  # MUTANT')],
        probes=[("tier", "PROBE_TIER_UNKNOWN_IS_PAID=true"),
                ("tier", "PROBE_TIER_PAID_COUNT=2"),
                ("tier", "PROBE_TIER_s002=ai_video/paid")],
        expect=["test_an_unrecognised_motion_type_never_lands_in_the_paid_tier"],
        proves=["assert back.shots[0].needs_paid_video() is False"],
    ),
    Mutation(
        "a Shot built with no tier stated is a paid shot",
        "CLAUDE.md motion tiers",
        "the dataclass default itself — every Shot constructed in code without a "
        "tier, including the ones the script stage builds before anyone has "
        "chosen anything, starts life in Tier C",
        [(MANIFEST, SHOT_DEFAULT_TIER,
          "    motion_type: MotionType = MotionType.AI_VIDEO  # MUTANT")],
        probes=[("tier", "PROBE_TIER_BARE_SHOT_IS_PAID=true")],
        expect=["test_a_bare_shot_is_not_a_paid_shot"],
        proves=['assert Shot(scene_id="s001").needs_paid_video() is False'],
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
    """Whether a probe printed exactly this line. Equality, not ``in``:
    ``PROBE_REBUY_STUCK_TOTAL_PAID=1`` is a substring of ``...=12``."""
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


def _read(path: Path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _purge_pycache(path: Path) -> None:
    """Drop any cached bytecode for ``path``.

    Not defensive tidiness — this harness produced a wrong result without it,
    and the shape is worth stating because it is invisible.

    CPython invalidates a ``.pyc`` on **(mtime, size)** of the source, mtime at
    one-second resolution. Two of the mutations below rewrite ``manifest.py``
    with replacements that happen to be the same length (``parallax`` ->
    ``ai_video``, ``PARALLAX`` -> ``AI_VIDEO``, each plus the same ``# MUTANT``
    comment), so the mutated file is byte-for-byte the same SIZE both times.
    Under ``--discover`` the two writes land within the same second, and the
    second probe imported the FIRST mutation's bytecode: the run reported an
    unrecognised-tier mutation as having done nothing, in a probe that was in
    fact still executing the missing-tier one.

    The full runs are not exposed to it — a pytest run between writes is thirty
    seconds — which is exactly why it would have been found late and believed.
    """
    cache = path.parent / "__pycache__"
    if cache.is_dir():
        for pyc in cache.glob(f"{path.stem}.*.pyc"):
            try:
                pyc.unlink()
            except OSError:      # a concurrent import holding it; the mtime
                pass             # difference will do the job in that case


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


def _restore_guard() -> tuple[dict, dict]:
    touched = sorted({p for m in MUTATIONS for p, _, _ in m.edits})
    return {p: _read(p) for p in touched}, digest(touched)


def discover() -> int:
    """Apply each mutation, run only its probes, print everything, restore.

    No pytest. This is for choosing signatures: a signature declared from
    imagination and never printed is a harness fault that reports as a failed
    mutation, and the fastest way to avoid it is to look at the real output
    before writing it down.
    """
    pristine, before = _restore_guard()
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
                print(f"    (no PROBE_ output)\n{out[-1200:]}")
    reclaim_root_artifacts()
    after = digest(list(pristine))
    dirty = [p.name for p in pristine if before[p] != after[p]]
    for p in pristine:
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

    # The reachability obligation, enforced rather than remembered. A mutation
    # must either name the assertion that has to fire, or say in writing why it
    # cannot and what carries the evidence instead. Refusing to run is what
    # stops "the probe covers this" from being a thing nobody ever wrote down --
    # which is how the field sat declared and unused here in the first place.
    silent = [m.name for m in MUTATIONS if not m.proves and not m.proves_note]
    if silent:
        print("mutations with neither `proves` nor a written `proves_note`:")
        for n in silent:
            print(f"  - {n}")
        return 1

    # The repository root is itself a project and a mutated run really can file a
    # generation ledger into it. Refuse to start on an occupied path rather than
    # clearing it: a generation ledger is the record of what was paid, and
    # deleting one to make room for a test artifact is not a decision a harness
    # may make unattended. This check is also what makes reclaim_root_artifacts()
    # safe -- ownership, not existence.
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
                    f"{mut.name}: no failing assertion said {phrase!r} — the test "
                    f"died on a cheaper assertion first, so the defect was never "
                    f"demonstrated")
        for phrase in mut.not_proves:
            if phrase in failed_asserts:
                print(f"    assertion UNEXPECTEDLY FIRED: {phrase!r}")
                unproven.append(
                    f"{mut.name}: {phrase!r} also failed — this mutation is not "
                    f"supposed to reach that assertion, so the test may be red "
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

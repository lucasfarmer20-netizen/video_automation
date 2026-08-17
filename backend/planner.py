"""Director Planner and Coverage Critic — planning only, never generation.

Turns a *scene* (contiguous beats with measured durations) into per-beat
``DirectorShot`` coverage, then criticises the result. Nothing here generates
media, spends money, or writes ``storyboard_manifest.json``. Plans land in
``director/<beat_id>.json`` with ``status="draft"`` and stay there until a human
locks them.

Three decisions shape this module:

**Scene-level, not beat-level.** Coverage problems are cross-beat by nature —
"three consecutive close-ups", "the protagonist is in 76% of this scene", "no
establishing shot before we are inside the tunnel" cannot be seen from inside one
beat. Planning per scene also cuts LLM calls by roughly the number of beats in it.

**The planner never names a model.** It describes what a shot needs — how long,
whether a character moves, how complex the motion is, whether a gesture must
complete — and ``capabilities.resolve`` maps that onto a backend. An LLM choosing
fal endpoints is how ``kling_2_5_turbo_pro``, a key in no registry, came to render
silently on Kling 2.1 Standard.

**Arithmetic is not delegated.** Coverage must sum to the beat's measured duration
exactly or the compile refuses it, and language models are unreliable at making a
list of decimals hit a target. The model proposes relative weights; ``_fit_to_beat``
scales them to land on the number. Asking for exactness and hoping is how you get
a plan that fails validation after the human has already reviewed it.
"""

from __future__ import annotations

import json
import os
from typing import Any

import anthropic

from . import capabilities, config, director
from .director import CoveragePlan, DirectorShot
from .manifest import Camera, Shot, Storyboard

MODEL = os.environ.get("DIRECTOR_MODEL", "claude-opus-5")
MAX_TOKENS = 16000
# The critic returns less than the planner but reasons over the whole scene, and
# on this model thinking is billed against the same ceiling. 4000 was not enough
# for 23 shots: the budget was gone before a single JSON token was emitted.
CRITIC_MAX_TOKENS = 12000
STREAM_TIMEOUT = float(os.environ.get("DIRECTOR_TIMEOUT_SECONDS", "600"))

SHOT_SIZES = ["ews", "ws", "mw", "m", "mcu", "cu", "ecu"]
ANGLES = ["front", "profile", "three_quarter", "rear_three_quarter", "ots",
          "high", "low", "overhead"]
PURPOSES = ["establishing", "master", "reaction", "insert", "cutaway", "detail",
            "transition"]
MOVES = ["static", "push_in", "pull_out", "pan_left", "pan_right", "tilt_up", "tilt_down"]
MOTION_TYPES = ["static", "parallax", "ai_video"]

# --- director profiles ---------------------------------------------------------
#
# Data, not branching code — the same pattern that keeps the backend registries
# from drifting. A profile expresses what Lucas wants; it never expresses what a
# model can do (that is capabilities.py) and never encodes identity reliability
# (that is measured, and lives in the ledger).

DIRECTOR_PROFILES: dict[str, dict] = {
    "documentary_illustrated": {
        "label": "Illustrated documentary (Bestiary default)",
        "shot_seconds": [6.0, 14.0],
        "camera_motion": "restrained",
        "environmental_coverage": "medium",
        "cutaway_density": "low",
        "face_exposure": "low",
        "max_ai_video_per_scene": 1,
        "note": "Long illustrated holds. Coverage is optional and often unwanted.",
    },
    "historical_docudrama": {
        "label": "Historical docudrama (Calluses)",
        "shot_seconds": [2.5, 5.5],
        "camera_motion": "restrained",
        "environmental_coverage": "high",
        "cutaway_density": "high",
        "face_exposure": "moderate",
        "max_ai_video_per_scene": 2,
        "note": "Observational. Hands, tools, environment and process over faces.",
    },
    "cinematic_documentary": {
        "label": "Cinematic documentary",
        "shot_seconds": [3.0, 7.0],
        "camera_motion": "moderate",
        "environmental_coverage": "high",
        "cutaway_density": "medium",
        "face_exposure": "moderate",
        "max_ai_video_per_scene": 3,
        "note": "Composed and deliberate; more camera movement than docudrama.",
    },
}

DEFAULT_PROFILE = "historical_docudrama"


# --- schemas -------------------------------------------------------------------
#
# Enum'd properties carry plain "string" types. A union type in an enum'd property
# ("type": ["string","null"]) is rejected outright by the structured-output
# validator -- that 400 cost a whole draft run once.

def _shot_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "purpose": {"type": "string", "enum": PURPOSES},
            "subject": {"type": "string",
                        "description": "What is on screen, in a few words."},
            "shot_size": {"type": "string", "enum": SHOT_SIZES},
            "angle": {"type": "string", "enum": ANGLES},
            "composition": {"type": "string",
                            "description": "How it is framed. One short clause."},
            "weight": {"type": "number",
                       "description": "Relative screen time, 1-10. Not seconds; "
                                      "the exact durations are computed."},
            "camera_move": {"type": "string", "enum": MOVES},
            "motion_type": {"type": "string", "enum": MOTION_TYPES},
            "character_motion": {"type": "boolean"},
            "face_visibility": {"type": "string",
                                "enum": ["none", "low", "moderate", "high"]},
            "motion_complexity": {"type": "string",
                                  "enum": ["none", "low", "medium", "high"]},
            "gestural": {"type": "boolean",
                         "description": "A described movement that must complete. "
                                        "Such a shot must never be trimmed."},
            "identity_critical": {"type": "boolean",
                                  "description": "A recognisable face anchor."},
            "prompt": {"type": "string",
                       "description": "The still image prompt. Scene only; never "
                                      "restate the medium and never mention camera "
                                      "movement or timing."},
            "motion_prompt": {"type": "string",
                              "description": "What moves, if anything. Empty for stills."},
            "reason": {"type": "string",
                       "description": "One sentence: why this shot earns its place."},
        },
        "required": ["purpose", "subject", "shot_size", "angle", "composition",
                     "weight", "camera_move", "motion_type", "character_motion",
                     "face_visibility", "motion_complexity", "gestural",
                     "identity_critical", "prompt", "motion_prompt", "reason"],
    }


PLAN_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "visual_strategy": {"type": "string",
                            "description": "Two sentences on how this scene is covered."},
        "blocking": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "environment": {"type": "string"},
                "characters": {"type": "array", "items": {"type": "string"}},
                "props": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["environment", "characters", "props"],
        },
        "beats": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "beat_id": {"type": "string"},
                    "intent": {"type": "string",
                               "description": "One sentence on what this beat must accomplish."},
                    "shots": {"type": "array", "items": _shot_schema()},
                },
                "required": ["beat_id", "intent", "shots"],
            },
        },
    },
    "required": ["visual_strategy", "blocking", "beats"],
}

CRITIC_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "warnings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "beat_id": {"type": "string",
                                "description": "Empty string when it applies to the scene."},
                    "shot_id": {"type": "string", "description": "Empty when not shot-specific."},
                    "kind": {"type": "string", "enum": [
                        "missing_establishing", "repeated_framing", "insufficient_cutaway",
                        "no_reaction_coverage", "impractical_duration", "identity_risk",
                        "unnecessarily_expensive", "timing_mismatch", "missing_reference",
                        "continuity", "other"]},
                    "detail": {"type": "string",
                               "description": "One concrete sentence. Name the shots."},
                    "suggestion": {"type": "string",
                                   "description": "The smallest change that fixes it."},
                },
                "required": ["beat_id", "shot_id", "kind", "detail", "suggestion"],
            },
        },
    },
    "required": ["warnings"],
}


SYSTEM = """\
You are a documentary director planning shot coverage for an existing, locked \
narration. You are not writing the film — the script, the beats and their exact \
durations are fixed and measured from recorded voiceover. Your job is to decide \
what the audience SEES while each beat is spoken.

A "beat" is one narration segment, commonly 15-40 seconds. That is far too long \
for a single image in a cinematic cut, so you break it into several Director \
Shots that add up to the beat.

HARD RULES

1. Coverage for a beat must account for its whole duration. Express each shot's \
share as "weight" (relative screen time, 1-10). Exact seconds are computed from \
your weights — do not attempt the arithmetic.
2. "motion_type" is a budget decision:
   - "static": a still with a subtle push or hold. Free.
   - "parallax": a 2.5D depth move on a still. Free. This should be most shots.
   - "ai_video": generated video, 3-10 seconds. This is a BUDGET TO ALLOCATE, not \
a prohibition. The brief gives you an allowance for the scene; your job is to decide \
which moments deserve it, not to avoid spending it. Give it to the moments where \
something physically HAPPENS on screen — a sledge striking, a wheel turning, water \
falling, smoke rolling, a figure moving through frame. Never for a landscape, a \
document, a map or an object at rest, and never merely because a shot matters. \
If the scene contains such a moment and you have allowance left, USE IT: a scene \
about physical work covered entirely in stills is a failure of the plan, not a \
saving, because the audience is told about an action and shown a photograph of it.
3. Generated video runs 3-10 seconds. An "ai_video" shot must be within that. If \
a motion moment needs longer, split it or carry it with parallax.
4. Set "gestural" true when the motion is a specific movement that must complete \
(a hand reaching, a head turning, a bucket rising). Such a shot is never trimmed, \
which constrains which models can serve it.
5. "prompt" describes only the still: subject, framing, light. Never restate the \
artistic medium — that is applied per episode — and never mention camera movement \
or timing, which live in camera_move and weight.
6. Respect the palette. If the episode's medium is monochrome or limited, do not \
introduce colours it cannot physically produce; carry the image on value, \
contrast and texture.

SYNCHRONY WITH THE NARRATION

The shots play in order over the beat's voiceover, so shot N lands on whatever is being said at that point. Read the narration as it will be spoken, estimate where each phrase falls, and order and weight your shots so each one arrives on the line it illustrates. An insert of hands turning a drill rod that plays four seconds after the line about turning the rod is a miss, not a detail. Cut on meaning: a new shot should arrive because the narration turned, not on a timer.

Vary the weights deliberately. Within a beat the longest shot should be roughly twice the shortest — a master or an establishing shot holds, an insert is quick. Weights that are all the same produce cuts on a fixed interval, which reads as a slideshow rather than an edit; if every shot in a beat carries the same weight, you have not decided what the beat is about.

FACES

Each entry in "characters" carries max_face_visibility. Do not exceed it.

A character with has_likeness_reference true can be generated as the same recognisable person across shots, so close-ups and face-forward coverage of them are legitimate. A character without one cannot: repeated shots produce different people, which reads as a continuity error rather than a style. For those, keep face_visibility at "low" or "none" and cover them observationally — from behind, in profile, half-lit, at a distance, or through their hands, tools and actions. That is not a compromise; it is how most documentary footage of working people is shot anyway.

Set identity_critical true only for the one or two shots per scene that must establish who someone is, and only for a character who has a likeness reference.

MOTION IS NOT FREE BY ACCIDENT

"parallax" moves a still by warping it under a camera move. A parallax shot with camera_move "static" therefore does not move at all — it is a held frame with extra steps. Give a shot a camera move unless stillness is genuinely the point, and remember that a slow push or a lateral drift over a depth-separated image is the cheapest motion available.

Neither "static" nor "parallax" can show an ACTION. A sledge striking a drill, a wheel turning, water falling, a hand completing a gesture — none of those happen on a still, however it is warped. If the narration turns on something physically occurring and the shot exists to show that occurrence, it needs "ai_video". That is what the paid tier is for, and a 3-5 second shot of it is far better value than a long one.

CRAFT

- Open a scene by establishing where we are before going close.
- Vary shot size. Three consecutive close-ups is a fault, not a style.
- Prefer the concrete and physical: hands, tools, surfaces, process, wear, weather.
- Inserts and cutaways are cheap, specific, and carry exposition better than a \
wide shot held for twenty seconds.
- Cut on meaning. A new shot should arrive because the narration turned, not on a \
timer.
- Restraint. This is observational documentary, not drama."""


def _text_of(response, what: str) -> str:
    """Pull the JSON text block out of a response, or say why there isn't one.

    ``next(b.text for b in response.content if b.type == "text")`` raises a bare
    StopIteration when the model produced no text block, which surfaced as an
    unexplained traceback with nothing about the cause. On Opus 5 the usual cause
    is the token ceiling: thinking is on by default and billed against the same
    max_tokens, so a budget that looks generous for the output alone can be spent
    before any text is emitted.
    """
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text:
        return text
    reason = getattr(response, "stop_reason", "unknown")
    if reason == "max_tokens":
        raise RuntimeError(
            f"The {what} hit its token ceiling before returning any JSON. Thinking "
            f"is billed against the same budget on this model — raise max_tokens in "
            f"backend/planner.py, or send fewer beats per scene."
        )
    if reason == "refusal":
        raise RuntimeError(f"The model declined the {what} request.")
    raise RuntimeError(f"The {what} returned no text block (stop_reason={reason}).")


def _client() -> anthropic.Anthropic:
    """Reuse the script stage's resolver rather than keeping a second copy.

    The deployment supplies the key as CLAUDE_API_KEY, not ANTHROPIC_API_KEY, and
    a duplicated fallback chain is exactly the kind of thing that agrees today and
    diverges the next time one of them is edited.
    """
    from .script import _client as script_client
    return script_client()


def profile(name: str | None) -> dict:
    p = dict(DIRECTOR_PROFILES.get(name or DEFAULT_PROFILE)
             or DIRECTOR_PROFILES[DEFAULT_PROFILE])
    p["key"] = name if name in DIRECTOR_PROFILES else DEFAULT_PROFILE
    return p


# Shorter than this is a flash frame, not a shot.
MIN_SHOT_SECONDS = 1.2


def _fit_to_beat(shots: list[dict], seconds: float) -> list[float]:
    """Turn relative weights into durations that sum to ``seconds`` exactly.

    The model supplies proportions; this makes them land on the number, because
    ``director.validate`` rejects a plan that misses the total by 0.01s and no
    language model reliably makes a list of decimals hit a target.

    Two things make this less trivial than it looks. A minimum shot length can
    push the total *above* the beat when the planner asks for many shots in a
    short one — ten shots in six seconds cannot all be 1.2s — so the list is first
    truncated to what the beat can physically hold. And the remainder is then
    absorbed across shots that have slack rather than dumped on one, which in an
    earlier version drove a single shot to **-4.8 seconds**.

    May return fewer durations than shots. The caller zips them, so the excess is
    dropped; it must say so rather than let shots vanish quietly.
    """
    if not shots or seconds <= 0:
        return []

    # A beat shorter than one shot still gets exactly one shot, its own length.
    floor = min(MIN_SHOT_SECONDS, seconds)
    capacity = max(1, int(seconds // floor))
    usable = shots[:capacity]

    weights = [max(0.1, float(s.get("weight") or 1.0)) for s in usable]
    total = sum(weights) or 1.0
    durs = [max(floor, round(seconds * w / total, 2)) for w in weights]

    # Absorb the remainder where there is room, largest shot first, never taking
    # any shot below the floor.
    drift = round(seconds - sum(durs), 2)
    for i in sorted(range(len(durs)), key=lambda k: -durs[k]):
        if abs(drift) < 0.005:
            break
        room = float("inf") if drift > 0 else -(durs[i] - floor)
        take = drift if drift > 0 else max(drift, room)
        durs[i] = round(durs[i] + take, 2)
        drift = round(drift - take, 2)

    return durs



def _snap_paid_durations(shots: list, durs: list[float]) -> list[float]:
    """Move paid shots onto a duration some model can actually generate.

    Generated video comes in fixed lengths -- 4s, 5s, "4s"/"6s"/"8s" depending on
    the endpoint -- while editorial durations are whatever the weights produced.
    A 3.34s paid shot is not merely awkward, it is unservable: the shortest legal
    duration anywhere is 4s, and a gestural shot cannot be trimmed back down.

    So the paid shot is snapped to the nearest legal length and the difference is
    absorbed by the FREE shots around it, which can be any duration at all. The
    beat total is preserved, which is what compilation requires.

    This is Round 4's option 3, deferred until the router existed. Without it the
    planner emits plans that only fail at generation time, after the human has
    already reviewed and locked them.
    """
    from . import capabilities

    out = list(durs)
    free = [i for i, s in enumerate(shots[:len(out)])
            if (s.get("motion_type") or "parallax") != "ai_video"]
    if not free:
        return out

    for i, s in enumerate(shots[:len(out)]):
        if (s.get("motion_type") or "parallax") != "ai_video":
            continue
        want = out[i]
        best, best_gap = None, None
        for key in capabilities.VIDEO_CAPS:
            picked, _ = capabilities.legal_durations(key, want)
            if picked is None:
                continue
            gap = abs(picked - want)
            if best is None or gap < best_gap:
                best, best_gap = float(picked), gap
        if best is None or abs(best - want) < 0.01:
            continue
        delta = best - want                      # usually positive: models round up
        # Take it off the free shots, largest first, never below the floor.
        room = sum(max(0.0, out[j] - MIN_SHOT_SECONDS) for j in free)
        if delta > room:
            continue                             # cannot pay for it; leave as planned
        out[i] = round(best, 2)
        remaining = delta
        for j in sorted(free, key=lambda k: -out[k]):
            if remaining <= 0.001:
                break
            take = min(remaining, out[j] - MIN_SHOT_SECONDS)
            out[j] = round(out[j] - take, 2)
            remaining = round(remaining - take, 2)
    return out

def _beat_context(sb: Storyboard, beat_ids: list[str]) -> list[dict]:
    out = []
    for bid in beat_ids:
        s = next((x for x in sb.shots if x.scene_id == bid), None)
        if not s:
            continue
        out.append({
            "beat_id": s.scene_id,
            "duration_seconds": round(float(s.camera.duration), 2),
            "narration": s.narration,
            "existing_visual": s.prompt,
            "style_medium": s.style_medium,
        })
    return out


def plan_scene(sb: Storyboard, beat_ids: list[str], profile_key: str | None = None,
               notes: str = "", log=print, replan: bool = False) -> dict:
    """Plan coverage for contiguous beats. Returns plans; writes them as drafts.

    Beats whose coverage is already locked or compiled are planned AROUND, not
    over: they stay in the brief so the scene reads continuously, but their plan
    on disk is left alone.

    That guard is not defensive tidiness. Saving a fresh draft over a compiled
    plan discards the record of coverage that has already been paid for and
    rendered, and -- because ``has_locked_coverage`` recognises only
    locked/compiling/compiled -- it simultaneously drops the beat's protection
    from the render loop, which would then render straight over the compiled
    clip. One planning call on a scene the survey recommends would have thrown
    away s012's $3.25 twice over.

    Pass ``replan=True`` to deliberately re-plan a locked beat.
    """
    prof = profile(profile_key)
    beats = _beat_context(sb, beat_ids)
    if not beats:
        raise ValueError(f"no such beats: {beat_ids}")

    protected: set[str] = set()
    if not replan:
        protected = {b for b in beat_ids if director.has_locked_coverage(b)}
        if protected and set(beat_ids) <= protected:
            raise ValueError(
                f"every requested beat already has locked coverage "
                f"({', '.join(sorted(protected))}). Pass replan=true to plan over it."
            )
        for b in sorted(protected):
            log(f"  {b}: coverage already locked — planning around it, not over it.")

    anchors, castable = {}, {}
    try:
        from .assets import _load_character_anchors
        from . import characters as _chars
        anchors = _load_character_anchors()
        for name, spec in (_chars.load_characters() or {}).items():
            ref = (spec or {}).get("reference_image") or ""
            castable[name] = bool(ref and config.resolve_media(ref))
    except Exception:  # noqa: BLE001
        pass

    brief = {
        "episode_title": sb.title,
        "channel": sb.channel,
        "cultural_origin": getattr(sb, "cultural_origin", ""),
        "style_medium": beats[0].get("style_medium", ""),
        "director_profile": {k: v for k, v in prof.items() if k != "note"},
        "profile_note": prof.get("note", ""),
        # Stated as an allowance to spend rather than left implicit in the profile
        # dict. The first two runs returned zero paid shots for a scene about
        # sledgehammers and rock, because "PAID and strictly limited" read as a
        # prohibition and nothing said how many were actually available.
        "ai_video_allowance_for_this_scene": prof.get("max_ai_video_per_scene", 0),
        # Which characters can carry a recognisable face, and which cannot.
        # Measured, not assumed: Spike A showed a likeness reference holds identity
        # across takes and text anchors alone do not.
        "characters": [
            {"name": n,
             "has_likeness_reference": bool(castable.get(n)),
             "max_face_visibility": "high" if castable.get(n) else "low"}
            for n in sorted(set(anchors) | set(castable))
        ] or [{"name": "(none defined)", "has_likeness_reference": False,
               "max_face_visibility": "none"}],
        "human_notes": notes,
        "beats": beats,
    }

    log(f"Planning coverage for {len(beats)} beat(s) as one scene "
        f"({prof['label']}) ...")
    user = (
        "Plan the shot coverage for this scene. Return one entry per beat, in "
        "order, with the shots that fill it.\n\n"
        + json.dumps(brief, indent=2, ensure_ascii=False)
    )

    with _client().messages.stream(
        model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": "high",
                       "format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
        timeout=STREAM_TIMEOUT,
    ) as stream:
        for _ in stream.text_stream:
            pass
        response = stream.get_final_message()

    if response.stop_reason == "max_tokens":
        raise RuntimeError(
            f"Coverage plan hit the {MAX_TOKENS}-token ceiling. Plan fewer beats "
            f"per scene, or raise MAX_TOKENS in backend/planner.py."
        )
    raw = json.loads(_text_of(response, "coverage plan"))

    plans: dict[str, CoveragePlan] = {}
    for entry in raw.get("beats") or []:
        bid = entry.get("beat_id")
        beat = next((x for x in sb.shots if x.scene_id == bid), None)
        if beat is None:
            log(f"  !! planner returned an unknown beat {bid!r} — skipped")
            continue
        if bid in protected:
            continue
        seconds = float(beat.camera.duration)
        shots = entry.get("shots") or []
        durs = _fit_to_beat(shots, seconds)
        # Paid shots must land on a length a model can generate, or the plan
        # cannot compile no matter how good it reads.
        durs = _snap_paid_durations(shots, durs)
        if len(durs) < len(shots):
            log(f"  {bid}: asked for {len(shots)} shots in {seconds:.1f}s — only "
                f"{len(durs)} fit above {MIN_SHOT_SECONDS}s; the rest were dropped")

        coverage: list[DirectorShot] = []
        for i, (s, d) in enumerate(zip(shots, durs), start=1):
            mt = s.get("motion_type") or "parallax"
            intent = {
                "duration": d,
                "gestural": bool(s.get("gestural")),
                "character_motion": bool(s.get("character_motion")),
                "motion_complexity": s.get("motion_complexity") or "low",
            }
            constraints: list[str] = []
            backend = ""
            if mt == "ai_video":
                r = capabilities.resolve(intent)
                backend = r.get("backend") or ""
                constraints = list(r.get("constraints") or [])
                if not backend:
                    # No legal model. Say so on the shot and drop it to a free
                    # tier rather than emitting a plan that cannot be produced.
                    constraints.append("downgraded_no_legal_model")
                    mt = "parallax"
            shot = DirectorShot(
                id=f"{bid}.{i:02d}", beat_id=bid,
                purpose=s.get("purpose", ""), subject=s.get("subject", ""),
                shot_size=s.get("shot_size", ""), angle=s.get("angle", ""),
                composition=s.get("composition", ""),
                camera=Camera(move=s.get("camera_move") or "static", duration=d),
                character_motion=bool(s.get("character_motion")),
                face_visibility=s.get("face_visibility") or "none",
                motion_complexity=s.get("motion_complexity") or "low",
                gestural=bool(s.get("gestural")),
                identity_critical=bool(s.get("identity_critical")),
                motion_type=mt, backend=backend,
                prompt=s.get("prompt", ""), motion_prompt=s.get("motion_prompt", ""),
                reason=s.get("reason", ""),
                constrained_by=constraints,
            )
            # Priced only once the shot exists, and by the same function the
            # compile pays through. Computing it inline here meant the quote knew
            # nothing about identity_critical, which buys four stills rather than
            # one -- so those shots were quoted a quarter of their still cost.
            shot.estimated_cost = director.quote_shot(shot)
            coverage.append(shot)

        plan = CoveragePlan(
            beat_id=bid, beat_duration=seconds, plan_id=raw_plan_id(beat_ids),
            scene_beats=list(beat_ids), status="draft", profile=prof["key"],
            created_by="planner", coverage=coverage,
            visual_strategy=raw.get("visual_strategy", ""),
            blocking=raw.get("blocking", {}) or {},
        )
        # Catch our own arithmetic before a human ever sees the plan.
        try:
            director.validate(plan, beat)
        except director.PlanError as exc:
            plan.warnings.append(f"internal: {exc}")
        director.save_plan(plan)
        # What the planner proposed, before anyone judges it.
        try:
            from . import ledger
            ledger.record_plan(beat_id=bid, plan_id=plan.plan_id,
                               profile=prof["key"], shots=coverage)
        except Exception as exc:  # noqa: BLE001 — telemetry must not fail a plan
            log(f"  (could not record plan to the ledger: {exc})")
        plans[bid] = plan
        log(f"  {bid}: {len(coverage)} shots, "
            f"{sum(1 for c in coverage if c.motion_type == 'ai_video')} paid, "
            f"est ${sum(c.estimated_cost for c in coverage):.2f}")

    return {
        "visual_strategy": raw.get("visual_strategy", ""),
        "blocking": raw.get("blocking", {}),
        "plans": plans,
        "skipped": sorted(protected),
        "profile": prof,
    }


def raw_plan_id(beat_ids: list[str]) -> str:
    return f"scene-{beat_ids[0]}-{beat_ids[-1]}" if beat_ids else "scene"



def survey(sb, profile_key: str | None = None) -> dict:
    """Which beats would benefit from coverage, and by how much.

    The planner covers whatever beats it is handed; nothing was answering the
    question that comes first -- WHICH beats are worth covering. That is the
    decision that follows narration, and it is arithmetic rather than judgement,
    so it is computed here instead of being asked of a model: a Tier-C beat longer
    than a generated clip will freeze for the remainder, and how many seconds that
    is can simply be worked out.

    Deliberately not an LLM call. It is free, instant, and the numbers are
    checkable -- and a model asked to rank 25 beats would produce a plausible
    ordering nobody could verify.
    """
    prof = profile(profile_key)
    lo, hi = prof["shot_seconds"]
    rows = []
    for shot in getattr(sb, "shots", []):
        dur = float(shot.camera.duration) if shot.camera else 0.0
        mt = getattr(shot.motion_type, "value", str(shot.motion_type or "parallax"))
        # A paid beat can only generate ~10s; the rest is a held frame.
        frozen = max(0.0, dur - 10.0) if mt == "ai_video" else 0.0
        # A beat far longer than the profile's shot length is one image held for
        # several shots' worth of screen time.
        shots_worth = dur / max(1.0, (lo + hi) / 2)

        if frozen >= 5.0:
            score, why = 3, (f"{frozen:.0f}s of this paid beat would be a frozen "
                             f"frame ({frozen/dur*100:.0f}% of it)")
        elif shots_worth >= 5:
            score, why = 2, (f"{dur:.0f}s on one image — about {shots_worth:.0f} "
                             f"shots' worth of screen time")
        elif shots_worth >= 3:
            score, why = 1, f"{dur:.0f}s on one image"
        else:
            score, why = 0, "short enough to hold as a single image"

        rows.append({
            "beat_id": shot.scene_id, "seconds": round(dur, 2), "motion_type": mt,
            "frozen_if_left": round(frozen, 1),
            "recommend": score, "reason": why,
            "narration": (shot.narration or "")[:110],
        })

    total = sum(r["seconds"] for r in rows) or 1.0
    frozen_total = sum(r["frozen_if_left"] for r in rows)
    strong = [r["beat_id"] for r in rows if r["recommend"] == 3]
    return {
        "beats": rows,
        "episode_seconds": round(total, 1),
        "frozen_if_nothing_covered": round(frozen_total, 1),
        "frozen_pct": round(frozen_total / total * 100),
        "recommended": strong,
        "scenes": _contiguous(strong),
        "note": ("Beats scored 3 would be mostly frozen frame if rendered normally. "
                 "Coverage there costs about the same and removes the freeze. "
                 "Score 0 beats are fine as single images."),
    }


def _contiguous(ids: list[str]) -> list[list[str]]:
    """Group recommended beats into scenes, since planning is scene-level."""
    def num(b):
        digits = "".join(c for c in b if c.isdigit())
        return int(digits) if digits else -1
    out, run = [], []
    for b in sorted(ids, key=num):
        if run and num(b) == num(run[-1]) + 1:
            run.append(b)
        else:
            if run:
                out.append(run)
            run = [b]
    if run:
        out.append(run)
    return out

def critique(sb: Storyboard, beat_ids: list[str], log=print) -> list[dict]:
    """Independent review of the coverage, without the planner's rationale.

    The shot list is presented stripped of ``reason`` and of the scene's stated
    visual strategy. A critic shown the planner's justification tends to agree
    with it — the same model, the same context, and a persuasive argument already
    in front of it. Asking "what is missing here?" of a bare shot list is a
    genuinely different question from "is this plan good?".
    """
    rows = []
    for bid in beat_ids:
        plan = director.load_plan(bid)
        beat = next((x for x in sb.shots if x.scene_id == bid), None)
        if not plan or beat is None:
            continue
        rows.append({
            "beat_id": bid,
            "beat_seconds": round(float(beat.camera.duration), 2),
            "narration": beat.narration,
            "shots": [{
                "shot_id": s.id, "purpose": s.purpose, "subject": s.subject,
                "shot_size": s.shot_size, "angle": s.angle,
                "seconds": s.duration, "camera_move": s.camera.move,
                "motion_type": s.motion_type,
                "face_visibility": s.face_visibility,
                "estimated_cost": s.estimated_cost,
            } for s in plan.coverage],
        })
    if not rows:
        return []

    log(f"Critiquing {sum(len(r['shots']) for r in rows)} shots across "
        f"{len(rows)} beat(s) ...")
    user = (
        "Here is a planned shot list for one scene, with the narration each beat "
        "carries. You did not write it and there is no rationale attached.\n\n"
        "Report only concrete, checkable problems. Do not score it, do not "
        "praise it, and return an empty list if it is sound.\n\n"
        + json.dumps(rows, indent=2, ensure_ascii=False)
    )
    with _client().messages.stream(
        model=MODEL, max_tokens=CRITIC_MAX_TOKENS,
        system=("You are a film editor checking whether a scene can actually be "
                "cut from the coverage provided. You care about whether the "
                "footage works, not whether it is ambitious."),
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": CRITIC_SCHEMA}},
        timeout=STREAM_TIMEOUT,
    ) as stream:
        for _ in stream.text_stream:
            pass
        response = stream.get_final_message()

    warnings = json.loads(_text_of(response, "coverage critique")).get("warnings") or []

    # Deterministic checks the model should not be trusted to do: arithmetic and
    # budget. These are facts, not opinions, and belong in code.
    for r in rows:
        plan = director.load_plan(r["beat_id"])
        if not plan:
            continue
        tot = round(plan.total_duration(), 2)
        if abs(tot - r["beat_seconds"]) > director.DURATION_TOLERANCE:
            warnings.append({
                "beat_id": r["beat_id"], "shot_id": "", "kind": "timing_mismatch",
                "detail": f"Coverage totals {tot:.2f}s against a {r['beat_seconds']:.2f}s beat.",
                "suggestion": "Re-plan this beat; it cannot compile as it stands.",
            })
        for s in plan.coverage:
            if s.motion_type != "ai_video":
                continue
            # Ask the router, rather than testing the editorial length against a
            # fixed 3-10s window. A 2.72s shot is perfectly producible — the
            # router generates the model's 3s minimum and trims back, which is
            # safe for ambient motion — so flagging it as impractical was wrong
            # and appeared on the planner's very first real run. What actually
            # matters is whether ANY configured model can serve the shot; that
            # also covers hand-authored plans, which never went through a router.
            served = capabilities.resolve(
                {"duration": s.duration, "gestural": s.gestural},
                prefer=[s.backend] if s.backend else None)
            if not served.get("backend"):
                warnings.append({
                    "beat_id": r["beat_id"], "shot_id": s.id,
                    "kind": "impractical_duration",
                    "detail": f"{s.id} is a paid shot of {s.duration:.2f}s and no "
                              f"configured model can produce it"
                              + (" without trimming a gesture" if s.gestural else "")
                              + ".",
                    "suggestion": "Shorten it, split it, or make it parallax.",
                })
    for w in warnings:
        log(f"  [{w.get('kind')}] {w.get('beat_id') or 'scene'}: {w.get('detail')}")
    return warnings


def scene_summary(beat_ids: list[str]) -> dict:
    """Cost and shape of a planned scene, computed from the saved drafts.

    Two different facts, reported side by side and never summed into each other:

    * ``estimated_cost`` — FORWARD. What producing this coverage is expected to
      cost, derived from the plan by ``director.quote_shot``.
    * ``spend`` — BACKWARD. What the generation ledger records as already gone,
      with the at-risk figure beside it, straight from ``generation.spend``.

    They used to be one number. ``compile_coverage`` added its spend into
    ``estimated_cost``, so a scene quoted at $1.99 reported $4.69 the moment it
    finished — on the same ten shots, with nothing about the plan changed, and
    that was the figure a re-compile would have quoted next. §6.1 asks for "spent
    so far" and "estimated remaining" as separate rows for exactly this reason.

    Spend is read from the ledger rather than mirrored onto the plan. A second
    copy of a money figure is how the quote and the charge drifted apart in the
    first place, and the plan cannot answer the question the ledger can: what may
    have been billed and nobody recorded.
    """
    shots = paid = 0
    cost = 0.0
    per_beat = []
    spends = []
    for bid in beat_ids:
        p = director.load_plan(bid)
        if not p:
            continue
        b_paid = sum(1 for s in p.coverage if s.motion_type == "ai_video")
        b_cost = sum(s.estimated_cost for s in p.coverage)
        shots += len(p.coverage); paid += b_paid; cost += b_cost
        b_spend = _beat_spend(bid)
        spends.append(b_spend)
        per_beat.append({"beat_id": bid, "shots": len(p.coverage),
                         "paid_shots": b_paid, "estimated_cost": round(b_cost, 2),
                         "spend": b_spend,
                         "status": p.status})
    return {"shots": shots, "paid_shots": paid,
            "estimated_cost": round(cost, 2), "spend": _total_spend(spends),
            "beats": per_beat}


def _beat_spend(beat_id: str) -> dict:
    """What one beat's ledger says, or why it cannot say.

    ``generation.spend`` raises rather than guessing when a ledger is present and
    unreadable, and a scene summary must not turn that into a $0.00 by catching
    it into a default.
    """
    from . import generation
    try:
        return generation.spend(beat_id)
    except Exception as exc:  # noqa: BLE001 — LedgerUnreadable and anything below it
        return generation.unknown_spend(f"{beat_id}: {exc}")


def _total_spend(spends: list[dict]) -> dict:
    """The scene's spend, in the same shape as one beat's.

    One unreadable beat makes the SCENE total unknown. Summing the readable ones
    and presenting the result as the total would be a confident number that is
    short by an unknown amount, which is the shape of every defect in this file's
    history.
    """
    from . import generation
    blocked = [s for s in spends if s.get("spent") is None]
    if blocked:
        return generation.unknown_spend(
            "; ".join(s.get("summary", "") for s in blocked))
    spent = round(float(sum(s["spent"] for s in spends)), 4)
    risk = round(float(sum(s["at_risk"] for s in spends)), 4)
    at_risk_attempts = sum(s["at_risk_attempts"] for s in spends)
    summary = f"${spent:.2f} billed"
    if at_risk_attempts:
        summary += (f" • ${risk:.2f} at risk on {at_risk_attempts} attempt"
                    f"{'s' if at_risk_attempts != 1 else ''} whose provider "
                    f"outcome was never recorded")
    return {
        "attempts": sum(s["attempts"] for s in spends),
        "failed": sum(s["failed"] for s in spends),
        "paid_attempts": sum(s["paid_attempts"] for s in spends),
        "spent": spent,
        "at_risk": risk,
        "at_risk_attempts": at_risk_attempts,
        "spend_is_certain": not at_risk_attempts,
        "summary": summary,
    }

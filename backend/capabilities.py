"""Spike C — what each generation backend can actually do, as data.

Model selection used to be split between prose and guesswork. ``VIDEO_BACKENDS``
carried a human-readable ``note`` ("Duration must be 4s/6s/8s"), and the render
path applied one hardcoded ceiling to every model:

    dur_int = max(3, min(10, int(round(target_dur))))

Neither is usable by a router. The prose is unparseable without a regex that
fails silently, and the ceiling is wrong in both directions — it caps an 8s-max
model at 10s and lets a 5s-max model be asked for 10. The same class of gap let
``kling_2_5_turbo_pro``, a key in no registry, resolve by substring match onto
Kling 2.1 Standard and render there silently for months.

This module makes the constraints machine-readable and gives the Director a
resolver that answers one question: *given what this shot needs, which backend is
legal and cheapest, and how many seconds should we actually ask for?*

Two things it deliberately is not:

* **Not an LLM decision.** The planner describes intent — duration, whether a
  character moves, how complex the motion is, what references it needs. Which fal
  endpoint serves that is application logic, so models can change without
  re-prompting or re-training anything.
* **Not authoritative on price.** fal is the authority on billing. ``cost_per_second``
  is this repo's transcription of fal's PUBLISHED tariff for the tier each call
  actually requests, so a plan can be costed before it is run. Every row carries
  ``price_basis``, ``price_source`` and ``price_checked`` — a bare float is not
  auditable, and an unsourced one is how the table came to sit 3x under fal's real
  seedance rate while a quote was derived from it and called an upper bound.
  Published pricing moves; re-check the sources and correct the row.

  (An earlier version of this note said the rates were "env-overridable". They
  never were — they are literals in the table below. The claim is removed rather
  than implemented: one sourced number that someone must edit deliberately is
  safer for money than a number an environment variable can move silently.)

All fields are optional at read time (``spec()`` fills defaults), so existing
callers of the registries are unaffected.
"""

from __future__ import annotations

import os
import re

from . import assets

# --- capability table ----------------------------------------------------------
#
# Keyed to assets.VIDEO_BACKENDS. Anything absent here falls back to CONTINUOUS,
# which is the permissive default: better to attempt a generation and get a clear
# API error than to silently exclude a model because nobody filled in a row.

# `verified: True` means the durations were observed from a real generation.
# Everything else is inferred from documentation or assumed, and assumption is
# exactly how a 3s request came back as a 5s clip and failed a gestural shot.
# Correct an entry the moment a run contradicts it.
CONTINUOUS: dict = {
    "allowed_durations": None,     # None => any integer length in [min, max]
    "min_seconds": 3,
    "max_seconds": 10,
    # The price of a model nobody has priced. Set to the DEAREST rate in the
    # table below rather than to a middling guess: an unregistered key is exactly
    # the case where nothing is known, and the one direction that must not happen
    # is quoting a human less than the call turns out to cost. This is the only
    # figure here that is deliberately conservative rather than published, and it
    # is conservative only within what this table knows.
    "cost_per_second": 0.3024,
    "price_basis": "unknown model — priced at the dearest configured rate",
    "price_source": "",
    "price_checked": "",
    "needs_start_image": True,
    "supports_reference_image": False,
    "supports_character_reference": False,
    "verified": False,
}

VIDEO_CAPS: dict[str, dict] = {
    "seedance_2_0": {
        # From fal's OpenAPI schema. Duration of the video in seconds. Supports 4 to 15 seconds, or auto to let the model decide based on the prompt.
        "allowed_durations": [4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0],
        "duration_values": ['auto', '4', '5', '6', '7', '8', '9', '10', '11', '12', '13', '14', '15'],
        "min_seconds": 4.0, "max_seconds": 15.0,
        "duration_wire_type": 'string',
        "duration_default": 'auto',
        "supports_generate_audio": True,
        # THE DEFAULT BACKEND, and the row that was 3x wrong: 0.10 against a
        # published 0.3024. A 5s seedance shot was quoted $0.60 after the old
        # floor against ~$1.51 of real billing.
        "cost_per_second": 0.3024,
        "price_basis": "standard tier, 720p (the model's default resolution). "
                       "The fast tier is 0.2419/s and 1080p is 0.682/s — this "
                       "path requests neither, and sends no resolution at all, "
                       "so it gets the 720p default.",
        "price_source": "https://fal.ai/models/bytedance/seedance-2.0/image-to-video",
        "price_checked": "2026-08-16",
        "needs_start_image": True,
        "supports_reference_image": False,
        "supports_character_reference": False,
        "verified": True,
    },
    "veo_3_1": {
        # From fal's OpenAPI schema. The duration of the generated video.
        "allowed_durations": [4.0, 6.0, 8.0],
        "duration_values": ['4s', '6s', '8s'],
        "min_seconds": 4.0, "max_seconds": 8.0,
        "duration_wire_type": 'string',
        "duration_default": '8s',
        "supports_generate_audio": True,
        # 0.40 was the WITH-AUDIO rate. director.generate_paid_clip sends
        # generate_audio=False unconditionally and says why (the beat's narration,
        # SFX and music are mixed separately), so this path is billed at half
        # that. Priced for the request this code actually makes; if audio is ever
        # turned on here, this doubles.
        "cost_per_second": 0.20,
        "price_basis": "720p/1080p WITHOUT audio. With audio it is 0.40/s; 4K is "
                       "0.40 silent and 0.60 with audio.",
        "price_source": "https://fal.ai/models/fal-ai/veo3.1/image-to-video",
        "price_checked": "2026-08-16",
        "needs_start_image": True,
        "supports_reference_image": True,
        "supports_character_reference": False,
        "verified": True,
    },
    "kling_2_1_standard": {
        # From fal's OpenAPI schema. The duration of the generated video in seconds
        "allowed_durations": [5.0, 10.0],
        "duration_values": ['5', '10'],
        "min_seconds": 5.0, "max_seconds": 10.0,
        "duration_wire_type": 'string',
        "duration_default": '5',
        "supports_generate_audio": False,
        # fal states this as a base plus an increment: "$0.28 for 5s, $0.056 for
        # every additional second". A flat 0.056/s reproduces BOTH published
        # figures exactly (5s -> 0.28, 10s -> 0.56), and 5 and 10 are the only
        # lengths this model allows, so nothing is lost by storing it per-second.
        "cost_per_second": 0.056,
        "price_basis": "$0.28 for 5s, +$0.056/s thereafter; exact per-second at "
                       "both allowed lengths",
        "price_source": "https://fal.ai/models/fal-ai/kling-video/v2.1/standard/image-to-video",
        "price_checked": "2026-08-16",
        "needs_start_image": True,
        "supports_reference_image": False,
        "supports_character_reference": False,
        "verified": True,
    },
    "kling_2_master": {
        # From fal's OpenAPI schema. The duration of the generated video in seconds
        "allowed_durations": [5.0, 10.0],
        "duration_values": ['5', '10'],
        "min_seconds": 5.0, "max_seconds": 10.0,
        "duration_wire_type": 'string',
        "duration_default": '5',
        "supports_generate_audio": False,
        # The one row that was already right. Same base-plus-increment shape:
        # "$1.40 for 5s, $0.28 per additional second" -> 0.28/s at 5s and 10s.
        "cost_per_second": 0.28,
        "price_basis": "$1.40 for 5s, +$0.28/s thereafter; exact per-second at "
                       "both allowed lengths",
        "price_source": "https://fal.ai/models/fal-ai/kling-video/v2/master/image-to-video",
        "price_checked": "2026-08-16",
        "needs_start_image": True,
        "supports_reference_image": True,
        "supports_character_reference": True,
        "verified": True,
    },
    "wan_2_7": {
        # From fal's OpenAPI schema. Output video duration in seconds (2-15).
        "allowed_durations": [2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0],
        "duration_values": [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
        "min_seconds": 2.0, "max_seconds": 15.0,
        "duration_wire_type": 'integer',
        "duration_default": 5,
        "supports_generate_audio": False,
        "cost_per_second": 0.10,
        "price_basis": "720p, the resolution this pipeline renders at "
                       "(director.CANON_WIDTH/HEIGHT is 1280x720). 1080p is "
                       "0.15/s; this path sends no resolution, so a change to "
                       "fal's default would move the real bill and not this row.",
        "price_source": "https://fal.ai/models/fal-ai/wan/v2.7/image-to-video",
        "price_checked": "2026-08-16",
        "needs_start_image": True,
        "supports_reference_image": False,
        "supports_character_reference": False,
        "verified": True,
    },
    "luma_dream_machine": {
        # From fal's OpenAPI schema. The duration of the generated video
        "allowed_durations": [5.0, 9.0],
        "duration_values": ['5s', '9s'],
        "min_seconds": 5.0, "max_seconds": 9.0,
        "duration_wire_type": 'string',
        "duration_default": '5s',
        "supports_generate_audio": False,
        # Ray-2 is priced off a 540p base of $0.50 per 5s, doubling per tier:
        # 720p is $1.00 per 5s = 0.20/s, and the 9s option scales proportionally.
        "cost_per_second": 0.20,
        "price_basis": "720p ($1.00 per 5s, 2x the 540p base of $0.50). This path "
                       "sends no resolution; 540p would bill 0.10/s, so this row "
                       "is the dearer of the two plausible tiers.",
        "price_source": "https://fal.ai/models/fal-ai/luma-dream-machine/ray-2/image-to-video",
        "price_checked": "2026-08-16",
        "needs_start_image": True,
        "supports_reference_image": True,
        "supports_character_reference": False,
        "verified": True,
    },
}

# Local tiers. Free, unlimited length, and the reason this pipeline can afford a
# ten-minute episode at all.
LOCAL_COST_PER_SECOND = 0.0
COST_PER_IMAGE = float(os.environ.get("COST_PER_IMAGE", "0.15"))

# --- what one Tier-C generation costs, for the quote AND for the ledger ---------
#
# Two numbers used to answer this question and they disagreed. The quote came
# from ``cost_per_second`` above; the figure written to the generation ledger came
# from a flat ``PAID_CLIP_COST = 0.60`` living in director.py. On the first real
# end-to-end compile the human approved "COMPILE & SPEND $1.99" and the ledger
# then recorded $0.60 for each of the two paid shots, quoted at $0.40 and $0.39 --
# a ~50% understatement on the paid tier.
#
# An overstated quote costs a human an unnecessary hesitation. An understated one
# takes consent for one amount and spends another, which is the single thing Gate
# 1 exists to prevent. So the two figures are collapsed into this one function,
# and every caller on both sides of consent goes through it.
#
# WHAT THIS IS NOT: an upper bound. The first version of this function claimed to
# be one -- max(table, a flat $0.60 floor) -- on the reasoning that taking the
# larger of two figures must be safe. It is not, because max() is only
# conservative if one of its arguments is, and the table was 3x UNDER fal's real
# seedance rate at the time. The floor bounded nothing; it merely papered over
# short requests. The docstring six lines above the table said so already: fal is
# the authority, these are transcriptions. You cannot derive a bound from a source
# that declares itself non-authoritative, and calling the result a bound is worse
# than having no bound at all, because it invites callers to stop checking.
#
# WHAT IT IS: the published tariff for the tier this pipeline actually requests,
# multiplied by the seconds actually requested. It can be wrong in BOTH
# directions -- if fal moves a price, if a model's default resolution changes
# under a request that pins none (see the wan and luma rows), or if this table is
# simply out of date. tests/test_fal_tariff.py transcribes fal's published figures
# independently and fails when the two disagree; that test, not this comment, is
# what keeps the number honest.
#
# The one thing it does guarantee is the thing it was written for: the quote and
# the ledger are the same function of the same inputs, so consent cannot be taken
# for one number while another is recorded.


def clip_price(key: str, generate_seconds: float) -> float:
    """Best-effort price of one Tier-C generation. Not a bound — see above.

    ``generate_seconds`` is the length actually requested of the model, not the
    editorial length of the shot -- a 3.34s shot billed as kling's 5s minimum is
    billed for five seconds. Callers get that number from
    :func:`legal_durations` / :func:`resolve`, never from ``Shot.duration``.
    """
    return round(float(generate_seconds) * float(spec(key)["cost_per_second"]), 4)


def spec(key: str) -> dict:
    """Capabilities for a video backend, with permissive defaults.

    The key is resolved through ``assets.VIDEO_BACKEND_ALIASES`` HERE rather than
    at the call sites. main.py resolved the alias when building the endpoint URL
    and then passed the RAW string to video_arguments/clamp_duration, so the
    request went to the right model while the limits came from the permissive
    CONTINUOUS fallback -- min 3, max 10, no duration_values, no
    supports_generate_audio. A legacy manifest naming an aliased model could
    therefore still be sent a duration its endpoint rejects, which is precisely
    the failure this table exists to prevent.

    Resolving inside spec() means no caller can get it wrong.
    """
    resolved = key
    try:
        aliases = getattr(assets, "VIDEO_BACKEND_ALIASES", {}) or {}
        if key not in VIDEO_CAPS:
            resolved = aliases.get(key) or aliases.get(str(key).lower()) or key
    except Exception:  # noqa: BLE001 — a missing alias table must not break lookup
        resolved = key
    caps = dict(CONTINUOUS)
    caps.update(VIDEO_CAPS.get(resolved, {}))
    caps["key"] = resolved
    caps["requested_key"] = key
    caps["label"] = (assets.VIDEO_BACKENDS.get(key, {}) or {}).get("label", key)
    caps["supports_extend"] = bool((assets.VIDEO_BACKENDS.get(key, {}) or {}).get("supports_extend"))
    return caps


def legal_durations(key: str, seconds: float) -> tuple[int | None, str]:
    """Nearest legal generation length for ``seconds`` on ``key``.

    Returns ``(length, note)``. ``length`` is None when the request cannot be
    served at all — a 3s shot on a model with a 4s floor, say — which is a fact
    the caller needs, not something to paper over by silently generating 4s.
    """
    caps = spec(key)
    allowed = caps.get("allowed_durations")
    lo, hi = int(caps["min_seconds"]), int(caps["max_seconds"])

    if allowed:
        at_or_above = [d for d in allowed if d >= seconds - 0.01]
        if at_or_above:
            pick = min(at_or_above)
            note = "" if abs(pick - seconds) < 0.05 else f"rounded {seconds:.2f}s up to {pick}s"
            return pick, note
        return None, f"{seconds:.2f}s exceeds the longest {caps['label']} allows ({max(allowed)}s)"

    if seconds > hi:
        # Deliberately None rather than a clamp. Silently serving a 45s request
        # with a 5s clip is how a beat ends up as five seconds of motion and forty
        # seconds of frozen frame — the exact failure this whole layer exists to
        # prevent. The caller needs to know the request cannot be met so it can
        # split the shot or drop it to a free tier.
        return None, (f"{seconds:.2f}s exceeds what {caps['label']} can generate "
                      f"({hi}s maximum)")
    if seconds < lo:
        return lo, f"raised {seconds:.2f}s to the {caps['label']} minimum of {lo}s"
    return int(round(seconds)), ""



def duration_argument(key: str, seconds: float):
    """The value this endpoint wants for ``duration``, in its own spelling.

    The wire type differs per model and is not interchangeable: wan_2_7 declares
    `integer`, veo wants "4s", kling wants "5", seedance wants "5". Sending "3" as
    a string to wan_2_7's integer field was silently ignored, the model used its
    default of 5, and a 3.34s gestural shot came back 5.00s and could not be
    trimmed. Nothing about that is visible without the schema.
    """
    caps = spec(key)
    picked, _ = legal_durations(key, seconds)
    if picked is None:
        return None
    values = caps.get("duration_values") or []
    # Match on the leading number so "4s", "4" and 4 all resolve from one place.
    for v in values:
        m = re.match(r"^(\d+(?:\.\d+)?)", str(v))
        if m and abs(float(m.group(1)) - picked) < 0.01:
            return v
    return int(picked) if caps.get("duration_wire_type") == "integer" else str(int(picked))

def video_arguments(key: str, seconds: float, *, generate_audio: bool = True,
                    cap_to_ceiling: bool = False) -> tuple[dict, str]:
    """The duration/audio half of a fal video request, in this model's own spelling.

    Returns ``(arguments, note)``. The note is empty when the request was served
    exactly, and otherwise says what was changed -- callers log it rather than
    discovering the difference in the finished cut.

    Replaces a hand-written block that lived byte-for-byte at two call sites in
    main.py:

        dur_int = max(3, min(10, int(round(target_dur))))
        arguments = {"duration": str(dur_int), "generate_audio": gen_audio}
        if "veo" in endpoint:      arguments["duration"] = "4s"/"6s"/"8s"
        elif "seedance" not in endpoint:
            arguments.pop("duration"); arguments.pop("generate_audio")

    Four separate defects in nine lines. kling, wan and luma -- three of the six
    registered backends, all selectable from the studio dropdown -- were sent NO
    duration at all, so each rendered its own 5s default and the rest of the beat
    became a freeze-frame. seedance, the default, was sent "3" for any beat under
    3.5s, which is not in its enum (minimum 4) and 422s, losing the beat's clip
    entirely. Every model was capped at 10s though seedance and wan allow 15. And
    veo rounded a 5.0s beat DOWN to "4s" while ``legal_durations`` rounds it up to
    6 -- the same shot came out a different length depending on which path ran it.

    ``cap_to_ceiling`` is the difference between the two callers. Director coverage
    plans a shot to fit a slot, so a request beyond the model's reach means the
    plan is wrong and must be re-routed or split -- omitting the duration is right.
    The batch render has no such option: the beat gets one clip and freeze-frame
    padding for the remainder, so asking for the model's MAXIMUM is strictly better
    than omitting the field and letting it fall back to a 5s default. Without this
    a 20s beat on seedance generated 5s and froze for 15 rather than generating 15
    and freezing for 5.
    """
    caps = spec(key)
    out: dict = {}
    picked, note = legal_durations(key, seconds)
    if picked is None and cap_to_ceiling:
        ceiling = float(caps["max_seconds"])
        picked, note = legal_durations(key, ceiling)
        if picked is not None:
            note = (f"{seconds:.2f}s exceeds what {caps['label']} can generate; "
                    f"asked for its maximum {picked}s (the remainder freeze-frames)")
    if picked is not None:
        dur = duration_argument(key, float(picked))
        if dur is not None:
            out["duration"] = dur
    if caps.get("supports_generate_audio"):
        out["generate_audio"] = bool(generate_audio)
    return out, note


def clamp_duration(key: str, seconds: float) -> int:
    """Legal length, falling back to the model's own ceiling.

    Replaces the hardcoded ``max(3, min(10, ...))`` that treated every model the
    same regardless of its real limits.
    """
    picked, _ = legal_durations(key, seconds)
    return int(picked if picked is not None else spec(key)["max_seconds"])


def resolve(intent: dict, prefer: list[str] | None = None) -> dict:
    """Choose a legal backend for one Director Shot's intent.

    ``intent`` uses the planner's vocabulary, not model names:
        duration, gestural, character_motion, face_visibility,
        motion_complexity, needs_character_reference

    Returns a dict carrying the choice, the length to *generate*, the estimated
    cost, and every constraint that was applied — because a shot whose framing was
    changed by a technical limit must be able to say so. Six months on, nobody
    remembers whether a film is observational by choice or because a model was
    weak in March.
    """
    want = float(intent.get("duration") or 0)
    gestural = bool(intent.get("gestural"))
    needs_charref = bool(intent.get("needs_character_reference"))

    # Ranked on the same figure that is reported, which is only correct because
    # that figure is now the published tariff. It briefly was not: while
    # clip_price applied a flat floor, ranking on it would have collapsed every
    # cheap model into one tie and silently re-routed shots, so the rank was
    # carried separately. With the floor gone there is one number again, and one
    # number is the point of this whole module.
    candidates: list[dict] = []
    for key in (prefer or list(VIDEO_CAPS)):
        caps = spec(key)
        if needs_charref and not caps["supports_character_reference"]:
            continue
        picked, note = legal_durations(key, want)
        if picked is None:
            continue
        # Generating longer than the shot and trimming back is fine for ambient
        # motion and wrong for a gesture: a designed movement cut at 80% stops the
        # head mid-turn. See director.fit_clip, which refuses that trim.
        if gestural and picked > want + 0.05:
            continue
        candidates.append({
            "backend": key, "label": caps["label"], "generate_seconds": picked,
            "estimated_cost": clip_price(key, picked),
            "note": note,
        })

    if not candidates:
        return {
            "backend": None, "generate_seconds": None, "estimated_cost": 0.0,
            "constraints": ["no_legal_backend"],
            "reason": (f"No configured model can produce {want:.2f}s"
                       + (" without trimming a gesture" if gestural else "")),
        }

    ranked = sorted(candidates,
                    key=lambda c: (c["estimated_cost"], c["generate_seconds"]))
    best = ranked[0]
    constraints = []
    if best["note"]:
        constraints.append("duration_quantized")
    if best["generate_seconds"] > want + 0.05:
        constraints.append("will_trim_to_editorial_length")
    return {
        "backend": best["backend"], "label": best["label"],
        "generate_seconds": best["generate_seconds"],
        "estimated_cost": best["estimated_cost"],
        "constraints": constraints,
        "reason": best["note"] or f"{best['label']} at {best['generate_seconds']}s",
        "alternatives": ranked[1:4],
    }


def estimate_shot_cost(motion_type: str, seconds: float, takes: int = 1,
                       backend: str | None = None) -> float:
    """What one Director Shot is expected to cost, stills included."""
    cost = COST_PER_IMAGE * max(1, takes)
    if motion_type == "ai_video":
        r = resolve({"duration": seconds}, prefer=[backend] if backend else None)
        cost += float(r.get("estimated_cost") or 0.0)
    return round(cost, 3)


def table() -> list[dict]:
    """The whole capability table, for the studio and for the contract doc."""
    return [spec(k) for k in assets.VIDEO_BACKENDS]

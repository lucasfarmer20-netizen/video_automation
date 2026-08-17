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
* **Not authoritative on price.** ``cost_per_second`` is an estimate for planning
  and ranking. fal is the authority on billing; these numbers exist so a plan can
  be costed *before* it is run, and are env-overridable because published pricing
  moves and a stale hardcoded number is worse than one you can correct.

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
    "cost_per_second": 0.12,
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
        "cost_per_second": 0.1,
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
        "cost_per_second": 0.4,
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
        "cost_per_second": 0.05,
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
        "cost_per_second": 0.28,
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
        "cost_per_second": 0.06,
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
        "cost_per_second": 0.14,
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
# It does not pretend to a precision it does not have. fal is the only authority
# on the real price and it is not known before the call, so this returns the
# UPPER of the two figures the system holds: the per-second table, and the flat
# per-clip floor that was observed in practice. A quote may now come out above the
# bill. It can no longer come out below it.
PAID_CLIP_FLOOR = float(os.environ.get("PAID_CLIP_FLOOR", "0.60"))


def clip_price(key: str, generate_seconds: float) -> float:
    """Upper bound on one Tier-C generation of ``generate_seconds`` on ``key``.

    ``generate_seconds`` is the length actually requested of the model, not the
    editorial length of the shot -- a 3.34s shot billed as kling's 5s minimum is
    billed for five seconds. Callers get that number from
    :func:`legal_durations` / :func:`resolve`, never from ``Shot.duration``.
    """
    table = float(generate_seconds) * float(spec(key)["cost_per_second"])
    return round(max(table, PAID_CLIP_FLOOR), 3)


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

    # Ranking is kept on the raw per-second table figure, while the cost REPORTED
    # is clip_price's bound. Ranking on the bound would collapse every model whose
    # table price sits under the floor into one tie and silently change which
    # model gets chosen -- a pricing fix has no business re-routing shots. The
    # rank is a parallel value rather than a key on the candidate so that only one
    # money number ever reaches a caller.
    candidates: list[tuple[float, dict]] = []
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
        candidates.append((round(picked * caps["cost_per_second"], 3), {
            "backend": key, "label": caps["label"], "generate_seconds": picked,
            "estimated_cost": clip_price(key, picked),
            "note": note,
        }))

    if not candidates:
        return {
            "backend": None, "generate_seconds": None, "estimated_cost": 0.0,
            "constraints": ["no_legal_backend"],
            "reason": (f"No configured model can produce {want:.2f}s"
                       + (" without trimming a gesture" if gestural else "")),
        }

    ranked = [c for _, c in sorted(candidates,
                                   key=lambda t: (t[0], t[1]["generate_seconds"]))]
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

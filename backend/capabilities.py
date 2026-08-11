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
        "allowed_durations": None,
        "min_seconds": 3, "max_seconds": 10,
        "cost_per_second": 0.10,
        "needs_start_image": True,
        "supports_reference_image": False,
        "supports_character_reference": False,
    },
    "veo_3_1": {
        # The constraint that broke coverage planning: a 3.2s Director Shot is
        # simply illegal here, and its 4s floor puts it out of reach for most
        # coverage, which runs 2-5s.
        "allowed_durations": [4, 6, 8],
        "min_seconds": 4, "max_seconds": 8,
        "cost_per_second": 0.40,
        "needs_start_image": True,
        "supports_reference_image": True,
        "supports_character_reference": False,
    },
    "kling_2_1_standard": {
        "allowed_durations": [5, 10],
        "min_seconds": 5, "max_seconds": 10,
        "cost_per_second": 0.05,
        "needs_start_image": True,
        "supports_reference_image": False,
        "supports_character_reference": False,
    },
    "kling_2_master": {
        "allowed_durations": [5, 10],
        "min_seconds": 5, "max_seconds": 10,
        "cost_per_second": 0.28,
        "needs_start_image": True,
        "supports_reference_image": True,
        "supports_character_reference": True,
    },
    "wan_2_7": {
        # MEASURED: asked for 3s, got exactly 5.00s. This entry previously claimed
        # continuous 3-5s, which was a guess, and the router duly picked it for a
        # 3.34s gestural shot that then could not be trimmed. Treat as fixed 5s
        # until something contradicts that.
        "allowed_durations": [5],
        "min_seconds": 5, "max_seconds": 5,
        "cost_per_second": 0.06,
        "needs_start_image": True,
        "supports_reference_image": False,
        "supports_character_reference": False,
        "verified": True,
    },
    "luma_dream_machine": {
        "allowed_durations": None,
        "min_seconds": 3, "max_seconds": 9,
        "cost_per_second": 0.14,
        "needs_start_image": True,
        "supports_reference_image": True,
        "supports_character_reference": False,
    },
}

# Local tiers. Free, unlimited length, and the reason this pipeline can afford a
# ten-minute episode at all.
LOCAL_COST_PER_SECOND = 0.0
COST_PER_IMAGE = float(os.environ.get("COST_PER_IMAGE", "0.15"))


def spec(key: str) -> dict:
    """Capabilities for a video backend, with permissive defaults."""
    caps = dict(CONTINUOUS)
    caps.update(VIDEO_CAPS.get(key, {}))
    caps["key"] = key
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

    candidates = []
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
            "estimated_cost": round(picked * caps["cost_per_second"], 3),
            "note": note,
        })

    if not candidates:
        return {
            "backend": None, "generate_seconds": None, "estimated_cost": 0.0,
            "constraints": ["no_legal_backend"],
            "reason": (f"No configured model can produce {want:.2f}s"
                       + (" without trimming a gesture" if gestural else "")),
        }

    best = min(candidates, key=lambda c: (c["estimated_cost"], c["generate_seconds"]))
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
        "alternatives": sorted(candidates, key=lambda c: c["estimated_cost"])[1:4],
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

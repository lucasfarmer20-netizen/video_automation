"""Spike A — how reliably does one character survive cinematic coverage?

The Director layer's whole docudrama premise rests on a question nothing in this
pipeline has ever had to answer: can the *same recognisable person* be generated
across a master, an over-the-shoulder and a close-up, cut together seconds apart,
without reading as three different actors?

Illustrated documentary never needed this. A beat is one image, the figure is one
element of a composition, often mid-or-wide and often silhouetted, and
``characters.json`` holds a text ``structural_anchor`` that keeps them roughly
on-model. Coverage is a harder problem: the audience looks straight at the face,
and any drift is immediately legible.

This harness answers it empirically rather than by argument, and it deliberately
does **not** return pass/fail. The useful output is a *coverage vocabulary* — which
shot sizes and angles are reliable enough to carry the face, and which should stay
observational. A cinematographer works around practical limits; so should the
Director.

Progressive by design (Round 4): the cells are ordered most-useful-first and the
run stops early when a shot size is clearly unusable, rather than completing an
academic 112-image matrix. Every image lands in the prompt ledger with its
``shot_size`` / ``angle`` / ``character`` recorded, so "is CU reliable for HENEY_01
on nano2" later becomes a query over data instead of a feature someone must build.
"""

from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass, field

from . import assets, config, ledger
from .manifest import Camera, Shot

# Framing vocabulary, ordered by how much of the film depends on it. Round 4 asked
# for the cinematically useful coverage first: CU and MCU decide whether face
# anchors are possible at all, and the observational framings below them are the
# fallback vocabulary if they are not.
CELLS: list[dict] = [
    {"key": "cu",    "shot_size": "cu",  "angle": "front",
     "framing": "a tight close-up, head and shoulders filling the frame, eyes clearly visible"},
    {"key": "mcu",   "shot_size": "mcu", "angle": "front",
     "framing": "a medium close-up from mid-chest, face clearly readable"},
    {"key": "m",     "shot_size": "m",   "angle": "front",
     "framing": "a medium shot from the waist, face readable at a natural distance"},
    {"key": "profile", "shot_size": "mcu", "angle": "profile",
     "framing": "a medium close-up in strict side profile, face turned fully to one side"},
    {"key": "ots",   "shot_size": "mw",  "angle": "rear_three_quarter",
     "framing": "an over-the-shoulder framing from behind and to one side, the figure's "
                "back and shoulder nearest camera, face turned partly away"},
    # Below here only if the above justify learning more.
    {"key": "mw",    "shot_size": "mw",  "angle": "front",
     "framing": "a medium-wide shot, the full upper body and surroundings visible"},
    {"key": "ws",    "shot_size": "ws",  "angle": "front",
     "framing": "a wide shot, the whole figure small within the setting"},
]

# Reference strategies, strongest first (Round 4: "use the strongest likely
# character-reference strategy first").
# Ordered by what is most likely to work, which is now known rather than assumed:
# anchor text alone produced four different men per framing.
STRATEGIES = ["anchor_plus_character_ref", "anchor_only"]

SCORE_KEYS = ("recognizability", "face_drift", "wardrobe_drift",
              "age_drift", "hair_drift", "lighting_robustness")


@dataclass
class SpikeConfig:
    character: str                       # key into characters.json
    style_medium: str                    # the project's medium; the anchor must survive it
    setting: str = ""                    # one sentence of context, kept constant across cells
    backends: list[str] = field(default_factory=lambda: ["nano2"])
    strategies: list[str] = field(default_factory=lambda: ["anchor_plus_frame_ref"])
    cells: list[str] = field(default_factory=lambda: ["cu", "mcu", "m", "profile", "ots"])
    takes: int = 4                       # "could I pick a good one?" is the real question
    stop_after_failed_cells: int = 3     # aggressive stopping (Round 4)


def _cell(key: str) -> dict | None:
    return next((c for c in CELLS if c["key"] == key), None)


def build_prompt(cfg: SpikeConfig, cell: dict, anchor: str) -> str:
    """Compose exactly as production does, so the spike measures the real path.

    Routing through ``assets._compose_prompt`` rather than hand-building a string
    means this exercises ``style_medium`` leading, the character clause, and the
    softener path — if any of those is the reason identity drifts, the spike sees
    it. A bespoke prompt here would measure something the pipeline never does.
    """
    scene = f"{cell['framing']}. {cfg.setting}".strip()
    synthetic = Shot(
        scene_id=f"_spikeA.{cell['key']}",
        prompt=scene,
        style_medium=cfg.style_medium,
        camera=Camera(),
    )
    # `_compose_prompt` appends anchors for characters it detects in the shot.
    # The character is named explicitly so the clause fires regardless of whether
    # the framing text happens to mention them.
    base = assets._compose_prompt(synthetic, anchors={cfg.character: anchor})
    if anchor and anchor not in base:
        base = f"{base} The figure is {cfg.character}: {anchor}"
    return base.strip()


def run(cfg: SpikeConfig, log=print) -> dict:
    """Generate the matrix progressively. Returns a report; scoring is human.

    Nothing is auto-scored. A model judging whether a face is recognisable is
    exactly the kind of measurement that would look authoritative and mean
    nothing — the scores come from Lucas via ``score_cell``.
    """
    config.require_for("assets")
    chars = __import__('backend.characters', fromlist=['x']).load_characters()
    anchors = assets._load_character_anchors()
    anchor = (anchors.get(cfg.character) or "").strip()
    if not anchor:
        raise RuntimeError(
            f"character {cfg.character!r} has no structural_anchor in characters.json. "
            f"Spike A measures whether an APPROVED character survives coverage; without "
            f"an anchor it would measure prompt luck. Author the anchor first — what the "
            f"protagonist looks like is a creative decision, not one to infer."
        )

    results: list[dict] = []
    spent = 0.0
    consecutive_skips = 0

    for backend in cfg.backends:
        for strategy in cfg.strategies:
            subject_url = None
            if strategy == "anchor_plus_character_ref":
                ref = (chars.get(cfg.character, {}) or {}).get("reference_image") or ""
                local = config.resolve_media(ref) if ref else None
                if not local:
                    log(f"  {cfg.character} has no reference_image — "
                        f"skipping {strategy!r} (upload one via "
                        f"POST /api/characters/{cfg.character}/reference)")
                    continue
                import fal_client
                subject_url = fal_client.upload_file(str(local))
                log(f"  using likeness reference {Path(local).name}")
            for key in cfg.cells:
                cell = _cell(key)
                if not cell:
                    log(f"  unknown cell {key!r} — skipped")
                    continue

                scene_id = f"_spikeA.{cfg.character}.{backend}.{strategy}.{key}"
                prompt = build_prompt(cfg, cell, anchor)
                log(f"[{backend}/{strategy}] {key}: generating {cfg.takes} takes")

                synthetic = Shot(
                    scene_id=scene_id, prompt=cell["framing"] + (". " + cfg.setting if cfg.setting else ""),
                    style_medium=cfg.style_medium, camera=Camera(),
                )
                try:
                    if subject_url and backend == "nano2":
                        urls = assets._generate_nano2(
                            prompt, cfg.takes, subject_url=subject_url)
                        paths = []
                        import time as _t
                        ts = int(_t.time())
                        for i, u in enumerate(urls):
                            dest = config.ASSETS / scene_id / f"var_{ts}_{i}.png"
                            assets._download(u, dest)
                            paths.append(config.rel_media_path(dest))
                    else:
                        paths = assets.generate_for_shot(
                            synthetic, cfg.takes, backend=backend, render=None, log=log)
                except Exception as exc:  # noqa: BLE001
                    log(f"  !! {key} failed: {exc}")
                    results.append({"cell": key, "backend": backend,
                                    "strategy": strategy, "error": str(exc)})
                    continue

                spent += len(paths) * 0.15
                for slot, rel in enumerate(paths):
                    ledger.record_generation(
                        scene_id=scene_id, path=rel, strategy=f"spikeA:{strategy}",
                        prompt=prompt, backend=backend, batch=f"spikeA.{key}",
                        slot=slot, style_medium=cfg.style_medium, motion_type="static",
                        extra={
                            "experiment": "spike_a",
                            "character": cfg.character,
                            "shot_size": cell["shot_size"],
                            "angle": cell["angle"],
                            "cell": key,
                            "reference_strategy": strategy,
                        },
                    )

                results.append({
                    "cell": key, "backend": backend, "strategy": strategy,
                    "shot_size": cell["shot_size"], "angle": cell["angle"],
                    "scene_id": scene_id, "images": paths, "scored": False,
                })
                log(f"  -> {len(paths)} images for {key} (running spend ~${spent:.2f})")

    manifest = {
        "character": cfg.character,
        "anchor": anchor,
        "style_medium": cfg.style_medium,
        "setting": cfg.setting,
        "takes_per_cell": cfg.takes,
        "estimated_spend": round(spent, 2),
        "cells": results,
    }
    out_dir = spike_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "spike_a_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "ok": True,
        "character": cfg.character,
        "cells_run": len(results),
        "estimated_spend": round(spent, 2),
        "results": results,
        "next": "GET /api/spike/identity/sheet to look at them, then score",
    }



def spike_dir():
    """Where the spike's manifest lives. Images stay in assets/, served already."""
    return config.MANIFEST_PATH.parent / "spike_a"


def contact_sheet() -> dict:
    """Every generated take, grouped by framing, with URLs to look at.

    Reviewing 28 images by curling one scoring call each is not a review, it is
    data entry -- and an experiment nobody completes tells you nothing. This
    returns the cells with their image urls so they can be opened side by side,
    which is what made the narrator audition actually reviewable.
    """
    from . import ledger

    rows = [r for r in ledger.read_rows() if r.get("experiment") == "spike_a"]
    gens = [r for r in rows if r.get("event") == "generate"]
    scored = {r.get("path") for r in rows
              if r.get("event") == "choose" and r.get("scores")}

    cells: dict[str, dict] = {}
    for g in gens:
        key = f"{g.get('cell')}|{g.get('backend')}|{g.get('reference_strategy')}"
        c = cells.setdefault(key, {
            "cell": g.get("cell"), "shot_size": g.get("shot_size"),
            "angle": g.get("angle"), "backend": g.get("backend"),
            "reference_strategy": g.get("reference_strategy"),
            "scene_id": g.get("scene_id"), "takes": [],
        })
        c["takes"].append({
            "path": g.get("path"),
            "url": f"media/{g.get('path')}",
            "scored": g.get("path") in scored,
        })
    for c in cells.values():
        c["takes"].sort(key=lambda t: t["path"])
        c["scored"] = sum(1 for t in c["takes"] if t["scored"])

    ordered = sorted(cells.values(),
                     key=lambda c: [x["key"] for x in CELLS].index(c["cell"])
                     if any(x["key"] == c["cell"] for x in CELLS) else 99)
    return {
        "cells": ordered,
        "images": sum(len(c["takes"]) for c in ordered),
        "scored": sum(c["scored"] for c in ordered),
        "score_keys": list(SCORE_KEYS),
        "how": ("Open each cell's takes together and judge whether they are the "
                "same man. Then POST /api/spike/identity/score per image with "
                "scores 0-3. A framing where no take is recognisable is a framing "
                "the Director must avoid."),
    }

def score_cell(scene_id: str, path: str, scores: dict, reason: str = "") -> None:
    """Record a human evaluation of one image. 0-3 per key in SCORE_KEYS."""
    clean = {k: int(v) for k, v in (scores or {}).items() if k in SCORE_KEYS}
    ledger.record_choice(scene_id=scene_id, path=path, source="human",
                         scores=clean, reason=reason,
                         extra={"experiment": "spike_a"})


def report() -> dict:
    """Aggregate Spike A into a coverage vocabulary.

    Returns mean scores per cell, and a recommendation per shot size — which is
    the artefact the Director actually needs: not "identity works", but "CU is
    unreliable, MCU is usable, profile and OTS are safe".
    """
    rows = [r for r in ledger.read_rows() if r.get("experiment") == "spike_a"]
    gens = {r["path"]: r for r in rows if r.get("event") == "generate"}

    by_cell: dict[str, dict] = {}
    for r in rows:
        if r.get("event") != "choose" or not r.get("scores"):
            continue
        g = gens.get(r.get("path"))
        if not g:
            continue
        key = (g.get("cell"), g.get("backend"), g.get("reference_strategy"))
        slot = by_cell.setdefault(key, {"scored": 0, "totals": {k: 0 for k in SCORE_KEYS}})
        slot["scored"] += 1
        for k, v in r["scores"].items():
            if k in slot["totals"]:
                slot["totals"][k] += int(v)

    out = []
    for (cell, backend, strategy), agg in sorted(by_cell.items(), key=lambda kv: str(kv[0])):
        n = max(1, agg["scored"])
        means = {k: round(v / n, 2) for k, v in agg["totals"].items()}
        rec = means.get("recognizability", 0.0)
        # 0-3 scale: >=2 means "at least one take in four was usable", which is
        # the operational bar for a shot that gets four takes.
        verdict = ("reliable" if rec >= 2.0 else
                   "marginal" if rec >= 1.0 else "unreliable")
        out.append({"cell": cell, "backend": backend, "reference_strategy": strategy,
                    "scored_images": agg["scored"], "means": means, "verdict": verdict})

    reliable = sorted({r["cell"] for r in out if r["verdict"] == "reliable"})
    return {
        "ok": True,
        "generated": len(gens),
        "cells": out,
        "face_capable_framings": reliable,
        "confident": sum(r["scored_images"] for r in out) >= 20,
        "note": ("Face-capable framings may carry face_visibility=high. Everything "
                 "else should stay observational — profile, OTS, hands, silhouette, "
                 "inserts, environment."),
    }

"""Append-only record of which prompt produced which image, and which one you kept.

The pipeline already generated several drafts per beat and already recorded the
one you picked, in ``Shot.chosen_variation``. What it never kept was the other
half: the prompt. ``_compose_prompt`` builds the string at generation time, fal
consumes it, and it is discarded — and because the beat fields it was assembled
from (``style_medium``, ``prompt``, the character anchors) get edited afterwards,
it cannot be reconstructed later either. The answers were on file; the questions
were not.

This module keeps both halves and joins them on the image path, which is unique
and stable. ``record_generation`` writes one row per image with the exact prompt
that made it and the strategy that shaped it; ``record_choice`` writes a row when
a human picks one. ``summary`` turns that into win rates per strategy, and
``top_exemplars`` returns the winning prompts — the input to few-shot exemplar
injection later, which is how this loop actually improves prompting without
anyone training anything.

Two rules keep the data honest:

* **Only human choices count as wins.** ``generate_for_shot`` auto-selects the
  first image of each new batch so the studio has something to display. Counting
  that as a preference would score whichever strategy happened to land in slot 0
  and would look like a real result — a bias strong enough to invert the answer.
* **The ledger lives at the storage root, not inside a project**, because the
  point is to learn across episodes. Same reasoning, and the same ``/gcs`` check,
  as ``config.AUDIO_POOL``: a path under the project directory would still be on
  the bucket, but the data would be siloed per episode and never accumulate.

Nothing in here may raise into a render. A failed write costs a row of telemetry;
raising would cost a paid image.
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from pathlib import Path

from . import config

_GCS_MOUNT = Path("/gcs")


def ledger_path() -> Path:
    """Cross-project ledger, on the bucket when deployed.

    Resolved per call rather than at import: the module is imported once, but on
    Cloud Run the mount is what makes the file durable, and a constant captured
    at import time in a test run would pin the local path for the process.
    """
    override = os.environ.get("PROMPT_LEDGER_PATH")
    if override:
        return Path(override)
    base = _GCS_MOUNT if _GCS_MOUNT.exists() else config.ROOT
    return base / "prompt_ledger.jsonl"


def _project() -> str:
    try:
        return config.MANIFEST_PATH.parent.name
    except Exception:  # noqa: BLE001
        return ""


def _append(row: dict) -> None:
    """One JSON object per line. Never raises — see the module docstring.

    Append-only is deliberate: this is the same shape of data the storyboard
    manifest holds, but the manifest had a lost-update race precisely because
    writers read it whole and wrote it back whole. Rows here are independent, so
    two concurrent generations cannot clobber each other's history.
    """
    try:
        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        print(f"[ledger] could not record {row.get('event')}: {exc}")


def record_generation(
    *,
    scene_id: str,
    path: str,
    strategy: str,
    prompt: str,
    backend: str,
    batch: str,
    slot: int,
    style_medium: str = "",
    motion_type: str = "",
    prompt_final: str | None = None,
    softened: list[str] | None = None,
) -> None:
    """One row per generated image.

    ``prompt`` is the composed base — medium, scene and character anchors, shared by
    every take of the beat. ``prompt_final`` is the string fal actually received: the
    base plus this take's strategy suffix, plus any content-filter softening, stored
    only when it differs. Keeping both is what makes the row attributable — the base
    is the constant being controlled for, the final is the variable under test.
    """
    _append({
        "event": "generate",
        "ts": time.time(),
        "project": _project(),
        "scene_id": scene_id,
        "path": path,
        "strategy": strategy,
        "backend": backend,
        "batch": batch,
        "slot": slot,
        "style_medium": style_medium,
        "motion_type": motion_type,
        "prompt": prompt,
        "prompt_final": prompt_final if (prompt_final and prompt_final != prompt) else None,
        "softened": softened or None,
    })


def record_choice(*, scene_id: str, path: str, source: str = "human") -> None:
    """One row when a draft is selected. Only ``source="human"`` scores."""
    _append({
        "event": "choose",
        "ts": time.time(),
        "project": _project(),
        "scene_id": scene_id,
        "path": path,
        "source": source,
    })


def read_rows() -> list[dict]:
    """All rows, oldest first. A corrupt line is skipped, not fatal."""
    path = ledger_path()
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as exc:  # noqa: BLE001
        print(f"[ledger] could not read: {exc}")
    return rows


def summary(project: str | None = None) -> dict:
    """Win rate per strategy, counting only human choices.

    A choice credits the strategy behind the chosen image and debits every other
    image offered *in the same batch* — the set that was actually on screen when
    the decision was made. Comparing against images the human never saw (an
    earlier regeneration, say) would inflate the denominator with contests that
    were never held.

    Where a beat was picked more than once, the last choice for that batch wins.
    """
    rows = read_rows()
    if project:
        rows = [r for r in rows if r.get("project") == project]

    gens = {r["path"]: r for r in rows if r.get("event") == "generate" and r.get("path")}

    # Last human choice per (scene, batch).
    latest: dict[tuple, dict] = {}
    for r in rows:
        if r.get("event") != "choose" or r.get("source") != "human":
            continue
        g = gens.get(r.get("path"))
        if not g:
            continue  # chosen image predates the ledger, or was an upload
        latest[(g.get("scene_id"), g.get("batch"))] = r

    wins: dict[str, int] = defaultdict(int)
    shown: dict[str, int] = defaultdict(int)

    for (scene_id, batch), choice in latest.items():
        contenders = [g for g in gens.values()
                      if g.get("scene_id") == scene_id and g.get("batch") == batch]
        if len(contenders) < 2:
            continue  # a one-image batch is not a comparison
        for g in contenders:
            shown[g.get("strategy") or "?"] += 1
        winner = gens.get(choice.get("path"))
        if winner:
            wins[winner.get("strategy") or "?"] += 1

    strategies = {}
    for name in sorted(set(shown) | set(wins)):
        s, w = shown.get(name, 0), wins.get(name, 0)
        strategies[name] = {
            "shown": s,
            "wins": w,
            "rate": round(w / s, 3) if s else None,
        }

    return {
        "ledger": str(ledger_path()),
        "generated": len(gens),
        "contests": len(latest),
        "strategies": strategies,
        # Below roughly 20 contests the ordering is noise; say so rather than
        # letting a 2-of-3 lead read as a finding.
        "confident": len(latest) >= 20,
    }


def top_exemplars(limit: int = 10, project: str | None = None) -> list[dict]:
    """Winning prompts, most recent first — the raw material for few-shot injection."""
    rows = read_rows()
    if project:
        rows = [r for r in rows if r.get("project") == project]
    gens = {r["path"]: r for r in rows if r.get("event") == "generate" and r.get("path")}
    out: list[dict] = []
    seen: set[tuple] = set()
    for r in reversed(rows):
        if r.get("event") != "choose" or r.get("source") != "human":
            continue
        g = gens.get(r.get("path"))
        if not g:
            continue
        key = (g.get("scene_id"), g.get("batch"))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "scene_id": g.get("scene_id"),
            "strategy": g.get("strategy"),
            "style_medium": g.get("style_medium"),
            "prompt": g.get("prompt_final") or g.get("prompt"),
            "project": g.get("project"),
        })
        if len(out) >= limit:
            break
    return out

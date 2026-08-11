"""Regenerate the video capability table from fal's own OpenAPI schemas.

Spike C replaced prose notes with structured data, and I then filled that
structure by hand from assumption. Every entry was wrong in some way, and the
errors were only discoverable by paying for a generation and watching it fail:

    wan_2_7             claimed continuous 3-5s     actually enum 2-15, type INTEGER
    seedance_2_0        claimed minimum 3s          actually minimum 4s
    luma_dream_machine  claimed continuous 3-9s     actually only "5s" or "9s"
    veo_3_1             claimed [4, 6, 8]           actually ["4s", "6s", "8s"] as STRINGS

The last column is the one that cost a paid generation. We sent "3" as a string
to a field declared integer; fal ignored it and used the default of 5, the clip
came back 5.00s for a 3.34s gestural shot, and the compile refused to trim it.
No amount of careful guessing finds that -- the wire TYPE differs per model and
is invisible from documentation prose.

fal publishes a queue OpenAPI document per endpoint, unauthenticated:

    https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=<endpoint>

so the durations a model accepts, and the exact form they must be sent in, are
readable rather than inferable.

    python -m scripts.sync_capabilities            # show what the schemas say
    python -m scripts.sync_capabilities --write    # rewrite VIDEO_CAPS

Run it whenever a model is added or a generation fails on a duration.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import assets  # noqa: E402

SCHEMA_URL = "https://fal.ai/api/openapi/queue/openapi.json?endpoint_id={}"


def fetch(endpoint: str) -> dict:
    with urllib.request.urlopen(SCHEMA_URL.format(endpoint), timeout=60) as r:
        return json.loads(r.read())


def _input_schema(doc: dict) -> dict:
    """The request body schema — the one carrying a `prompt`, not the output."""
    best = {}
    for name, sch in (doc.get("components", {}).get("schemas") or {}).items():
        props = (sch or {}).get("properties") or {}
        if "prompt" in props or "image_url" in props:
            if "Input" in name:
                return sch
            best = best or sch
    return best


def duration_spec(doc: dict) -> dict | None:
    """How this endpoint expresses duration, exactly as it must be sent."""
    props = (_input_schema(doc) or {}).get("properties") or {}
    dur = props.get("duration")
    if not dur:
        return None
    values = dur.get("enum")
    out: dict = {
        "wire_type": dur.get("type"),        # "string" | "integer" — NOT interchangeable
        "default": dur.get("default"),
        "description": (dur.get("description") or "")[:160],
    }
    if values:
        # Keep the send-values verbatim ("4s", "5", 5) and the seconds separately;
        # routing needs numbers, the API needs its own spelling.
        out["values"] = values
        seconds = []
        for v in values:
            m = re.match(r"^(\d+(?:\.\d+)?)", str(v))
            if m:
                seconds.append(float(m.group(1)))
        out["seconds"] = sorted(set(seconds))
    else:
        for k in ("minimum", "maximum"):
            if k in dur:
                out[k] = dur[k]
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true",
                    help="rewrite backend/capabilities.py VIDEO_CAPS")
    args = ap.parse_args()

    found: dict[str, dict] = {}
    for key, spec in assets.VIDEO_BACKENDS.items():
        endpoint = spec["endpoint"]
        try:
            doc = fetch(endpoint)
        except Exception as exc:  # noqa: BLE001
            print(f"{key:20} !! could not fetch schema: {exc}")
            continue
        d = duration_spec(doc)
        if not d:
            print(f"{key:20} no duration field — leaving as configured")
            continue
        found[key] = d
        vals = d.get("values")
        rng = f"{d.get('minimum')}-{d.get('maximum')}" if not vals else ""
        print(f"{key:20} type={d['wire_type']:<8} default={d.get('default')!r:>6} "
              f"{('values=' + str(vals)) if vals else 'range=' + rng}")

    if not args.write:
        print("\n(dry run — pass --write to update backend/capabilities.py)")
        return

    path = Path(__file__).resolve().parent.parent / "backend" / "capabilities.py"
    src = path.read_text(encoding="utf-8")
    lines = ["VIDEO_CAPS: dict[str, dict] = {"]
    for key, d in found.items():
        prev = {}
        try:
            from backend import capabilities
            prev = dict(capabilities.VIDEO_CAPS.get(key, {}))
        except Exception:  # noqa: BLE001
            pass
        secs = d.get("seconds")
        lines.append(f'    "{key}": {{')
        lines.append(f'        # From fal\'s OpenAPI schema. {d.get("description","").strip()}')
        if secs:
            lines.append(f'        "allowed_durations": {secs},')
            lines.append(f'        "duration_values": {d["values"]!r},')
            lines.append(f'        "min_seconds": {min(secs)}, "max_seconds": {max(secs)},')
        else:
            lines.append(f'        "allowed_durations": None,')
            lines.append(f'        "min_seconds": {d.get("minimum", 3)}, '
                         f'"max_seconds": {d.get("maximum", 10)},')
        lines.append(f'        "duration_wire_type": {d["wire_type"]!r},')
        lines.append(f'        "duration_default": {d.get("default")!r},')
        # Read from the schema, not remembered. main.py used to decide this with
        # `elif "seedance" not in endpoint`, which was right by luck and wrong in
        # kind -- the same shape of guess that made every duration entry wrong.
        lines.append(f'        "supports_generate_audio": {bool(d.get("generate_audio"))!r},')
        for k in ("cost_per_second", "needs_start_image", "supports_reference_image",
                  "supports_character_reference"):
            if k in prev:
                lines.append(f'        "{k}": {prev[k]!r},')
        lines.append('        "verified": True,')
        lines.append("    },")
    lines.append("}")
    block = "\n".join(lines)

    start = src.index("VIDEO_CAPS: dict[str, dict] = {")
    end = src.index("\n}\n", start) + 3
    path.write_text(src[:start] + block + "\n" + src[end:], encoding="utf-8")
    print(f"\nwrote {len(found)} entries to {path}")


if __name__ == "__main__":
    main()

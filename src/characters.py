"""Character-development stage: build locked character reference sheets.

Recurring characters drift across the Nano Banana style-transfer batch (a face,
a costume, the man's axe). This stage generates a clean, neutral **character
reference sheet** per character — seeded from an already-approved render to
preserve likeness, then restyled to a plain, even-lit, full-figure portrait so
it transfers cleanly when passed as a ``Shot.references`` anchor.

Unlike ``assets.NANO_STYLE_INSTRUCTION`` (which forbids reusing the reference's
subject), this stage deliberately *keeps* the same character and only cleans up
the presentation.

Flow (mirrors the storyboard gate): generate N candidate sheets per character →
a human picks the best → :func:`lock_sheet` promotes the pick to the canonical
``references/char_<name>.png`` anchor and registers it in ``references.json``.

Registry — ``characters.json`` (repo root):
    { "man": {"description": "...", "seed": ["assets/s010/var_2.png"]}, ... }

CLI:
    python -m src.characters                  # candidate sheets, all characters
    python -m src.characters --name man       # one character
    python -m src.characters --variations 4
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import fal_client
import requests

from . import config
from .assets import NANO_ENDPOINT, ref_urls, _save_references, load_references

DEFAULT_VARIATIONS = 3
CHARACTERS_CONFIG = config.ROOT / "characters.json"
SHEETS_DIR = config.REFERENCES_DIR / "sheets"  # candidate sheets (pre-lock)

# Character-sheet instruction: KEEP the reference character, clean the framing.
CHAR_SHEET_INSTRUCTION = (
    "Study BOTH the art style and the specific character/creature shown in the "
    "reference image(s). Redraw that SAME character with an identical face, build, "
    "hair and costume, as a clean character reference sheet: one full-figure, "
    "front-facing, neutral standing pose on a plain neutral parchment background, "
    "even soft lighting with no dramatic cast shadow hiding the face, the whole "
    "costume and any signature prop clearly visible. Keep the Deep Root Lore style: "
    "heavy black ink linework, loose cross-hatching, muted earth-tone watercolor "
    "washes, aged paper grain. The character is: "
)


def load_characters() -> dict:
    if CHARACTERS_CONFIG.exists():
        return json.loads(CHARACTERS_CONFIG.read_text(encoding="utf-8"))
    return {}


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(url, timeout=180)
    resp.raise_for_status()
    dest.write_bytes(resp.content)


def generate_sheet(name: str, spec: dict, n: int = DEFAULT_VARIATIONS) -> list[str]:
    """Generate n candidate reference sheets for one character."""
    style_urls = ref_urls(["style"])
    seed_urls = [fal_client.upload_file(str(config.ROOT / p)) for p in spec.get("seed", [])]
    image_urls = style_urls + seed_urls
    if not image_urls:
        raise RuntimeError(f"{name}: no style/seed references to condition on.")
    prompt = CHAR_SHEET_INSTRUCTION + spec["description"]

    paths: list[str] = []
    for i in range(n):
        result = fal_client.subscribe(
            NANO_ENDPOINT,
            arguments={"prompt": prompt, "image_urls": image_urls, "aspect_ratio": "3:4"},
            with_logs=False,
        )
        images = result.get("images") or []
        if images:
            dest = SHEETS_DIR / name / f"cand_{i}.png"
            _download(images[0]["url"], dest)
            paths.append(str(dest.relative_to(config.ROOT)).replace("\\", "/"))
    return paths


def generate_all(only: str | None = None, n: int = DEFAULT_VARIATIONS) -> dict[str, list[str]]:
    config.require_for("assets")
    chars = load_characters()
    if only:
        chars = {only: chars[only]} if only in chars else {}
        if not chars:
            raise SystemExit(f"Character {only!r} not in characters.json")
    out: dict[str, list[str]] = {}
    for name, spec in chars.items():
        print(f"Generating {n} candidate sheets for {name} ...")
        try:
            out[name] = generate_sheet(name, spec, n)
            print(f"  -> {len(out[name])} candidates")
        except Exception as exc:  # noqa: BLE001 — resilient batch, report + continue
            print(f"  !! {name} FAILED: {exc}")
            out[name] = []
    return out


def lock_sheet(name: str, candidate_index: int) -> str:
    """Promote a chosen candidate sheet to the canonical anchor for ``name``.

    Copies ``references/sheets/<name>/cand_<i>.png`` to ``references/char_<name>.png``
    and (re)registers it in ``references.json``, replacing any cached URLs so the
    new anchor is re-uploaded on the next assets run.
    """
    src = SHEETS_DIR / name / f"cand_{candidate_index}.png"
    if not src.exists():
        raise FileNotFoundError(src)
    dest = config.REFERENCES_DIR / f"char_{name}.png"
    dest.write_bytes(src.read_bytes())
    reg = load_references()
    reg[name] = {"files": [dest.name]}  # drop stale urls -> forces re-upload
    _save_references(reg)
    return str(dest.relative_to(config.ROOT)).replace("\\", "/")


def _main() -> None:
    parser = argparse.ArgumentParser(description="Deep Root Lore character-sheet stage.")
    parser.add_argument("--name", help="single character to generate (default: all).")
    parser.add_argument("--variations", type=int, default=DEFAULT_VARIATIONS)
    args = parser.parse_args()

    result = generate_all(only=args.name, n=args.variations)
    total = sum(len(v) for v in result.values())
    print(f"\nGenerated {total} candidate sheets across {len(result)} character(s).")
    print("Pick the best per character, then call characters.lock_sheet(name, index).")


if __name__ == "__main__":
    _main()

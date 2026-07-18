"""Character-development stage: lock style-agnostic Structural Feature Anchors.

Recurring characters must stay recognizable even as ``style_medium`` transforms
across cultures (ukiyo-e -> codex -> bronze relief). A style-locked *image* anchor
can't survive that jump, so the canonical anchor is now **text**: a concise,
style-agnostic description of a character's INVARIANT physical traits — distinct
scars, specific artifacts/props, precise anatomical markings — carrying no medium,
rendering technique, palette, pose, or lighting. ``assets.py`` appends the anchor
for every character a shot lists in ``Shot.references`` (see ``_character_clause``),
so the CFG-flux engine holds identity as the medium changes.

Anchors are drafted by Claude from each character's description and stored in
``characters.json`` under ``structural_anchor``; a human reviews/edits them there.

An optional neutral **visual** reference sheet can still be generated (seeded from
an approved render), but it is now a plain style-agnostic structural study — not
the legacy style-transfer path.

Registry — ``characters.json`` (repo root):
    { "man": {"description": "...", "seed": ["assets/s010/var_2.png"],
              "structural_anchor": "..."}, ... }

CLI:
    python -m src.characters --anchors             # draft anchors, all characters
    python -m src.characters --anchors --name man  # one character
    python -m src.characters --sheets --name man   # optional neutral visual sheet
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import fal_client
import requests

from . import config
from .assets import NANO_ENDPOINT, _save_references, load_references

DEFAULT_VARIATIONS = 3
CHARACTERS_CONFIG = config.CHARACTERS_CONFIG
SHEETS_DIR = config.REFERENCES_DIR / "sheets"  # candidate sheets (pre-lock)

# Claude system prompt for distilling a style-agnostic Structural Feature Anchor.
ANCHOR_SYSTEM = (
    "You define STRUCTURAL FEATURE ANCHORS for a recurring folkloric character in a "
    "documentary series whose art medium changes every episode. Output ONLY the "
    "invariant, identity-carrying PHYSICAL traits that must stay constant no matter "
    "the medium: distinct scars, specific artifacts/props the figure always carries, "
    "precise anatomical markings, fixed proportions, and countable features (number "
    "of fingers, horns, eyes, limbs), permanent bodily distinctions. Write ONE compact "
    "comma-separated clause, prompt-ready, present tense. EXCLUDE everything style- or "
    "scene-dependent: no art medium, rendering technique, linework, color-as-style, "
    "palette, mood, lighting, pose, camera, background, or narrative. Be concrete and "
    "visual. No preamble — output only the clause."
)

# Optional NEUTRAL visual-sheet instruction: keep the character, drop all style.
CHAR_SHEET_INSTRUCTION = (
    "Study the specific character/creature in the reference image(s) and redraw the "
    "SAME individual — identical face, build, and distinguishing features — as a "
    "neutral, STYLE-AGNOSTIC structural reference: a plain graphite/ink study on a "
    "blank background, full figure front view plus a head close-up, flat even lighting, "
    "no dramatic shadow, no rendered art style, medium, or color grading. Clearly show "
    "the invariant physical anchors (distinct scars, specific artifacts/props, precise "
    "anatomical markings) that must stay constant no matter what art medium is used "
    "later. The character is: "
)


def load_characters() -> dict:
    if CHARACTERS_CONFIG.exists():
        return json.loads(CHARACTERS_CONFIG.read_text(encoding="utf-8"))
    return {}


def save_characters(chars: dict) -> None:
    CHARACTERS_CONFIG.write_text(
        json.dumps(chars, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# structural feature anchors (style-agnostic text — the canonical anchor)
# --------------------------------------------------------------------------- #
def generate_anchor(name: str, spec: dict, model: str | None = None, client=None) -> str:
    """Distill a character's description into a style-agnostic structural anchor."""
    config.require_for("script")  # anchors are a Claude (text) call
    import anthropic

    from .script import DEFAULT_MODEL

    client = client or anthropic.Anthropic()
    desc = (spec.get("description") or "").strip()
    resp = client.messages.create(
        model=model or DEFAULT_MODEL, max_tokens=400, system=ANCHOR_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Character: {name}\n\nDescription:\n{desc}\n\n"
                       "Write the Structural Feature Anchor.",
        }],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()


def set_anchor(name: str, anchor: str) -> None:
    """Lock a structural anchor into ``characters.json`` for ``name``."""
    chars = load_characters()
    if name not in chars:
        raise KeyError(f"{name!r} not in characters.json")
    chars[name]["structural_anchor"] = anchor.strip()
    save_characters(chars)


def generate_all_anchors(only: str | None = None) -> dict[str, str]:
    """Draft + persist a structural anchor for some or all characters."""
    chars = load_characters()
    if only:
        chars = {only: chars[only]} if only in chars else {}
        if not chars:
            raise SystemExit(f"Character {only!r} not in characters.json")
    out: dict[str, str] = {}
    for name, spec in chars.items():
        print(f"Drafting structural anchor for {name} ...")
        try:
            anchor = generate_anchor(name, spec)
            set_anchor(name, anchor)
            out[name] = anchor
            print(f"  -> {anchor[:80]}{'...' if len(anchor) > 80 else ''}")
        except Exception as exc:  # noqa: BLE001 — resilient batch, report + continue
            print(f"  !! {name} FAILED: {exc}")
            out[name] = ""
    return out


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(url, timeout=180)
    resp.raise_for_status()
    dest.write_bytes(resp.content)


def generate_sheet(name: str, spec: dict, n: int = DEFAULT_VARIATIONS) -> list[str]:
    """Generate n candidate NEUTRAL structural reference sheets for one character.

    Seeded only from the character's approved render(s) — no legacy 'style' ref —
    so the sheet documents likeness/anchors without baking in an art medium.
    """
    seed_urls = [fal_client.upload_file(str(config.ROOT / p)) for p in spec.get("seed", [])]
    if not seed_urls:
        raise RuntimeError(f"{name}: no seed reference(s) to condition on (set 'seed' in characters.json).")
    image_urls = seed_urls
    prompt = CHAR_SHEET_INSTRUCTION + (spec.get("description") or "")

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
    parser = argparse.ArgumentParser(description="The Illuminated Bestiary character stage.")
    parser.add_argument("--name", help="single character (default: all).")
    parser.add_argument("--anchors", action="store_true",
                        help="draft style-agnostic structural anchors (Claude) into characters.json.")
    parser.add_argument("--sheets", action="store_true",
                        help="generate optional neutral visual reference sheets (fal).")
    parser.add_argument("--variations", type=int, default=DEFAULT_VARIATIONS)
    args = parser.parse_args()

    if args.anchors:
        res = generate_all_anchors(only=args.name)
        done = sum(1 for v in res.values() if v)
        print(f"\nDrafted {done}/{len(res)} anchor(s) into {CHARACTERS_CONFIG.name}. Review/edit them there.")
    if args.sheets:
        result = generate_all(only=args.name, n=args.variations)
        total = sum(len(v) for v in result.values())
        print(f"\nGenerated {total} candidate sheets across {len(result)} character(s).")
        print("Pick the best per character, then call characters.lock_sheet(name, index).")
    if not (args.anchors or args.sheets):
        parser.print_help()


if __name__ == "__main__":
    _main()

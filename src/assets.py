"""Assets stage: fal flux draft-image generation through the Deep Root Lore LoRA.

Generates N cheap draft variations per storyboard beat so the human can pick or
regenerate at the dashboard (Gate 1). Uses the trained style LoRA when
``lora_config.json`` is present (every prompt carries the trigger word); falls
back to plain ``flux/schnell`` if the LoRA isn't trained yet. This is Tier-1
(cheap) generation only — the paid video stage stays behind the approval gate.

Pure helpers (``load_lora``, ``style_prompt``) are unit-testable without a key;
the fal calls are isolated in ``_generate`` / ``_download``.

CLI:
    python -m src.assets                 # all beats, 3 variations each
    python -m src.assets --scene s001    # one beat
    python -m src.assets --variations 4 --limit 2
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import fal_client
import requests

from . import config
from .manifest import Shot, Storyboard, load, save

DEFAULT_VARIATIONS = 3
LORA_ENDPOINT = "fal-ai/flux-lora"
SCHNELL_ENDPOINT = "fal-ai/flux/schnell"
IMAGE_SIZE = "landscape_16_9"


def load_lora() -> dict | None:
    """Return the trained-LoRA pointer, or None if training hasn't run yet."""
    if not config.LORA_CONFIG.exists():
        return None
    data = json.loads(config.LORA_CONFIG.read_text(encoding="utf-8"))
    return data if data.get("lora_url") else None


def style_prompt(prompt: str, lora: dict | None) -> str:
    """Ensure the LoRA trigger word is present so the style actually fires."""
    if not lora:
        return prompt
    trigger = (lora.get("trigger_word") or "").strip()
    if trigger and trigger.lower() not in prompt.lower():
        return f"{trigger} {prompt}"
    return prompt


def _generate(prompt: str, n: int, lora: dict | None) -> list[str]:
    """Call fal and return image URLs (LoRA endpoint, or schnell fallback)."""
    if lora:
        endpoint = lora.get("inference_endpoint", LORA_ENDPOINT)
        args = {
            "prompt": prompt,
            "loras": [{"path": lora["lora_url"], "scale": 1.0}],
            "num_images": n,
            "image_size": IMAGE_SIZE,
            "num_inference_steps": 28,
            "guidance_scale": 3.5,
            "enable_safety_checker": False,  # horror imagery trips false positives
            "output_format": "png",
        }
    else:
        endpoint = SCHNELL_ENDPOINT
        args = {
            "prompt": prompt,
            "num_images": n,
            "image_size": IMAGE_SIZE,
            "num_inference_steps": 4,
            "enable_safety_checker": False,
        }
    result = fal_client.subscribe(endpoint, arguments=args, with_logs=False)
    return [img["url"] for img in result.get("images", [])]


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    dest.write_bytes(resp.content)


def generate_for_shot(shot: Shot, n: int, lora: dict | None) -> list[str]:
    """Generate + download n draft variations for one beat; record their paths."""
    urls = _generate(style_prompt(shot.prompt, lora), n, lora)
    rel_paths: list[str] = []
    for i, url in enumerate(urls):
        dest = config.ASSETS / shot.scene_id / f"var_{i}.png"
        _download(url, dest)
        rel_paths.append(str(dest.relative_to(config.ROOT)).replace("\\", "/"))
    shot.draft_variations = rel_paths
    return rel_paths


def generate_drafts(
    storyboard: Storyboard,
    n: int = DEFAULT_VARIATIONS,
    only: set[str] | None = None,
    limit: int | None = None,
    lora: dict | None = None,
    skip_existing: bool = True,
    save_after_each: bool = False,
) -> Storyboard:
    """Generate draft variations for some or all beats. Mutates the storyboard.

    Resilient for long batches: skips beats that already have drafts (so a
    re-run resumes), tolerates a single beat failing, and can persist the
    manifest after each beat so a crash never loses completed work.
    """
    config.require_for("assets")
    if lora is None:
        lora = load_lora()

    shots = storyboard.shots
    if only:
        shots = [s for s in shots if s.scene_id in only]
    if limit:
        shots = shots[:limit]

    failures: list[str] = []
    for shot in shots:
        if skip_existing and shot.draft_variations:
            print(f"{shot.scene_id}: already has {len(shot.draft_variations)} drafts — skipping.")
            continue
        try:
            print(f"Generating {n} drafts for {shot.scene_id} ...")
            paths = generate_for_shot(shot, n, lora)
            print(f"  -> {len(paths)} images")
        except Exception as exc:  # one bad beat must not kill the batch
            print(f"  !! {shot.scene_id} FAILED: {exc}")
            failures.append(shot.scene_id)
        if save_after_each:
            save(storyboard)

    if failures:
        print(f"\nFailed beats ({len(failures)}): {failures} — re-run to retry just these.")
    return storyboard


def _main() -> None:
    parser = argparse.ArgumentParser(description="Deep Root Lore draft-image stage.")
    parser.add_argument("--scene", nargs="*", help="scene id(s) to generate (default: all).")
    parser.add_argument("--variations", type=int, default=DEFAULT_VARIATIONS)
    parser.add_argument("--limit", type=int, default=None, help="cap number of beats.")
    parser.add_argument("--force", action="store_true", help="regenerate beats that already have drafts.")
    args = parser.parse_args()

    storyboard = load()
    if not storyboard.shots:
        raise SystemExit("Manifest has no beats — run the script stage first.")

    lora = load_lora()
    print("Style LoRA: " + (lora["lora_url"] if lora else "not trained yet -> flux/schnell fallback"))

    generate_drafts(
        storyboard,
        n=args.variations,
        only=set(args.scene) if args.scene else None,
        limit=args.limit,
        lora=lora,
        skip_existing=not args.force,
        save_after_each=True,
    )
    save(storyboard)
    print(f"Saved draft variations into {config.MANIFEST_PATH}")


if __name__ == "__main__":
    _main()

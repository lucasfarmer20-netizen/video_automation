"""Assets stage: draft-image generation.

Default backend is **``fal-ai/flux-general``** (FLUX.1 [dev] with *real* CFG):
each beat's positive prompt leads with a historical art medium authentic to the
entity's culture (the script stage composes it), and a shared ``NEGATIVE_PROMPT``
filters the generic modern-digital-art look. Real CFG (``use_real_cfg``) is what
makes the negative prompt actually bite — it runs a positive+negative pass per
step (~2x base-flux cost), the deliberate trade for cultural authenticity.

Legacy backends (retained, off by default):
- ``--backend nano``      Nano Banana style-transfer from ``references.json``.
- ``--backend flux-lora`` trained "DEEPROOTLORE" ink LoRA.
- ``--backend flux``      base flux/dev + STYLE_BLOCK (no negative prompt).

CLI:
    python -m src.assets                       # all beats, flux-general (real CFG)
    python -m src.assets --scene s004          # one beat
    python -m src.assets --backend nano        # legacy style-transfer
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
DEFAULT_BACKEND = "flux-cfg"
STYLE_REF = "style"  # implicit style reference applied to every Nano Banana beat

CFG_ENDPOINT = "fal-ai/flux-general"  # FLUX.1 [dev] w/ real CFG -> honours negative_prompt
NANO_ENDPOINT = "fal-ai/nano-banana/edit"
LORA_ENDPOINT = "fal-ai/flux-lora"
BASE_ENDPOINT = "fal-ai/flux/dev"
IMAGE_SIZE = "landscape_16_9"

# Real-CFG knobs for the flux-general backend. use_real_cfg=True is what makes the
# negative prompt actually apply (plain flux-dev is guidance-distilled and ignores
# it); it costs ~2x since each step runs a positive AND a negative pass.
CFG_STEPS = 28
REAL_CFG_SCALE = 4.0
GUIDANCE_SCALE = 3.5

# Shared negative prompt (CFG backend only): filter the generic modern-digital-art
# "house style" so beats read as authentic historical illustration in the medium
# the script stage chose per culture — not an AI render.
NEGATIVE_PROMPT = (
    "modern digital art, digital painting, 3d render, octane render, cgi, "
    "anime, manga, anime texture, cel shading, vector art, flat illustration, "
    "smooth airbrushed gradients, concept art, artstation, deviantart, trending, "
    "photorealistic, photograph, hdr, oversaturated, neon, glossy plastic, "
    "video game screenshot, ai-generated look, digital artifacts, compression "
    "artifacts, jpeg artifacts, watermark, signature, text, logo, lowres, blurry"
)

# Instruction that turns Nano Banana's edit endpoint into style-transfer: match
# the reference's STYLE, invent a new scene, never copy the reference's subjects.
NANO_STYLE_INSTRUCTION = (
    "Study the art style of the reference image(s): ink-and-watercolor graphic-novel "
    "illustration, heavy black ink linework and cross-hatching, muted earth-tone "
    "watercolor washes, warm amber candlelight against deep near-black shadow, strong "
    "chiaroscuro, aged paper grain, cinematic 16:9. Do NOT reuse the subjects, "
    "characters, or setting of the reference images. Create a brand-new illustration "
    "in that exact style showing: "
)

# Style anchor for the flux fallback backend.
STYLE_BLOCK = (
    "A dark folkloric horror illustration in ink and watercolor, heavy black ink "
    "linework with loose expressive cross-hatching, muted earth-tone watercolor "
    "washes, warm amber candlelight glowing against deep near-black shadow, strong "
    "chiaroscuro, aged paper grain, desaturated moody palette, cinematic 16:9"
)


# --- reference registry -----------------------------------------------------
def load_references() -> dict:
    if config.REFERENCES_CONFIG.exists():
        return json.loads(config.REFERENCES_CONFIG.read_text(encoding="utf-8"))
    return {}


def _save_references(reg: dict) -> None:
    config.REFERENCES_CONFIG.write_text(json.dumps(reg, indent=2) + "\n", encoding="utf-8")


def ref_urls(names: list[str]) -> list[str]:
    """Resolve reference names -> fal image URLs, uploading + caching as needed."""
    reg = load_references()
    urls: list[str] = []
    changed = False
    for name in names:
        entry = reg.get(name)
        if not entry:
            continue
        files = entry.get("files") or ([entry["file"]] if entry.get("file") else [])
        cached = entry.get("urls") or []
        if len(cached) != len(files):  # (re)upload if not cached for all files
            cached = [fal_client.upload_file(str(config.REFERENCES_DIR / f)) for f in files]
            entry["files"], entry["urls"] = files, cached
            changed = True
        urls.extend(cached)
    if changed:
        _save_references(reg)
    return urls


# --- generation backends ----------------------------------------------------
def _generate_nano(scene_prompt: str, image_urls: list[str], n: int) -> list[str]:
    """Nano Banana edit: style-transfer the references onto a new scene, n times."""
    prompt = NANO_STYLE_INSTRUCTION + scene_prompt
    out: list[str] = []
    for _ in range(n):  # one image per call -> n distinct variations
        result = fal_client.subscribe(
            NANO_ENDPOINT,
            arguments={"prompt": prompt, "image_urls": image_urls, "aspect_ratio": "16:9"},
            with_logs=False,
        )
        images = result.get("images") or []
        if images:
            out.append(images[0]["url"])
    return out


def load_lora() -> dict | None:
    if not config.LORA_CONFIG.exists():
        return None
    data = json.loads(config.LORA_CONFIG.read_text(encoding="utf-8"))
    return data if data.get("lora_url") else None


def style_prompt(prompt: str, lora: dict | None) -> str:
    """Anchor style for the flux backends: LoRA trigger, or a leading STYLE_BLOCK."""
    if not lora:
        return f"{STYLE_BLOCK}. {prompt}"
    trigger = (lora.get("trigger_word") or "").strip()
    if trigger and trigger.lower() not in prompt.lower():
        return f"{trigger} {prompt}"
    return prompt


def _generate_flux_cfg(
    prompt: str, n: int, negative: str = NEGATIVE_PROMPT, steps: int = CFG_STEPS,
    guidance: float = GUIDANCE_SCALE, real_cfg: float = REAL_CFG_SCALE,
) -> list[str]:
    """FLUX.1 [dev] with real CFG so the negative prompt is actually applied.

    The positive ``prompt`` is expected to *lead with* the culture-appropriate
    historical art medium (the script stage composes it); this backend only adds
    the shared negative prompt that filters the generic modern-digital-art look.
    ``use_real_cfg`` runs a positive+negative pass per step (~2x base-flux cost).
    Knobs (``steps``/``guidance``/``real_cfg``/``negative``) are overridable per
    project via ``Storyboard.render`` (edited in the dashboard).
    """
    result = fal_client.subscribe(
        CFG_ENDPOINT,
        arguments={
            "prompt": prompt,
            "negative_prompt": negative,
            "use_real_cfg": True,
            "real_cfg_scale": real_cfg,
            "num_inference_steps": steps,
            "guidance_scale": guidance,
            "image_size": IMAGE_SIZE,
            "num_images": n,
            "enable_safety_checker": False,
            "output_format": "png",
        },
        with_logs=False,
    )
    return [img["url"] for img in result.get("images", [])]


def _resolve_render(render) -> tuple[str, int, float, float]:
    """(negative, steps, guidance, real_cfg) from a RenderConfig or module defaults.

    An empty ``negative_prompt`` on the config falls back to ``NEGATIVE_PROMPT``
    (the config keeps it empty to avoid a manifest <-> assets import cycle).
    """
    if render is None:
        return NEGATIVE_PROMPT, CFG_STEPS, GUIDANCE_SCALE, REAL_CFG_SCALE
    negative = (getattr(render, "negative_prompt", "") or "").strip() or NEGATIVE_PROMPT
    return (
        negative,
        getattr(render, "num_inference_steps", CFG_STEPS),
        getattr(render, "guidance_scale", GUIDANCE_SCALE),
        getattr(render, "real_cfg_scale", REAL_CFG_SCALE),
    )


def _generate_flux(prompt: str, n: int, lora: dict | None) -> list[str]:
    if lora:
        endpoint = lora.get("inference_endpoint", LORA_ENDPOINT)
        args = {
            "prompt": prompt,
            "loras": [{"path": lora["lora_url"], "scale": 1.0}],
            "num_images": n,
            "image_size": IMAGE_SIZE,
            "num_inference_steps": 28,
            "guidance_scale": 3.5,
            "enable_safety_checker": False,
            "output_format": "png",
        }
    else:
        endpoint = BASE_ENDPOINT
        args = {
            "prompt": prompt,
            "num_images": n,
            "image_size": IMAGE_SIZE,
            "num_inference_steps": 28,
            "guidance_scale": 3.5,
            "enable_safety_checker": False,
        }
    result = fal_client.subscribe(endpoint, arguments=args, with_logs=False)
    return [img["url"] for img in result.get("images", [])]


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(url, timeout=180)
    resp.raise_for_status()
    dest.write_bytes(resp.content)


def _load_character_anchors() -> dict[str, str]:
    """Map character name -> its style-agnostic Structural Feature Anchor text.

    Read straight from ``characters.json`` (not via ``characters.py``, to avoid an
    import cycle). Characters without an anchor are omitted.
    """
    if not config.CHARACTERS_CONFIG.exists():
        return {}
    data = json.loads(config.CHARACTERS_CONFIG.read_text(encoding="utf-8"))
    return {
        name: (spec.get("structural_anchor") or "").strip()
        for name, spec in data.items()
        if isinstance(spec, dict) and (spec.get("structural_anchor") or "").strip()
    }


def _character_clause(shot: Shot, anchors: dict[str, str]) -> str:
    """Structural anchors for whichever characters this shot lists in ``references``.

    These invariant physical traits are appended after the scene so the CFG-flux
    engine holds identity continuity even as ``style_medium`` transforms across
    cultures. Style/medium is deliberately excluded from anchors.
    """
    present = [anchors[n] for n in (shot.references or []) if anchors.get(n)]
    if not present:
        return ""
    return (" The recurring figure keeps these fixed identifying features "
            "regardless of art medium: " + "; ".join(present) + ".")


def _compose_prompt(shot: Shot, anchors: dict[str, str] | None = None) -> str:
    """Lead the image prompt with the beat's historical medium, then the scene,
    then any present character's locked structural anchors.

    ``style_medium`` (set per culture by the script stage) *is* the style, so it
    leads; ``prompt`` is the scene description. Falls back to the raw prompt for
    legacy beats that baked the medium into the prompt text. Character anchors
    are appended last so identity survives medium changes.
    """
    medium = (shot.style_medium or "").strip()
    scene = (shot.prompt or "").strip()
    base = f"{medium}. {scene}" if medium and scene else (medium or scene)
    if anchors is None:
        anchors = _load_character_anchors()
    return (base + _character_clause(shot, anchors)).strip()


def generate_for_shot(
    shot: Shot, n: int, backend: str = DEFAULT_BACKEND, lora: dict | None = None,
    render=None,
) -> list[str]:
    """Generate + download n draft variations for one beat; record their paths.

    ``render`` is a ``Storyboard.render`` (RenderConfig) whose knobs override the
    flux-cfg defaults; ``None`` uses the module defaults.
    """
    if backend == "flux-cfg":
        # Default: medium-leading positive prompt + shared negative prompt (real CFG).
        negative, steps, guidance, real_cfg = _resolve_render(render)
        gen_urls = _generate_flux_cfg(_compose_prompt(shot), n, negative, steps, guidance, real_cfg)
    elif backend == "nano":
        urls = ref_urls([STYLE_REF, *(shot.references or [])])
        if not urls:
            raise RuntimeError(
                "No references resolved — populate references.json with a 'style' entry."
            )
        gen_urls = _generate_nano(shot.prompt, urls, n)
    else:
        if lora is None and backend == "flux-lora":
            lora = load_lora()
        gen_urls = _generate_flux(style_prompt(shot.prompt, lora), n, lora)

    rel_paths: list[str] = []
    for i, url in enumerate(gen_urls):
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
    backend: str = DEFAULT_BACKEND,
    skip_existing: bool = True,
    save_after_each: bool = False,
) -> Storyboard:
    """Generate draft variations for some or all beats. Mutates the storyboard.

    Resilient for long batches: skips beats that already have drafts, tolerates a
    single beat failing, and can persist after each beat. Per-project generation
    knobs are read from ``storyboard.render``.
    """
    config.require_for("assets")
    render = getattr(storyboard, "render", None)

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
            print(f"Generating {n} drafts for {shot.scene_id} ({backend}) ...")
            paths = generate_for_shot(shot, n, backend=backend, render=render)
            print(f"  -> {len(paths)} images")
        except Exception as exc:
            print(f"  !! {shot.scene_id} FAILED: {exc}")
            failures.append(shot.scene_id)
        if save_after_each:
            save(storyboard)

    if failures:
        print(f"\nFailed beats ({len(failures)}): {failures} — re-run to retry just these.")
    return storyboard


def _main() -> None:
    parser = argparse.ArgumentParser(description="The Illuminated Bestiary draft-image stage.")
    parser.add_argument("--scene", nargs="*", help="scene id(s) to generate (default: all).")
    parser.add_argument("--variations", type=int, default=DEFAULT_VARIATIONS)
    parser.add_argument("--limit", type=int, default=None, help="cap number of beats.")
    parser.add_argument("--backend", default=DEFAULT_BACKEND, choices=["flux-cfg", "nano", "flux", "flux-lora"])
    parser.add_argument("--force", action="store_true", help="regenerate beats that already have drafts.")
    args = parser.parse_args()

    storyboard = load()
    if not storyboard.shots:
        raise SystemExit("Manifest has no beats — run the script stage first.")

    print(f"Backend: {args.backend}")
    generate_drafts(
        storyboard,
        n=args.variations,
        only=set(args.scene) if args.scene else None,
        limit=args.limit,
        backend=args.backend,
        skip_existing=not args.force,
        save_after_each=True,
    )
    save(storyboard)
    print(f"Saved draft variations into {config.MANIFEST_PATH}")


if __name__ == "__main__":
    _main()

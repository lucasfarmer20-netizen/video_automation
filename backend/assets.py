"""Assets stage: draft-image generation.

Default backend is **``nano2``** — Nano Banana 2 / Gemini 3 Pro Image
(``fal-ai/gemini-3-pro-image-preview``): a reasoning image model with strong
prompt adherence, text rendering, and character consistency. Each beat's prompt
leads with the culture-authentic historical art medium (composed by the script
stage), plus the per-shot character anchors, with the avoidance list folded into
the prompt text (the model has no ``negative_prompt`` field). ~$0.15/image.

Other backends:
- ``--backend flux-cfg``  FLUX.1 [dev] (flux-general) + NAG negative prompt (~$0.04, cheaper).
- ``--backend nano``      original Nano Banana style-transfer from ``references.json``.
- ``--backend flux-lora`` trained "DEEPROOTLORE" ink LoRA (legacy).
- ``--backend flux``      base flux/dev + STYLE_BLOCK (legacy).

CLI:
    python -m backend.assets                       # all beats, Nano Banana 2
    python -m backend.assets --scene s004          # one beat
    python -m backend.assets --backend flux-cfg    # cheaper flux fallback
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import fal_client
import requests

from . import config, ledger
from .manifest import MotionType, Shot, Storyboard, load, save

DEFAULT_VARIATIONS = 3
DEFAULT_BACKEND = "nano2"
STYLE_REF = "style"  # implicit style reference applied to every Nano Banana beat

NANO2_ENDPOINT = "fal-ai/nano-banana"  # Nano Banana 2 (Gemini 3.1 Flash Image)
NANO2_EDIT_ENDPOINT = "fal-ai/nano-banana/edit"  # image-conditioned (frame ref)
NANO2_RESOLUTION = "2K"               # 1K/2K same price ($0.15/img); 4K is 2x
CFG_ENDPOINT = "fal-ai/flux-general"  # FLUX.1 [dev] w/ NAG negative prompt (cheaper fallback)
NANO_ENDPOINT = "fal-ai/nano-banana/edit"
LORA_ENDPOINT = "fal-ai/flux-lora"
BASE_ENDPOINT = "fal-ai/flux/dev"
IMAGE_SIZE = "landscape_16_9"

# --- Backend registry: the single source of truth ---------------------------
#
# The script stage's schema enum and the studio's backend dropdown are both
# derived from this. They used to be hand-maintained separately and had drifted
# to ZERO overlap: Claude could only recommend three flux variants, the UI only
# offered nano2/flux-cfg, and this module implemented a dozen. A per-beat model
# choice therefore landed in the manifest and was silently ignored.
#
# ``handler`` selects the call shape: nano2 and flux-cfg have bespoke argument
# sets (edit endpoint / NAG negative), everything else uses the generic
# text-to-image call. Legacy keys (nano, flux, flux-lora) still work but are
# deliberately absent — CLAUDE.md marks them deprecated, so they are not offered.
IMAGE_BACKENDS: dict[str, dict] = {
    "nano2": {
        "label": "Nano Banana 2 — Gemini 3 Pro Image",
        "endpoint": NANO2_ENDPOINT,
        "handler": "nano2",
        "note": "Reasoning model. Best prompt adherence, text rendering and character consistency. ~$0.15/img.",
    },
    "flux-cfg": {
        "label": "FLUX.1 dev + NAG negative",
        "endpoint": CFG_ENDPOINT,
        "handler": "flux_cfg",
        "note": "Cheapest. True negative prompt via NAG. ~$0.04/img.",
    },
    "flux_2_pro": {
        "label": "FLUX.2 Pro",
        "endpoint": "fal-ai/flux-2-pro",
        "handler": "generic",
        "note": "Detailed cinematic frames, rich texture.",
    },
    "flux_2_max": {
        "label": "FLUX.2 Max",
        "endpoint": "fal-ai/flux-2-max",
        "handler": "generic",
        "note": "Highest-fidelity flux tier.",
    },
    "flux_1_1_pro_ultra": {
        "label": "FLUX1.1 Pro Ultra",
        "endpoint": "fal-ai/flux-pro/v1.1-ultra",
        "handler": "generic",
        "note": "High resolution, wide panoramic compositions.",
    },
    "flux_1_dev_turbo": {
        "label": "FLUX.1 dev (fast)",
        "endpoint": "fal-ai/flux/dev",
        "handler": "generic",
        "note": "Fast, cheap drafts.",
    },
    "ideogram_4": {
        "label": "Ideogram v4",
        "endpoint": "ideogram/v4",
        "handler": "generic",
        "note": "Strong typography and lettering inside the image.",
    },
    "ideogram_4_instant": {
        "label": "Ideogram v4 Instant",
        "endpoint": "ideogram/v4/instant",
        "handler": "generic",
        "note": "Faster, cheaper Ideogram tier.",
    },
    "wan_2_7_image": {
        "label": "Wan 2.7 (text-to-image)",
        "endpoint": "fal-ai/wan/v2.7/text-to-image",
        "handler": "generic",
        "note": "Alternative look; useful as a tie-breaker.",
    },
}

#: Keys offered to the script stage and the studio, in display order.
IMAGE_BACKEND_KEYS: list[str] = list(IMAGE_BACKENDS)


# --- Video backend registry ------------------------------------------------
#
# Same drift problem the image backends had: the endpoint map lived in main.py,
# a duplicate lived in this module, the script schema had its own enum, and the
# studio dropdown had a fourth list. They disagreed, and one disagreement was
# fatal — every Kling request resolved to "fal-ai/kling-video/v3/image-to-video",
# which fal returns 404 for. A render batch died on it after ten minutes.
#
# Every endpoint below was verified against fal's OpenAPI registry. Note that
# seedance is genuinely NOT under the fal-ai/ namespace; that is not a typo.
VIDEO_BACKENDS: dict[str, dict] = {
    "seedance_2_0": {
        "label": "Seedance 2.0 (image-to-video)",
        "endpoint": "bytedance/seedance-2.0/image-to-video",
        "supports_extend": True,
        "note": "Default. Supports native video extension for continuous chaining.",
    },
    "veo_3_1": {
        "label": "Google Veo 3.1 (image-to-video)",
        "endpoint": "fal-ai/veo3.1/image-to-video",
        "supports_extend": False,
        "note": "Strong motion realism. Duration must be 4s/6s/8s.",
    },
    "kling_2_1_standard": {
        "label": "Kling 2.1 Standard (image-to-video)",
        "endpoint": "fal-ai/kling-video/v2.1/standard/image-to-video",
        "supports_extend": False,
        "note": "Cheaper Kling tier.",
    },
    "kling_2_master": {
        "label": "Kling 2 Master (image-to-video)",
        "endpoint": "fal-ai/kling-video/v2/master/image-to-video",
        "supports_extend": False,
        "note": "Higher-quality, pricier Kling tier.",
    },
    "wan_2_7": {
        "label": "Wan 2.7 (image-to-video)",
        "endpoint": "fal-ai/wan/v2.7/image-to-video",
        "supports_extend": False,
        "note": "Good for subtle drift and b-roll motion.",
    },
    "luma_dream_machine": {
        "label": "Luma Dream Machine Ray-2",
        "endpoint": "fal-ai/luma-dream-machine/ray-2/image-to-video",
        "supports_extend": True,
        "note": "Alternative look.",
    },
}

VIDEO_BACKEND_KEYS: list[str] = list(VIDEO_BACKENDS)

# Legacy keys and raw endpoint strings already sitting in manifests. Kling v3 was
# never a real endpoint, so anything pointing at it is remapped to the closest
# tier that exists rather than left to 404.
VIDEO_BACKEND_ALIASES: dict[str, str] = {
    "kling": "kling_2_1_standard",
    "kling_v3": "kling_2_1_standard",
    "kling-video": "kling_2_1_standard",
    "kling_2_5_turbo_pro": "kling_2_1_standard",
    "fal-ai/kling-video/v3/image-to-video": "kling_2_1_standard",
    "seedance": "seedance_2_0",
    "seedance-2.0": "seedance_2_0",
    "bytedance/seedance-2.0": "seedance_2_0",
    "fal-ai/bytedance/seedance-2.0": "seedance_2_0",
    "veo": "veo_3_1",
    "veo_3": "veo_3_1",
    "wan": "wan_2_7",
    "luma": "luma_dream_machine",
    "hunyuan_video": "wan_2_7",
    "hunyuan": "wan_2_7",
}


def resolve_video_backend(key: str | None) -> dict:
    """Map any stored video-model string to a registry entry. Never returns None."""
    k = (key or "").strip()
    if k in VIDEO_BACKENDS:
        return VIDEO_BACKENDS[k]
    alias = VIDEO_BACKEND_ALIASES.get(k) or VIDEO_BACKEND_ALIASES.get(k.lower())
    if alias:
        return VIDEO_BACKENDS[alias]
    low = k.lower()
    for token, target in (("seedance", "seedance_2_0"), ("veo", "veo_3_1"),
                          ("kling", "kling_2_1_standard"), ("wan", "wan_2_7"),
                          ("luma", "luma_dream_machine")):
        if token in low:
            return VIDEO_BACKENDS[target]
    return VIDEO_BACKENDS["seedance_2_0"]

# flux-general knobs. Negative prompts are applied via NAG (Normalized Attention
# Guidance) on this guidance-distilled model — NOT real CFG. (`use_real_cfg` + a
# negative prompt currently 422s the endpoint: "Could not load pipeline". NAG needs
# no second pass, so it's also cheaper.) nag_scale controls how hard the negative
# bites; higher = more aggressive filtering of the modern-digital-art look.
CFG_STEPS = 28
NAG_SCALE = 5.0
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
    if config.references_config().exists():
        return json.loads(config.references_config().read_text(encoding="utf-8"))
    return {}


def _save_references(reg: dict) -> None:
    config.references_config().write_text(json.dumps(reg, indent=2) + "\n", encoding="utf-8")


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
            cached = [fal_client.upload_file(str(config.references_dir() / f)) for f in files]
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


def _generate_nano2(prompt: str, n: int, negative: str = NEGATIVE_PROMPT,
                    frame_url: str | None = None,
                    subject_url: str | None = None) -> list[str]:
    """Nano Banana 2 (Gemini 3 Pro Image) generation — the default backend.

    A reasoning model, not CFG-diffusion, so there is no ``negative_prompt`` field:
    the medium-leading positive prompt (from ``_compose_prompt``) carries the style,
    and the avoidance list is folded into the prompt text. Strong prompt adherence,
    text rendering, and character consistency. ~$0.15/image (2K, same price as 1K).

    If ``frame_url`` is set (the project's global frame reference), the edit endpoint
    conditions on it: the model keeps that image's border / page-edges / framing but
    renders a brand-new interior in the shot's own medium — the fix for shots drifting
    to wildly different borders.
    """
    # Positive phrasing only. This used to append "NOT: <60-token avoidance
    # list>". Instruction-following models handle negation poorly -- naming
    # "anime, cgi, 3d render, octane" can act as content injection rather than
    # exclusion, and the list consumed a large share of the attention budget.
    # Describing what the artifact IS rules out the same failures implicitly.
    # (flux-cfg is unaffected: it gets a real negative via the NAG field.)
    interior = (
        f"{prompt} Render this as a genuine surviving artifact of the stated "
        f"historical medium — every mark made with that tradition's own tools and "
        f"materials, carrying the irregularity real handwork leaves: uneven ink "
        f"load, visible tool travel, the grain and wear of the physical support. "
        f"A flat photographic reproduction of an aged original, not a modern "
        f"illustration imitating one."
    )
    if subject_url:
        # A SUBJECT reference, which is the opposite instruction to a frame
        # reference. The frame path exists to keep a page border and replace the
        # interior; this one keeps the PERSON and replaces everything else.
        #
        # Spike A ran on anchor text alone and returned four different men per
        # framing, none of them the documented likeness -- a 130-word structural
        # description lost to three words of setting. Text cannot carry a specific
        # real face; an image of that face might.
        full = (
            "The person in the reference image is the subject of this photograph. "
            "Preserve their identity exactly: the same face, bone structure, hairline, "
            "build and age, recognisably the same individual. Change everything else "
            "to match the description below — the framing, pose, clothing, setting "
            "and lighting are all as described, not as they appear in the reference. "
            f"The photograph: {interior}"
        )
        result = fal_client.subscribe(
            NANO2_EDIT_ENDPOINT,
            arguments={
                "prompt": full,
                "image_urls": [subject_url],
                "num_images": n,
                "aspect_ratio": "16:9",
                "resolution": NANO2_RESOLUTION,
                "output_format": "png",
            },
            with_logs=False,
        )
        return [img["url"] for img in result.get("images", [])]

    if frame_url:
        full = (
            "Use the reference image ONLY as the page frame: match its border, margins, "
            "page edges, and overall framing EXACTLY. Inside that frame, create a NEW "
            "illustration — do not reuse the reference's subject or interior artwork. "
            f"The interior illustration: {interior}"
        )
        result = fal_client.subscribe(
            NANO2_EDIT_ENDPOINT,
            arguments={
                "prompt": full,
                "image_urls": [frame_url],
                "num_images": n,
                "aspect_ratio": "16:9",
                "resolution": NANO2_RESOLUTION,
                "output_format": "png",
            },
            with_logs=False,
        )
    else:
        result = fal_client.subscribe(
            NANO2_ENDPOINT,
            arguments={
                "prompt": interior,
                "num_images": n,
                "aspect_ratio": "16:9",
                "resolution": NANO2_RESOLUTION,
                "output_format": "png",
            },
            with_logs=False,
        )
    return [img["url"] for img in result.get("images", [])]


def generate_image_edit(public_image_url: str, prompt: str, n: int, backend: str, render=None) -> list[str]:
    """Route an image edit/Image-to-Image request to the appropriate Fal.ai model."""
    import fal_client
    
    # Resolve project render settings
    negative, steps, guidance, nag = _resolve_render(render)
    
    # 1. Gemini / Nano Banana 2 edit endpoint
    if backend == "nano2":
        result = fal_client.subscribe(
            NANO2_EDIT_ENDPOINT,
            arguments={
                "prompt": prompt,
                "image_urls": [public_image_url],
                "num_images": n,
                "aspect_ratio": "16:9",
                "resolution": NANO2_RESOLUTION,
                "output_format": "png",
            },
            with_logs=False,
        )
        return [img["url"] for img in result.get("images", [])]
        
    # 2. Flux Image-to-Image (using dev/image-to-image or general/image-to-image if applicable)
    elif "flux" in backend or backend == "flux-cfg":
        result = fal_client.subscribe(
            "fal-ai/flux/dev/image-to-image",
            arguments={
                "prompt": prompt,
                "image_url": public_image_url,
                "strength": 0.5,
                "num_images": n,
                "image_size": IMAGE_SIZE,
                "num_inference_steps": 30,
                "enable_safety_checker": False,
            },
            with_logs=False,
        )
        return [img["url"] for img in result.get("images", [])]
        
    # 3. Ideogram Image-to-Image (using ideogram/v4 remix/image-to-image)
    elif "ideogram" in backend:
        result = fal_client.subscribe(
            "ideogram/v4",
            arguments={
                "prompt": prompt,
                "image_url": public_image_url,
                "num_images": n,
                "aspect_ratio": "16:9",
            },
            with_logs=False,
        )
        return [img["url"] for img in result.get("images", [])]
        
    elif "wan" in backend:
        result = fal_client.subscribe(
            "fal-ai/wan/v2.7/text-to-image",
            arguments={
                "prompt": prompt,
                "image_urls": [public_image_url],
                "num_images": n,
                "aspect_ratio": "16:9",
            },
            with_logs=False,
        )
        return [img["url"] for img in result.get("images", [])]

    # Default fallback to Flux dev image-to-image
    else:
        result = fal_client.subscribe(
            "fal-ai/flux/dev/image-to-image",
            arguments={
                "prompt": prompt,
                "image_url": public_image_url,
                "strength": 0.5,
                "num_images": n,
                "image_size": IMAGE_SIZE,
                "enable_safety_checker": False,
            },
            with_logs=False,
        )
        return [img["url"] for img in result.get("images", [])]


def _generate_flux_cfg(
    prompt: str, n: int, negative: str = NEGATIVE_PROMPT, steps: int = CFG_STEPS,
    guidance: float = GUIDANCE_SCALE, nag: float = NAG_SCALE,
) -> list[str]:
    """FLUX.1 [dev] (flux-general) with a NAG-applied negative prompt.

    The positive ``prompt`` is expected to *lead with* the culture-appropriate
    historical art medium (the script stage composes it); this backend adds the
    shared negative prompt that filters the generic modern-digital-art look via
    NAG (``nag_scale``), which works on this distilled model without real CFG.
    Knobs (``steps``/``guidance``/``nag``/``negative``) are overridable per project
    via ``Storyboard.render`` (edited in the dashboard).
    """
    result = fal_client.subscribe(
        CFG_ENDPOINT,
        arguments={
            "prompt": prompt,
            "negative_prompt": negative,
            "nag_scale": nag,
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
    """(negative, steps, guidance, nag) from a RenderConfig or module defaults.

    An empty ``negative_prompt`` on the config falls back to ``NEGATIVE_PROMPT``
    (the config keeps it empty to avoid a manifest <-> assets import cycle).
    """
    if render is None:
        return NEGATIVE_PROMPT, CFG_STEPS, GUIDANCE_SCALE, NAG_SCALE
    negative = (getattr(render, "negative_prompt", "") or "").strip() or NEGATIVE_PROMPT
    return (
        negative,
        getattr(render, "num_inference_steps", CFG_STEPS),
        getattr(render, "guidance_scale", GUIDANCE_SCALE),
        getattr(render, "nag_scale", NAG_SCALE),
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
    if not config.characters_config().exists():
        return {}
    data = json.loads(config.characters_config().read_text(encoding="utf-8"))
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


# Several endpoints (the FLUX pro/ultra family, ideogram) cap or ignore
# num_images and return a single image however many were asked for, which is why
# beats came back with one draft instead of three.
MAX_TOPUP_CALLS = 4

# fal's output tolerance on the FLUX pro/ultra endpoints, 1 (strictest) to 6.
# Unset it defaults to 2, which rejects ordinary historical documentary subjects.
OUTPUT_TOLERANCE = int(os.environ.get("FAL_OUTPUT_TOLERANCE", "5"))

# Phrasings that read as violence out of context and trip fal's *prompt*
# moderation on otherwise ordinary archival subjects. That filter is server-side
# and cannot be disabled, so the only honest remedy is to say the same thing in
# words that are not ambiguous. Each replacement must preserve the meaning --
# this is rephrasing, not laundering.
_PROMPT_SOFTENERS: list[tuple[str, str]] = [
    ("strung along", "working along"),
    ("strung across", "spread across"),
    ("hanging from", "suspended by ropes from"),
    ("dangling from", "roped to"),
    ("bodies", "figures"),
    ("scarred", "worn"),
    ("beaten", "weathered"),
    ("blasting", "quarrying"),
    ("blast", "quarry charge"),
]


def soften_prompt(prompt: str) -> tuple[str, list[str]]:
    """Rephrase known false-positive triggers. Returns (prompt, changes made)."""
    out, changed = prompt, []
    for bad, good in _PROMPT_SOFTENERS:
        if bad in out.lower():
            # Case-insensitive single pass, preserving the rest of the text.
            import re as _re
            out = _re.sub(_re.escape(bad), good, out, flags=_re.I)
            changed.append(f"{bad!r} -> {good!r}")
    return out, changed


def _is_content_policy(exc: Exception) -> bool:
    s = str(exc).lower()
    return "content_policy_violation" in s or "flagged by a content checker" in s


def _subscribe_topup(endpoint: str, build_args, prompt: str, n: int,
                     log=print, sink: dict | None = None) -> list[str]:
    """Call ``endpoint`` until ``n`` images exist, or the attempts run out.

    Two failure modes are handled here rather than losing the beat:

    * an endpoint that returns fewer images than asked for -- call again for the
      remainder instead of accepting one draft where three were requested;
    * fal's prompt moderation rejecting an innocuous historical description --
      retry once with the phrasing softened, and say exactly what was changed so
      the edit is visible rather than silent.
    """
    urls: list[str] = []
    active = prompt
    softened = False

    for attempt in range(MAX_TOPUP_CALLS):
        want = n - len(urls)
        if want <= 0:
            break
        try:
            result = fal_client.subscribe(endpoint, arguments=build_args(active, want),
                                          with_logs=False)
        except Exception as exc:  # noqa: BLE001
            if _is_content_policy(exc) and not softened:
                new_prompt, changes = soften_prompt(active)
                softened = True
                if changes:
                    log(f"  prompt was rejected by fal's content filter; rephrasing: "
                        + "; ".join(changes))
                    active = new_prompt
                    continue
                log("  prompt was rejected by fal's content filter and nothing "
                    "matched the rephrase list — edit the beat's prompt in the studio.")
            raise
        got = [img["url"] for img in (result.get("images") or []) if img.get("url")]
        urls.extend(got)
        if not got:
            break
        if len(got) < want and attempt == 0:
            log(f"  endpoint returned {len(got)} of {want} images; topping up.")

    if len(urls) < n:
        log(f"  got {len(urls)} of {n} variations after {MAX_TOPUP_CALLS} attempt(s).")
    if sink is not None:
        # The caller composed `prompt`, but softening happens in here, so only
        # this function knows what fal actually received. The ledger needs the
        # string that made the image, not the one we intended to send.
        sink["final_prompt"] = active
        sink["softened"] = _softenings(prompt, active)
    return urls[:n]


def _softenings(before: str, after: str) -> list[str]:
    """Which rephrasings actually fired, by comparing the two strings."""
    if before == after:
        return []
    return [f"{bad!r} -> {good!r}" for bad, good in _PROMPT_SOFTENERS
            if bad in before.lower() and good in after]


def _with_softening(call, prompt: str, log=print) -> tuple[list[str], str, list[str]]:
    """Run ``call(prompt)``; on a content-filter rejection rephrase once and retry.

    ``_subscribe_topup`` has always done this for the generic endpoints, but
    ``nano2`` — the *default* backend — and ``flux-cfg`` call ``fal_client.subscribe``
    directly and had no such retry. The softener list was written in response to a
    beat that failed on the default backend, and then never ran there: the same
    prompt recovered on a fallback endpoint and failed outright on the one almost
    every beat uses.
    """
    try:
        return call(prompt), prompt, []
    except Exception as exc:  # noqa: BLE001
        if not _is_content_policy(exc):
            raise
        softer, changes = soften_prompt(prompt)
        if not changes:
            log("  prompt was rejected by fal's content filter and nothing matched "
                "the rephrase list — edit the beat's prompt in the studio.")
            raise
        log("  prompt was rejected by fal's content filter; rephrasing: "
            + "; ".join(changes))
        return call(softer), softer, changes


# --- Prompt variant strategies -------------------------------------------------
#
# Three drafts per beat used to mean one prompt rendered at three seeds, so the
# take you picked measured seed luck and carried no information about prompting.
# Generating each draft from a *different* prompt strategy turns the same click
# into a controlled comparison, at the same image count and the same cost, and
# `ledger.py` keeps the score.
#
# Each strategy is a short suffix on the composed prompt. Short is deliberate:
# the avoidance list was already cut from `_generate_nano2` for consuming the
# attention budget, and a long strategy clause would reintroduce that problem
# while confounding the very thing being measured.
#
# Every clause must be palette-safe. `style_medium` can be monochrome or a limited
# mineral palette, and the script stage is explicitly instructed not to introduce
# colours the medium cannot produce; a strategy that named a hue would break that
# rule on every beat it touched. These carry on value, depth and process instead.
PROMPT_VARIANTS = os.environ.get("PROMPT_VARIANTS", "1").lower() not in ("0", "false", "no")

PROMPT_STRATEGIES: dict[str, str] = {
    # The control. Without it there is no way to tell whether embellishment helps
    # at all, only which embellishment wins.
    "baseline": "",
    # Hypothesis: separable depth planes make better parallax. This one is worth
    # more than its win rate suggests, because ~70% of beats are Tier B and a flat
    # image is what makes a depth-warp look like a sliding sticker.
    "depth_staged": (
        " Stage the scene in three clearly separated planes: a defined foreground "
        "element, the subject in the midground, and a distinctly deeper background, "
        "with real spatial separation between them."
    ),
    # Hypothesis: naming the physical process beats naming the style.
    "medium_forward": (
        " Foreground the physical process of the medium itself — the tool marks, the "
        "grain and absorbency of the support, the registration and density of the "
        "pigment — as prominently as the subject."
    ),
    # Hypothesis: the house lighting grammar is better stated than implied.
    "chiaroscuro": (
        " Light it from a single low source so most of the frame falls into deep "
        "shadow, the subject reading as a silhouette edge against the one lit area."
    ),
}


def _beat_ordinal(scene_id: str) -> int:
    digits = "".join(ch for ch in (scene_id or "") if ch.isdigit())
    return int(digits) if digits else 0


def strategies_for(scene_id: str, n: int) -> list[str]:
    """Which strategies fill this beat's n slots, rotated by beat number.

    Rotation is the whole point. `generate_for_shot` auto-selects slot 0 and the
    studio shows it first, so a fixed order would give whichever strategy sits in
    slot 0 both a display advantage and every auto-selection — position bias
    dressed up as a result. Rotating by beat ordinal spreads each strategy evenly
    across every slot, and being derived from the scene_id rather than randomised
    keeps a regenerated beat reproducible.
    """
    names = list(PROMPT_STRATEGIES)
    if not names or n <= 0:
        return ["baseline"] * max(n, 0)
    k = _beat_ordinal(scene_id)
    return [names[(k + i) % len(names)] for i in range(n)]


def apply_strategy(name: str, base_prompt: str) -> str:
    suffix = PROMPT_STRATEGIES.get(name, "")
    return (base_prompt + suffix).strip() if suffix else base_prompt


def generate_for_shot(
    shot: Shot, n: int, backend: str = DEFAULT_BACKEND, lora: dict | None = None,
    render=None, log=print, subject_url: str | None = None,
) -> list[str]:
    """Generate + download n draft variations for one beat; record their paths.

    ``render`` is a ``Storyboard.render`` (RenderConfig) whose knobs override the
    flux-cfg defaults; ``None`` uses the module defaults.

    Unless ``PROMPT_VARIANTS`` is off, the n drafts come from n *different* prompt
    strategies rather than n seeds of one prompt, and every image is written to the
    ledger with the prompt that made it.
    """
    if isinstance(backend, str) and "," in backend:
        backends = [b.strip() for b in backend.split(",") if b.strip()]
        all_rel_paths = []
        for b in backends:
            try:
                paths = generate_for_shot(shot, n, backend=b, lora=lora, render=render,
                                          log=log, subject_url=subject_url)
                all_rel_paths.extend(paths)
            except Exception as e:
                log(f"Error generating for backend {b}: {e}")
        return all_rel_paths

    prompt_driven = (
        backend in ("nano2", "flux-cfg")
        or IMAGE_BACKENDS.get(backend, {}).get("handler") == "generic"
    )
    base_prompt = _compose_prompt(shot)

    import time
    ts = int(time.time())
    batch = str(ts)

    # Each entry: (strategy, prompt_sent, softenings, url)
    made: list[tuple[str, str, list[str], str]] = []

    if PROMPT_VARIANTS and prompt_driven and n > 1:
        # One image per call, each from a different prompt strategy. Same image
        # count and (fal prices per image) the same spend as one n-image call,
        # for more round trips — and it removes a failure mode for free: the
        # endpoints that cap or ignore `num_images` and quietly returned one draft
        # where three were asked for cannot do that when every request asks for one.
        for slot, name in enumerate(strategies_for(shot.scene_id, n)):
            prompt = apply_strategy(name, base_prompt)
            try:
                urls, sent, softened = _generate_urls(
                    shot, prompt, 1, backend, lora, render, log=log,
                    subject_url=subject_url)
            except Exception as exc:  # noqa: BLE001
                # One strategy failing must not cost the beat its other takes.
                # But `continue` alone made the shortfall invisible: the job went
                # on to report success, and this line was the only record that
                # anything had gone wrong -- in memory, gone with the container.
                log(f"  !! variant {slot} ({name}) failed: {exc}")
                try:
                    ledger.record_failure(
                        scene_id=shot.scene_id, strategy=name, backend=backend,
                        batch=batch, slot=slot, error=f"{type(exc).__name__}: {exc}",
                        prompt=prompt, style_medium=(shot.style_medium or ""))
                except Exception as lexc:  # noqa: BLE001 — telemetry never fails a beat
                    log(f"  (could not record the failure to the ledger: {lexc})")
                continue
            for url in urls:
                made.append((name, sent, softened, url))
    else:
        urls, sent, softened = _generate_urls(
            shot, base_prompt, n, backend, lora, render, log=log,
            subject_url=subject_url)
        for url in urls:
            made.append(("baseline", sent, softened, url))

    if n > 1 and len(made) < n:
        log(f"  {shot.scene_id}: {len(made)} of {n} takes — the rest failed or were "
            f"not returned.")

    rel_paths: list[str] = []
    for i, (name, sent, softened, url) in enumerate(made):
        dest = config.assets_dir() / shot.scene_id / f"var_{ts}_{i}.png"
        _download(url, dest)
        rel = config.rel_media_path(dest)
        rel_paths.append(rel)
        ledger.record_generation(
            scene_id=shot.scene_id, path=rel, strategy=name, prompt=base_prompt,
            prompt_final=sent, softened=softened, backend=backend, batch=batch, slot=i,
            style_medium=(shot.style_medium or ""),
            motion_type=getattr(shot.motion_type, "value", str(shot.motion_type or "")),
        )

    existing = list(shot.draft_variations or [])
    new_start_idx = len(existing)
    shot.draft_variations = existing + rel_paths
    if rel_paths:
        # Note for the ledger: this is a placeholder so the studio has something
        # to display, NOT a preference. Only an explicit pick is recorded as a win.
        shot.chosen_variation = new_start_idx
        shot.draft_image = rel_paths[0]
    return rel_paths


def _generate_urls(shot: Shot, prompt: str, n: int, backend: str,
                   lora: dict | None, render, log=print,
                   subject_url: str | None = None) -> tuple[list[str], str, list[str]]:
    """One generation request. Returns (urls, prompt actually sent, softenings).

    Split out of ``generate_for_shot`` so a beat can be produced either as a single
    call for n images or as n single-image calls under different prompt strategies,
    without the backend dispatch existing in two places and drifting.
    """
    if backend == "nano2":
        # Default: Nano Banana 2 (Gemini 3 Pro Image), medium-leading prompt + folded negatives,
        # optionally conditioned on the project's global frame reference.
        negative, _steps, _guidance, _nag = _resolve_render(render)
        frame_url = (getattr(render, "reference_image_url", "") or "").strip() or None
        # A likeness reference wins over the frame reference: Spike A showed text
        # anchors alone produce a different person every take, and there is no
        # point preserving a page border while losing the face.
        return _with_softening(
            lambda p: _generate_nano2(p, n, negative, frame_url,
                                      subject_url=subject_url), prompt, log)
    if backend == "flux-cfg":
        # medium-leading positive prompt + NAG negative prompt (cheaper fallback).
        negative, steps, guidance, nag = _resolve_render(render)
        return _with_softening(
            lambda p: _generate_flux_cfg(p, n, negative, steps, guidance, nag), prompt, log)
    if IMAGE_BACKENDS.get(backend, {}).get("handler") == "generic":
        endpoint = IMAGE_BACKENDS[backend]["endpoint"]

        def _args(p: str, want: int) -> dict:
            a = {"prompt": p, "num_images": want}
            if "ideogram" in backend or "wan" in backend:
                a["aspect_ratio"] = "16:9"
            else:
                a["image_size"] = IMAGE_SIZE
                a["enable_safety_checker"] = False
                # The FLUX pro/ultra family defaults this to 2 (near-strictest)
                # when it is not sent, which rejects ordinary historical
                # documentary subjects. This is the endpoint's own documented
                # output tolerance; prompt moderation is enforced server-side by
                # fal and is not a client setting.
                if "flux" in endpoint:
                    a["safety_tolerance"] = str(OUTPUT_TOLERANCE)
            return a

        sink: dict = {}
        urls = _subscribe_topup(endpoint, _args, prompt, n, log=log, sink=sink)
        return urls, sink.get("final_prompt", prompt), sink.get("softened", [])
    # Legacy paths below build their own prompt (a style reference image, or the
    # trained LoRA trigger) rather than the composed medium-leading one, so the
    # strategy suffix does not apply and they are never given variant prompts.
    if backend == "nano":
        urls = ref_urls([STYLE_REF, *(shot.references or [])])
        if not urls:
            raise RuntimeError(
                "No references resolved — populate references.json with a 'style' entry."
            )
        return _generate_nano(shot.prompt, urls, n), shot.prompt, []
    if lora is None and backend == "flux-lora":
        lora = load_lora()
    styled = style_prompt(shot.prompt, lora)
    return _generate_flux(styled, n, lora), styled, []


def generate_drafts(
    storyboard: Storyboard,
    n: int = DEFAULT_VARIATIONS,
    only: set[str] | None = None,
    limit: int | None = None,
    backend: str = DEFAULT_BACKEND,
    skip_existing: bool = True,
    save_after_each: bool = False,
    save_fn=None,
    log=print,
) -> Storyboard:
    """Generate draft variations for some or all beats. Mutates the storyboard.

    Resilient for long batches: skips beats that already have drafts, tolerates a
    single beat failing, and can persist after each beat. Per-project generation
    knobs are read from ``storyboard.render``.

    ``save_fn`` overrides how the storyboard is persisted between beats — the API
    passes ``save_current_project`` so Firestore stays in step with the JSON, and
    so a batch that dies halfway keeps the beats it already paid for. ``log``
    routes progress into the job log the UI polls.
    """
    config.require_for("assets")
    render = getattr(storyboard, "render", None)
    persist = save_fn or (lambda sb: save(sb))

    shots = storyboard.shots
    if only:
        shots = [s for s in shots if s.scene_id in only]
    if limit:
        shots = shots[:limit]

    pending = [s for s in shots if not (skip_existing and s.draft_variations)]
    log(f"{len(pending)} beat(s) to generate, {len(shots) - len(pending)} already drafted.")

    failures: list[str] = []
    short: list[tuple[str, int]] = []          # beats that came back with fewer takes
    for i, shot in enumerate(shots, start=1):
        if skip_existing and shot.draft_variations:
            log(f"{shot.scene_id}: already has {len(shot.draft_variations)} drafts — skipping.")
            continue
        # Per-episode model with a per-beat override: the storyboard's backend is
        # the default, and a beat only diverges when Claude (or the human) set
        # image_model on it. Previously one backend was applied to every beat, so
        # a per-beat choice in the manifest was silently discarded.
        shot_backend = (getattr(shot, "image_model", None) or "").strip() or backend
        try:
            tag = shot_backend if shot_backend == backend else f"{shot_backend} (override)"
            log(f"[{i}/{len(shots)}] Generating {n} drafts for {shot.scene_id} ({tag}) ...")
            paths = generate_for_shot(shot, n, backend=shot_backend, render=render, log=log)
            log(f"  -> {len(paths)} image(s) for {shot.scene_id}")
            if len(paths) < n:
                short.append((shot.scene_id, len(paths)))
        except Exception as exc:
            log(f"  !! {shot.scene_id} FAILED: {exc}")
            failures.append(shot.scene_id)
        if save_after_each:
            persist(storyboard)

    if failures:
        log(f"Failed beats ({len(failures)}): {failures} — re-run to retry just these.")

    # A beat that returns one take out of three is not a failure by any check this
    # loop makes -- it has drafts, it did not raise, and `skip_existing` will pass
    # over it on the next run. So the batch reported success and 19 of 25 beats
    # silently carried a third of the takes that were asked for. Say it here, at
    # the end, where the number is visible without reading 200 log lines.
    if short:
        got = sum(c for _, c in short)
        log(f"SHORT: {len(short)} of {len(shots)} beat(s) returned fewer than {n} "
            f"takes ({got} images where {len(short) * n} were asked for).")
        for sid, c in short:
            log(f"  {sid}: {c}/{n}")
        log("  Failure rows are in the prompt ledger (event=generate_failed) — "
            "GET /api/prompts/ledger, or ledger.failures().")
    return storyboard


def extract_final_frame(video_path: str | Path, output_image_path: str | Path) -> Path:
    """Extract the final frame from a video segment using OpenCV and save it as an image.

    Used for continuous frame-to-video generation so that subsequent Fal.ai video calls start
    seamlessly from the last frame of the preceding generated segment.
    """
    import cv2
    v_path = Path(video_path)
    o_path = Path(output_image_path)

    if not v_path.exists():
        raise FileNotFoundError(f"Video file not found: {v_path}")

    cap = cv2.VideoCapture(str(v_path))
    if not cap.isOpened():
        raise RuntimeError(f"Failed to open video file with OpenCV: {v_path}")

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total_frames <= 0:
        cap.release()
        raise ValueError(f"Video file has no readable frames: {v_path}")

    cap.set(cv2.CAP_PROP_POS_FRAMES, total_frames - 1)
    ret, frame = cap.read()

    if not ret or frame is None:
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        last_good = None
        while True:
            r, f = cap.read()
            if not r or f is None:
                break
            last_good = f
        frame = last_good

    cap.release()

    if frame is None:
        raise RuntimeError(f"Could not extract final frame from: {v_path}")

    o_path.parent.mkdir(parents=True, exist_ok=True)
    success = cv2.imwrite(str(o_path), frame)
    if not success:
        raise RuntimeError(f"OpenCV failed to write final frame image to: {o_path}")

    print(f"Extracted final frame ({frame.shape[1]}x{frame.shape[0]}) from {v_path.name} -> {o_path.name}")
    return o_path


def _main() -> None:
    parser = argparse.ArgumentParser(description="The Illuminated Bestiary draft-image stage.")
    parser.add_argument("--scene", nargs="*", help="scene id(s) to generate (default: all).")
    parser.add_argument("--variations", type=int, default=DEFAULT_VARIATIONS)
    parser.add_argument("--limit", type=int, default=None, help="cap number of beats.")
    parser.add_argument("--backend", default=DEFAULT_BACKEND, choices=["nano2", "flux-cfg", "nano", "flux", "flux-lora", "flux_2_max", "flux_2_pro", "flux_1_1_pro_ultra", "flux_1_dev_turbo", "ideogram_4", "ideogram_4_instant"])
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

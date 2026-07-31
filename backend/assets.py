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
from pathlib import Path

import fal_client
import requests

from . import config
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


def _generate_nano2(prompt: str, n: int, negative: str = NEGATIVE_PROMPT,
                    frame_url: str | None = None) -> list[str]:
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
    interior = (
        f"{prompt} Render this authentically in the stated historical art medium — "
        f"a real artifact of that tradition, NOT: {negative}."
    )
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
    if isinstance(backend, str) and "," in backend:
        backends = [b.strip() for b in backend.split(",") if b.strip()]
        all_rel_paths = []
        for b in backends:
            try:
                paths = generate_for_shot(shot, n, backend=b, lora=lora, render=render)
                all_rel_paths.extend(paths)
            except Exception as e:
                print(f"Error generating for backend {b}: {e}")
        return all_rel_paths

    if backend == "nano2":
        # Default: Nano Banana 2 (Gemini 3 Pro Image), medium-leading prompt + folded negatives,
        # optionally conditioned on the project's global frame reference.
        negative, _steps, _guidance, _nag = _resolve_render(render)
        frame_url = (getattr(render, "reference_image_url", "") or "").strip() or None
        gen_urls = _generate_nano2(_compose_prompt(shot), n, negative, frame_url)
    elif backend == "flux-cfg":
        # medium-leading positive prompt + NAG negative prompt (cheaper fallback).
        negative, steps, guidance, nag = _resolve_render(render)
        gen_urls = _generate_flux_cfg(_compose_prompt(shot), n, negative, steps, guidance, nag)
    elif backend in ("flux_2_max", "flux_2_pro", "flux_1_1_pro_ultra", "flux_1_dev_turbo", "ideogram_4", "ideogram_4_instant", "wan_2_7_image"):
        endpoints = {
            "flux_2_max": "fal-ai/flux-2-max",
            "flux_2_pro": "fal-ai/flux-2-pro",
            "flux_1_1_pro_ultra": "fal-ai/flux-pro/v1.1-ultra",
            "flux_1_dev_turbo": "fal-ai/flux/dev",
            "ideogram_4": "ideogram/v4",
            "ideogram_4_instant": "ideogram/v4/instant",
            "wan_2_7_image": "fal-ai/wan/v2.7/text-to-image",
        }
        endpoint = endpoints[backend]
        
        args = {
            "prompt": _compose_prompt(shot),
            "num_images": n,
        }
        if "ideogram" in backend or "wan" in backend:
            args["aspect_ratio"] = "16:9"
        else:
            args["image_size"] = IMAGE_SIZE;
            args["enable_safety_checker"] = False
            
        result = fal_client.subscribe(
            endpoint,
            arguments=args,
            with_logs=False,
        )
        gen_urls = [img["url"] for img in result.get("images", [])]
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

    import time
    ts = int(time.time())
    rel_paths: list[str] = []
    for i, url in enumerate(gen_urls):
        dest = config.ASSETS / shot.scene_id / f"var_{ts}_{i}.png"
        _download(url, dest)
        try:
            rel = str(dest.relative_to(config.ROOT)).replace("\\", "/")
        except ValueError:
            rel = str(dest).replace("\\", "/").lstrip("/")
        rel_paths.append(rel)

    existing = list(shot.draft_variations or [])
    new_start_idx = len(existing)
    shot.draft_variations = existing + rel_paths
    if rel_paths:
        shot.chosen_variation = new_start_idx
        shot.draft_image = rel_paths[0]
    return rel_paths


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
    for i, shot in enumerate(shots, start=1):
        if skip_existing and shot.draft_variations:
            log(f"{shot.scene_id}: already has {len(shot.draft_variations)} drafts — skipping.")
            continue
        try:
            log(f"[{i}/{len(shots)}] Generating {n} drafts for {shot.scene_id} ({backend}) ...")
            paths = generate_for_shot(shot, n, backend=backend, render=render)
            log(f"  -> {len(paths)} image(s) for {shot.scene_id}")
        except Exception as exc:
            log(f"  !! {shot.scene_id} FAILED: {exc}")
            failures.append(shot.scene_id)
        if save_after_each:
            persist(storyboard)

    if failures:
        log(f"Failed beats ({len(failures)}): {failures} — re-run to retry just these.")
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


def generate_continuous_video_sequence(
    storyboard: Storyboard,
    out_dir: Path | None = None,
    default_model: str = "fal-ai/kling-video/v3/image-to-video",
) -> list[Path]:
    """Generate a continuous sequence of video segments where each segment begins with
    the exact final frame of the preceding video segment.

    1. Starts with the initial draft image for beat 1.
    2. Calls Fal.ai image-to-video for the current beat.
    3. Saves/downloads the generated video segment .mp4.
    4. Uses OpenCV (`extract_final_frame`) to extract the final frame of that segment.
    5. Feeds the extracted final frame image into the next Fal.ai video call for seamless sequence flow.
    """
    import fal_client
    config.require_for("video")

    if out_dir is None:
        ep_slug = getattr(storyboard, "title", "episode").lower().replace(" ", "_")
        out_dir = config.ROOT / "render" / ep_slug

    out_dir.mkdir(parents=True, exist_ok=True)
    generated_video_paths: list[Path] = []
    previous_final_frame: Path | None = None

    shots = [s for s in storyboard.shots if s.motion_type == MotionType.AI_VIDEO or getattr(s, "flow_hero", False)]
    if not shots:
        print("No AI video / hero beats assigned for video generation.")
        return []

    for idx, shot in enumerate(shots):
        model_key = shot.video_model or default_model
        MAP = {
            "seedance_2_0": "bytedance/seedance-2.0/image-to-video",
            "seedance": "bytedance/seedance-2.0/image-to-video",
            "seedance-2.0": "bytedance/seedance-2.0/image-to-video",
            "seedance-2.0/image-to-video": "bytedance/seedance-2.0/image-to-video",
            "bytedance/seedance-2.0": "bytedance/seedance-2.0/image-to-video",
            "bytedance/seedance-2.0/image-to-video": "bytedance/seedance-2.0/image-to-video",
            "fal-ai/bytedance/seedance-2.0": "bytedance/seedance-2.0/image-to-video",
            "fal-ai/bytedance/seedance-2.0/image-to-video": "bytedance/seedance-2.0/image-to-video",
            "veo_3_1": "fal-ai/veo3.1/image-to-video",
            "kling_2_5_turbo_pro": "fal-ai/kling-video/v3/image-to-video",
            "wan_2_7": "fal-ai/wan/v2.7/image-to-video",
            "hunyuan_video": "fal-ai/hunyuan-video/image-to-video",
            "luma_dream_machine": "fal-ai/luma-dream-machine/ray-2/image-to-video",
        }
        model_endpoint = MAP.get(model_key, model_key)
        if "seedance" in model_endpoint.lower():
            model_endpoint = "bytedance/seedance-2.0/image-to-video"
        elif not model_endpoint.startswith("fal-ai/") and not "/" in model_endpoint[:10]:
            model_endpoint = f"fal-ai/{model_endpoint.lstrip('/')}"
        print(f"\n--- [Continuous Frame-to-Video {idx+1}/{len(shots)}] Beat {shot.scene_id} ({model_endpoint}) ---")

        if previous_final_frame and previous_final_frame.exists():
            starting_image_path = previous_final_frame
            print(f"Chaining from previous beat's final frame: {starting_image_path.name}")
        else:
            found = config.resolve_media(shot.draft_image, shot.scene_id)
            if not found:
                backend = getattr(storyboard.render, "backend", DEFAULT_BACKEND)
                generate_for_shot(shot, n=1, backend=backend, render=storyboard.render)
                shot.draft_image = shot.draft_variations[0]
                found = config.resolve_media(shot.draft_image, shot.scene_id)
            starting_image_path = found

        if not starting_image_path or not starting_image_path.exists():
            raise FileNotFoundError(f"Starting image not found on disk: {shot.draft_image}")

        print(f"Uploading starting frame to Fal.ai: {starting_image_path.name}...")
        public_image_url = fal_client.upload_file(str(starting_image_path))

        target_dur = float(getattr(shot.camera, "duration", 6.0))
        dur_int = max(3, min(10, int(round(target_dur))))

        motion_prompt = shot.motion_prompt or f"Cinematic continuous motion, high-quality, authentic detail, {shot.prompt}"
        if f"{dur_int}s" not in motion_prompt and "second" not in motion_prompt:
            motion_prompt = f"{motion_prompt} (duration: ~{dur_int} seconds)"

        print(f"Triggering Fal.ai video generation ({dur_int}s target): {motion_prompt[:80]}...")
        arguments = {
            "image_url": public_image_url,
            "prompt": motion_prompt,
            "duration": str(dur_int),
        }
        if "veo" in model_endpoint.lower():
            if dur_int <= 5:
                arguments["duration"] = "4s"
            elif dur_int <= 7:
                arguments["duration"] = "6s"
            else:
                arguments["duration"] = "8s"
        elif "seedance" not in model_endpoint.lower():
            arguments.pop("duration", None)

        result = fal_client.subscribe(
            model_endpoint,
            arguments=arguments,
            with_logs=True,
        )

        video_url = result.get("video", {}).get("url") or result.get("file", {}).get("url")
        if not video_url:
            raise RuntimeError(f"No video URL returned from Fal.ai for {shot.scene_id}")

        dest_video_path = out_dir / f"{shot.scene_id}.mp4"
        print(f"Downloading segment video -> {dest_video_path.name}...")
        _download(video_url, dest_video_path)
        generated_video_paths.append(dest_video_path)

        # Extract final frame for the NEXT segment call
        extracted_frame_dest = config.ASSETS / shot.scene_id / f"final_frame_{shot.scene_id}.png"
        previous_final_frame = extract_final_frame(dest_video_path, extracted_frame_dest)

    print(f"\nCompleted continuous frame-to-video sequence: {len(generated_video_paths)} segments rendered.")
    return generated_video_paths


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

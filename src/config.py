"""Central configuration: secret loading and path constants.

All secrets are read from the environment via ``os.environ.get`` — never
hardcoded (see CLAUDE.md). Paths are derived from this file's location so no
absolute path is ever baked into the codebase.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv

# Load a local .env if present. A real .env is never committed (it is gitignored);
# in production the variables may be set in the environment directly.
load_dotenv()

# --- Paths (derived, never hardcoded absolutes) -----------------------------
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
ASSETS = ROOT / "assets"              # generated media cache (gitignored)
AUDIO_POOL = ROOT / "audio_pool"      # curated, owned/licensed music (gitignored)
LORA_TRAINING = ROOT / "lora_training"  # style-LoRA training frames (gitignored)
MANIFEST_PATH = ROOT / "storyboard_manifest.json"
LORA_CONFIG = ROOT / "lora_config.json"  # legacy "DEEPROOTLORE" LoRA pointer (flux-lora fallback)
REFERENCES_DIR = ROOT / "references"     # style + character reference images
REFERENCES_CONFIG = ROOT / "references.json"  # name -> files/urls registry
CHARACTERS_CONFIG = ROOT / "characters.json"  # name -> {description, seed, structural_anchor}
MODELS_DIR = ROOT / "models"             # local ML models (gitignored, large)
# Depth-Anything V2 (ONNX) for the 2.5D depth stage; overridable via env.
DEPTH_MODEL = os.environ.get("DEPTH_MODEL") or str(MODELS_DIR / "depth_anything_v2_vits.onnx")
AUDIO_DIR = ROOT / "audio"               # generated narration + sfx (gitignored)

# ElevenLabs narration voice + model (overridable via env). Default voice: "Adam".
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "pNInz6obpgDQGcFmaJgB")
ELEVENLABS_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")

# Vesper — the channel's narrator persona. Placeholder for the ElevenLabs voice
# binding; set VESPER_VOICE_ID in .env to the chosen voice before the live run.
# Empty string means "not yet bound" (audio falls back to ELEVENLABS_VOICE_ID).
VESPER_VOICE_ID = os.environ.get("VESPER_VOICE_ID", "")

# --- Secrets (fetched natively; presence validated on demand) ---------------
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
FAL_KEY = os.environ.get("FAL_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# Which key each stage needs, for clear failures before an API is ever called.
REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "script": ("ANTHROPIC_API_KEY",),
    "audio": ("ELEVENLABS_API_KEY",),
    "assets": ("FAL_KEY",),
    "video": ("FAL_KEY",),
}


class MissingKeyError(RuntimeError):
    """Raised when a required environment variable is absent."""


def require(*names: str) -> None:
    """Raise :class:`MissingKeyError` if any named env var is unset/empty.

    Call this at the top of a stage before touching its API so failures are
    loud and early rather than deep inside a request.
    """
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise MissingKeyError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". Set them in your .env (see .env.example)."
        )


def require_for(stage: str) -> None:
    """Validate the keys a named pipeline ``stage`` depends on."""
    require(*REQUIRED_KEYS.get(stage, ()))


def ensure_dirs() -> None:
    """Create the local working directories if they do not yet exist."""
    for path in (ASSETS, AUDIO_POOL, LORA_TRAINING):
        path.mkdir(parents=True, exist_ok=True)


RENDER_DIR = ROOT / "render"

# --- Channel bookends: fixed book-open intro + book-close outro (same every ep) ---
INTRO_DIR = ROOT / "intro"
INTRO_CLIP = INTRO_DIR / "intro_v1.mp4"       # book opening (already made)
OUTRO_CLIP = INTRO_DIR / "outro_v1.mp4"       # book closing (make in Veo/Flow -> drop here)
INTRO_VO = INTRO_DIR / "intro_vo.mp3"         # fixed Vesper VO (TTS'd once)
OUTRO_VO = INTRO_DIR / "outro_vo.mp3"
INTRO_MUSIC = INTRO_DIR / "intro_music.mp3"   # optional fixed channel music sting
OUTRO_MUSIC = INTRO_DIR / "outro_music.mp3"
INTRO_FINAL = INTRO_DIR / "intro_final.mp4"   # composited segment, reused every episode
OUTRO_FINAL = INTRO_DIR / "outro_final.mp4"
INTRO_VO_TEXT = ("Every creature in this book was drawn by the people who feared it. "
                 "Open the page, and meet one.")
OUTRO_VO_TEXT = ("The account closes here. But what it describes was never only a story. "
                 "Turn the page again soon.")


def slug(text: str) -> str:
    """Filesystem-safe episode slug derived from a title."""
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_") or "episode"


def episode_paths(title: str) -> dict:
    """Per-episode output dirs, namespaced by the title slug, so two episodes
    never clobber each other's narration / sfx / render clips."""
    s = slug(title)
    return {
        "slug": s,
        "narration": AUDIO_DIR / s / "narration",
        "sfx": AUDIO_DIR / s / "sfx",
        "render": RENDER_DIR / s,
    }

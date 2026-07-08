"""Central configuration: secret loading and path constants.

All secrets are read from the environment via ``os.environ.get`` — never
hardcoded (see CLAUDE.md). Paths are derived from this file's location so no
absolute path is ever baked into the codebase.
"""

from __future__ import annotations

import os
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
LORA_CONFIG = ROOT / "lora_config.json"  # trained "Deep Root Lore" LoRA pointer

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

"""One-time setup tool: train the "Deep Root Lore" style LoRA on fal.

Zips the curated frames in lora_training/, uploads them, runs
fal-ai/flux-lora-fast-training in style mode, and writes the resulting
LoRA pointer to lora_config.json for src/assets.py to use.

This is dev tooling, not part of the pipeline runtime. Run once:
    .venv\\Scripts\\python.exe scripts\\train_lora.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

# Make the repo root importable so `from src import config` works.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fal_client  # noqa: E402

from src import config  # noqa: E402

TRAINING_ENDPOINT = "fal-ai/flux-lora-fast-training"
TRIGGER_WORD = "DEEPROOTLORE"
STEPS = 1000
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

_seen_logs: set[str] = set()


def _collect_images() -> list[Path]:
    """Top-level image files in lora_training/ (excludes subfolders)."""
    return sorted(
        p for p in config.LORA_TRAINING.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )


def _zip_images(images: list[Path]) -> Path:
    tmp = Path(tempfile.gettempdir()) / "deeprootlore_lora_train.zip"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for img in images:
            zf.write(img, arcname=img.name)  # flat, names only
    return tmp


def _on_update(update) -> None:
    """Stream fal's training logs — deduped and crash-proof.

    fal resends the full cumulative log on every poll, so dedupe on message.
    A print must never raise (it once killed the run via UnicodeEncodeError),
    so swallow anything here.
    """
    try:
        if isinstance(update, fal_client.InProgress):
            for log in (update.logs or []):
                msg = log.get("message") if isinstance(log, dict) else str(log)
                if msg and msg not in _seen_logs:
                    _seen_logs.add(msg)
                    print("  [fal]", msg)
    except Exception:
        pass


def main() -> None:
    # Windows consoles default to cp1252, which can't encode fal's Unicode
    # progress glyphs; force UTF-8 so log prints never raise.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    config.require("FAL_KEY")

    images = _collect_images()
    if len(images) < 5:
        raise SystemExit(
            f"Only {len(images)} images in {config.LORA_TRAINING}; need >= 5 to train."
        )
    print(f"Training on {len(images)} frames from {config.LORA_TRAINING}")

    zip_path = _zip_images(images)
    print(f"Zipped -> {zip_path} ({zip_path.stat().st_size // 1024} KB). Uploading...")
    images_data_url = fal_client.upload_file(str(zip_path))
    print("Uploaded. Starting fal training (this takes ~15-20 min)...")

    result = fal_client.subscribe(
        TRAINING_ENDPOINT,
        arguments={
            "images_data_url": images_data_url,
            "trigger_word": TRIGGER_WORD,
            "is_style": True,   # style LoRA: skip subject masking
            "steps": STEPS,
        },
        with_logs=True,
        on_queue_update=_on_update,
    )

    lora_url = result["diffusers_lora_file"]["url"]
    config_url = (result.get("config_file") or {}).get("url")

    cfg = {
        "lora_url": lora_url,
        "config_url": config_url,
        "trigger_word": TRIGGER_WORD,
        "inference_endpoint": "fal-ai/flux-lora",
        "steps": STEPS,
        "image_count": len(images),
        "trained_at": datetime.now().isoformat(timespec="seconds"),
    }
    config.LORA_CONFIG.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

    print("\nDONE. Deep Root Lore LoRA trained.")
    print(f"  trigger word : {TRIGGER_WORD}")
    print(f"  lora_url     : {lora_url}")
    print(f"  saved pointer -> {config.LORA_CONFIG}")


if __name__ == "__main__":
    main()

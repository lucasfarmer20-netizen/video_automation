"""Schema and I/O for ``storyboard_manifest.json`` — the pipeline's state file.

This module defines the strict, lightweight run state and the gate logic that
keeps the pipeline from spending money before a human has signed off. It holds
no API/domain logic — only serialization, validation, and gate checks.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

from . import config

MANIFEST_VERSION = 1


class MotionType(str, Enum):
    """Per-shot motion tier — how a shot is animated, and what it costs.

    ``STATIC`` and ``PARALLAX`` render locally for free; ``AI_VIDEO`` calls a
    paid fal.ai video model and is only reachable once Gate 1 is cleared.
    """

    STATIC = "static"        # Tier A: still + procedural FX (local, $0)
    PARALLAX = "parallax"    # Tier B: 2.5D depth-parallax (local, $0)
    AI_VIDEO = "ai_video"    # Tier C: fal image-to-video (paid, gated)


@dataclass
class Camera:
    """Slow camera move applied to a shot during render."""

    move: str = "push_in"   # push_in | push_out | pan_left | pan_right | static
    duration: float = 6.0   # seconds
    speed: float = 1.0      # per-shot move-speed multiplier (1.5 = 50% faster)


@dataclass
class RenderConfig:
    """Per-project image-generation knobs, editable from the dashboard.

    These override ``assets.py``'s module defaults for this one storyboard. An
    empty ``negative_prompt`` means "use the ``assets.NEGATIVE_PROMPT`` default"
    (kept empty to avoid a manifest <-> assets import cycle).
    """

    guidance_scale: float = 3.5
    real_cfg_scale: float = 4.0
    num_inference_steps: int = 28
    negative_prompt: str = ""       # "" => fall back to assets.NEGATIVE_PROMPT


@dataclass
class Shot:
    """A single storyboard beat."""

    scene_id: str
    narration: str = ""                        # words the narrator speaks over this beat
    prompt: str = ""                           # scene description -> flux draft prompt
    style_medium: str = ""                     # historical art medium leading the image (per culture)
    chosen_variation: int | None = None        # index into generated drafts
    motion_type: MotionType = MotionType.PARALLAX
    camera: Camera = field(default_factory=Camera)
    fx: list[str] = field(default_factory=list)  # visual FX, e.g. ["candle_flicker", "grain"]
    sfx: str = ""                              # ElevenLabs sound-effects prompt (ambience/foley)
    references: list[str] = field(default_factory=list)  # character refs (Nano Banana), style is implicit
    video_model: str | None = None            # set only when motion_type == AI_VIDEO
    flow_hero: bool = False                    # manual VEO/Flow hero (export -> Google Flow -> import bridge)
    audio_anchor: float | None = None         # librosa beat (s) this cut lands on
    draft_variations: list[str] = field(default_factory=list)  # paths to fal draft variations
    draft_image: str | None = None            # path to chosen draft still
    approved: bool = False                    # human sign-off (Gate 1)
    notes: str = ""                           # human continuity/review notes (Gate 1)

    def needs_paid_video(self) -> bool:
        return self.motion_type == MotionType.AI_VIDEO


@dataclass
class Storyboard:
    """Full run state, persisted to ``storyboard_manifest.json``."""

    version: int = MANIFEST_VERSION
    title: str = ""
    cultural_origin: str = ""         # entity's real ethnography; drives each shot's style_medium
    script_locked: bool = False       # Script gate
    storyboard_approved: bool = False  # Gate 1 (human pressed "approve")
    music_track: str | None = None    # selected file from audio_pool/
    render: RenderConfig = field(default_factory=RenderConfig)  # dashboard-editable gen knobs
    shots: list[Shot] = field(default_factory=list)

    # --- Gate logic ---------------------------------------------------------
    def gate_cleared(self) -> bool:
        """True only when it is safe to call a paid video API.

        Requires the storyboard to be approved and every Tier-C shot to be both
        approved and assigned a concrete video model. Mirrors the hard human
        gate enforced in ``pipeline.py``.
        """
        if not self.storyboard_approved:
            return False
        return all(
            s.approved and bool(s.video_model)
            for s in self.shots
            if s.needs_paid_video()
        )

    def paid_shots(self) -> list[Shot]:
        return [s for s in self.shots if s.needs_paid_video()]

    # --- Serialization ------------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Storyboard":
        shots = [
            Shot(
                **{
                    **shot,
                    "motion_type": MotionType(shot.get("motion_type", "parallax")),
                    "camera": Camera(**shot["camera"])
                    if isinstance(shot.get("camera"), dict)
                    else Camera(),
                }
            )
            for shot in data.get("shots", [])
        ]
        raw_render = data.get("render") or {}
        render = RenderConfig(
            **{k: raw_render[k] for k in raw_render if k in RenderConfig.__dataclass_fields__}
        )
        return cls(
            version=data.get("version", MANIFEST_VERSION),
            title=data.get("title", ""),
            cultural_origin=data.get("cultural_origin", ""),
            script_locked=data.get("script_locked", False),
            storyboard_approved=data.get("storyboard_approved", False),
            music_track=data.get("music_track"),
            render=render,
            shots=shots,
        )


def load(path: Path | None = None) -> Storyboard:
    """Load the manifest. An empty/absent file yields a fresh Storyboard."""
    path = path or config.MANIFEST_PATH
    if not path.exists():
        return Storyboard()
    text = path.read_text(encoding="utf-8").strip()
    if not text or text == "{}":
        return Storyboard()
    return Storyboard.from_dict(json.loads(text))


def save(storyboard: Storyboard, path: Path | None = None) -> None:
    """Persist the manifest atomically as pretty JSON."""
    path = path or config.MANIFEST_PATH
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(storyboard.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)

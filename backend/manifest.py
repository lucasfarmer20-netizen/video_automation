"""Dataclasses and Firestore interface for storyboard project metadata and shots.

Preserves the original manifest structure while storing project state in Firestore.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, List, Optional
# Firestore uses Application Default Credentials: the attached service account on
# Cloud Run, or GOOGLE_APPLICATION_CREDENTIALS locally. Never hardcode a key
# filename here — the Dockerfile does `COPY . .`, so a key sitting in the repo
# gets baked into the published image.
try:
    from google.cloud import firestore

    db = firestore.Client()
except Exception as exc:  # noqa: BLE001 — the app still runs on local JSON manifests
    print(f"Firestore unavailable ({exc.__class__.__name__}: {exc}); using local manifests.")
    db = None

MANIFEST_VERSION = 1


class MotionType(str, Enum):
    STATIC = "static"
    PARALLAX = "parallax"
    AI_VIDEO = "ai_video"


@dataclass
class Camera:
    move: str = "push_in"
    duration: float = 6.0
    speed: float = 1.0


@dataclass
class RenderConfig:
    backend: str = "nano2"
    video_model: str = "seedance_2_0"
    video_chaining: str = "native_extend"
    video_audio: bool = True
    guidance_scale: float = 3.5
    nag_scale: float = 5.0
    num_inference_steps: int = 28
    negative_prompt: str = ""
    reference_image: str = ""
    reference_image_url: str = ""


@dataclass
class Shot:
    scene_id: str
    narration: str = ""
    prompt: str = ""
    style_medium: str = ""
    motion_prompt: str = ""
    chosen_variation: Optional[int] = None
    motion_type: MotionType = MotionType.PARALLAX
    camera: Camera = field(default_factory=Camera)
    fx: List[str] = field(default_factory=list)
    sfx: str = ""
    references: List[str] = field(default_factory=list)
    video_model: Optional[str] = None
    video_audio: Optional[bool] = None
    image_model: Optional[str] = None
    flow_hero: bool = False
    hero_clip: bool = False
    audio_anchor: Optional[float] = None
    draft_variations: List[str] = field(default_factory=list)
    draft_image: Optional[str] = None
    video_variations: List[str] = field(default_factory=list)
    video_clip: Optional[str] = None
    approved: bool = False
    notes: str = ""

    def needs_paid_video(self) -> bool:
        return self.motion_type == MotionType.AI_VIDEO


@dataclass
class Storyboard:
    id: str = ""
    version: int = MANIFEST_VERSION
    title: str = ""
    channel: str = "bestiary"
    cultural_origin: str = ""
    script_locked: bool = False
    storyboard_approved: bool = False
    music_track: Optional[str] = None
    # ElevenLabs voice for this episode's narration. Persisted here because the
    # manifest is the only store that survives a restart -- /api/voice/settings
    # used to assign to a module global in config, so a designed Vesper voice was
    # silently lost on the next cold start and narration fell back to the stock
    # default. Empty means "use VESPER_VOICE_ID / ELEVENLABS_VOICE_ID".
    voice_id: str = ""
    render: RenderConfig = field(default_factory=RenderConfig)
    shots: List[Shot] = field(default_factory=list)

    def gate_cleared(self) -> bool:
        if not self.storyboard_approved:
            return False
        return all(
            s.approved and bool(s.video_model)
            for s in self.shots
            if s.needs_paid_video()
        )

    def paid_shots(self) -> list[Shot]:
        return [s for s in self.shots if s.needs_paid_video()]

    def to_dict(self) -> dict:
        d = asdict(self)
        # Convert Enum to string
        if "shots" in d:
            for s in d["shots"]:
                if isinstance(s.get("motion_type"), MotionType):
                    s["motion_type"] = s["motion_type"].value
        return d

    @classmethod
    def from_dict(cls, project_id: str, data: dict, shots_list: List[dict] = None) -> Storyboard:
        shots = []
        if shots_list:
            known = set(Shot.__dataclass_fields__)
            for shot in shots_list:
                # Drop unknown keys rather than raising. Shot(**shot) used to
                # explode on any extra field, and because get_current_project
                # catches that and falls through to "create a fresh project",
                # one stray key was enough to overwrite a whole storyboard with
                # an empty one. RenderConfig already filters this way.
                fields = {k: v for k, v in shot.items() if k in known}
                extra = set(shot) - known
                if extra:
                    print(f"manifest: ignoring unknown shot field(s) {sorted(extra)} on {shot.get('scene_id')}")
                fields["motion_type"] = MotionType(shot.get("motion_type", "parallax"))
                fields["camera"] = (
                    Camera(**shot["camera"]) if isinstance(shot.get("camera"), dict) else Camera()
                )
                shots.append(Shot(**fields))
        raw_render = data.get("render") or {}
        render = RenderConfig(
            **{k: raw_render[k] for k in raw_render if k in RenderConfig.__dataclass_fields__}
        )
        return cls(
            id=project_id,
            version=data.get("version", MANIFEST_VERSION),
            title=data.get("title", ""),
            channel=data.get("channel", "bestiary"),
            cultural_origin=data.get("cultural_origin", ""),
            script_locked=data.get("script_locked", False),
            storyboard_approved=data.get("storyboard_approved", False),
            music_track=data.get("music_track"),
            voice_id=data.get("voice_id", "") or "",
            render=render,
            shots=shots,
        )


def load_project(project_id: str) -> Optional[Storyboard]:
    """Load a storyboard manifest and its shots from Firestore."""
    p_ref = db.collection("projects").document(project_id)
    p_doc = p_ref.get()
    if not p_doc.exists:
        return None
    
    # Load shots
    shots_ref = p_ref.collection("beats").order_by("scene_id").stream()
    shots_list = [shot.to_dict() for shot in shots_ref]
    
    return Storyboard.from_dict(project_id, p_doc.to_dict(), shots_list)


def save_project(sb: Storyboard) -> None:
    """Save storyboard manifest and all its shots to Firestore."""
    if not sb.id:
        raise ValueError("Storyboard ID is required to save to Firestore")
    
    p_ref = db.collection("projects").document(sb.id)
    
    # Save main project metadata
    data = sb.to_dict()
    shots_data = data.pop("shots", [])
    p_ref.set(data)
    
    # Save shots to subcollection
    batch = db.batch()
    for shot in shots_data:
        s_id = shot.get("scene_id")
        s_ref = p_ref.collection("beats").document(s_id)
        batch.set(s_ref, shot)
    batch.commit()


def list_projects(channel: Optional[str] = None) -> List[dict]:
    """List all projects in Firestore, optionally filtered by channel."""
    query = db.collection("projects")
    if channel:
        query = query.where("channel", "==", channel)
    
    docs = query.stream()
    res = []
    for doc in docs:
        d = doc.to_dict()
        d["id"] = doc.id
        
        # Count beats
        try:
            beats_stream = db.collection("projects").document(doc.id).collection("beats").stream()
            d["beats_count"] = sum(1 for _ in beats_stream)
        except Exception:
            d["beats_count"] = 0
            
        res.append(d)
    return res


def load(path: Path | None = None) -> Storyboard:
    """Load the manifest from a local JSON path.

    ``path`` defaults to the active manifest (``config.MANIFEST_PATH``) so the
    module CLIs and every ``load()`` call site keep working. An empty or absent
    file yields a fresh Storyboard.
    """
    import json
    from . import config

    path = Path(path) if path is not None else config.MANIFEST_PATH
    if not path.exists():
        return Storyboard()
    text = path.read_text(encoding="utf-8").strip()
    if not text or text == "{}":
        return Storyboard()
    
    # Parse the json data
    data = json.loads(text)
    # Derive a project ID from the path
    project_id = "".join([c if c.isalnum() else "_" for c in str(path).replace("\\", "/").strip("/")])
    
    # Load shots list from data["shots"] if present
    shots_list = data.get("shots", [])
    
    return Storyboard.from_dict(project_id, data, shots_list)


def save(storyboard: Storyboard, path: Path | None = None) -> None:
    """Persist the manifest atomically as pretty JSON on local disk.

    ``path`` defaults to the active manifest (``config.MANIFEST_PATH``).
    """
    import json
    from . import config

    path = Path(path) if path is not None else config.MANIFEST_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    
    # Convert storyboard to dict
    sb_dict = storyboard.to_dict()
    # Ensure shots list is inside the dictionary so it matches the original format exactly
    sb_dict["shots"] = [asdict(s) for s in storyboard.shots]
    # Convert Enums in shots
    for s in sb_dict["shots"]:
        if isinstance(s.get("motion_type"), MotionType):
            s["motion_type"] = s["motion_type"].value
        elif isinstance(s.get("motion_type"), str):
            pass # already string
            
    tmp.write_text(
        json.dumps(sb_dict, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


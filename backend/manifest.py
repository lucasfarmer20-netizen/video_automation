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
    # Total travel for the move across the whole beat, as a fraction (0.15 = a
    # push from 100% to 115% scale). 0 means "auto": motion.camera_amounts()
    # derives it from `duration` so long and short beats drift at the same rate.
    amount: float = 0.0
    # Hold this beat's duration against audio.sync_durations(), which otherwise
    # refits every beat to its narration length. Without it, trimming a beat in
    # the studio and then re-running narration silently discards the trim.
    duration_locked: bool = False


@dataclass
class AudioLayer:
    """One audio clip under a beat.

    Replaces the old one-prompt-one-file-per-beat model, which could not express
    an offset, a fade, an uploaded file, or more than one sound. A layer is
    positioned relative to its beat's start and may cross the boundary in either
    direction -- a negative offset starts it under the previous shot, which is
    how you set a mood before the cut lands.
    """
    id: str = ""                 # stable key; file lives at audio/sfx/<scene>__<id>.mp3
    prompt: str = ""             # empty when the file was uploaded rather than generated
    file: str = ""               # media-root-relative; empty until generated/uploaded
    source: str = "generated"    # "generated" | "uploaded"
    gain: float = 1.0
    offset: float = 0.0          # seconds from the beat start; negative starts early
    fade_in: float = 0.0
    fade_out: float = 0.0
    label: str = ""
    # Tile this layer to cover its beat. ``None`` means decide by source:
    # generated layers come from an environmental prompt (room tone, weather,
    # fire) and are beds, so they loop; an uploaded file may be a one-shot and is
    # left alone. Set explicitly to override either way.
    loop: Optional[bool] = None


@dataclass
class Grade:
    """Look controls applied to the base plate before any motion.

    One set of names, used at two scopes: ``Storyboard.grade`` is the episode
    default and ``Shot.grade`` is a *sparse* override of the same keys. That is
    deliberate — the alternative (separate per-shot field names) is how you end
    up with several controls that mean the same thing and no defined precedence.

    ``key_light`` is only honest on photographic plates. On an illustrated
    medium the light is painted into the artwork and the depth map is nearly
    featureless, so shading it does almost nothing; measured on a real Bestiary
    plate, a key light pushed to double strength barely read at all. Defaults
    are therefore set per channel — see DEFAULT_GRADES.
    """
    brightness: float = 0.0        # exposure in stops; 0 = unchanged
    contrast: float = 0.0          # -1..1 around mid-grey
    temperature: int = 5600        # Kelvin. LOW = warm/orange, high = cool/blue
    saturation: float = 1.0
    rim_light: float = 0.0         # 0..1 edge glow from depth discontinuities
    key_light: str = ""            # "" | left | right | top | front
    key_intensity: float = 0.0     # 0..1


# Bestiary plates are woodblock/manuscript media with the light already painted
# in; Calluses plates are photographic and have real depth structure to shade.
DEFAULT_GRADES: dict[str, dict] = {
    "bestiary": {"rim_light": 0.0, "key_light": "", "key_intensity": 0.0},
    "calluses": {"rim_light": 0.22, "key_light": "right", "key_intensity": 0.55,
                 "temperature": 4200},
}


@dataclass
class MotionConfig:
    """Per-episode parallax defaults. Every beat inherits these; a beat overrides
    with ``Camera.speed`` (multiplier) or ``Camera.amount`` (absolute travel).

    Rates are travel *per second*, not per beat — that is the whole point. Fixed
    per-beat totals looked fine at 6s and vanished at 24s, because what the eye
    reads as motion is the rate. ``zoom_max``/``pan_max`` stop a very long beat
    from pushing so far the framing falls apart.
    """
    speed: float = 1.0          # project-wide multiplier on both rates
    zoom_rate: float = 0.011    # extra magnification per second
    pan_rate: float = 0.009     # fraction of frame width per second
    zoom_max: float = 0.22      # ceiling on rate-derived zoom travel
    pan_max: float = 0.18


@dataclass
class MixConfig:
    """Per-episode audio mix levels (linear gain, 1.0 = unity).

    Defaults put SFX and the music bed where documentary ambience normally sits
    (roughly -16 dB under narration). The old hardcoded 0.55 was only ~5 dB down,
    which is why foley sat on top of Vesper instead of behind her.
    """
    narration: float = 1.0
    sfx: float = 0.15
    music: float = 0.20
    # Bus mutes, and solo. Solo wins over mute: soloing SFX silences the others
    # whatever their mute state, which is what every mixer does and what makes
    # solo useful for checking whether ambience carries a beat on its own.
    mute_narration: bool = False
    mute_sfx: bool = False
    mute_music: bool = False
    solo: str = ""              # "" | narration | sfx | music


@dataclass
class RenderConfig:
    backend: str = "nano2"
    # Draft takes per beat. Set by the script stage's budget plan when a budget is
    # given, otherwise the default 3. Lives here rather than as a call-site literal
    # because five separate places used to hardcode n=3, so raising it meant finding
    # all five. Capped in practice by the number of prompt strategies.
    variations: int = 3
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
    # Sparse override of Storyboard.grade -- only the keys set here differ from
    # the episode default. Kept as a plain dict so adding a look control never
    # needs a manifest migration.
    grade: dict = field(default_factory=dict)
    # Per-beat trim on top of the episode mix bus (MixConfig), 1.0 = no change.
    # The episode fader sets the bus level; these seat individual beats against
    # it. Necessary because the raw stems vary ~13 dB between beats (fal's
    # stable-audio is inconsistent), which one master fader cannot fix.
    gain_narration: float = 1.0
    gain_sfx: float = 1.0
    # Narration placement, same semantics as an AudioLayer.
    offset_narration: float = 0.0
    fade_in_narration: float = 0.0
    fade_out_narration: float = 0.0
    # Layered ambience. Empty means "fall back to the legacy single sfx file",
    # so existing manifests keep working untouched -- see resolve_sfx_layers().
    sfx_layers: List[AudioLayer] = field(default_factory=list)
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
    # Score direction for this episode, written by the script stage and used as
    # the default prompt when generating a bed. Kept separate from music_track
    # (the chosen file) so regenerating never loses the creative intent.
    music_prompt: str = ""
    # ElevenLabs voice for this episode's narration. Persisted here because the
    # manifest is the only store that survives a restart -- /api/voice/settings
    # used to assign to a module global in config, so a designed Vesper voice was
    # silently lost on the next cold start and narration fell back to the stock
    # default. Empty means "use VESPER_VOICE_ID / ELEVENLABS_VOICE_ID".
    voice_id: str = ""
    render: RenderConfig = field(default_factory=RenderConfig)
    motion: MotionConfig = field(default_factory=MotionConfig)
    grade: Grade = field(default_factory=Grade)
    mix: MixConfig = field(default_factory=MixConfig)
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
                fields["sfx_layers"] = [
                    AudioLayer(**{k: v for k, v in (lay or {}).items()
                                  if k in AudioLayer.__dataclass_fields__})
                    for lay in (shot.get("sfx_layers") or [])
                ]
                fields["motion_type"] = MotionType(shot.get("motion_type", "parallax"))
                # Filter camera keys for the same reason as the shot keys above:
                # one unrecognised field must never cost the whole storyboard.
                raw_cam = shot.get("camera")
                fields["camera"] = (
                    Camera(**{k: v for k, v in raw_cam.items()
                              if k in Camera.__dataclass_fields__})
                    if isinstance(raw_cam, dict) else Camera()
                )
                shots.append(Shot(**fields))
        raw_render = data.get("render") or {}
        render = RenderConfig(
            **{k: raw_render[k] for k in raw_render if k in RenderConfig.__dataclass_fields__}
        )
        raw_mix = data.get("mix") or {}
        mix = MixConfig(
            **{k: raw_mix[k] for k in raw_mix if k in MixConfig.__dataclass_fields__}
        )
        raw_grade = data.get("grade") or {}
        grade = Grade(**{k: raw_grade[k] for k in raw_grade
                         if k in Grade.__dataclass_fields__})
        raw_motion = data.get("motion") or {}
        motion_cfg = MotionConfig(
            **{k: raw_motion[k] for k in raw_motion if k in MotionConfig.__dataclass_fields__}
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
            music_prompt=data.get("music_prompt", "") or "",
            voice_id=data.get("voice_id", "") or "",
            render=render,
            motion=motion_cfg,
            grade=grade,
            mix=mix,
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


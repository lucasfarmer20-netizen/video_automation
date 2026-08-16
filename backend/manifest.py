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


class StorageUnavailable(RuntimeError):
    """The durable store is configured but could not answer. NOT "no record".

    Two answers used to arrive at the caller as the same thing -- an exception it
    swallowed, or ``None``:

    * **not found**: the store answered, and this project has no document there.
      Legitimate, common, and correctly handled by reading the JSON manifest.
    * **unavailable**: the store did not answer at all -- unreachable backend,
      missing database, denied credentials, a stream that died halfway through
      the beats.

    Only the first is evidence about the project. The second is evidence about
    the infrastructure, and treating it as the first is how a caller comes to
    serve whatever happens to be on local disk as though it were authoritative.
    On Cloud Run that disk is frequently ``/app`` -- the repo baked into the
    image by ``COPY . .`` -- which is ephemeral, so the substituted state
    disappears at the next cold start with nothing having reported a problem.

    Named for :class:`backend.generation.LedgerUnreadable`, and for the same
    reason: "present but unreadable" and "not there" are different facts, and a
    plausible value in place of an unavailable one is worse than an error,
    because nobody can tell it is wrong.

    ``db is None`` is deliberately NOT this error. That is decided once, at
    import, and means Firestore was never configured for this process -- the
    local JSON manifest is then the store of record rather than a fallback.
    """


class MotionType(str, Enum):
    STATIC = "static"
    PARALLAX = "parallax"
    AI_VIDEO = "ai_video"


def approval_is_explicit(value: object) -> bool:
    """Is ``value`` an approval a human actually gave? Contract §5.4.

    ``value is True``, and nothing else. Not truthiness -- truthiness is the
    defect this replaces. :meth:`Storyboard.gate_cleared` read ``s.approved``
    for its truth value, and every non-empty string is truthy, so a Tier-C beat
    carrying ``approved: "no"`` -- or ``"false"``, or ``"pending"`` -- cleared
    Gate 1 and reached the paid video API. The string said no; Python said yes.

    ``1`` fails too, and deliberately, and the reason is not pedantry about
    ``1 == True``. It is that a field holding ``1`` was written by something
    which did not know the field is a boolean, and nothing here can tell what
    else that writer got wrong -- the same beat's ``video_model`` came out of
    the same hand. ``"true"`` fails for the identical reason: a serialiser
    loose enough to stringify a boolean is not a serialiser whose other fields
    have been checked. Gate 1 exists so that nobody has to guess.

    The direction of error is the whole design, and it is not symmetric. A beat
    wrongly shown as unapproved costs one click. A beat wrongly treated as
    approved costs money on work nobody sanctioned. So everything ambiguous
    resolves to NOT approved.

    Named after :func:`backend.director.approval_is_current`, which asks the
    other half of §5.4 -- that one asks whether an approval still covers the
    plan it was given for, this one asks whether there is an approval at all.
    """
    return value is True


def _explicit_approval_flag(raw: object, field: str, where: str) -> bool:
    """One approval flag read off untrusted JSON, plus a note when it wasn't one.

    Degrades to ``False`` rather than raising, for the reason the unknown-key
    filter in :meth:`Storyboard.from_dict` exists: a ``from_dict`` that raises
    takes the project offline, because ``get_current_project``
    (backend/main.py:280-294) answers a manifest that exists but will not parse
    with HTTP 500 rather than overwriting it. That trade is only acceptable
    because this degrade runs toward the safe side -- unlike the ledger's
    ``_SHAPE``/``_valid`` gate (backend/generation.py:135-191), which raises,
    because a spend total silently computed over a bad row is a wrong bill and
    there is no safe direction to fall in.

    Absent and ``null`` degrade silently. Neither is a mistyped value; both mean
    "no decision recorded", which is what the ``False`` default already says. A
    wrong *type* is worth saying out loud -- the same rule, for the same reason,
    as the ``sfx_layers`` note below: a note that fires on the idiom the loader
    accepts everywhere else teaches people to ignore notes.

    Note what this does NOT do: it never spells out what an approval is. It asks
    :func:`approval_is_explicit` and believes the answer. An earlier revision
    wrote ``return raw is True`` here, which is the same rule and was the
    problem -- the mutation harness weakened ``approval_is_explicit`` to
    ``bool(value)`` and this function went on refusing correctly, because it was
    no longer consulting it in any way that mattered. Two copies of a rule are
    two rules, and the second one is the one nobody remembers to change.
    """
    if approval_is_explicit(raw):
        return True
    if raw is False or raw is None:
        return False
    print(f"manifest: {field}={raw!r} on {where} is {type(raw).__name__}, not a "
          f"boolean; reading it as NOT approved (§5.4). Re-approve to restore "
          f"it -- Gate 1 will not spend on a value nobody checked.")
    return False


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
    # Display name for the narrator, shown wherever the UI names the voice.
    # Empty means "use config.DEFAULT_NARRATOR_NAME" (Vesper), so existing
    # projects need no migration and adding this changes nothing for them.
    # Resolve it through narrator_name() rather than reading the field directly.
    narrator_name: str = ""
    # Narrator profile key (see backend/casting.py). Empty means "use whatever
    # this episode already does" -- voice_id, then the env defaults -- so adding
    # profiles changes nothing for existing projects.
    vo_profile: str = ""
    render: RenderConfig = field(default_factory=RenderConfig)
    motion: MotionConfig = field(default_factory=MotionConfig)
    grade: Grade = field(default_factory=Grade)
    mix: MixConfig = field(default_factory=MixConfig)
    shots: List[Shot] = field(default_factory=list)

    def gate_cleared(self) -> bool:
        """Gate 1: may the pipeline call a paid video API? Contract §5.4.

        Both approval terms go through :func:`approval_is_explicit`, and this is
        the last line of defence rather than the only one -- ``from_dict``
        already refuses to build a shot around a non-boolean ``approved``, so a
        manifest loaded from disk or Firestore has been normalised before it
        reaches here.

        Asserting it a second time is the shape backend/main.py:3129-3139 uses
        for the compile route, and it is load-bearing for the same reason: not
        every Storyboard comes through ``from_dict``. ``/api/approve`` sets
        ``s.approved`` on a live object (backend/main.py:4315), pipeline stages
        build shots in memory, and any endpoint added later can assign the field
        directly. Those never pass the boundary check, so this is the only check
        they get -- and a gate that trusts its input is one edit away from being
        bypassed.

        ``video_model`` keeps its ``bool()`` on purpose. That term asks whether
        a model was *chosen*, so an empty string and ``None`` are the same
        answer; it is not a permission, and nothing is authorised by it.
        """
        if not approval_is_explicit(self.storyboard_approved):
            return False
        return all(
            approval_is_explicit(s.approved) and bool(s.video_model)
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
                # explode on any extra field, and a from_dict that raises takes
                # the project offline: get_current_project (backend/main.py:280-
                # 294) refuses to overwrite a manifest that exists but will not
                # parse, and raises HTTP 500 instead. That refusal is correct --
                # it replaced an older branch that answered a failed load by
                # seeding a fresh empty Storyboard over the top -- but it means
                # one stray key is a hard 500 on every read until a human
                # intervenes. RenderConfig already filters this way.
                fields = {k: v for k, v in shot.items() if k in known}
                extra = set(shot) - known
                if extra:
                    print(f"manifest: ignoring unknown shot field(s) {sorted(extra)} on {shot.get('scene_id')}")
                scene = shot.get("scene_id")
                # Same rule as the shot keys, applied to the nested layers: an
                # unreadable one is dropped, loudly, and the rest of the shot
                # stands. Dropping has to be a real drop. Rebuilding an
                # unreadable layer as a default AudioLayer() leaves a phantom --
                # no file, no prompt -- and resolve_sfx_layers only asks whether
                # the list is non-empty, so the phantom suppresses the legacy
                # <scene>.mp3 / sfx-prompt fallback beneath it and the beat loses
                # ambience it already had, with nothing raised and nothing
                # logged. That is worse than the exception it replaces, so
                # "cannot be read" must reach an empty list, not a placeholder.
                #
                # `is not None` rather than a truthiness test, which would let
                # `{}`, `""` and `0` past the guard and drop them to [] with no
                # note -- the silent failure this exists to remove, reintroduced
                # one level up. `is not None` rather than `"sfx_layers" in shot`
                # because an explicit null is JSON for "no layers", not a
                # mistyped value: every other nested field here reads
                # `data.get(...) or {}` and coerces null to empty without
                # comment, and a note that fires on the idiom the loader accepts
                # everywhere else teaches people to ignore notes. A wrong *type*
                # is worth saying out loud; "nothing" is not.
                raw_layers = shot.get("sfx_layers")
                if raw_layers is not None and not isinstance(raw_layers, (list, tuple)):
                    print(f"manifest: dropping sfx_layers on {scene}: expected a "
                          f"list of layers, got {type(raw_layers).__name__}")
                    raw_layers = ()
                layers = []
                for lay in (raw_layers or ()):
                    readable = ({k: v for k, v in lay.items()
                                 if k in AudioLayer.__dataclass_fields__}
                                if isinstance(lay, dict) else {})
                    # A mapping with no recognised key at all -- `{}`, or nothing
                    # but unknown keys -- rebuilds as that same phantom, so it
                    # drops too. A mapping with ANY recognised key is kept,
                    # however thin, and that is load-bearing rather than
                    # cautious: POST /api/shot/{scene_id}/layers
                    # (add_or_update_layer, backend/main.py:4622; the append is
                    # at 4645-4648) creates exactly a fileless, promptless
                    # AudioLayer(id=...), because in the studio a layer exists
                    # before its audio is generated. A loader that
                    # dropped layers for having no file would delete the layer
                    # the UI had just created, on the very next read.
                    if not readable:
                        print(f"manifest: dropping unreadable sfx layer {lay!r} on {scene}")
                        continue
                    layers.append(AudioLayer(**readable))
                fields["sfx_layers"] = layers
                # An unrecognised tier degrades instead of raising, for the same
                # reason the keys are filtered. PARALLAX is the fallback because
                # it is the free local tier: a value nobody recognises must move
                # the beat away from spend, never toward the paid video API.
                #
                # Know that this degrade is not only logged, it is PERSISTED.
                # from_dict feeds load(), and anything that loads then saves --
                # the Firestore bootstrap at backend/main.py:88-94 on a project's
                # first sync, or any PATCH endpoint afterwards -- writes the
                # substituted tier back. A manifest carrying a tier this build
                # does not recognise is rewritten to parallax, with one line of
                # stdout as the whole audit trail. Preserving the raw string
                # instead would be worse: the value would sit in the manifest
                # unreadable by anything, and Gate 1 could not reason about it.
                # This is a known cost, not an oversight.
                # Approval is a boolean or it is not an approval (§5.4). This is
                # the tier rule below applied to the other half of Gate 1, and
                # for the same stated reason: a value nobody checked must move
                # the beat AWAY from spend, never toward the paid video API.
                # Before this, a hand-edited or loosely-serialised
                # `approved: "no"` reached gate_cleared() intact and passed its
                # truthiness test, so the beat that said no was billed.
                #
                # Know that this degrade, like the tier's, is PERSISTED -- the
                # Firestore bootstrap at backend/main.py:88-94, or any PATCH
                # afterwards, writes `false` back over the unreadable value. That
                # is intended here rather than merely tolerated: what the file
                # says afterwards is what the gate believes, instead of a value
                # the gate has to keep re-deciding about. The alternative --
                # coercing "no" to True so the beat still looks approved -- is
                # the original defect with a migration's name on it.
                fields["approved"] = _explicit_approval_flag(
                    shot.get("approved"), "approved", f"beat {scene}")
                raw_tier = shot.get("motion_type", "parallax")
                try:
                    fields["motion_type"] = MotionType(raw_tier)
                except ValueError:
                    print(f"manifest: unrecognised motion_type {raw_tier!r} on "
                          f"{scene}; falling back to parallax")
                    fields["motion_type"] = MotionType.PARALLAX
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
            # The project-level half of §5.4, normalised for the same reason as
            # the per-beat half: gate_cleared() short-circuits on this field, so
            # `storyboard_approved: "no"` used to satisfy `if not ...` and hand
            # the decision straight to the per-shot loop.
            storyboard_approved=_explicit_approval_flag(
                data.get("storyboard_approved"), "storyboard_approved",
                f"project {project_id!r}"),
            music_track=data.get("music_track"),
            music_prompt=data.get("music_prompt", "") or "",
            voice_id=data.get("voice_id", "") or "",
            narrator_name=data.get("narrator_name", "") or "",
            vo_profile=data.get("vo_profile", "") or "",
            render=render,
            motion=motion_cfg,
            grade=grade,
            mix=mix,
            shots=shots,
        )


def narrator_name(sb: Optional[Storyboard]) -> str:
    """The narrator's display name for this project.

    Never inline a narrator into UI copy or a prompt -- call this. The product
    must not assume a narrator (contract §4); this channel happens to configure
    one (CLAUDE.md), and the difference lives entirely in this function.
    """
    from . import config
    name = (getattr(sb, "narrator_name", "") or "").strip() if sb else ""
    return name or config.DEFAULT_NARRATOR_NAME


def load_project(project_id: str) -> Optional[Storyboard]:
    """Load a storyboard manifest and its shots from Firestore.

    Three outcomes, and they are three, not two:

    * a :class:`Storyboard` -- the store answered and holds this project;
    * ``None`` -- the store answered and holds no document for this project, or
      Firestore is not configured for this process at all;
    * :class:`StorageUnavailable` -- the store could not answer.

    Every caller used to see the third as the second (via its own
    ``except Exception``), which is the whole defect: "there is no record" and
    "I could not look" lead to different correct behaviour, and only the first
    of them justifies reading local disk instead.
    """
    if db is None:
        # Firestore was never configured here -- decided at import above, and
        # printed there once rather than on every read. Local JSON is the store
        # of record in that mode (the documented local-development path), so
        # this is "no document here", not "the store is down".
        return None

    try:
        p_ref = db.collection("projects").document(project_id)
        p_doc = p_ref.get()
    except Exception as exc:  # noqa: BLE001 -- classified, not swallowed
        # Deliberately broad, and deliberately NOT a fall-through. google.cloud
        # is an optional dependency here, so naming its exception classes would
        # mean importing a package that may be absent; and the failure modes are
        # open-ended anyway (transport, auth, quota, a 404 for the database
        # itself). What matters is that every one of them leaves this function
        # as a typed error the caller must decide about, instead of as a `None`
        # indistinguishable from an empty store.
        raise StorageUnavailable(
            f"could not read project {project_id!r} from the durable store: "
            f"{exc.__class__.__name__}: {exc}. Refusing to answer from local "
            f"disk as though it were authoritative -- on Cloud Run that copy "
            f"may be the image's ephemeral one."
        ) from exc

    if not p_doc.exists:
        return None

    try:
        shots_ref = p_ref.collection("beats").order_by("scene_id").stream()
        shots_list = [shot.to_dict() for shot in shots_ref]
    except Exception as exc:  # noqa: BLE001 -- classified, not swallowed
        # A separate arm because the beats arrive as a lazily-evaluated stream:
        # the project document can read cleanly and the subcollection still die
        # mid-iteration. Letting that surface as a Storyboard with an empty or
        # truncated `shots` list is the confident-zero failure exactly -- a
        # storyboard that has lost beats looks like a storyboard that never had
        # them, and the first save afterwards would make that permanent, since
        # save_project() deletes every beat document absent from what it writes.
        raise StorageUnavailable(
            f"read project {project_id!r} from the durable store but could not "
            f"read its beats: {exc.__class__.__name__}: {exc}. A partial "
            f"storyboard is not a shorter one."
        ) from exc

    # Outside the guard on purpose: a failure in from_dict is a defect in this
    # module's parsing, not evidence about the store, and mislabelling it would
    # send the caller looking at infrastructure.
    return Storyboard.from_dict(project_id, p_doc.to_dict(), shots_list)


def save_project(sb: Storyboard) -> None:
    """Save storyboard manifest and all its shots to Firestore.

    Raises :class:`StorageUnavailable` when the write could not reach the
    durable store, for the same reason ``load_project`` does: a save that
    silently did not happen is reported to the user as a save that did, and the
    local JSON mirror it leaves behind is ephemeral on Cloud Run.

    Returns quietly when Firestore is not configured (``db is None``). That is
    not a failed write -- in that mode there is no Firestore to write to and the
    local manifest is the store of record.
    """
    if not sb.id:
        raise ValueError("Storyboard ID is required to save to Firestore")

    if db is None:
        return

    try:
        _save_project_to_firestore(sb)
    except Exception as exc:  # noqa: BLE001 -- classified, not swallowed
        raise StorageUnavailable(
            f"could not write project {sb.id!r} to the durable store: "
            f"{exc.__class__.__name__}: {exc}. The change has NOT been "
            f"persisted durably."
        ) from exc


def _save_project_to_firestore(sb: Storyboard) -> None:
    """The Firestore write itself. Split out so its caller can classify it."""
    p_ref = db.collection("projects").document(sb.id)

    # Save main project metadata
    data = sb.to_dict()
    shots_data = data.pop("shots", [])
    p_ref.set(data)

    # Save shots to subcollection.
    #
    # This upserted and never deleted, and `load_project` streams the WHOLE
    # subcollection back. Redraft an episode from 25 beats to 15 and s016-s025
    # stay behind as zombies -- carrying the old script's narration, prompts,
    # approved=true and motion_type -- so the next read returns 25 beats, 10 of
    # them from an episode that no longer exists. Nothing anywhere reconciled it.
    keep = {shot.get("scene_id") for shot in shots_data}
    beats = p_ref.collection("beats")
    stale = [d.id for d in beats.list_documents() if d.id not in keep]         if hasattr(beats, "list_documents") else         [d.id for d in beats.stream() if d.id not in keep]

    batch = db.batch()
    for shot in shots_data:
        s_id = shot.get("scene_id")
        s_ref = beats.document(s_id)
        batch.set(s_ref, shot)
    for s_id in stale:
        batch.delete(beats.document(s_id))
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

    ``path`` defaults to the manifest of the project this call belongs to --
    the bound context under HTTP, the process global for CLI runs -- so the
    module CLIs and every ``load()`` call site keep working. An empty or absent
    file yields a fresh Storyboard.
    """
    import json
    from . import config

    path = Path(path) if path is not None else config.manifest_path()
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

    ``path`` defaults to the manifest of the project this call belongs to.
    Reading the process global here instead would let a bound request write one
    project's storyboard into another's directory -- a silent whole-file
    overwrite, and the worst version of the §11.3 failure.
    """
    import json
    from . import config

    path = Path(path) if path is not None else config.manifest_path()
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


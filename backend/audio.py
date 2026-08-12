"""Audio stage: narration (TTS), music analysis, and layered sound effects.

Three concerns, all feeding ``timeline.py``:

* **Narration** — ElevenLabs TTS of each locked beat's narration -> per-shot mp3.
  Honours the script gate: refuses to run until ``script_locked`` is true.
* **Music analysis** — librosa reads the chosen ``audio_pool/`` track for tempo,
  beat + onset times, and silent gaps, so cuts can land on the rhythm.
* **Sound effects** — ElevenLabs text-to-sound-effects turns each beat's ``sfx``
  prompt into per-shot ambience/foley, layered under narration + music at
  assembly.

Generation (TTS / SFX) calls a paid API, so ``config.require_for("audio")`` gates
each. Music analysis is local and free.

CLI:
    python -m backend.audio --music                 # analyse the selected track
    python -m backend.audio --narration             # TTS every locked beat
    python -m backend.audio --sfx --scene s001 s006 # SFX for specific beats
    python -m backend.audio --sfx-test "eerie wind" # one-off SFX probe
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from . import config
from .ffmpeg_bin import ffmpeg_bin
from .manifest import Storyboard, load, save

NARRATION_DIR = config.AUDIO_DIR / "narration"
SFX_DIR = config.AUDIO_DIR / "sfx"
OUTPUT_FORMAT = "mp3_44100_128"
SFX_MAX_SECONDS = 22.0          # ElevenLabs sound-effects hard cap


# --------------------------------------------------------------------------- #
# ElevenLabs helpers
# --------------------------------------------------------------------------- #
def _client():
    """Build an ElevenLabs client, validating the key first (paid stage)."""
    config.require_for("audio")
    from elevenlabs.client import ElevenLabs

    return ElevenLabs(api_key=config.ELEVENLABS_API_KEY)


def _write_stream(stream, dest: Path) -> Path:
    """Write an ElevenLabs byte-iterator response to ``dest``, atomically.

    Streaming straight into the final path meant a first-chunk error or a
    container reclaim mid-stream left a truncated or zero-byte mp3 at exactly the
    name every later run tests with ``.exists()`` -- so the beat reported
    "narration exists -- skipping" forever, and nothing but a manual delete
    recovered it. Downstream that poison file is worse than a missing one:
    ``sync_durations`` raises out of librosa and abandons the whole pass, and
    ``build_preview`` takes the entire preview down over one beat.

    ``generate_sfx_fal`` already learned this (see the unlink in its handler); the
    narration path never got it.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".partial")
    try:
        with open(tmp, "wb") as fh:
            for chunk in stream:
                if chunk:
                    fh.write(chunk)
        if tmp.stat().st_size == 0:
            raise RuntimeError("the voice stream produced no audio")
        # os.replace is atomic within a directory, so a reader never sees a
        # half-written mp3 under the real name.
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dest


def _has_audio(p: Path) -> bool:
    """A file counts as present only if it has bytes in it."""
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# narration (TTS)
# --------------------------------------------------------------------------- #
def synthesize_narration(storyboard: Storyboard | None = None,
                         only: set[str] | None = None,
                         voice_id: str | None = None) -> list[Path]:
    """TTS each beat's narration to ``audio/narration/<scene>.mp3``.

    Blocked by the script gate until the storyboard's script is locked.
    """
    sb = storyboard or load()
    if not sb.script_locked:
        raise RuntimeError(
            "Script gate: narration is blocked until the script is locked "
            "(set script_locked=true once the narration is approved)."
        )
    client = _client()
    voice, model_id, v_settings = resolve_voice(sb, voice_id)
    print(f"Narrating with ElevenLabs voice {voice} on {model_id}")
    narr_dir = config.episode_paths(sb.title)["narration"]
    out: list[Path] = []
    for shot in sb.shots:
        if only and shot.scene_id not in only:
            continue
        text = (shot.narration or "").strip()
        if not text:
            continue
        dest = narr_dir / f"{shot.scene_id}.mp3"
        if _has_audio(dest):
            print(f"{shot.scene_id}: narration exists — skipping.")
            out.append(dest)
            continue
        print(f"TTS {shot.scene_id}: {text[:56]}...")
        stream = client.text_to_speech.convert(
            voice_id=voice, text=text,
            model_id=model_id, output_format=OUTPUT_FORMAT,
            voice_settings=v_settings,
        )
        out.append(_write_stream(stream, dest))
    return out


def _voice_settings(stability: float | None = None, style: float | None = None):
    """VoiceSettings for a TTS call, defaulting to the process-level knobs."""
    s = config.ELEVENLABS_STABILITY if stability is None else float(stability)
    st = config.ELEVENLABS_STYLE_EXAGGERATION if style is None else float(style)
    try:
        from elevenlabs import VoiceSettings
        return VoiceSettings(
            stability=s,
            similarity_boost=config.ELEVENLABS_SIMILARITY_BOOST,
            style=st,
            use_speaker_boost=config.ELEVENLABS_SPEAKER_BOOST,
        )
    except ImportError:
        return {
            "stability": s,
            "similarity_boost": config.ELEVENLABS_SIMILARITY_BOOST,
            "style": st,
            "use_speaker_boost": config.ELEVENLABS_SPEAKER_BOOST,
        }


def resolve_voice(sb, voice_id: str | None = None) -> tuple[str, str, object]:
    """Which voice, model and settings this episode narrates with.

    Order: an explicit argument, then the episode's VO profile, then the
    episode's own saved voice_id, then the process defaults. A profile carries
    its settings and model with it, because a narrator is a voice AND how it is
    driven -- the same voice at stability 0.35 and 0.7 is a different performer.

    Backward compatible by construction: an episode naming no profile, or naming
    one that does not exist, falls through to exactly the path it used before
    profiles existed. Nothing is migrated onto a new narrator.
    """
    from . import casting

    if (voice_id or "").strip():
        return voice_id.strip(), config.ELEVENLABS_MODEL, _voice_settings()

    profile = None
    try:
        profile = casting.resolve_profile(sb) if sb is not None else None
    except Exception as exc:  # noqa: BLE001 — a bad profile must not stop narration
        print(f"VO profile could not be resolved ({exc}); using episode defaults.")

    if profile and (profile.voice_id or "").strip():
        try:
            from elevenlabs import VoiceSettings
            settings = VoiceSettings(**profile.settings())
        except ImportError:
            settings = profile.settings()
        print(f"VO profile '{profile.id}' ({profile.name}) -> voice {profile.voice_id}")
        return profile.voice_id.strip(), profile.model_id, settings

    voice = ((getattr(sb, "voice_id", "") or "").strip()
             or config.VESPER_VOICE_ID or config.ELEVENLABS_VOICE_ID)
    return voice, config.ELEVENLABS_MODEL, _voice_settings()


def sample_voice(text: str, voice_id: str | None = None,
                 stability: float | None = None, style: float | None = None,
                 storyboard: Storyboard | None = None) -> bytes:
    """Render a short line with a *saved* voice and return the MP3 bytes.

    The design flow already auditions brand-new voices, but there was no way to
    hear an existing voice, or to hear what stability/style changes do — the
    settings endpoint returns numbers and no audio. This closes that: it uses
    the same client, model and VoiceSettings shape as synthesize_narration, so
    the sample is representative of the real narration rather than a different
    code path that might sound different.

    Nothing is written to disk; a sample is throwaway and must never be mistaken
    for a beat's narration.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("sample_voice needs some text")
    # Samples are billed per character like any TTS, so cap them.
    text = text[:400]

    sb = storyboard or load()
    voice = (
        (voice_id or "").strip()
        or (getattr(sb, "voice_id", "") or "").strip()
        or config.VESPER_VOICE_ID
        or config.ELEVENLABS_VOICE_ID
    )
    if not voice:
        raise RuntimeError("No voice selected and no ELEVENLABS_VOICE_ID configured.")

    stream = _client().text_to_speech.convert(
        voice_id=voice, text=text,
        model_id=config.ELEVENLABS_MODEL, output_format=OUTPUT_FORMAT,
        voice_settings=_voice_settings(stability, style),
    )
    buf = bytearray()
    for chunk in stream:
        if chunk:
            buf.extend(chunk)
    if not buf:
        raise RuntimeError("ElevenLabs returned no audio for the sample.")
    return bytes(buf)


def resolve_sfx_layers(shot, sfx_dir) -> list:
    """The beat's SFX layers, or a synthetic one wrapping the legacy single file.

    Existing manifests have `sfx` (a prompt) and one file at <scene>.mp3 with no
    layers. Rather than migrate them -- which would rewrite every manifest and
    break anything still reading the old shape -- the old form is presented as a
    one-element layer list at read time.
    """
    from .manifest import AudioLayer
    layers = list(getattr(shot, "sfx_layers", None) or [])
    if layers:
        return layers
    prompt = (getattr(shot, "sfx", "") or "").strip()
    legacy = sfx_dir / f"{shot.scene_id}.mp3"
    if not prompt and not legacy.is_file():
        return []
    return [AudioLayer(
        id="legacy", prompt=prompt,
        file=str(legacy) if legacy.is_file() else "",
        gain=float(getattr(shot, "gain_sfx", 1.0) or 1.0),
        label="sfx",
    )]


def loop_to_length(seg, sr: int, seconds: float, crossfade: float = 0.35):
    """Tile an ambience bed to cover ``seconds``, crossfading each seam.

    SFX are generated per beat from an environmental prompt — room tone, weather,
    fire, water — so they are beds, not one-shots, and a bed that stops partway
    through its beat is heard as the room going dead. The music bed was looped
    here from the start; ambience never was, which did not show while beats were
    short. When durations were refit to narration length, stems generated against
    the old six-second durations left half a minute of silence under every beat.

    Returns the segment unchanged when it is already long enough; never pads with
    silence, which is the failure this exists to prevent.
    """
    import numpy as np

    need = int(round(seconds * sr))
    n = int(seg.shape[0])
    if need <= 0 or n == 0:
        return seg[: max(need, 0)]
    if n >= need:
        return seg[:need]

    # A crossfade longer than half the clip would fold it onto itself.
    xf = int(min(float(crossfade), n / (2.0 * sr)) * sr)
    if xf <= 0:
        reps = -(-need // n)  # ceil
        return np.tile(seg, (reps, 1))[:need]

    down = np.linspace(1.0, 0.0, xf, dtype=np.float32)[:, None]
    up = 1.0 - down
    out = seg
    while out.shape[0] < need:
        seam = out[-xf:] * down + seg[:xf] * up
        out = np.concatenate([out[:-xf], seam, seg[xf:]], axis=0)
    return out[:need]


def layer_loops(layer) -> bool:
    """Whether a layer should be tiled to its beat. See ``AudioLayer.loop``."""
    explicit = getattr(layer, "loop", None)
    if explicit is not None:
        return bool(explicit)
    return (getattr(layer, "source", "generated") or "generated") == "generated"


def apply_fades(seg, sr: int, fade_in: float, fade_out: float):
    """Linear fades in place on an (n, channels) float array."""
    import numpy as np
    n = seg.shape[0]
    fi = int(min(max(fade_in, 0.0), 30.0) * sr)
    fo = int(min(max(fade_out, 0.0), 30.0) * sr)
    if fi > 0:
        k = min(fi, n)
        seg[:k] *= np.linspace(0.0, 1.0, k, dtype=seg.dtype)[:, None]
    if fo > 0:
        k = min(fo, n)
        seg[n - k:] *= np.linspace(1.0, 0.0, k, dtype=seg.dtype)[:, None]
    return seg


def peaks(path: Path, buckets: int = 240) -> list[float]:
    """Downsampled 0..1 peak envelope for a clip, for drawing a waveform.

    Decodes via ffmpeg to raw 8 kHz mono PCM rather than librosa: this runs over
    30 clips per episode and librosa's mp3 path is an order of magnitude slower
    for a result that gets reduced to a few hundred numbers anyway.
    """
    import subprocess
    import numpy as np
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        return []
    proc = subprocess.run(
        [_ffmpeg_bin(), "-v", "error", "-i", str(path),
         "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", "8000", "-"],
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        return []
    y = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    if y.size == 0:
        return []
    n = min(buckets, y.size)
    edges = np.linspace(0, y.size, n + 1, dtype=int)
    env = [float(np.abs(y[a:b]).max()) if b > a else 0.0 for a, b in zip(edges[:-1], edges[1:])]
    top = max(env) or 1.0
    # Normalised per clip: this is for seeing shape and timing, not level. Level
    # is what the trim sliders and the mixer are for.
    return [round(v / top, 3) for v in env]


def sync_durations(storyboard: Storyboard | None = None, pad: float = 0.8,
                   min_dur: float = 3.0, report: dict | None = None) -> int:
    """Fit each shot's ``camera.duration`` to its narration clip (VO length + pad).

    Narration-led pacing: a beat must hold at least as long as its voiceover, or
    the VOs overlap the following shots (the default 6s slot is far shorter than a
    typical 15-25s documentary beat). Mutates the storyboard in place and returns
    the number of shots changed; the caller persists it (so the active manifest
    path is respected).

    Beats with ``camera.duration_locked`` are left alone — that is the whole
    point of the flag, since re-running narration would otherwise throw away a
    trim made in the studio. A locked beat shorter than its own VO is reported
    through ``report``: it is legal (you may want the voice to carry over a cut)
    but it is almost always a mistake, and silently overlapping narration is the
    kind of thing you only notice on the finished master.
    """
    import librosa

    sb = storyboard or load()
    narr_dir = config.episode_paths(sb.title)["narration"]
    changed = 0
    for shot in sb.shots:
        f = narr_dir / f"{shot.scene_id}.mp3"
        if not f.exists() or shot.camera is None:
            continue
        vo = float(librosa.get_duration(path=str(f)))
        new = round(max(min_dur, vo + pad), 2)
        if getattr(shot.camera, "duration_locked", False):
            if report is not None:
                entry = {"scene_id": shot.scene_id, "duration": float(shot.camera.duration),
                         "vo": round(vo, 2), "would_be": new}
                if shot.camera.duration < vo:
                    report.setdefault("overrun", []).append(entry)
                report.setdefault("locked", []).append(entry)
            continue
        if abs(shot.camera.duration - new) > 0.05:
            shot.camera.duration = new
            changed += 1
    return changed


# --------------------------------------------------------------------------- #
# music analysis (librosa, local/free)
# --------------------------------------------------------------------------- #
def _resolve_track(path: str | Path | None) -> Path:
    """Resolve a music path (or the manifest's ``music_track``) to a real file.

    A bare filename is looked up inside ``audio_pool/``; an absolute path is used
    as-is. Raises if nothing is selected or the file is missing.
    """
    track = path or load().music_track
    if not track:
        raise RuntimeError(
            "No music_track set — drop a WAV/MP3 in audio_pool/ and set "
            "music_track in the manifest."
        )
    p = Path(track)
    if not p.is_absolute():
        p = config.AUDIO_POOL / p
    if not p.exists():
        raise FileNotFoundError(p)
    return p


def detect_transients(path: str | Path | None = None, hop_length: int = 512,
                      delta: float = 0.06, wait: int = 3) -> list[float]:
    """Percussive transients of the music track as a clean list of timestamps.

    Complements :func:`analyze_music`'s beat/onset read: builds the onset-strength
    envelope and runs ``peak_pick`` to isolate sharp energy spikes — the hard hits
    a cut can snap to. Returns sorted seconds. Local and free.

    ``delta`` is the peak-pick threshold above the local mean (raise it to keep
    only the strongest hits); ``wait`` is the minimum frame gap between peaks.
    """
    import librosa

    p = _resolve_track(path)
    y, sr = librosa.load(str(p), mono=True)
    envelope = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop_length)
    peaks = librosa.util.peak_pick(
        envelope, pre_max=3, post_max=3, pre_avg=3, post_avg=5,
        delta=delta, wait=wait,
    )
    times = librosa.frames_to_time(peaks, sr=sr, hop_length=hop_length)
    return [round(float(t), 3) for t in times]


def analyze_music(path: str | Path | None = None, top_db: int = 30,
                  min_gap: float = 0.35) -> dict:
    """Analyse the selected music track: tempo, beats, onsets, silent gaps."""
    import librosa
    import numpy as np

    p = _resolve_track(path)
    y, sr = librosa.load(str(p), mono=True)
    duration = float(librosa.get_duration(y=y, sr=sr))
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    beat_times = librosa.frames_to_time(beat_frames, sr=sr)
    onset_times = librosa.onset.onset_detect(y=y, sr=sr, units="time")

    # Silent gaps = the complement of the non-silent intervals.
    nonsilent = librosa.effects.split(y, top_db=top_db) / sr
    silences: list[tuple[float, float]] = []
    prev = 0.0
    for start, end in nonsilent:
        if start - prev > min_gap:
            silences.append((round(prev, 3), round(float(start), 3)))
        prev = float(end)
    if duration - prev > min_gap:
        silences.append((round(prev, 3), round(duration, 3)))

    return {
        "track": str(p.name),
        "duration": round(duration, 3),
        "tempo": round(float(np.atleast_1d(tempo)[0]), 2),
        "beats": [round(float(t), 3) for t in beat_times],
        "onsets": [round(float(t), 3) for t in onset_times],
        "silences": silences,
    }


# --------------------------------------------------------------------------- #
# sound effects (SFX)
# --------------------------------------------------------------------------- #
def generate_sfx(prompt: str, dest: Path, duration_seconds: float | None = None,
                 prompt_influence: float = 0.3) -> Path:
    """Generate one ElevenLabs sound effect from ``prompt`` -> ``dest``."""
    client = _client()
    kwargs: dict = {"text": prompt, "output_format": OUTPUT_FORMAT,
                    "prompt_influence": prompt_influence}
    if duration_seconds:
        kwargs["duration_seconds"] = min(float(duration_seconds), SFX_MAX_SECONDS)
    stream = client.text_to_sound_effects.convert(**kwargs)
    return _write_stream(stream, Path(dest))


# --- Music bed generation ---------------------------------------------------
#
# Every endpoint and limit below was read from fal's OpenAPI registry, not
# assumed. None reaches a typical 5-6 minute episode runtime, which is fine:
# timeline.build and build_preview both loop the bed to cover the full runtime,
# so a good 2-4 minute loop is the target rather than a one-shot full-length cue.
#
# Argument shapes genuinely differ per model — ace-step wants "tags" rather than
# a prompt, elevenlabs takes milliseconds — so each entry carries its own builder.
MUSIC_BACKENDS: dict[str, dict] = {
    "elevenlabs_music": {
        "label": "ElevenLabs Music",
        "endpoint": "fal-ai/elevenlabs/music",
        "max_seconds": 300,
        "build": lambda p, s: {"prompt": p, "music_length_ms": int(s * 1000)},
    },
    "ace_step": {
        "label": "ACE-Step",
        "endpoint": "fal-ai/ace-step",
        "max_seconds": 240,
        "build": lambda p, s: {"tags": p, "duration": float(s), "lyrics": ""},
    },
    "stable_audio_25": {
        "label": "Stable Audio 2.5",
        "endpoint": "fal-ai/stable-audio-25/text-to-audio",
        "max_seconds": 190,
        "build": lambda p, s: {"prompt": p, "seconds_total": int(s)},
    },
    "cassette": {
        "label": "Cassette Music Generator",
        "endpoint": "cassetteai/music-generator",
        "max_seconds": 180,
        "build": lambda p, s: {"prompt": p, "duration": int(s)},
    },
}

MUSIC_BACKEND_KEYS: list[str] = list(MUSIC_BACKENDS)
DEFAULT_MUSIC_BACKEND = "elevenlabs_music"


def _ffmpeg_bin() -> str:
    """System ffmpeg (per CLAUDE.md), falling back to the imageio-bundled one.

    Thin alias kept for callers in this module; the resolution lives in
    ``ffmpeg_bin`` so every module answers this question the same way.
    """
    return ffmpeg_bin()


def is_mp3(path: Path) -> bool:
    """True if the file really is MP3, by content — not by extension."""
    try:
        head = Path(path).open("rb").read(4)
    except OSError:
        return False
    # ID3 tag, or a raw MPEG audio frame sync (11 set bits).
    return head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)


def transcode_to_mp3(src: Path, bitrate: str = "192k", normalize_lufs: float | None = None,
                     log=print) -> Path:
    """Re-encode ``src`` to MP3 beside itself and remove the original.

    Generators hand back uncompressed audio: ACE-Step returns RIFF/WAV, and
    stable-audio's SFX are WAV too. A 44 MB music bed and 55 MB of SFX are
    ~10x their MP3 size, which is paid for on every export bundle, every GCS
    read and every mix.

    ``normalize_lufs`` applies ffmpeg loudnorm to a target integrated loudness.
    Left off by default: it changes how the audio sounds, which is a creative
    decision, not a storage one.
    """
    src = Path(src)
    if not src.is_file() or src.stat().st_size == 0:
        raise FileNotFoundError(f"Nothing to transcode at {src}")
    if src.suffix.lower() == ".mp3" and is_mp3(src) and normalize_lufs is None:
        return src

    dest = src.with_suffix(".mp3")
    tmp = src.with_name(src.stem + ".transcode.mp3")
    before = src.stat().st_size

    cmd = [_ffmpeg_bin(), "-y", "-v", "error", "-i", str(src)]
    if normalize_lufs is not None:
        cmd += ["-af", f"loudnorm=I={normalize_lufs}:TP=-1.5:LRA=11"]
    cmd += ["-codec:a", "libmp3lame", "-b:a", bitrate, str(tmp)]

    import subprocess
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed on {src.name}: {(proc.stderr or '').strip()[:300]}")

    # Replace only once the new file is known good, so a failed transcode can
    # never destroy the only copy of a generated asset.
    if src != dest:
        src.unlink()
    tmp.replace(dest)
    after = dest.stat().st_size
    log(f"{src.name} -> {dest.name}: {before/1e6:.1f} MB -> {after/1e6:.1f} MB "
        f"({100 - after/max(before,1)*100:.0f}% smaller)")
    return dest


def generate_music(prompt: str, dest: Path, duration_seconds: float = 180.0,
                   backend: str = DEFAULT_MUSIC_BACKEND, log=print) -> Path:
    """Generate a music bed into ``dest``. Returns the written path.

    Unlike generate_sfx_fal this deliberately does NOT swallow failures into an
    empty file — a zero-byte music bed breaks the ffmpeg mix downstream and looks
    like a rendering bug rather than a generation one.
    """
    import fal_client
    import requests

    config.require_for("assets")
    spec = MUSIC_BACKENDS.get(backend) or MUSIC_BACKENDS[DEFAULT_MUSIC_BACKEND]
    secs = max(10.0, min(float(duration_seconds), float(spec["max_seconds"])))
    if secs < duration_seconds:
        log(f"Clamped to {spec['label']}'s {spec['max_seconds']}s limit; the timeline loops the bed to cover the full runtime.")

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"Generating {secs:.0f}s music bed via {spec['endpoint']}: {prompt[:80]}")

    result = fal_client.subscribe(spec["endpoint"], arguments=spec["build"](prompt, secs), with_logs=False)
    url = ""
    for key in ("audio", "audio_file", "output", "audio_url"):
        node = result.get(key)
        if isinstance(node, dict) and node.get("url"):
            url = node["url"]; break
        if isinstance(node, str) and node.startswith("http"):
            url = node; break
    if not url:
        raise RuntimeError(f"{spec['label']} returned no audio URL. Response keys: {sorted(result)}")

    r = requests.get(url, timeout=300)
    r.raise_for_status()
    data = r.content

    # Name the file after what actually arrived. Models differ: ACE-Step returns
    # RIFF/WAV even when the request looks like it should yield mp3, and a WAV
    # sitting behind a .mp3 name is both misleading and ~10x the bytes. ffmpeg and
    # librosa sniff content so playback survives the mismatch, but anything doing
    # extension-based filtering does not.
    ext = ".mp3"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        ext = ".wav"
    elif data[:4] == b"fLaC":
        ext = ".flac"
    elif data[:4] == b"OggS":
        ext = ".ogg"
    elif data[4:8] == b"ftyp":
        ext = ".m4a"
    if dest.suffix.lower() != ext:
        log(f"Response is {ext[1:].upper()}, not {dest.suffix[1:].upper() or 'unknown'} — saving as {dest.stem}{ext}")
        dest = dest.with_suffix(ext)

    dest.write_bytes(data)
    log(f"Wrote music bed {dest.name} ({dest.stat().st_size:,} bytes)")

    # Store MP3 regardless of what the model returned. A bed is a loopable
    # underscore mixed well under narration, so lossless buys nothing and costs
    # ~10x the bytes on every export, GCS read and mix.
    if ext != ".mp3":
        try:
            dest = transcode_to_mp3(dest, log=log)
        except Exception as exc:  # noqa: BLE001 — the bed is usable either way
            log(f"Could not transcode the bed to MP3 ({exc}); keeping {dest.suffix}.")
    return dest


def generate_sfx_fal(prompt: str, dest: Path, duration_seconds: float | None = None) -> Path:
    """Generate ambient sound effects using Fal.ai (fal-ai/stable-audio)."""
    import fal_client
    import requests
    
    config.require_for("assets")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    
    dur = min(float(duration_seconds or 10.0), 47.0)
    print(f"Generating Fal SFX ({dur:.1f}s) for prompt: '{prompt}'...")
    
    try:
        res = fal_client.subscribe(
            "fal-ai/stable-audio",
            arguments={
                "prompt": prompt,
                "seconds_total": int(dur),
                "steps": 100
            }
        )
        
        audio_url = res.get("audio_file", {}).get("url")
        if not audio_url:
            raise RuntimeError("Fal Stable Audio returned no audio URL")
            
        r = requests.get(audio_url, timeout=60)
        r.raise_for_status()
        # stable-audio returns WAV. Write what arrived, then store MP3: 15 beats
        # of ambience was 55 MB uncompressed.
        raw = dest.with_suffix(".wavtmp") if dest.suffix.lower() == ".mp3" else dest
        raw.write_bytes(r.content)
        try:
            dest = transcode_to_mp3(raw, bitrate="128k", log=print)
        except Exception as exc:  # noqa: BLE001
            print(f"Could not transcode SFX to MP3 ({exc}); keeping raw audio.")
            if raw != dest:
                raw.replace(dest)
        print(f"Wrote Fal SFX to {dest} ({dest.stat().st_size:,} bytes)")
        return dest
    except Exception as e:
        # Never leave a zero-byte file behind. dest.exists() is what the render
        # stage checks before regenerating, so an empty placeholder made the
        # failure permanent -- and librosa raises on it, which took the whole
        # preview build down rather than just losing one beat's ambience.
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"Fal SFX generation failed for '{prompt[:60]}': {e}") from e


def generate_shot_sfx(storyboard: Storyboard | None = None,
                      only: set[str] | None = None) -> list[Path]:
    """Generate a sound effect for every beat that carries an ``sfx`` prompt."""
    sb = storyboard or load()
    sfx_dir = config.episode_paths(sb.title)["sfx"]
    out: list[Path] = []
    for shot in sb.shots:
        if only and shot.scene_id not in only:
            continue
        prompt = (shot.sfx or "").strip()
        if not prompt:
            continue
        dest = sfx_dir / f"{shot.scene_id}.mp3"
        if dest.exists():
            print(f"{shot.scene_id}: sfx exists — skipping.")
            out.append(dest)
            continue
        dur = shot.camera.duration if shot.camera else None
        print(f"SFX {shot.scene_id}: {prompt[:56]}...")
        try:
            out.append(generate_sfx_fal(prompt, dest, duration_seconds=dur))
        except Exception as exc:  # noqa: BLE001 — one beat must not end the batch
            print(f"  !! {shot.scene_id} SFX failed: {exc}")
    return out


def _voice_headers() -> dict:
    config.require_for("audio")
    return {"xi-api-key": config.ELEVENLABS_API_KEY, "Content-Type": "application/json"}


def compose_voice_description(description: str, gender: str = "", age: str = "",
                              accent: str = "") -> str:
    """Fold the old structured fields into one natural-language description.

    The current API takes prose only — gender/age/accent are no longer separate
    parameters — but the studio still offers them as selects, so they are merged
    rather than dropped.
    """
    bits = [(description or "").strip()]
    traits = [t.replace("_", " ").strip() for t in (gender, age, accent) if (t or "").strip()]
    if traits:
        bits.append(f"The voice is {', '.join(traits)}.")
    return " ".join(b for b in bits if b).strip()


def design_voice(description: str, sample_text: str = "", gender: str = "",
                 age: str = "", accent: str = "") -> dict:
    """Generate candidate voices from a description. Returns previews to audition.

    Uses POST /v1/text-to-voice/design. The old /v1/voice-generation/generate-voice
    endpoint this replaced now returns 404 — ElevenLabs retired it, which is why
    the Voice Studio failed with "404 Client Error: Not Found".

    The new flow is two-step and returns THREE candidates rather than one: design
    previews here, then promote the chosen one with save_designed_voice(). A
    preview is not a usable voice — its generated_voice_id cannot be passed to
    text-to-speech until it has been saved.
    """
    import requests

    full = compose_voice_description(description, gender, age, accent)
    if not full:
        raise ValueError("A voice description is required.")

    payload: dict = {"voice_description": full}
    if (sample_text or "").strip():
        payload["text"] = sample_text.strip()
        payload["auto_generate_text"] = False
    else:
        payload["auto_generate_text"] = True

    resp = requests.post(
        "https://api.elevenlabs.io/v1/text-to-voice/design",
        headers=_voice_headers(), json=payload, timeout=180,
    )
    resp.raise_for_status()
    data = resp.json()

    previews = []
    for pv in data.get("previews") or []:
        b64 = pv.get("audio_base_64") or ""
        media = pv.get("media_type") or "audio/mpeg"
        previews.append({
            "generated_voice_id": pv.get("generated_voice_id", ""),
            "duration_secs": pv.get("duration_secs"),
            "audio_data_uri": f"data:{media};base64,{b64}" if b64 else "",
        })
    return {"previews": previews, "text": data.get("text", ""), "voice_description": full}


def save_designed_voice(voice_name: str, voice_description: str,
                        generated_voice_id: str) -> dict:
    """Promote an audition preview into a real, reusable voice.

    Until this runs the preview is throwaway — only the voice_id returned here
    can be used for narration.
    """
    import requests

    if not (voice_name or "").strip():
        raise ValueError("A voice name is required.")
    if not (generated_voice_id or "").strip():
        raise ValueError("A generated_voice_id from a preview is required.")

    resp = requests.post(
        "https://api.elevenlabs.io/v1/text-to-voice/create-voice-from-preview",
        headers=_voice_headers(),
        json={
            "voice_name": voice_name.strip(),
            "voice_description": (voice_description or "").strip() or voice_name.strip(),
            "generated_voice_id": generated_voice_id.strip(),
        },
        timeout=180,
    )
    resp.raise_for_status()
    data = resp.json()
    return {"voice_id": data.get("voice_id", ""), "name": data.get("name", voice_name)}


def list_voices() -> list[dict]:
    """Voices available on the account, for the studio's picker."""
    import requests

    resp = requests.get("https://api.elevenlabs.io/v1/voices",
                        headers={"xi-api-key": config.ELEVENLABS_API_KEY}, timeout=60)
    resp.raise_for_status()
    return [
        {"voice_id": v.get("voice_id", ""), "name": v.get("name", ""),
         "category": v.get("category", "")}
        for v in resp.json().get("voices", [])
    ]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _main() -> None:
    parser = argparse.ArgumentParser(description="The Illuminated Bestiary audio stage.")
    parser.add_argument("--music", action="store_true", help="analyse the selected music track.")
    parser.add_argument("--transients", action="store_true", help="list percussive transient timestamps (peak_pick).")
    parser.add_argument("--narration", action="store_true", help="TTS locked-script narration.")
    parser.add_argument("--sfx", action="store_true", help="generate per-beat sound effects.")
    parser.add_argument("--sfx-test", metavar="PROMPT", help="generate one probe SFX and exit.")
    parser.add_argument("--scene", nargs="*", help="limit narration/sfx to these scene id(s).")
    args = parser.parse_args()
    only = set(args.scene) if args.scene else None

    if args.sfx_test:
        dest = generate_sfx(args.sfx_test, SFX_DIR / "_test.mp3", duration_seconds=6.0)
        print(f"Wrote {dest}")
    if args.music:
        print(json.dumps(analyze_music(), indent=2))
    if args.transients:
        print(json.dumps(detect_transients(), indent=2))
    if args.narration:
        outs = synthesize_narration(only=only)
        print(f"Narration: {len(outs)} clip(s) in {NARRATION_DIR}")
    if args.sfx:
        outs = generate_shot_sfx(only=only)
        print(f"SFX: {len(outs)} clip(s) in {SFX_DIR}")
    if not any((args.sfx_test, args.music, args.transients, args.narration, args.sfx)):
        parser.print_help()


if __name__ == "__main__":
    _main()

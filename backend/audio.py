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
from pathlib import Path

from . import config
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
    """Write an ElevenLabs byte-iterator response to ``dest``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as fh:
        for chunk in stream:
            if chunk:
                fh.write(chunk)
    return dest


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
    # Explicit argument, then the episode's own saved voice, then env defaults.
    voice = (
        voice_id
        or (getattr(sb, "voice_id", "") or "").strip()
        or config.VESPER_VOICE_ID
        or config.ELEVENLABS_VOICE_ID
    )
    print(f"Narrating with ElevenLabs voice {voice}")
    narr_dir = config.episode_paths(sb.title)["narration"]
    out: list[Path] = []
    for shot in sb.shots:
        if only and shot.scene_id not in only:
            continue
        text = (shot.narration or "").strip()
        if not text:
            continue
        dest = narr_dir / f"{shot.scene_id}.mp3"
        if dest.exists():
            print(f"{shot.scene_id}: narration exists — skipping.")
            out.append(dest)
            continue
        print(f"TTS {shot.scene_id}: {text[:56]}...")
        try:
            from elevenlabs import VoiceSettings
            v_settings = VoiceSettings(
                stability=config.ELEVENLABS_STABILITY,
                similarity_boost=config.ELEVENLABS_SIMILARITY_BOOST,
                style=config.ELEVENLABS_STYLE_EXAGGERATION,
                use_speaker_boost=config.ELEVENLABS_SPEAKER_BOOST,
            )
        except ImportError:
            v_settings = {
                "stability": config.ELEVENLABS_STABILITY,
                "similarity_boost": config.ELEVENLABS_SIMILARITY_BOOST,
                "style": config.ELEVENLABS_STYLE_EXAGGERATION,
                "use_speaker_boost": config.ELEVENLABS_SPEAKER_BOOST,
            }
        stream = client.text_to_speech.convert(
            voice_id=voice, text=text,
            model_id=config.ELEVENLABS_MODEL, output_format=OUTPUT_FORMAT,
            voice_settings=v_settings,
        )
        out.append(_write_stream(stream, dest))
    return out


def sync_durations(storyboard: Storyboard | None = None, pad: float = 0.8,
                   min_dur: float = 3.0) -> int:
    """Fit each shot's ``camera.duration`` to its narration clip (VO length + pad).

    Narration-led pacing: a beat must hold at least as long as its voiceover, or
    the VOs overlap the following shots (the default 6s slot is far shorter than a
    typical 15-25s documentary beat). Mutates the storyboard in place and returns
    the number of shots changed; the caller persists it (so the active manifest
    path is respected).
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
        dest.write_bytes(r.content)
        print(f"Wrote Fal SFX to {dest}")
        return dest
    except Exception as e:
        print(f"Fal SFX generation failed ({e}); falling back to local empty placeholder audio.")
        dest.write_bytes(b"")
        return dest


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
        out.append(generate_sfx_fal(prompt, dest, duration_seconds=dur))
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

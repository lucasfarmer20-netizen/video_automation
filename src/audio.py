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
    python -m src.audio --music                 # analyse the selected track
    python -m src.audio --narration             # TTS every locked beat
    python -m src.audio --sfx --scene s001 s006 # SFX for specific beats
    python -m src.audio --sfx-test "eerie wind" # one-off SFX probe
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
    voice = voice_id or config.VESPER_VOICE_ID or config.ELEVENLABS_VOICE_ID
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
        stream = client.text_to_speech.convert(
            voice_id=voice, text=text,
            model_id=config.ELEVENLABS_MODEL, output_format=OUTPUT_FORMAT,
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
        out.append(generate_sfx(prompt, dest, duration_seconds=dur))
    return out


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

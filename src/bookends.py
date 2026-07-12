"""Channel bookends: the fixed book-open intro and book-close outro.

These are CHANNEL wrapper assets — identical on every episode, not per-episode
shots. Each is the book clip with Vesper's fixed VO mixed over an optional music
sting, composited ONCE into ``intro/intro_final.mp4`` / ``intro/outro_final.mp4``
and reused for every video. Consistency comes from the frame (see CLAUDE.md).

Flow:
    1. ``--vo``       TTS the fixed intro+outro lines once (Vesper voice).
    2. drop the book-close clip from Veo/Flow at ``intro/outro_v1.mp4`` (intro
       clip already exists); optional music stings at ``intro/{intro,outro}_music.mp3``.
    3. ``--compose``  build the final segment(s) from whatever clips are present.

CLI:
    python -m src.bookends --vo
    python -m src.bookends --compose
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from . import config

HEIGHT = 720
OUT_W = HEIGHT * 16 // 9


def _tts(text: str, dest: Path, voice: str | None = None) -> Path:
    """TTS one fixed line to ``dest`` in Vesper's (or the default) voice."""
    config.require_for("audio")
    from elevenlabs.client import ElevenLabs

    client = ElevenLabs(api_key=config.ELEVENLABS_API_KEY)
    voice = voice or config.VESPER_VOICE_ID or config.ELEVENLABS_VOICE_ID
    stream = client.text_to_speech.convert(
        voice_id=voice, text=text, model_id=config.ELEVENLABS_MODEL,
        output_format="mp3_44100_128",
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as fh:
        for chunk in stream:
            if chunk:
                fh.write(chunk)
    return dest


def build_vo() -> list[Path]:
    """Generate the two fixed channel VO clips (idempotent — overwrites)."""
    return [
        _tts(config.INTRO_VO_TEXT, config.INTRO_VO),
        _tts(config.OUTRO_VO_TEXT, config.OUTRO_VO),
    ]


def _probe(path: Path) -> float:
    from . import timeline
    return timeline._probe_seconds(path)


def compose(kind: str) -> Path:
    """Composite a bookend: book clip + fixed VO (+ music sting) -> final mp4.

    Video is normalized to the render format (1280x720, 24fps) and held on its last
    frame if the VO runs longer than the clip, so the whole VO is heard. Output is a
    self-contained segment ready to sit at the front/back of every episode.
    """
    clip = config.INTRO_CLIP if kind == "intro" else config.OUTRO_CLIP
    vo = config.INTRO_VO if kind == "intro" else config.OUTRO_VO
    music = config.INTRO_MUSIC if kind == "intro" else config.OUTRO_MUSIC
    out = config.INTRO_FINAL if kind == "intro" else config.OUTRO_FINAL
    if not clip.exists():
        raise FileNotFoundError(f"{kind} clip missing: {clip} (make it in Veo/Flow)")

    clip_dur = _probe(clip)
    vo_dur = _probe(vo) if vo.exists() else 0.0
    target = round(max(clip_dur, vo_dur), 2)
    pad = max(0.0, target - clip_dur)

    vchain = (f"[0:v]scale={OUT_W}:{HEIGHT}:force_original_aspect_ratio=decrease,"
              f"pad={OUT_W}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,fps=24")
    if pad > 0.05:
        vchain += f",tpad=stop_mode=clone:stop_duration={pad:.2f}"
    vchain += "[v]"

    inputs = ["-i", str(clip)]
    parts = [vchain]
    amix = []
    idx = 1
    if vo.exists():
        inputs += ["-i", str(vo)]
        parts.append(f"[{idx}:a]apad[vo]")
        amix.append("[vo]")
        idx += 1
    if music.exists():
        inputs += ["-i", str(music)]
        parts.append(f"[{idx}:a]volume=0.30,apad[mus]")
        amix.append("[mus]")
        idx += 1

    cmd = ["ffmpeg", "-y", "-v", "error", *inputs]
    maps = ["-map", "[v]"]
    if amix:
        if len(amix) > 1:
            parts.append(f"{''.join(amix)}amix=inputs={len(amix)}:duration=longest[a]")
        else:
            parts.append(f"{amix[0]}anull[a]")
        maps += ["-map", "[a]", "-c:a", "aac", "-b:a", "160k"]
    cmd += ["-filter_complex", ";".join(parts), *maps,
            "-t", f"{target}", "-c:v", "libx264", "-crf", "20",
            "-pix_fmt", "yuv420p", str(out)]
    subprocess.run(cmd, check=True)
    return out


def _main() -> None:
    parser = argparse.ArgumentParser(description="The Illuminated Bestiary channel bookends.")
    parser.add_argument("--vo", action="store_true", help="TTS the fixed intro+outro VO (Vesper).")
    parser.add_argument("--compose", action="store_true", help="composite available bookend segment(s).")
    args = parser.parse_args()
    if args.vo:
        for p in build_vo():
            print(f"VO -> {p.relative_to(config.ROOT)}")
    if args.compose:
        for kind in ("intro", "outro"):
            clip = config.INTRO_CLIP if kind == "intro" else config.OUTRO_CLIP
            if clip.exists():
                print(f"composed {kind} -> {compose(kind).relative_to(config.ROOT)}")
            else:
                print(f"{kind}: clip missing ({clip.name}) — skipped.")
    if not (args.vo or args.compose):
        parser.print_help()


if __name__ == "__main__":
    _main()

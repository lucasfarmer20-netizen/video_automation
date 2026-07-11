"""Timeline stage: assemble the cut into an OpenTimelineIO timeline + FCPXML.

Builds a narration-led multi-track timeline from everything the earlier stages
produced and writes it out for DaVinci Resolve (Gate 2 — the human finishes the
cut there; the pipeline never auto-renders a master).

Tracks (top to bottom):
    V1  — the rendered shot clips (``render/<scene>.mp4``), each held for its
          shot's ``camera.duration`` (which is the narration length + a breath).
    A1  — narration (Vesper TTS per beat), synced to each shot's start.
    A2  — sound effects (the curated per-beat ``sfx`` clips).
    A3  — the music bed, looped to cover the full runtime.

Emits ``<slug>.otio`` (native, Resolve imports it directly) and ``<slug>.fcpxml``
(via the fcpx_xml adapter) next to the manifest. Cut points are narration-led;
``audio.analyze_music`` beat/onset times are available for later fine-tuning in
Resolve.

CLI:
    python -m src.timeline
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

import opentimelineio as otio

from . import config
from .manifest import Storyboard, load

FPS = 24


# --------------------------------------------------------------------------- #
# small OTIO helpers
# --------------------------------------------------------------------------- #
def _probe_seconds(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True,
    )
    return float(out.stdout.strip() or 0.0)


def _rt(seconds: float) -> otio.opentime.RationalTime:
    return otio.opentime.RationalTime(round(seconds * FPS), FPS)


def _range(seconds: float, start: float = 0.0) -> otio.opentime.TimeRange:
    return otio.opentime.TimeRange(_rt(start), _rt(seconds))


def _clip(name: str, path: Path, seconds: float, media_seconds: float | None = None,
          src_start: float = 0.0) -> otio.schema.Clip:
    media = otio.schema.ExternalReference(
        target_url=path.resolve().as_uri(),
        available_range=_range(media_seconds if media_seconds else seconds),
    )
    return otio.schema.Clip(name=name, media_reference=media,
                            source_range=_range(seconds, src_start))


def _gap(seconds: float) -> otio.schema.Gap:
    return otio.schema.Gap(source_range=_range(seconds))


def _fill(track: otio.schema.Track, clip_seconds: float, shot_seconds: float,
          clip: otio.schema.Clip | None) -> None:
    """Append ``clip`` (if any) then pad with a gap so the track matches the shot."""
    if clip is not None:
        track.append(clip)
        remainder = shot_seconds - clip_seconds
        if remainder > 0.02:
            track.append(_gap(remainder))
    else:
        track.append(_gap(shot_seconds))


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #
def build(storyboard: Storyboard | None = None, render_dir: Path | None = None,
          out_stem: str | None = None) -> tuple[Path, Path | None, float]:
    """Assemble the timeline and write .otio (+ .fcpxml). Returns (otio, fcpxml, runtime)."""
    sb = storyboard or load()
    render_dir = Path(render_dir) if render_dir else (config.ROOT / "render")
    narr_dir = config.AUDIO_DIR / "narration"
    sfx_dir = config.AUDIO_DIR / "sfx"

    tl = otio.schema.Timeline(name=sb.title or "The Illuminated Bestiary")
    V = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)
    A_narr = otio.schema.Track(name="Narration", kind=otio.schema.TrackKind.Audio)
    A_sfx = otio.schema.Track(name="SFX", kind=otio.schema.TrackKind.Audio)
    A_music = otio.schema.Track(name="Music", kind=otio.schema.TrackKind.Audio)

    runtime = 0.0
    missing: list[str] = []
    for shot in sb.shots:
        dur = float(shot.camera.duration) if shot.camera else 6.0
        runtime += dur

        # V1 — the rendered clip (or a gap if it's not there yet)
        mp4 = render_dir / f"{shot.scene_id}.mp4"
        if mp4.exists():
            V.append(_clip(shot.scene_id, mp4, dur))
        else:
            missing.append(shot.scene_id)
            V.append(_gap(dur))

        # A1 — narration for this beat
        nf = narr_dir / f"{shot.scene_id}.mp3"
        if nf.exists():
            nd = min(_probe_seconds(nf), dur)
            _fill(A_narr, nd, dur, _clip(f"{shot.scene_id}_vo", nf, nd))
        else:
            _fill(A_narr, 0, dur, None)

        # A2 — sound effect, only where the beat carries one
        xf = sfx_dir / f"{shot.scene_id}.mp3"
        if (shot.sfx or "").strip() and xf.exists():
            xd = min(_probe_seconds(xf), dur)
            _fill(A_sfx, xd, dur, _clip(f"{shot.scene_id}_sfx", xf, xd))
        else:
            _fill(A_sfx, 0, dur, None)

    # A3 — music bed, looped to cover the runtime
    if sb.music_track:
        mpath = config.AUDIO_POOL / sb.music_track
        if mpath.exists():
            mdur = _probe_seconds(mpath)
            filled = 0.0
            i = 0
            while filled < runtime - 0.05 and mdur > 0:
                seg = min(mdur, runtime - filled)
                A_music.append(_clip(f"music_{i}", mpath, seg, media_seconds=mdur))
                filled += seg
                i += 1

    tl.tracks.extend([V, A_narr, A_sfx, A_music])

    slug = re.sub(r"[^a-z0-9]+", "_", (out_stem or sb.title or "deep_root_lore").lower()).strip("_") or "timeline"
    otio_path = config.ROOT / f"{slug}.otio"
    fcpxml_path: Path | None = config.ROOT / f"{slug}.fcpxml"
    otio.adapters.write_to_file(tl, str(otio_path))
    try:
        otio.adapters.write_to_file(tl, str(fcpxml_path), adapter_name="fcpx_xml")
    except Exception as exc:  # noqa: BLE001
        print(f"  !! FCPXML export failed ({exc}); .otio is still valid.")
        fcpxml_path = None

    if missing:
        print(f"  note: {len(missing)} shot(s) had no render yet -> left as gaps: {missing}")
    return otio_path, fcpxml_path, runtime


def build_preview(storyboard: Storyboard | None = None, render_dir: Path | None = None,
                  out: Path | None = None, height: int = 480) -> tuple[Path, float]:
    """Render a compact *review proxy*: the clips concatenated with a
    narration+SFX+music mix muxed in, scaled down for quick sharing.

    This is NOT the master (that is finished by hand in Resolve, per Gate 2) —
    it just lets a reviewer watch the assembled cut end to end.
    """
    import subprocess
    import tempfile

    import librosa
    import numpy as np
    from scipy.io import wavfile

    sb = storyboard or load()
    render_dir = Path(render_dir) if render_dir else (config.ROOT / "render")
    narr_dir, sfx_dir = config.AUDIO_DIR / "narration", config.AUDIO_DIR / "sfx"
    sr = 44100

    durs = [float(s.camera.duration) if s.camera else 6.0 for s in sb.shots]
    offsets, acc = [], 0.0
    for d in durs:
        offsets.append(acc)
        acc += d
    runtime = acc
    total = int(runtime * sr)
    mix = np.zeros((total, 2), np.float32)

    def _load_stereo(path: Path, level: float) -> np.ndarray:
        y, _ = librosa.load(str(path), sr=sr, mono=False)
        if y.ndim == 1:
            y = np.stack([y, y])
        return (y.T.astype(np.float32)) * level

    def _place(path: Path, off: float, level: float) -> None:
        seg = _load_stereo(path, level)
        start = int(off * sr)
        end = min(start + seg.shape[0], total)
        if end > start:
            mix[start:end] += seg[: end - start]

    for shot, off in zip(sb.shots, offsets):
        nf = narr_dir / f"{shot.scene_id}.mp3"
        if nf.exists():
            _place(nf, off, 1.0)
        xf = sfx_dir / f"{shot.scene_id}.mp3"
        if (shot.sfx or "").strip() and xf.exists():
            _place(xf, off, 0.55)
    if sb.music_track:
        mp = config.AUDIO_POOL / sb.music_track
        if mp.exists():
            bed = _load_stereo(mp, 0.22)
            pos = 0
            while pos < total:
                seg = bed[: min(bed.shape[0], total - pos)]
                mix[pos: pos + seg.shape[0]] += seg
                pos += bed.shape[0]

    peak = float(np.max(np.abs(mix))) or 1.0
    if peak > 1.0:
        mix /= peak

    scratch = Path(tempfile.gettempdir())
    wav = scratch / "_leshy_mix.wav"
    wavfile.write(str(wav), sr, (mix * 32767).astype(np.int16))
    concat = scratch / "_leshy_concat.txt"
    concat.write_text(
        "".join(f"file '{(render_dir / (s.scene_id + '.mp4')).resolve().as_posix()}'\n"
                for s in sb.shots),
        encoding="utf-8",
    )
    tmpv = scratch / "_leshy_v.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                    "-i", str(concat), "-c", "copy", str(tmpv)], check=True)
    out = Path(out) if out else (render_dir / "_preview.mp4")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(tmpv), "-i", str(wav),
                    "-vf", f"scale=-2:{height}", "-c:v", "libx264", "-crf", "30",
                    "-preset", "veryfast", "-c:a", "aac", "-b:a", "128k",
                    "-shortest", str(out)], check=True)
    return out, runtime


def _main() -> None:
    parser = argparse.ArgumentParser(description="The Illuminated Bestiary timeline assembly.")
    parser.add_argument("--preview", action="store_true",
                        help="also render a watchable review-proxy mp4 (not the master).")
    args = parser.parse_args()
    otio_path, fcpxml_path, runtime = build()
    print(f"runtime: {runtime:.1f}s (~{runtime/60:.1f} min)")
    print(f"wrote: {otio_path.name}" + (f" + {fcpxml_path.name}" if fcpxml_path else ""))
    if args.preview:
        out, _ = build_preview()
        print(f"preview: {out}")


if __name__ == "__main__":
    _main()


if __name__ == "__main__":
    _main()

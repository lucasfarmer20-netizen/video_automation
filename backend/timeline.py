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
    python -m backend.timeline
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

import opentimelineio as otio

from . import audio, config
from .ffmpeg_bin import ffmpeg_bin, ffprobe_bin
from .manifest import Storyboard, load

FPS = 24


# --------------------------------------------------------------------------- #
# small OTIO helpers
# --------------------------------------------------------------------------- #
def placeholder_clip(dest: Path, seconds: float, label: str = "") -> Path:
    """A black clip of exactly ``seconds``, standing in for a missing render.

    Deliberately blank rather than a caption: drawing text needs a font
    configuration that is not guaranteed on every host, and a placeholder that
    fails to render is worse than one that is plainly empty. What identifies it
    is not the picture — that lives in the slot (see backend/slots.py), which
    knows the shot, the beat and the duration this stands in for.

    Rebuilt whenever the length changes, so a re-timed beat cannot leave a
    placeholder of the old length behind and shift everything after it.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    want = max(0.04, round(float(seconds), 3))
    if dest.is_file():
        try:
            if abs(_probe_seconds(dest) - want) <= 0.05:
                return dest
        except Exception:  # noqa: BLE001 - a bad probe just means rebuild it
            pass
    subprocess.run(
        [ffmpeg_bin(), "-y", "-v", "error",
         "-f", "lavfi", "-i", f"color=c=black:s=1280x720:r=24:d={want}",
         "-t", f"{want}", "-c:v", "libx264", "-crf", "30", "-pix_fmt", "yuv420p",
         "-an", str(dest)],
        check=True,
    )
    return dest


def _probe_seconds(path: Path) -> float:
    try:
        out = subprocess.run(
            [ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=10
        )
        val = float(out.stdout.strip() or 0.0)
        if val > 0:
            return val
    except Exception:
        pass
        
    try:
        import librosa
        return float(librosa.get_duration(filename=str(path)))
    except Exception:
        pass
    return 6.0


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
def _make_resolve_compatible(path: Path, width: int, height: int, event_name: str) -> None:
    """Patch the OTIO fcpx adapter's output so DaVinci Resolve accepts it.

    The adapter emits a flat ``<project>`` directly under ``<fcpxml>`` with a
    dimensionless ``<format>`` — Resolve rejects that ("Unable to find inherited
    value for key 'library'"). We add width/height to the format and wrap the
    project in the required ``<library><event>`` hierarchy.
    """
    import xml.etree.ElementTree as ET

    root = ET.parse(path).getroot()
    for fmt in root.iter("format"):
        fmt.set("width", str(width))
        fmt.set("height", str(height))
        if not fmt.get("name"):
            fmt.set("name", f"FFVideoFormat{height}p")
    proj = root.find("project")
    if proj is not None and root.find("library") is None:
        root.remove(proj)
        event = ET.SubElement(ET.SubElement(root, "library"), "event")
        event.set("name", event_name or "The Illuminated Bestiary")
        event.append(proj)
        
    # Add DaVinci Resolve markers to video clips
    for clip in root.iter("clip"):
        name = clip.get("name", "")
        if name and not clip.find("marker"):
            marker = ET.SubElement(clip, "marker")
            marker.set("start", "0s")
            marker.set("duration", "1s")
            marker.set("value", f"Beat {name}")
            if "hero" in name.lower() or "video" in name.lower():
                marker.set("color", "blue")
            elif "sfx" in name.lower():
                marker.set("color", "orange")
            elif "vo" in name.lower():
                marker.set("color", "green")

    body = ET.tostring(root, encoding="unicode")
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n\n' + body + "\n",
        encoding="utf-8",
    )


def _lay_beat(track, dur: float, head: float, clip_seconds: float,
              make_clip) -> None:
    """Append head-gap + clip + tail-gap totalling EXACTLY the beat's frame count.

    Each piece used to be converted to frames independently, so head/clip/tail
    could round to one frame more or fewer than the beat itself. One frame per
    beat is invisible; across a 158-beat episode it accumulated to over a second,
    and the audio tracks ended a different length from V1 -- narration drifting
    progressively early through the FCPXML while build_preview, which mixes
    sample-accurately, stayed locked.

    Quantising the beat ONCE and deriving the tail by subtraction makes the parts
    sum to the whole by construction rather than by luck.
    """
    total_f = _rt(dur).value
    head_f = max(0, min(_rt(max(0.0, head)).value, total_f))
    clip_f = max(0, min(_rt(max(0.0, clip_seconds)).value, total_f - head_f))
    tail_f = total_f - head_f - clip_f

    if clip_f <= 0:
        if total_f > 0:
            track.append(_gap_frames(total_f))
        return
    if head_f > 0:
        track.append(_gap_frames(head_f))
    track.append(make_clip(clip_f / FPS))
    if tail_f > 0:
        track.append(_gap_frames(tail_f))


def _gap_frames(frames: int):
    return otio.schema.Gap(source_range=otio.opentime.TimeRange(
        otio.opentime.RationalTime(0, FPS), otio.opentime.RationalTime(frames, FPS)))


def _layer_source(lay, sfx_dir: Path, scene_id: str) -> Path | None:
    """Resolve one SFX layer to a real file, the way ``build_preview`` does."""
    src = Path(lay.file) if lay.file else (sfx_dir / f"{scene_id}.mp3")
    if not src.is_absolute():
        src = config.resolve_media(str(src), scene_id) or (sfx_dir / src.name)
    src = Path(src) if src else None
    return src if src and src.is_file() else None


def build(storyboard: Storyboard | None = None, render_dir: Path | None = None,
          out_stem: str | None = None) -> tuple[Path, Path | None, float]:
    """Assemble the timeline and write .otio (+ .fcpxml). Returns (otio, fcpxml, runtime)."""
    sb = storyboard or load()
    ep = config.episode_paths(sb.title)
    render_dir = Path(render_dir) if render_dir else ep["render"]
    narr_dir = ep["narration"]
    sfx_dir = ep["sfx"]

    tl = otio.schema.Timeline(name=sb.title or "The Illuminated Bestiary")
    V = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)
    A_narr = otio.schema.Track(name="Narration", kind=otio.schema.TrackKind.Audio)
    # One track per layer slot. build() tested the legacy single-file shape --
    # `shot.sfx` non-empty AND <scene>.mp3 present -- while the studio writes
    # layers as <scene>__L1.mp3 with an empty prompt on uploads. So the A2 track
    # got a pure gap and the Resolve deliverable (and the bundle ZIP, which packs
    # only what the XML references) shipped with NO ambience at all, while the
    # preview mixed it correctly. That breaks the Gate-2 promise that both
    # finishing routes come from the same approved manifest.
    _layer_lists = {sh.scene_id: audio.resolve_sfx_layers(sh, sfx_dir) for sh in sb.shots}
    _n_sfx = max([len(v) for v in _layer_lists.values()] or [0]) or 1
    A_sfx_tracks = [
        otio.schema.Track(name="SFX" if _n_sfx == 1 else f"SFX {i + 1}",
                          kind=otio.schema.TrackKind.Audio)
        for i in range(_n_sfx)
    ]
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

        # A1 — narration, at the offset the studio set for it.
        #
        # This butted every VO to the head of its beat and ignored
        # offset_narration entirely, while build_preview honours it (and the
        # fades) at timeline.py's _place call. So the reviewer approved a cut at
        # Gate 2 and Resolve received a different one, with the voice shifted by
        # whatever offset had been dialled in. Same disagreement the SFX branch
        # below already had, and the same fix.
        #
        # Fades are deliberately NOT emitted: OTIO expresses them as effects that
        # the fcpx adapter does not round-trip, and inventing a representation
        # here would be a third opinion about the mix. build_preview remains the
        # authority on levels and fades; this track carries position and length.
        nf = narr_dir / f"{shot.scene_id}.mp3"
        if nf.exists():
            head = min(max(0.0, float(getattr(shot, "offset_narration", 0.0) or 0.0)), dur)
            nd = min(_probe_seconds(nf), max(0.0, dur - head))
            _lay_beat(A_narr, dur, head, nd,
                      lambda secs: _clip(f"{shot.scene_id}_vo", nf, secs))
        else:
            A_narr.append(_gap_frames(_rt(dur).value))

        # A2.. — every ambience layer this beat carries, one per track, each
        # positioned at its own offset. Every track advances by exactly `dur` so
        # the beats stay aligned across tracks.
        layers = _layer_lists.get(shot.scene_id) or []
        for idx, track in enumerate(A_sfx_tracks):
            lay = layers[idx] if idx < len(layers) else None
            src = _layer_source(lay, sfx_dir, shot.scene_id) if lay else None
            if src is None:
                track.append(_gap_frames(_rt(dur).value))
                continue
            # A negative offset would start this sound under the previous beat,
            # which a per-beat track cannot express; clamp to the beat's head.
            head = min(max(0.0, float(lay.offset or 0.0)), dur)
            xd = min(_probe_seconds(src), max(0.0, dur - head))
            name = f"{shot.scene_id}_{lay.label or 'sfx'}{idx + 1}"
            _lay_beat(track, dur, head, xd, lambda secs, n=name, sp=src: _clip(n, sp, secs))

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

    # Every audio track must end exactly where the picture does. Each piece used
    # to be rounded to frames independently, so tracks drifted apart by a frame
    # per beat and narration landed progressively early in the FCPXML. This is the
    # check that keeps it that way -- a note, not an exception, because a timeline
    # that is slightly wrong is still worth delivering, and silence is what let
    # the drift accumulate unnoticed in the first place.
    v_frames = sum(i.source_range.duration.value for i in V)
    for _t in (A_narr, *A_sfx_tracks):
        t_frames = sum(i.source_range.duration.value for i in _t)
        if t_frames != v_frames:
            drift = (t_frames - v_frames) / FPS
            print(f"  !! {_t.name} is {drift:+.3f}s against the picture "
                  f"({t_frames} vs {v_frames} frames) — tracks will not stay in sync.")

    tl.tracks.extend([V, A_narr, *A_sfx_tracks, A_music])

    slug = re.sub(r"[^a-z0-9]+", "_", (out_stem or sb.title or "deep_root_lore").lower()).strip("_") or "timeline"
    # Write next to the manifest, i.e. into the project directory on the GCS
    # mount. These used to go to config.ROOT — /app inside the container, which
    # is ephemeral in-memory storage: the export was unreachable by any route and
    # evaporated on the next cold start, while the UI reported it as ready.
    out_dir_root = config.project_dir()
    out_dir_root.mkdir(parents=True, exist_ok=True)
    otio_path = out_dir_root / f"{slug}.otio"
    fcpxml_path: Path | None = out_dir_root / f"{slug}.fcpxml"
    otio.adapters.write_to_file(tl, str(otio_path))
    try:
        otio.adapters.write_to_file(tl, str(fcpxml_path), adapter_name="fcpx_xml")
        _make_resolve_compatible(fcpxml_path, 1280, 720, sb.title)
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
    ep = config.episode_paths(sb.title)
    render_dir = Path(render_dir) if render_dir else ep["render"]
    narr_dir, sfx_dir = ep["narration"], ep["sfx"]
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

    def _place(path: Path, off: float, level: float,
               fade_in: float = 0.0, fade_out: float = 0.0,
               loop_to: float | None = None) -> None:
        seg = _load_stereo(path, level)
        # Tile before fading, so the fade-out lands on the end of the filled
        # span rather than on the end of the first repetition.
        if loop_to and loop_to > 0:
            seg = audio.loop_to_length(seg, sr, loop_to)
        if fade_in or fade_out:
            seg = audio.apply_fades(seg, sr, fade_in, fade_out)
        start = int(off * sr)
        # A layer may start before the episode does (a negative offset on the
        # first beat). Trim the head rather than dropping the whole clip.
        if start < 0:
            seg = seg[-start:]
            start = 0
        end = min(start + seg.shape[0], total)
        if end > start:
            mix[start:end] += seg[: end - start]

    # Levels come from the manifest (Storyboard.mix) so they are tunable per
    # episode instead of being frozen into the renderer.
    mix_cfg = getattr(sb, "mix", None)
    solo = (getattr(mix_cfg, "solo", "") or "").strip()

    def _bus(name: str, level: float) -> float:
        """Level after solo and mute. Solo wins: it is the point of solo."""
        if solo:
            return level if solo == name else 0.0
        return 0.0 if getattr(mix_cfg, f"mute_{name}", False) else level

    lvl_narr = _bus("narration", float(getattr(mix_cfg, "narration", 1.0)))
    lvl_sfx = _bus("sfx", float(getattr(mix_cfg, "sfx", 0.15)))
    lvl_music = _bus("music", float(getattr(mix_cfg, "music", 0.20)))
    if solo or lvl_narr == 0 or lvl_sfx == 0 or lvl_music == 0:
        print(f"mix: narration={lvl_narr:.2f} sfx={lvl_sfx:.2f} music={lvl_music:.2f}"
              + (f" (solo: {solo})" if solo else " (muted buses)"))

    for shot, off, dur in zip(sb.shots, offsets, durs):
        # Bus level x per-beat trim. The trim is what lets a beat whose stem came
        # back 13 dB hot sit with the rest without moving the whole bus.
        g_narr = float(getattr(shot, "gain_narration", 1.0) or 1.0)
        nf = narr_dir / f"{shot.scene_id}.mp3"
        if nf.exists():
            _place(nf, off + float(getattr(shot, "offset_narration", 0.0) or 0.0),
                   lvl_narr * g_narr,
                   float(getattr(shot, "fade_in_narration", 0.0) or 0.0),
                   float(getattr(shot, "fade_out_narration", 0.0) or 0.0))

        # Layers, each positioned relative to this beat. A negative offset starts
        # a sound under the previous shot -- _place clamps to the episode, so a
        # layer that would begin before 0:00 is simply trimmed at the head.
        for lay in audio.resolve_sfx_layers(shot, sfx_dir):
            src = Path(lay.file) if lay.file else (sfx_dir / f"{shot.scene_id}.mp3")
            if not src.is_absolute():
                src = config.resolve_media(str(src), shot.scene_id) or (sfx_dir / src.name)
            if not src or not Path(src).is_file():
                continue
            lay_off = float(lay.offset or 0.0)
            # Fill from where the layer starts to the end of its beat. A layer
            # that starts early (negative offset) has further to cover, not less.
            fill = (dur - lay_off) if audio.layer_loops(lay) else None
            _place(Path(src), off + lay_off,
                   lvl_sfx * float(lay.gain or 1.0),
                   float(lay.fade_in or 0.0), float(lay.fade_out or 0.0),
                   loop_to=fill)
    if sb.music_track:
        mp = config.AUDIO_POOL / sb.music_track
        if mp.exists():
            bed = _load_stereo(mp, lvl_music)
            pos = 0
            while pos < total:
                seg = bed[: min(bed.shape[0], total - pos)]
                mix[pos: pos + seg.shape[0]] += seg
                pos += bed.shape[0]

    peak = float(np.max(np.abs(mix))) or 1.0
    if peak > 1.0:
        mix /= peak

    # Per-episode temp names. These were hardcoded "_leshy_*" from when the
    # pipeline built one episode, which is both confusing in a traceback and a
    # collision waiting to happen: two builds would write the same three files.
    scratch = Path(tempfile.gettempdir())
    tag = config.slug(sb.title or "episode")[:40] or "episode"
    wav = scratch / f"_{tag}_mix.wav"
    wavfile.write(str(wav), sr, (mix * 32767).astype(np.int16))
    concat = scratch / f"_{tag}_concat.txt"
    # A beat with no clip gets a placeholder rather than blocking the cut.
    # Contract §6.2 is explicit that Draft 1 may be built before generation
    # finishes; refusing outright meant a single un-rendered beat withheld the
    # whole film, which is the opposite of "get me to a watchable first cut".
    #
    # The placeholder is a real clip of the beat's own length, so everything
    # after it sits at the right time and the runtime is honest. What it must
    # never be is silent substitution (§11.2): it is not another beat's footage,
    # and every one of them is named in the log and counted in the sidecar.
    missing = [s.scene_id for s in sb.shots
               if not (render_dir / f"{s.scene_id}.mp4").is_file()]
    placeholder_dir = render_dir / "_placeholders"
    sources: list[Path] = []
    for s in sb.shots:
        real = render_dir / f"{s.scene_id}.mp4"
        if real.is_file():
            sources.append(real)
            continue
        seconds = float(s.camera.duration) if s.camera else 0.0
        sources.append(placeholder_clip(placeholder_dir / f"{s.scene_id}.mp4",
                                        seconds, s.scene_id))
    if missing:
        print(f"  note: {len(missing)} beat(s) have no render yet -> placeholders: "
              f"{', '.join(missing[:8])}{' ...' if len(missing) > 8 else ''}")

    concat.write_text(
        "".join(f"file '{p.resolve().as_posix()}'\n" for p in sources),
        encoding="utf-8",
    )
    tmpv = scratch / f"_{tag}_v.mp4"
    subprocess.run([ffmpeg_bin(), "-y", "-v", "error", "-f", "concat", "-safe", "0",
                    "-i", str(concat), "-c", "copy", str(tmpv)], check=True)
    out = Path(out) if out else (render_dir / "_preview.mp4")
    subprocess.run([ffmpeg_bin(), "-y", "-v", "error", "-i", str(tmpv), "-i", str(wav),
                    "-vf", f"scale=-2:{height}", "-c:v", "libx264", "-crf", "30",
                    "-preset", "veryfast", "-c:a", "aac", "-b:a", "128k",
                    "-shortest", str(out)], check=True)

    # The preview's own timing, written beside it. The timeline view is laid out
    # from LIVE manifest durations, but this file was rendered from whatever they
    # were at build time -- so a playhead driven by the live manifest would point
    # at the wrong beat the moment anything is retimed, silently. Mapping video
    # time through this sidecar instead makes the playhead describe the video
    # that actually exists, and lets the UI say when the two have diverged.
    import json as _json
    meta = {
        "runtime": round(runtime, 3),
        "built_at": int(Path(out).stat().st_mtime) if Path(out).exists() else 0,
        "height": height,
        "beats": [
            {"scene_id": s.scene_id, "start": round(o, 3), "duration": round(d, 3)}
            for s, o, d in zip(sb.shots, offsets, durs)
        ],
    }
    try:
        (Path(out).parent / "_preview.json").write_text(
            _json.dumps(meta, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"preview: could not write timing sidecar ({exc})")
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

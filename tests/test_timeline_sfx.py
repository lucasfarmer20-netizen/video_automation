"""FCPXML/OTIO must carry the same ambience the preview mixes.

Gate 2 promises both finishing routes come from one approved manifest. build()
tested the legacy single-file shape (`shot.sfx` non-empty AND <scene>.mp3 on
disk) while the studio writes layers as <scene>__L1.mp3 with an empty prompt on
uploads -- so the Resolve deliverable, and the bundle ZIP that packs only what
the XML references, shipped with no ambience at all.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(m, types.ModuleType(m))

otio = pytest.importorskip("opentimelineio")
from backend import timeline as T  # noqa: E402
from backend.manifest import AudioLayer, Camera, Shot, Storyboard  # noqa: E402


def _mp3(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00" * 2048)
    return p


def _sb(shots):
    return Storyboard(title="T", shots=shots)


def _audio_tracks(tl):
    return [t for t in tl.tracks if t.kind == otio.schema.TrackKind.Audio]


def test_layered_sfx_reaches_the_timeline(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "_probe_seconds", lambda p: 3.0)
    sfx_dir = tmp_path / "sfx"
    l1 = _mp3(sfx_dir / "s001__L1.mp3")
    l2 = _mp3(sfx_dir / "s001__L2.mp3")

    shot = Shot(scene_id="s001", narration="n", prompt="p",
                camera=Camera(move="static", duration=10.0), sfx="")
    shot.sfx_layers = [AudioLayer(id="a", prompt="", file=str(l1), label="wind"),
                       AudioLayer(id="b", prompt="", file=str(l2), label="fire")]

    tl = otio.schema.Timeline()
    monkeypatch.setattr(T.otio.schema, "Timeline", lambda **k: tl, raising=False)

    layers = T.audio.resolve_sfx_layers(shot, sfx_dir)
    assert len(layers) == 2, "both uploaded layers must resolve"
    # The legacy predicate that build() used would have rejected this beat:
    assert not ((shot.sfx or "").strip() and (sfx_dir / "s001.mp3").exists())
    for lay in layers:
        assert T._layer_source(lay, sfx_dir, "s001") is not None


def test_a_layer_with_an_empty_prompt_still_resolves(tmp_path):
    """Uploaded layers carry no prompt, which is what the old test keyed on."""
    sfx_dir = tmp_path / "sfx"
    f = _mp3(sfx_dir / "s002__L1.mp3")
    shot = Shot(scene_id="s002", narration="n", prompt="p",
                camera=Camera(move="static", duration=8.0), sfx="")
    shot.sfx_layers = [AudioLayer(id="a", prompt="", file=str(f), label="room")]
    assert len(T.audio.resolve_sfx_layers(shot, sfx_dir)) == 1


def test_legacy_single_file_beats_still_work(tmp_path):
    """The old shape must keep resolving -- manifests were not migrated."""
    sfx_dir = tmp_path / "sfx"
    _mp3(sfx_dir / "s003.mp3")
    shot = Shot(scene_id="s003", narration="n", prompt="p",
                camera=Camera(move="static", duration=6.0), sfx="a wind prompt")
    layers = T.audio.resolve_sfx_layers(shot, sfx_dir)
    assert len(layers) == 1 and layers[0].id == "legacy"
    assert T._layer_source(layers[0], sfx_dir, "s003") is not None


def test_a_missing_layer_file_resolves_to_nothing(tmp_path):
    sfx_dir = tmp_path / "sfx"; sfx_dir.mkdir()
    shot = Shot(scene_id="s004", narration="n", prompt="p",
                camera=Camera(move="static", duration=6.0), sfx="")
    lay = AudioLayer(id="a", prompt="", file=str(sfx_dir / "gone.mp3"), label="x")
    assert T._layer_source(lay, sfx_dir, "s004") is None


def test_build_emits_one_audio_track_per_layer_and_keeps_beats_aligned(tmp_path, monkeypatch):
    """The real deliverable: run build() and inspect the OTIO it writes."""
    monkeypatch.setattr(T, "_probe_seconds", lambda p: 3.0)

    proj = tmp_path / "proj"
    sfx_dir, narr_dir, render_dir = proj / "sfx", proj / "narr", proj / "render"
    for d in (sfx_dir, narr_dir, render_dir):
        d.mkdir(parents=True)
    monkeypatch.setattr(T.config, "episode_paths",
                        lambda title: {"render": render_dir, "narration": narr_dir,
                                       "sfx": sfx_dir, "root": proj})
    monkeypatch.setattr(T.config, "MANIFEST_PATH", proj / "storyboard_manifest.json")

    l1 = _mp3(sfx_dir / "s001__L1.mp3")
    l2 = _mp3(sfx_dir / "s001__L2.mp3")
    s1 = Shot(scene_id="s001", narration="n", prompt="p",
              camera=Camera(move="static", duration=10.0), sfx="")
    s1.sfx_layers = [AudioLayer(id="a", prompt="", file=str(l1), label="wind"),
                     AudioLayer(id="b", prompt="", file=str(l2), label="fire",
                                offset=2.0)]
    # A second beat with no ambience at all -- its slot must still advance.
    s2 = Shot(scene_id="s002", narration="n", prompt="p",
              camera=Camera(move="static", duration=6.0), sfx="")

    otio_path, _fcp, runtime = T.build(_sb([s1, s2]), render_dir=render_dir,
                                       out_stem=str(tmp_path / "out"))
    tl = otio.adapters.read_from_file(str(otio_path))
    sfx_tracks = [t for t in _audio_tracks(tl) if t.name.startswith("SFX")]

    assert len(sfx_tracks) == 2, "one track per layer slot"
    for t in sfx_tracks:
        secs = sum(i.source_range.duration.to_seconds() for i in t)
        assert abs(secs - runtime) < 0.05, f"{t.name} drifted from the timeline"

    named = [i.name for t in sfx_tracks for i in t if isinstance(i, otio.schema.Clip)]
    assert any("wind" in n for n in named) and any("fire" in n for n in named)


def test_audio_tracks_end_exactly_where_the_picture_does(tmp_path, monkeypatch):
    """Head/clip/tail were each rounded to frames independently, so they could sum
    to one frame more or fewer than the beat. Invisible per beat; over 158 beats it
    put narration more than a second early in the FCPXML while build_preview, which
    mixes sample-accurately, stayed locked."""
    proj = tmp_path / "proj"
    sfx_dir, narr_dir, render_dir = proj / "sfx", proj / "narr", proj / "render"
    for d in (sfx_dir, narr_dir, render_dir):
        d.mkdir(parents=True)
    monkeypatch.setattr(T.config, "episode_paths",
                        lambda title: {"render": render_dir, "narration": narr_dir,
                                       "sfx": sfx_dir, "root": proj})
    monkeypatch.setattr(T.config, "MANIFEST_PATH", proj / "storyboard_manifest.json")
    # Awkward lengths on purpose: these are the ones that round badly.
    monkeypatch.setattr(T, "_probe_seconds", lambda p: 1.134)

    shots = []
    for i in range(40):
        sid = f"s{i:03d}"
        _mp3(narr_dir / f"{sid}.mp3")
        _mp3(sfx_dir / f"{sid}__L1.mp3")
        sh = Shot(scene_id=sid, narration="n", prompt="p",
                  camera=Camera(move="static", duration=4.566 + (i % 5) * 0.077),
                  sfx="")
        sh.offset_narration = 0.761
        sh.sfx_layers = [AudioLayer(id="a", prompt="",
                                    file=str(sfx_dir / f"{sid}__L1.mp3"),
                                    label="room", offset=0.313)]
        shots.append(sh)

    otio_path, _f, _rt = T.build(_sb(shots), render_dir=render_dir,
                                 out_stem=str(tmp_path / "out"))
    tl = otio.adapters.read_from_file(str(otio_path))
    video = [t for t in tl.tracks if t.kind == otio.schema.TrackKind.Video][0]
    v_frames = sum(i.source_range.duration.value for i in video)

    for t in _audio_tracks(tl):
        if t.name == "Music":
            continue                       # looped bed, deliberately not beat-aligned
        frames = sum(i.source_range.duration.value for i in t)
        assert frames == v_frames, (
            f"{t.name} drifted {(frames - v_frames) / T.FPS:+.3f}s from the picture")

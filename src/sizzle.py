"""Sizzle-reel proof-of-concept builder for the 'Global Bestiary' format.

Builds two ~30s reels end to end, self-contained and resilient (skips work that
already exists, so it can resume):

* Reel A — folk-art: the Yuki-onna (Japan) in ukiyo-e woodblock.
* Reel B — photoreal: Black Shuck (England) in cinematic photorealism.

Both open on the photoreal bestiary book, then dive into the entity's world in
its own art register. Documentary cold-open VO (Vesper) + region ambience +
accents, depth-parallax motion, no gore (monetization-safe).

Pipeline per reel: fal Flux stills -> depth+parallax render -> Vesper TTS +
ElevenLabs ambience/SFX -> ffmpeg mux. Outputs sizzle/<reel>.mp4.

    python -m src.sizzle              # both reels
    python -m src.sizzle --reel A     # one
"""

from __future__ import annotations

import argparse
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy.io import wavfile

from . import assets, config
from . import depth as depthmod
from . import motion
from .manifest import Camera, MotionType, Shot

SIZZLE = config.ROOT / "sizzle"
FONT = r"C:\Windows\Fonts\georgiab.ttf"
FONT_REG = r"C:\Windows\Fonts\georgia.ttf"


@dataclass
class RShot:
    sid: str
    prompt: str
    move: str = "push_in"
    weight: float = 1.0
    fx: list[str] = field(default_factory=list)
    sfx: str = ""
    title: tuple[str, str] | None = None  # (name, subtitle) -> generated card


@dataclass
class Reel:
    key: str
    name: str
    style: str
    vo: str
    bed: str
    shots: list[RShot]


UKIYO = (", ukiyo-e Japanese woodblock print, nishiki-e, bold sumi ink outlines, "
         "flat mineral pigment colors, visible woodgrain and washi paper texture, "
         "Edo period, in the style of Kuniyoshi and Hokusai yokai prints, no gore")
PHOTO = (", photorealistic, cinematic, volumetric fog, storm lighting, desaturated "
         "moody color, film grain, atmospheric folk horror, no gore, no blood")
BOOK = ("photorealistic weathered human hands opening a huge ancient leather-bound "
        "tome on a dark wooden table by warm candlelight, turning to a chapter page "
        "of aged parchment with an ornate hand-inked title and an old {ill}, "
        "cinematic, shallow depth of field, dust motes in the light")

REELS = [
    Reel(
        key="A", name="Yuki-onna", style=UKIYO,
        vo=("In the mountains of northern Japan, they say the snow has a face. "
            "She comes on the worst night of the blizzard, in the shape of a beautiful "
            "woman. And every traveler who has met her agrees on one detail: her breath. "
            "A white cold that stops a man where he stands. Upright. Eyes open. Frozen "
            "solid by dawn. She leaves no footprints. She casts no warmth. And the oldest "
            "story says she was a wife once, to a man who swore he would never tell what "
            "he saw. He told."),
        bed=("A haunting minimal Japanese ambient bed for horror: a low shakuhachi flute "
             "drone, a single distant koto note, howling cold mountain wind, sparse and "
             "ominous, no beat"),
        shots=[
            RShot("a01", BOOK.format(ill="woodblock illustration"), "push_in", 1.2, ["dust motes"]),
            RShot("a02", "Yuki-onna the snow woman, a tall pale spectral woman in a flowing "
                  "white kimono standing in a mountain blizzard at night, long black hair, "
                  "serene and terrible pale face, swirling snow and wind" + UKIYO,
                  "push_in", 1.15, ["cold mist"], sfx="a sudden sharp gust of howling cold winter wind"),
            RShot("a03", "a lone traveler in a straw mino raincloak and conical hat struggling "
                  "through a deep mountain blizzard at night, tiny beneath towering snow-laden "
                  "pines, a paper lantern glowing" + UKIYO, "push_in", 1.0, ["drifting mist", "dust motes"]),
            RShot("a04", "the pale Yuki-onna leaning close over a sleeping traveler inside a "
                  "snow-covered mountain hut, her white frozen breath drifting over him, frost "
                  "blooming across the walls, cold blue palette" + UKIYO, "push_in", 1.0, ["cold mist"]),
            RShot("a05", "a single figure standing upright and utterly still in deep snow at dawn, "
                  "encased in glittering white frost, eyes open, silent snow-covered mountains "
                  "behind, eerie stillness, no gore" + UKIYO, "push_out", 1.1, ["dust motes"]),
            RShot("a06", "", weight=0.8, title=("YUKI-ONNA", "THE GLOBAL BESTIARY")),
        ],
    ),
    Reel(
        key="B", name="Black Shuck", style=PHOTO,
        vo=("On the storm coast of eastern England, they still lock their doors against a "
            "dog. Not a wolf. A dog. Black as the sea, the size of a calf, with a single "
            "eye that burns like a coal. They call him Black Shuck. For four hundred years "
            "the accounts say the same thing: he appears on the church path, on the empty "
            "road, at the foot of a dying man's bed. In 1577, they say he came through the "
            "doors of Bungay church during the service, and left scorch marks burned into "
            "the wood. You can still visit that door. They have never once repaired the "
            "scratches."),
        bed=("A dark cinematic horror ambient bed: a deep sub-bass drone, distant rolling "
             "thunder, howling North Sea storm wind against old stone, an ominous swell, no beat"),
        shots=[
            RShot("b01", BOOK.format(ill="engraving of a great black hound"), "push_in", 1.2, ["dust motes"]),
            RShot("b02", "a colossal shaggy black dog with matted wet fur and a single burning "
                  "ember eye emerging from thick coastal fog on a storm-lashed English churchyard "
                  "path at night, immense and unnaturally still" + PHOTO, "push_in", 1.15, ["cold fog"],
                  sfx="a single low, deep, wet, menacing dog growl, distant and ominous, no barking"),
            RShot("b03", "a rain-lashed medieval English village lane at night, cobblestones and "
                  "puddles, a huge black hound silhouette waiting at the far end beneath a "
                  "flickering lantern, storm and dread" + PHOTO, "push_in", 1.0, ["cold fog"]),
            RShot("b04", "the interior of an old stone English church during a violent storm, "
                  "candlelight and dramatic shadow, a massive dark hound shape bursting through the "
                  "great wooden doors, a terrified congregation in Elizabethan dress recoiling, no gore"
                  + PHOTO, "push_in", 1.0, ["candle flicker"],
                  sfx="a violent thunderclap and heavy wooden church doors bursting open in a storm"),
            RShot("b05", "a photorealistic close-up of deep claw scratch marks scorched black into an "
                  "ancient weathered oak church door, daylight, a small brass historical plaque beside "
                  "it, documentary photograph" + PHOTO, "push_in", 1.1, []),
            RShot("b06", "", weight=0.8, title=("BLACK SHUCK", "THE GLOBAL BESTIARY")),
        ],
    ),
]


# --------------------------------------------------------------------------- #
def _client():
    from elevenlabs.client import ElevenLabs
    return ElevenLabs(api_key=config.ELEVENLABS_API_KEY)


def _probe(path: Path) -> float:
    o = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    return float(o.stdout.strip() or 0.0)


def _tts(text: str, dest: Path, client) -> None:
    if dest.exists():
        return
    stream = client.text_to_speech.convert(
        voice_id=config.ELEVENLABS_VOICE_ID, text=text,
        model_id=config.ELEVENLABS_MODEL, output_format="mp3_44100_128")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as fh:
        for c in stream:
            if c:
                fh.write(c)


def _sfx(text: str, dest: Path, client, seconds: float | None = None) -> None:
    if dest.exists() or not text:
        return
    kw = {"text": text, "output_format": "mp3_44100_128", "prompt_influence": 0.3}
    if seconds:
        kw["duration_seconds"] = min(float(seconds), 22.0)
    stream = client.text_to_sound_effects.convert(**kw)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as fh:
        for c in stream:
            if c:
                fh.write(c)


def _title_card(name: str, sub: str, dest: Path, w: int = 1280, h: int = 720) -> None:
    img = Image.new("RGB", (w, h), (8, 7, 6))
    d = ImageDraw.Draw(img)
    f1 = ImageFont.truetype(FONT, 92)
    f2 = ImageFont.truetype(FONT_REG, 30)
    tb = d.textbbox((0, 0), name, font=f1)
    d.text(((w - (tb[2] - tb[0])) / 2, h / 2 - 70), name, font=f1, fill=(226, 205, 165))
    sb = d.textbbox((0, 0), sub, font=f2)
    d.text(((w - (sb[2] - sb[0])) / 2, h / 2 + 40), sub, font=f2, fill=(150, 140, 120))
    d.line([(w / 2 - 120, h / 2 + 20), (w / 2 + 120, h / 2 + 20)], fill=(120, 96, 60), width=2)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest)


def _mix_audio(reel: Reel, adir: Path, offsets: list[float], runtime: float, dest: Path) -> None:
    import librosa
    sr = 44100
    total = int(runtime * sr)
    mix = np.zeros((total, 2), np.float32)

    def load(path: Path, level: float) -> np.ndarray:
        y, _ = librosa.load(str(path), sr=sr, mono=False)
        if y.ndim == 1:
            y = np.stack([y, y])
        return y.T.astype(np.float32) * level

    def place(path: Path, off: float, level: float) -> None:
        seg = load(path, level)
        s = int(off * sr)
        e = min(s + seg.shape[0], total)
        if e > s:
            mix[s:e] += seg[: e - s]

    place(adir / "vo.mp3", 0.0, 1.0)                       # narration
    bedp = adir / "bed.mp3"
    if bedp.exists():
        bed = load(bedp, 0.28)
        pos = 0
        while pos < total:
            seg = bed[: min(bed.shape[0], total - pos)]
            mix[pos: pos + seg.shape[0]] += seg
            pos += bed.shape[0]
    for shot, off in zip(reel.shots, offsets):            # accents
        sp = adir / f"{shot.sid}_sfx.mp3"
        if shot.sfx and sp.exists():
            place(sp, off, 0.6)

    peak = float(np.max(np.abs(mix))) or 1.0
    if peak > 1.0:
        mix /= peak
    wavfile.write(str(dest), sr, (mix * 32767).astype(np.int16))


def build_reel(reel: Reel) -> Path:
    rdir = SIZZLE / reel.key
    imgdir, clipdir, adir = rdir / "img", rdir / "clips", rdir / "audio"
    for d in (imgdir, clipdir, adir):
        d.mkdir(parents=True, exist_ok=True)
    client = _client()
    print(f"\n=== Reel {reel.key}: {reel.name} ===")

    # 1) stills (or title cards)
    for shot in reel.shots:
        img = imgdir / f"{shot.sid}.png"
        if img.exists():
            continue
        if shot.title:
            print(f"  title card {shot.sid}")
            _title_card(*shot.title, img)
        else:
            print(f"  flux {shot.sid}")
            urls = assets._generate_flux(shot.prompt, 1, None)
            assets._download(urls[0], img)

    # 2) narration + ambience + accents
    _tts(reel.vo, adir / "vo.mp3", client)
    vo_secs = _probe(adir / "vo.mp3")
    _sfx(reel.bed, adir / "bed.mp3", client, seconds=22.0)
    for shot in reel.shots:
        if shot.sfx:
            _sfx(shot.sfx, adir / f"{shot.sid}_sfx.mp3", client, seconds=5.0)

    # 3) durations scaled so the video matches the VO length
    wsum = sum(s.weight for s in reel.shots)
    durs = [round(vo_secs * s.weight / wsum, 2) for s in reel.shots]
    offsets, acc = [], 0.0
    for d in durs:
        offsets.append(acc)
        acc += d
    runtime = acc

    # 4) render each shot (parallax for scenes, static hold for the title card)
    for shot, dur in zip(reel.shots, durs):
        clip = clipdir / f"{shot.sid}.mp4"
        if clip.exists():
            continue
        img = imgdir / f"{shot.sid}.png"
        s = Shot(
            scene_id=shot.sid,
            motion_type=MotionType.STATIC if shot.title else MotionType.PARALLAX,
            camera=Camera(move=("static" if shot.title else shot.move), duration=dur),
            fx=shot.fx,
            draft_image=str(img.relative_to(config.ROOT)).replace("\\", "/"),
        )
        print(f"  render {shot.sid} ({dur}s)")
        motion.render_shot(s, height=720, out_dir=clipdir)

    # 5) mix audio + concat video + mux
    mixwav = adir / "mix.wav"
    _mix_audio(reel, adir, offsets, runtime, mixwav)
    scratch = Path(tempfile.gettempdir())
    listp = scratch / f"_sizzle_{reel.key}.txt"
    listp.write_text("".join(f"file '{(clipdir / (s.sid + '.mp4')).resolve().as_posix()}'\n"
                             for s in reel.shots), encoding="utf-8")
    tmpv = scratch / f"_sizzle_{reel.key}.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                    "-i", str(listp), "-c", "copy", str(tmpv)], check=True)
    out = SIZZLE / f"sizzle_{reel.key}_{reel.name.lower().replace(' ', '_').replace('-', '_')}.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(tmpv), "-i", str(mixwav),
                    "-c:v", "libx264", "-crf", "22", "-preset", "medium", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "160k", "-shortest", str(out)], check=True)
    print(f"  -> {out}  ({runtime:.1f}s)")
    return out


def _main() -> None:
    ap = argparse.ArgumentParser(description="Global Bestiary sizzle-reel builder.")
    ap.add_argument("--reel", choices=["A", "B"], help="build only one reel.")
    args = ap.parse_args()
    config.require_for("assets")   # FAL
    config.require_for("audio")    # ElevenLabs
    outs = []
    for reel in REELS:
        if args.reel and reel.key != args.reel:
            continue
        try:
            outs.append(build_reel(reel))
        except Exception as exc:  # noqa: BLE001
            print(f"  !! Reel {reel.key} FAILED: {exc}")
    print("\nDONE. Reels:")
    for o in outs:
        print("  ", o)


if __name__ == "__main__":
    _main()

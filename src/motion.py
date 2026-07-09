"""Motion stage: 2.5D parallax + procedural-FX render engine (local, $0).

The free-motion workhorse that keeps per-video cost down: it turns an approved
still into a moving clip without touching a paid video API. Two tiers:

* ``static``   - the still held under a slow camera move-free hold plus procedural
  FX (grain, vignette breathe, candle flicker, mist).
* ``parallax`` - depth layers (from ``depth.py``) drift at distance-scaled rates
  under a slow camera move, gaps hidden by the inpainted background plate.

Frames are composited with Pillow (per-plane affine + alpha) and NumPy (FX), then
encoded to H.264 via imageio-ffmpeg (system ffmpeg). Silent clips; ``audio.py`` /
``timeline.py`` own sound and assembly.

CLI:
    python -m src.motion                 # render every non-paid approved shot
    python -m src.motion --scene s001    # one shot
    python -m src.motion --fps 30 --height 1080
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import imageio
import numpy as np
from PIL import Image
from scipy import ndimage

from . import config
from . import depth as depthmod
from .manifest import MotionType, Shot, load

RENDER_DIR = config.ROOT / "render"
DEFAULT_FPS = 24
DEFAULT_HEIGHT = 720            # 16:9 -> 1280x720; bump to 1080 for finals
OVERSCAN = 1.14                 # headroom so camera moves never expose an edge


# --------------------------------------------------------------------------- #
# easing + camera
# --------------------------------------------------------------------------- #
def _ease(t: float) -> float:
    """Smoothstep — slow in, slow out, for unhurried cinematic moves."""
    return t * t * (3.0 - 2.0 * t)


def _camera(move: str, t: float) -> tuple[float, float, float]:
    """Return (zoom, dx, dy) for the *camera* at eased time ``t`` in [0, 1].

    dx/dy are fractions of frame size; zoom is an extra scale on top of OVERSCAN.
    Amplitudes stay within the overscan margin so no border is ever revealed.
    """
    e = _ease(t)
    if move == "push_in":
        return 0.032 * e, 0.0, 0.0
    if move == "push_out":
        return 0.032 * (1.0 - e), 0.0, 0.0
    if move == "pan_left":
        return 0.0, (0.026 * (0.5 - e)), 0.0
    if move == "pan_right":
        return 0.0, (0.026 * (e - 0.5)), 0.0
    return 0.0, 0.0, 0.0  # static


def _plane_parallax(depth_center: float) -> float:
    """Nearer planes (higher depth) move/scale a little more under the camera."""
    return 0.25 + 0.55 * depth_center


# --------------------------------------------------------------------------- #
# per-plane transform + compositing
# --------------------------------------------------------------------------- #
def _prep(img: np.ndarray, out_w: int, out_h: int) -> Image.Image:
    """Resize a plate to the oversized working canvas (RGBA)."""
    w = int(round(out_w * OVERSCAN))
    h = int(round(out_h * OVERSCAN))
    mode = "RGBA" if img.shape[-1] == 4 else "RGB"
    return Image.fromarray(img, mode).resize((w, h), Image.LANCZOS).convert("RGBA")


def _transform(plane: Image.Image, zoom: float, dx: float, dy: float,
               out_w: int, out_h: int) -> Image.Image:
    """Sub-pixel scale-about-centre + translate, sampled straight to output size.

    Uses a single bilinear affine warp (no integer paste), so slow moves stay
    smooth instead of stair-stepping frame to frame.
    """
    W, H = plane.size
    s = 1.0 + zoom
    cx, cy = out_w / 2.0, out_h / 2.0
    ox, oy = (W - out_w) / 2.0, (H - out_h) / 2.0   # centre-crop offset (overscan)
    tx, ty = dx * out_w, dy * out_h
    # output (xo,yo) -> input (xi,yi): xi = a*xo + b*yo + c ; yi = d*xo + e*yo + f
    a = e = 1.0 / s
    c = ox + cx * (1.0 - 1.0 / s) - tx
    f = oy + cy * (1.0 - 1.0 / s) - ty
    return plane.transform((out_w, out_h), Image.AFFINE, (a, 0.0, c, 0.0, e, f),
                           resample=Image.BILINEAR, fillcolor=(0, 0, 0, 0))


def _composite_frame(layers: depthmod.ParallaxLayers, bg_img: Image.Image,
                     plane_imgs: list[tuple[Image.Image, float]],
                     move: str, t: float, out_w: int, out_h: int) -> np.ndarray:
    """Build one composited RGB frame at time ``t``."""
    zoom, dx, dy = _camera(move, t)

    # Background gets the smallest share of the move (far plane).
    frame = _transform(bg_img, zoom * 0.15, dx * 0.15, dy * 0.15, out_w, out_h)
    # Foreground planes, far -> near, each scaled by its parallax factor.
    for pimg, dcenter in plane_imgs:
        p = _plane_parallax(dcenter)
        frame.alpha_composite(_transform(pimg, zoom * p, dx * p, dy * p, out_w, out_h))
    return np.asarray(frame.convert("RGB")).astype(np.float32) / 255.0


# --------------------------------------------------------------------------- #
# procedural FX (operate on float RGB frames in [0, 1])
# --------------------------------------------------------------------------- #
def _radial(out_w: int, out_h: int) -> np.ndarray:
    yy, xx = np.mgrid[0:out_h, 0:out_w].astype(np.float32)
    cx, cy = out_w / 2.0, out_h / 2.0
    r = np.sqrt(((xx - cx) / cx) ** 2 + ((yy - cy) / cy) ** 2)
    return np.clip(r / math.sqrt(2), 0.0, 1.0)


def _smooth_noise(h: int, w: int, scale: int, rng: np.random.Generator) -> np.ndarray:
    """Low-frequency smooth noise field in [0, 1], via upsampled coarse noise."""
    ch, cw = max(2, h // scale), max(2, w // scale)
    coarse = rng.random((ch, cw), dtype=np.float32)
    return np.asarray(Image.fromarray((coarse * 255).astype(np.uint8))
                      .resize((w, h), Image.BICUBIC)).astype(np.float32) / 255.0


class FX:
    """Precomputes reusable fields; applies enabled effects per frame.

    Restraint is the house style: a gentle vignette and temporally-static paper
    grain on every shot, with mist/flicker/motes only where a shot asks for them.
    Grain is fixed across frames (real paper doesn't shimmer) which also keeps the
    clip compressible.
    """

    def __init__(self, fx: list[str], out_w: int, out_h: int, seed: int = 7):
        self.tokens = " ".join(fx).lower()
        self.w, self.h = out_w, out_h
        rng = np.random.default_rng(seed)
        self.vig = _radial(out_w, out_h)
        # soft, low-frequency haze (a few big blobs, heavily blurred) = atmosphere.
        # Generated wider than the frame so a drifting window never wraps (no seam).
        self._mist_pad = int(out_w * 0.09) + 2
        haze = _smooth_noise(out_h, out_w + self._mist_pad, 100, rng)
        mist = ndimage.gaussian_filter(haze, sigma=max(out_w, out_h) / 48.0)
        self.mist = mist - float(mist.mean())            # shape (h, w + pad)
        # temporally-static *paper* grain: coarse (clumps like paper tooth), not
        # per-pixel snow. Generated small then upsampled.
        g = rng.random((max(2, out_h // 3), max(2, out_w // 3)), dtype=np.float32)
        self.grain = np.asarray(
            Image.fromarray((g * 255).astype(np.uint8)).resize((out_w, out_h), Image.BILINEAR),
            dtype=np.float32,
        ) / 255.0 - 0.5
        # sparse dust-mote seed field
        self.motes = rng.random((out_h, out_w), dtype=np.float32)

    def _has(self, *keys: str) -> bool:
        return any(k in self.tokens for k in keys)

    def apply(self, frame: np.ndarray, t: float) -> np.ndarray:
        f = frame
        f = f * (1.0 - 0.38 * (self.vig ** 2.2)[..., None])
        if self._has("candle", "flicker", "firelight", "fire"):
            f = f * (1.0 + 0.045 * math.sin(t * 2 * math.pi * 4.5))
        if self._has("mist", "smoke", "fog", "light ray", "rays", "haze"):
            off = min(int(t * self.w * 0.06), self._mist_pad)  # slide, never wrap
            veil = self.mist[:, off:off + self.w] * 0.16
            f = f + veil[..., None] * np.array([0.9, 0.85, 0.72], np.float32)
        if self._has("dust", "mote", "ember", "spark"):
            roll = int(t * self.h * 0.05)
            motes = (np.roll(self.motes, roll, axis=0) > 0.9994).astype(np.float32)
            f = f + motes[..., None] * np.array([0.9, 0.82, 0.55], np.float32)
        # paper grain, modulated by luminance so shadows stay clean (no snow on black)
        lum = f.mean(axis=2)
        f = f + (self.grain * (0.3 + 0.7 * np.clip(lum, 0.0, 1.0)))[..., None] * 0.022
        return np.clip(f, 0.0, 1.0)


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #
def render_shot(shot: Shot, fps: int = DEFAULT_FPS, height: int = DEFAULT_HEIGHT,
                out_dir: Path = RENDER_DIR) -> Path:
    """Render one approved shot to a silent H.264 clip; return the output path."""
    if not shot.draft_image:
        raise ValueError(f"{shot.scene_id}: no chosen draft_image to render.")
    src = config.ROOT / shot.draft_image
    out_w, out_h = height * 16 // 9, height
    duration = float(shot.camera.duration) if shot.camera else 6.0
    move = shot.camera.move if shot.camera else "static"
    n_frames = max(1, int(round(duration * fps)))

    img = depthmod.load_rgb(src)
    if shot.motion_type == MotionType.PARALLAX:
        layers = depthmod.separate_layers(img, depthmod.estimate_depth(img), n_planes=4)
        bg_img = _prep(layers.background, out_w, out_h)
        plane_imgs = [(_prep(rgba, out_w, out_h), d) for rgba, d in layers.planes]
    else:  # STATIC — single held plate, no depth split, FX only
        layers = None
        bg_img = _prep(img, out_w, out_h)
        plane_imgs = []

    fx = FX(shot.fx or [], out_w, out_h, seed=abs(hash(shot.scene_id)) % 9973)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{shot.scene_id}.mp4"
    writer = imageio.get_writer(
        out_path, fps=fps, codec="libx264", macro_block_size=None,
        ffmpeg_log_level="error",
        output_params=["-crf", "20", "-pix_fmt", "yuv420p", "-preset", "medium"],
    )
    try:
        for i in range(n_frames):
            t = i / max(1, n_frames - 1)
            if shot.motion_type == MotionType.PARALLAX:
                frame = _composite_frame(layers, bg_img, plane_imgs, move, t, out_w, out_h)
            else:
                frame = np.asarray(bg_img.convert("RGB")).astype(np.float32) / 255.0
                # crop overscan to output for the static plate
                sh, sw = frame.shape[:2]
                y0, x0 = (sh - out_h) // 2, (sw - out_w) // 2
                frame = frame[y0:y0 + out_h, x0:x0 + out_w]
            frame = fx.apply(frame, t)
            writer.append_data((frame * 255).astype(np.uint8))
    finally:
        writer.close()
    return out_path


def render_all(only: set[str] | None = None, fps: int = DEFAULT_FPS,
               height: int = DEFAULT_HEIGHT) -> list[Path]:
    """Render every approved local-tier shot (static/parallax). Paid shots skipped."""
    sb = load()
    outs: list[Path] = []
    for shot in sb.shots:
        if only and shot.scene_id not in only:
            continue
        if shot.motion_type == MotionType.AI_VIDEO:
            continue  # Tier-C handled by the paid video stage, not here
        if not only and not shot.approved:
            continue
        print(f"Rendering {shot.scene_id} ({shot.motion_type.value}, {shot.camera.move}) ...")
        try:
            p = render_shot(shot, fps=fps, height=height)
            print(f"  -> {p.relative_to(config.ROOT)}")
            outs.append(p)
        except Exception as exc:  # noqa: BLE001 — resilient batch
            print(f"  !! {shot.scene_id} FAILED: {exc}")
    return outs


def _main() -> None:
    parser = argparse.ArgumentParser(description="Deep Root Lore local motion engine.")
    parser.add_argument("--scene", nargs="*", help="scene id(s) to render (default: all approved local).")
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    args = parser.parse_args()
    outs = render_all(only=set(args.scene) if args.scene else None,
                      fps=args.fps, height=args.height)
    print(f"\nRendered {len(outs)} clip(s) into {RENDER_DIR.relative_to(config.ROOT)}/")


if __name__ == "__main__":
    _main()

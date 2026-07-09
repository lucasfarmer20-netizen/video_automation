"""Motion stage: 2.5D parallax + procedural-FX render engine (local, $0).

The free-motion workhorse that keeps per-video cost down: it turns an approved
still into a moving clip without touching a paid video API. Two tiers:

* ``static``   - the still held under a slow, move-free hold plus procedural FX
  (grain, vignette, candle flicker, mist).
* ``parallax`` - a continuous per-pixel depth warp (depth from ``depth.py``):
  each pixel is inverse-sampled with a displacement set by its own depth under a
  slow camera move, so objects stay coherent (nearer moves more).

Frames are warped/composited with NumPy + SciPy and encoded to H.264 via
imageio-ffmpeg (system ffmpeg). Silent clips; ``audio.py`` / ``timeline.py`` own
sound and assembly.

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


# --------------------------------------------------------------------------- #
# easing + camera
# --------------------------------------------------------------------------- #
def _ease(t: float) -> float:
    """Smoothstep — slow in, slow out, for unhurried cinematic moves."""
    return t * t * (3.0 - 2.0 * t)


def _camera(move: str, t: float) -> tuple[float, float, float]:
    """Return (zoom, dx, dy) for the *camera* at eased time ``t`` in [0, 1].

    dx/dy are fractions of frame size; zoom is an extra magnification. Depth
    inverse-warping clamps at the edges, so no border is ever revealed.
    """
    e = _ease(t)
    if move == "push_in":
        return 0.05 * e, 0.0, 0.0
    if move == "push_out":
        return 0.05 * (1.0 - e), 0.0, 0.0
    if move == "pan_left":
        return 0.0, (0.04 * (0.5 - e)), 0.0
    if move == "pan_right":
        return 0.0, (0.04 * (e - 0.5)), 0.0
    return 0.0, 0.0, 0.0  # static


# --------------------------------------------------------------------------- #
# continuous depth warp (2.5D parallax)
# --------------------------------------------------------------------------- #
# Every pixel is displaced by its own depth (inverse-sampled), so objects move as
# coherent units — no discrete planes to split a trunk across a boundary (which
# doubled outlines) and no hard disocclusion holes; depth discontinuities become
# gentle local stretches instead. `disp` gives even far pixels a little motion so
# a push-in reads as the whole frame breathing, nearer faster.
def _warp_frame(src: np.ndarray, disp: np.ndarray, base_y: np.ndarray,
                base_x: np.ndarray, move: str, t: float, speed: float,
                out_w: int, out_h: int) -> np.ndarray:
    """Inverse-warp the source by the per-pixel depth displacement at time ``t``."""
    zoom, dx, dy = _camera(move, t)
    zoom, dx, dy = zoom * speed, dx * speed, dy * speed
    cx, cy = out_w / 2.0, out_h / 2.0
    scale = 1.0 + zoom * disp                    # nearer pixels magnify more
    sx = cx + (base_x - cx) / scale - (dx * out_w) * disp
    sy = cy + (base_y - cy) / scale - (dy * out_h) * disp
    coords = [sy, sx]
    out = np.empty((out_h, out_w, 3), np.float32)
    for c in range(3):
        out[..., c] = ndimage.map_coordinates(src[..., c], coords, order=1, mode="nearest")
    return out


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

    Restraint is the house style: a gentle vignette and fine film grain on every
    shot, with mist/flicker/motes only where a shot asks for them. Grain is the
    procedural film model — animated on twos, signal-dependent (see ``apply``).
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
        self._seed = int(seed)                           # per-frame film-grain seed
        # sparse dust-mote seed field
        self.motes = rng.random((out_h, out_w), dtype=np.float32)

    def _has(self, *keys: str) -> bool:
        return any(k in self.tokens for k in keys)

    def apply(self, frame: np.ndarray, t: float, fi: int) -> np.ndarray:
        f = frame
        f = f * (1.0 - 0.38 * (self.vig ** 2.2)[..., None])
        if self._has("candle", "flicker", "firelight", "fire"):
            f = f * (1.0 + 0.045 * math.sin(t * 2 * math.pi * 4.5))
        if self._has("mist", "smoke", "fog", "light ray", "rays", "haze"):
            off = min(int(t * self.w * 0.06), self._mist_pad)  # slide, never wrap
            veil = self.mist[:, off:off + self.w] * 0.16
            f = f + veil[..., None] * np.array([0.9, 0.85, 0.72], np.float32)
        if self._has("dust", "mote", "ember", "spark"):
            # Sparse, *soft*, dim specks that only show where light already falls
            # ("dust in light") — so they read as motes, not snow on the black.
            roll = int(t * self.h * 0.04)
            spec = (np.roll(self.motes, roll, axis=0) > 0.9997).astype(np.float32)
            spec = ndimage.gaussian_filter(spec, 0.8)          # soften to glows
            spec = spec * np.clip(f.mean(axis=2) * 1.6, 0.0, 1.0)  # gate to lit areas
            f = f + spec[..., None] * np.array([0.8, 0.72, 0.5], np.float32) * 0.6
        # Film grain: fresh monochromatic noise, but held "on twos" (updates every
        # 2nd frame ~= 12 fps) so it doesn't boil, given a grain "size" by a light
        # blur, signal-dependent (peaks in midtones, clean in the black).
        grng = np.random.default_rng(self._seed * 9769 + fi // 2)
        n = ndimage.gaussian_filter(
            grng.standard_normal((self.h, self.w), dtype=np.float32), 0.7)
        n *= 1.0 / (float(n.std()) + 1e-6)               # restore unit std after blur
        lum = f.mean(axis=2)
        mod = np.clip(lum * 2.4, 0.0, 1.0) * (1.0 - 0.35 * np.clip(lum, 0.0, 1.0))
        f = f + (n * mod)[..., None] * 0.022              # ~half the previous opacity
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
    speed = float(shot.camera.speed) if shot.camera else 1.0
    n_frames = max(1, int(round(duration * fps)))

    img = depthmod.load_rgb(src)
    src_rgb = np.asarray(Image.fromarray(img).resize((out_w, out_h), Image.LANCZOS),
                         dtype=np.float32) / 255.0
    is_parallax = shot.motion_type == MotionType.PARALLAX
    if is_parallax:
        depth = depthmod.estimate_depth(img)
        depth = np.asarray(Image.fromarray(depth).resize((out_w, out_h), Image.BILINEAR),
                           dtype=np.float32)
        depth = ndimage.gaussian_filter(depth, sigma=3.0)   # gentler disparity edges
        depth = (depth - depth.min()) / (depth.max() - depth.min() + 1e-6)
        disp = (0.25 + 0.75 * depth).astype(np.float32)     # even far pixels move a little
        base_y, base_x = (a.astype(np.float32) for a in np.mgrid[0:out_h, 0:out_w])

    # stable per-shot seed (avoids hash randomization across runs)
    seed = (sum(bytes(shot.scene_id, "utf-8")) * 131) % 100003
    fx = FX(shot.fx or [], out_w, out_h, seed=seed)

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
            if is_parallax:
                frame = _warp_frame(src_rgb, disp, base_y, base_x, move, t, speed, out_w, out_h)
            else:
                frame = src_rgb.copy()   # static plate; FX only
            frame = fx.apply(frame, t, i)
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

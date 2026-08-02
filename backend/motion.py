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
    python -m backend.motion                 # render every non-paid approved shot
    python -m backend.motion --scene s001    # one shot
    python -m backend.motion --fps 30 --height 1080
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
# A camera move is perceptible by its *rate*, not its total travel. The old
# constants (5% zoom / 4% pan across the whole shot) were tuned when beats ran
# ~6s; narration-led beats now run 19-25s, so the same totals spread out to
# ~0.2%/s — well under the ~0.7%/s where slow motion stops reading as motion at
# all. Amounts are therefore derived from the beat duration and capped, so a 6s
# beat and a 24s beat drift at the same speed on screen.
ZOOM_RATE = 0.011      # extra magnification per second
PAN_RATE = 0.009       # fraction of frame width per second
ZOOM_MAX = 0.22        # total travel ceiling (a 24s push tops out at +22%)
PAN_MAX = 0.18
EASE_EDGE = 0.15       # fraction of the beat spent easing in / out


def _ease(t: float, edge: float = EASE_EDGE) -> float:
    """Constant-velocity ramp with smoothstep shoulders.

    Pure smoothstep across a 24s beat leaves the first and last several seconds
    essentially frozen, which is most of what read as "no motion". Easing only
    the outer ``edge`` fraction keeps the move gentle at the cut points while
    holding a steady, visible drift through the body of the shot.
    """
    edge = min(max(edge, 1e-3), 0.49)
    t = min(max(t, 0.0), 1.0)
    if t < edge:
        u = t / edge
        return 0.5 * edge * u * u * (3.0 - 2.0 * u) / (1.0 - edge)
    if t > 1.0 - edge:
        u = (1.0 - t) / edge
        return 1.0 - 0.5 * edge * u * u * (3.0 - 2.0 * u) / (1.0 - edge)
    return (t - 0.5 * edge) / (1.0 - edge)


def camera_amounts(duration: float, amount: float = 0.0, speed: float = 1.0,
                   motion_cfg=None) -> tuple[float, float]:
    """Total (zoom, pan) travel for a beat of ``duration`` seconds.

    Resolution order, cheapest override last:

    1. ``motion_cfg`` — the project's MotionConfig (rates, caps, global speed).
    2. ``speed`` — the beat's ``Camera.speed`` multiplier, on top of the project.
    3. ``amount`` — the beat's ``Camera.amount``. Absolute and final: 0.15 means
       a 100% -> 115% push, exactly, ignoring rates and both speed multipliers.
       This is the "End Scale" field in the storyboard UI, and a number typed
       there must be the number that renders.
    """
    if amount and amount > 0:
        return float(amount), float(amount)
    zr = float(getattr(motion_cfg, "zoom_rate", ZOOM_RATE))
    pr = float(getattr(motion_cfg, "pan_rate", PAN_RATE))
    zc = float(getattr(motion_cfg, "zoom_max", ZOOM_MAX))
    pc = float(getattr(motion_cfg, "pan_max", PAN_MAX))
    mult = float(getattr(motion_cfg, "speed", 1.0)) * max(0.0, float(speed))
    d = max(0.1, float(duration))
    # Cap the rate-derived travel first, then apply the multipliers — so raising
    # speed still speeds the shot up rather than silently hitting the ceiling.
    return min(zr * d, zc) * mult, min(pr * d, pc) * mult


def _camera(move: str, t: float, zoom_amt: float = 0.066,
            pan_amt: float = 0.054) -> tuple[float, float, float]:
    """Return (zoom, dx, dy) for the *camera* at eased time ``t`` in [0, 1].

    dx/dy are fractions of frame size; zoom is an extra magnification. Depth
    inverse-warping clamps at the edges, so no border is ever revealed.
    """
    e = _ease(t)
    if move == "push_in":
        return zoom_amt * e, 0.0, 0.0
    if move == "push_out":
        return zoom_amt * (1.0 - e), 0.0, 0.0
    if move == "pan_left":
        return 0.0, (pan_amt * (0.5 - e)), 0.0
    if move == "pan_right":
        return 0.0, (pan_amt * (e - 0.5)), 0.0
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
                base_x: np.ndarray, move: str, t: float,
                out_w: int, out_h: int,
                zoom_amt: float = 0.066, pan_amt: float = 0.054) -> np.ndarray:
    """Inverse-warp the source by the per-pixel depth displacement at time ``t``.

    Speed multipliers are already folded into zoom_amt/pan_amt by
    camera_amounts(); applying them again here would square them.
    """
    zoom, dx, dy = _camera(move, t, zoom_amt, pan_amt)
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
                out_dir: Path = RENDER_DIR, placeholder: bool = False,
                motion_cfg=None) -> Path:
    """Render one approved shot to a silent H.264 clip; return the output path.

    ``placeholder=True`` renders a Tier-C (ai_video) shot from its still as a
    stand-in until the paid Kling clip exists — using the local parallax engine
    if the shot has a camera move, else a static hold.

    ``motion_cfg`` is the project's Storyboard.motion; omitted, the module
    defaults apply.
    """
    if not shot.draft_image:
        raise ValueError(f"{shot.scene_id}: no chosen draft_image to render.")
    # Go through the shared resolver: manifest paths are repo-relative locally
    # but land under /gcs on Cloud Run, so `config.ROOT / draft_image` misses
    # there and the whole free render tier silently fails.
    src = config.resolve_media(shot.draft_image, shot.scene_id)
    if src is None:
        raise FileNotFoundError(
            f"{shot.scene_id}: draft image not found on disk: {shot.draft_image}"
        )
    out_w, out_h = height * 16 // 9, height
    duration = float(shot.camera.duration) if shot.camera else 6.0
    move = shot.camera.move if shot.camera else "static"
    speed = float(shot.camera.speed) if shot.camera else 1.0
    amount = float(getattr(shot.camera, "amount", 0.0) or 0.0) if shot.camera else 0.0
    zoom_amt, pan_amt = camera_amounts(duration, amount, speed, motion_cfg)
    n_frames = max(1, int(round(duration * fps)))

    img = depthmod.load_rgb(src)
    src_rgb = np.asarray(Image.fromarray(img).resize((out_w, out_h), Image.LANCZOS),
                         dtype=np.float32) / 255.0
    is_parallax = shot.motion_type == MotionType.PARALLAX or (placeholder and move != "static")
    if is_parallax:
        depth = depthmod.estimate_depth(img)
        depth = np.asarray(Image.fromarray(depth).resize((out_w, out_h), Image.BILINEAR),
                           dtype=np.float32)
        depth = ndimage.gaussian_filter(depth, sigma=3.0)   # gentler disparity edges
        depth = (depth - depth.min()) / (depth.max() - depth.min() + 1e-6)
        # Far pixels still move a little (the whole frame breathes), but a wider
        # spread means near/far separate visibly instead of the plate sliding as
        # one flat card.
        disp = (0.15 + 0.85 * depth).astype(np.float32)
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
                frame = _warp_frame(src_rgb, disp, base_y, base_x, move, t,
                                    out_w, out_h, zoom_amt, pan_amt)
            else:
                frame = src_rgb.copy()   # static plate; FX only
            frame = fx.apply(frame, t, i)
            writer.append_data((frame * 255).astype(np.uint8))
    finally:
        writer.close()
    return out_path


def render_all(only: set[str] | None = None, fps: int = DEFAULT_FPS,
               height: int = DEFAULT_HEIGHT, placeholders: bool = False,
               storyboard=None) -> list[Path]:
    """Render approved local-tier shots into this episode's render dir. With
    ``placeholders``, also render the paid ai_video shots from their stills as
    stand-ins."""
    sb = storyboard or load()
    out_dir = config.episode_paths(sb.title)["render"]
    outs: list[Path] = []
    for shot in sb.shots:
        if only and shot.scene_id not in only:
            continue
        if getattr(shot, "hero_clip", False):
            print(f"{shot.scene_id}: imported hero clip — keeping it, not re-rendering.")
            continue
        is_ai = shot.motion_type == MotionType.AI_VIDEO
        if is_ai and not placeholders:
            continue  # Tier-C handled by the paid video stage, not here
        if not only and not shot.approved:
            continue
        tag = "placeholder" if is_ai else shot.motion_type.value
        print(f"Rendering {shot.scene_id} ({tag}, {shot.camera.move}) ...")
        try:
            p = render_shot(shot, fps=fps, height=height, out_dir=out_dir,
                            placeholder=is_ai, motion_cfg=getattr(sb, "motion", None))
            print(f"  -> {p.relative_to(config.ROOT)}")
            outs.append(p)
        except Exception as exc:  # noqa: BLE001 — resilient batch
            print(f"  !! {shot.scene_id} FAILED: {exc}")
    return outs


def _main() -> None:
    parser = argparse.ArgumentParser(description="The Illuminated Bestiary local motion engine.")
    parser.add_argument("--scene", nargs="*", help="scene id(s) to render (default: all approved local).")
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    parser.add_argument("--placeholders", action="store_true",
                        help="also render ai_video shots from their stills as stand-ins.")
    args = parser.parse_args()
    outs = render_all(only=set(args.scene) if args.scene else None,
                      fps=args.fps, height=args.height, placeholders=args.placeholders)
    print(f"\nRendered {len(outs)} clip(s) into {config.episode_paths(load().title)['render'].relative_to(config.ROOT)}/")


if __name__ == "__main__":
    _main()

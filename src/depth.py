"""Depth stage: monocular depth -> 2.5D layer separation -> gap inpaint (local, $0).

From a single still this produces exactly what the parallax renderer (``motion.py``)
needs: a normalized depth map, a small stack of depth-banded foreground layers with
soft alpha, and an **inpainted background plate** so that when the foreground drifts
it never tears a hole in the frame.

Depth backend is pluggable and never assumes CUDA (see CLAUDE.md):

* If an ONNX depth model is configured (``config.DEPTH_MODEL``) and ``onnxruntime``
  is importable, that runs (DirectML/CPU) — the intended production path.
* Otherwise a dependency-light CPU heuristic runs so the pipeline works out of the
  box on the current environment (no onnxruntime / opencv needed).

Inpainting uses a SciPy nearest-neighbour fill (``distance_transform_edt``) rather
than ``cv2.inpaint``, keeping the stage free of a heavy OpenCV dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _norm(a: np.ndarray) -> np.ndarray:
    """Robust 1-99 percentile normalize to [0, 1]."""
    a = a.astype(np.float32)
    lo, hi = np.percentile(a, 1.0), np.percentile(a, 99.0)
    if hi <= lo:
        return np.zeros_like(a)
    return np.clip((a - lo) / (hi - lo), 0.0, 1.0)


def load_rgb(path: str | Path) -> np.ndarray:
    """Load an image as HxWx3 uint8 RGB."""
    return np.asarray(Image.open(path).convert("RGB"))


# --------------------------------------------------------------------------- #
# depth estimation
# --------------------------------------------------------------------------- #
def _heuristic_depth(gray: np.ndarray) -> np.ndarray:
    """A dependency-light monocular depth cue (0 = far, 1 = near).

    Combines two priors that hold well for these cinematic 16:9 plates: content
    lower in the frame tends to be nearer, and nearer regions carry more
    high-frequency detail than distant, shadow-swallowed ones.
    """
    h, w = gray.shape
    y = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None] * np.ones((1, w), np.float32)
    blur = ndimage.gaussian_filter(gray, sigma=max(h, w) / 128.0)
    detail = _norm(ndimage.gaussian_filter(np.abs(gray - blur), sigma=2.0))
    depth = 0.55 * y + 0.45 * detail
    depth = ndimage.gaussian_filter(depth, sigma=max(h, w) / 200.0)
    return _norm(depth)


# Depth-Anything V2 (ONNX) — loaded once, reused. DirectML on AMD/Windows, CPU
# fallback. Absent model / runtime => None => heuristic covers it.
_SESSION: "object | None" = None
_SESSION_TRIED = False
_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
_IMAGENET_STD = np.array([0.229, 0.224, 0.225], np.float32)
_INPUT_LONG = 518  # ViT input; Depth-Anything wants dims that are multiples of 14


def _round14(n: float, lo: int = 14) -> int:
    return max(lo, int(round(n / 14.0)) * 14)


def _get_session():
    """Lazily build (and cache) the ONNX session; return None if unavailable."""
    global _SESSION, _SESSION_TRIED
    if _SESSION_TRIED:
        return _SESSION
    _SESSION_TRIED = True

    from . import config

    model_path = getattr(config, "DEPTH_MODEL", None)
    if not model_path or not Path(model_path).exists():
        return None
    try:
        import onnxruntime as ort
    except Exception:
        return None
    for providers in (["DmlExecutionProvider", "CPUExecutionProvider"],
                      ["CPUExecutionProvider"]):
        try:
            _SESSION = ort.InferenceSession(str(model_path), providers=providers)
            return _SESSION
        except Exception:
            continue
    return None


def _try_onnx_depth(img: np.ndarray) -> np.ndarray | None:
    """Run Depth-Anything V2 (ONNX) if available; return normalized depth or None.

    Output convention matches the heuristic: near = 1, far = 0 (the model emits a
    disparity-like map where nearer is larger, which normalizes straight through).
    """
    sess = _get_session()
    if sess is None:
        return None
    try:
        h0, w0 = img.shape[:2]
        if w0 >= h0:
            tw, th = _INPUT_LONG, _round14(_INPUT_LONG * h0 / w0)
        else:
            th, tw = _INPUT_LONG, _round14(_INPUT_LONG * w0 / h0)
        x = np.asarray(Image.fromarray(img[..., :3]).resize((tw, th), Image.LANCZOS),
                       dtype=np.float32) / 255.0
        x = (x - _IMAGENET_MEAN) / _IMAGENET_STD
        x = np.transpose(x, (2, 0, 1))[None].astype(np.float32)
        iname = sess.get_inputs()[0].name
        oname = sess.get_outputs()[0].name
        depth = np.squeeze(sess.run([oname], {iname: x})[0]).astype(np.float32)
        depth = np.asarray(Image.fromarray(depth).resize((w0, h0), Image.BICUBIC),
                           dtype=np.float32)
        return _norm(depth)
    except Exception:
        return None


def estimate_depth(img: np.ndarray) -> np.ndarray:
    """Return a normalized depth map (HxW float32, 0 = far, 1 = near)."""
    onnx = _try_onnx_depth(img)
    if onnx is not None:
        return onnx
    gray = (img[..., :3].astype(np.float32).mean(axis=2)) / 255.0
    return _heuristic_depth(gray)


# --------------------------------------------------------------------------- #
# inpainting
# --------------------------------------------------------------------------- #
def inpaint_nearest(rgb: np.ndarray, mask: np.ndarray, soften: float = 3.0) -> np.ndarray:
    """Fill ``mask`` (True) pixels from their nearest un-masked neighbour, softened.

    Cheap but effective for the small reveals a parallax shift exposes.
    """
    if not mask.any():
        return rgb.copy()
    idx = ndimage.distance_transform_edt(mask, return_distances=False, return_indices=True)
    filled = rgb[tuple(idx)]
    soft = ndimage.gaussian_filter(filled.astype(np.float32), sigma=(soften, soften, 0))
    out = np.where(mask[..., None], soft, rgb.astype(np.float32))
    return np.clip(out, 0, 255).astype(np.uint8)


# --------------------------------------------------------------------------- #
# layer separation
# --------------------------------------------------------------------------- #
@dataclass
class ParallaxLayers:
    """Back-to-front planes for the parallax renderer.

    ``background`` is a full, hole-free RGB plate (nearest plane removed +
    inpainted). ``planes`` are the moving foreground cut-outs as RGBA, ordered
    far -> near, each tagged with its mean depth (0..1) so the renderer can scale
    parallax by distance.
    """

    background: np.ndarray                       # HxWx3 uint8
    depth: np.ndarray                            # HxW float32
    planes: list[tuple[np.ndarray, float]]       # [(RGBA uint8, depth_center)]


def separate_layers(img: np.ndarray, depth: np.ndarray, n_planes: int = 4) -> ParallaxLayers:
    """Split a still into an inpainted backdrop + ``n_planes`` non-overlapping depth
    bands (far -> near).

    The bands tile the frame, so a static composite reproduces the source exactly
    and object outlines stay intact; a small parallax shift only exposes the
    backdrop in the narrow disocclusion gaps at depth discontinuities.
    """
    rgb = img[..., :3]
    qs = np.quantile(depth, np.linspace(0.0, 1.0, n_planes + 1))
    # Backdrop used only to fill disocclusion gaps: keep the farthest band, inpaint the rest.
    background = inpaint_nearest(rgb, depth >= float(qs[1]))

    planes: list[tuple[np.ndarray, float]] = []
    for i in range(n_planes):
        lo, hi = float(qs[i]), float(qs[i + 1])
        center = (lo + hi) / 2.0
        band = (depth >= lo) if i == n_planes - 1 else ((depth >= lo) & (depth < hi))
        a = band.astype(np.float32)
        a = ndimage.maximum_filter(a, size=3)                        # dilate: don't thin edges
        a = np.clip(ndimage.gaussian_filter(a, sigma=0.8), 0.0, 1.0)  # ~1px feather only
        rgba = np.dstack([rgb, (a * 255).astype(np.uint8)])
        planes.append((rgba, center))
    return ParallaxLayers(background=background, depth=depth, planes=planes)


def layers_from_image(path: str | Path, n_planes: int = 4) -> ParallaxLayers:
    """Convenience: image path -> depth -> separated parallax layers."""
    img = load_rgb(path)
    return separate_layers(img, estimate_depth(img), n_planes=n_planes)

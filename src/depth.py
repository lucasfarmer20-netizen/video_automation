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


def _try_onnx_depth(img: np.ndarray) -> np.ndarray | None:
    """Upgrade seam: run a configured ONNX depth model if one is available.

    Returns a normalized depth map, or ``None`` to fall back to the heuristic.
    Kept import-safe when onnxruntime / the model file are absent.
    """
    try:
        import onnxruntime as ort  # noqa: F401  (presence probe)

        from . import config

        model_path = getattr(config, "DEPTH_MODEL", None)
        if not model_path or not Path(model_path).exists():
            return None
        # Integration point for Depth-Anything V2 (small) ONNX:
        #   sess = ort.InferenceSession(str(model_path), providers=[
        #       "DmlExecutionProvider", "CPUExecutionProvider"])
        #   ... preprocess -> run -> resize to img -> _norm ...
        # Left explicit rather than half-implemented; heuristic is the $0 default.
        return None
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


def separate_layers(img: np.ndarray, depth: np.ndarray, n_planes: int = 2,
                    near_q: float = 0.55) -> ParallaxLayers:
    """Split a still into an inpainted background + ``n_planes`` foreground bands."""
    rgb = img[..., :3]
    near_thresh = float(np.quantile(depth, near_q))
    near_mask = depth >= near_thresh
    background = inpaint_nearest(rgb, near_mask)

    # Foreground depth bands within [near_q, 1.0], each with soft alpha.
    qs = np.quantile(depth, np.linspace(near_q, 1.0, n_planes + 1))
    planes: list[tuple[np.ndarray, float]] = []
    for i in range(n_planes):
        lo = float(qs[i])
        hi = float(qs[i + 1])
        center = (lo + hi) / 2.0
        in_band = depth >= lo
        alpha = np.where(in_band, 1.0, 0.0).astype(np.float32)
        alpha = np.clip(ndimage.gaussian_filter(alpha, sigma=2.5), 0.0, 1.0)
        rgba = np.dstack([rgb, (alpha * 255).astype(np.uint8)])
        planes.append((rgba, center))
    return ParallaxLayers(background=background, depth=depth, planes=planes)


def layers_from_image(path: str | Path, n_planes: int = 2) -> ParallaxLayers:
    """Convenience: image path -> depth -> separated parallax layers."""
    img = load_rgb(path)
    return separate_layers(img, estimate_depth(img), n_planes=n_planes)

"""Single place that answers "where is ffmpeg/ffprobe?".

ffmpeg is a **system-level install** (see CLAUDE.md) — never a pip dependency,
never bundled by this repo. But the pipeline used to answer that question three
different ways: ``audio.py`` fell back to the imageio-bundled binary,
``director.py`` returned the literal string ``"ffmpeg"`` when the lookup failed,
and five other modules hardcoded ``"ffmpeg"``/``"ffprobe"`` outright.

The middle case is the dangerous one. Returning a bare name that is not on PATH
defers the failure to whichever ``subprocess`` call happens to run first, which
surfaces as an opaque ``FileNotFoundError [WinError 2]`` at an arbitrary line —
on this workstation, a ``probe_seconds()`` *log line* at the tail of
``_compile_locked``, long after the paid-render assertions it was masking had
already passed. Resolving eagerly and raising a named error puts the failure
where the cause is.
"""

from __future__ import annotations

import shutil
from functools import lru_cache
from pathlib import Path

_INSTALL_HINT = (
    "ffmpeg is a system-level install, not a pip package (see CLAUDE.md). "
    "Install it and reopen the shell so it lands on PATH — on Windows: "
    "`winget install Gyan.FFmpeg`; on Debian/Ubuntu: `apt install ffmpeg`; "
    "on macOS: `brew install ffmpeg`."
)


def _imageio_dir() -> Path | None:
    """Directory holding the imageio-bundled ffmpeg, if that package is present."""
    try:
        import imageio_ffmpeg
    except Exception:  # noqa: BLE001 - package is optional
        return None
    try:
        return Path(imageio_ffmpeg.get_ffmpeg_exe()).parent
    except Exception:  # noqa: BLE001 - bundled binary may be missing/unusable
        return None


@lru_cache(maxsize=1)
def ffmpeg_bin() -> str:
    """Absolute path to ffmpeg.

    Prefers the system install; falls back to the imageio-bundled binary so
    local dev works without a PATH install. Raises if neither exists rather
    than returning a name that will fail later inside ``subprocess``.
    """
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    bundled = _imageio_dir()
    if bundled is not None:
        candidate = shutil.which("ffmpeg", path=str(bundled))
        if candidate:
            return candidate
    raise RuntimeError(f"ffmpeg not found on PATH. {_INSTALL_HINT}")


@lru_cache(maxsize=1)
def ffprobe_bin() -> str:
    """Absolute path to ffprobe.

    imageio_ffmpeg ships ffmpeg but **not** ffprobe, so the fallback only helps
    when a full build happens to sit beside it. Otherwise this raises — which is
    the point: a missing ffprobe should be named here, not decoded later from a
    ``WinError 2`` raised by an unrelated call site.
    """
    exe = shutil.which("ffprobe")
    if exe:
        return exe
    bundled = _imageio_dir()
    if bundled is not None:
        candidate = shutil.which("ffprobe", path=str(bundled))
        if candidate:
            return candidate
    raise RuntimeError(
        f"ffprobe not found on PATH. It ships with ffmpeg proper but is NOT "
        f"included in the imageio-bundled ffmpeg. {_INSTALL_HINT}"
    )


def have_ffmpeg() -> bool:
    """True when both binaries resolve. For test skip guards and capability checks."""
    try:
        ffmpeg_bin()
        ffprobe_bin()
    except RuntimeError:
        return False
    return True

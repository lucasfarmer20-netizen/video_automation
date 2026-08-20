"""Render to local disk, publish to the bucket once.

Every mp4 in this pipeline used to be encoded straight onto the GCS-FUSE mount:
ffmpeg was handed ``/gcs/<project>/render/...mp4`` as its output path and streamed
the muxer's writes at it. Object storage is not a filesystem. Each write is a
whole-object mutation, GCS rate-limits mutations **per object** at roughly one per
second, and an mp4 muxer does not write once -- it patches sizes and rewrites as
it goes. So the encode's mutation count is a function of how the muxer happens to
flush, which is exactly the wrong thing for it to be a function of.

That is not a hypothesis. Cloud Run, 2026-08-18, the ``calluses`` project::

    05:06:20  [DIRECTOR:S001] Compiling coverage for s001 (10 shots)...
    05:06:20  [DIRECTOR:S001] [1/10] s001.01: already rendered - skipping   (x10)
    05:06:31  gcsfuse: Retrying ComposeObject for "calluses/TestRun/render/s001.mp4":
              Error 429: ... exceeded the rate limit for object mutation operations
    05:06:34  [DIRECTOR:S001]   concatenated 10 clips -> s001.mp4
    05:07:24  [DIRECTOR:S001]   s001.mp4: trimmed 37.46s -> 37.39s

Ten of ten shots were skipped, so *nothing was encoded from frames at all*. The
429 still came, on the beat clip, because ``concat`` copied the assembled file
over ``render/s001.mp4`` and then ``fit_clip`` re-encoded and copied over the same
object fifty seconds later. Two whole-object rewrites of one object inside a
minute. The same day, ``s006.mp4`` took 82 rate-limited retries and its concat
alone burned about three and a half minutes of wall clock.

The fix is the standard pattern for FUSE-mounted object storage and it is one
sentence: **encode to local disk, then copy the finished file to the bucket
once.** One mutation per object instead of however many the muxer felt like.

Three decisions are recorded here rather than in a commit message, because each
one is a trade and the next person needs the reasoning, not the conclusion.
"""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from .atomic import replace_with_retry

# --- Decision 1: where scratch lives, how long it lives, and what bounds it ---
#
# WHERE. ``tempfile.gettempdir()`` by default, which honours TMPDIR and is where
# ``timeline.py`` and ``sizzle.py`` already stage their intermediates -- this is
# not a new convention, it is the existing one applied to the paths that missed
# it. ``RENDER_SCRATCH_DIR`` overrides it, and exists for one reason: on Cloud
# Run /tmp is tmpfs, so scratch is RAM and counts against the instance memory
# limit. An operator whose episodes have outgrown that can point this at a
# mounted disk without touching code.
#
# HOW LONG. One publish. ``staged()`` creates a directory for a single output and
# removes it the moment that output is published, so the working set is the clip
# in flight and never the episode's accumulated renders. A beat's sub-clips are
# published to the bucket as they finish and read back from there by the concat,
# so they are not held.
#
# WHAT BOUNDS IT. The per-publish lifetime bounds every path here except one. A
# 720p/crf20 sub-clip is a few MB; a 30s beat clip is 30-60MB; those come and go
# one at a time. ``timeline.build_preview`` is the exception: it stages the
# whole-episode concat AND the muxed preview at the same time, so a ten-minute
# episode at crf30 is roughly 150-250MB twice over -- call it 500MB peak. Against
# a 4Gi instance observed at ~26% mean that is comfortable, but it is the number
# that grows with runtime and nothing here caps it. Stated rather than fixed:
# capping it means chunking the preview encode, which is a different change.
# build_preview already staged in gettempdir() before this module existed, so
# that exposure is not new -- it is just now the only unbounded one.
_SCRATCH_ENV = "RENDER_SCRATCH_DIR"


def scratch_root() -> Path:
    root = Path(os.environ.get(_SCRATCH_ENV) or tempfile.gettempdir())
    root.mkdir(parents=True, exist_ok=True)
    return root


class PublishError(OSError):
    """A render succeeded but could not be copied to its destination.

    Carries ``rescued``: the absolute path the finished bytes are still sitting
    at. See Decision 2 -- this exception exists so that path can be said out
    loud instead of being deleted along with the scratch directory.
    """

    def __init__(self, message: str, rescued: Path | None = None):
        super().__init__(message)
        self.rescued = rescued


# --- Decision 2: a copy that fails must not lose a render that succeeded ------
#
# The render is the expensive half. Frames have been warped, a model may have
# been paid for the still underneath, and once the bytes exist they are real
# work. Losing them to a failed copy is strictly worse than the behaviour this
# module replaces, so the copy is the only step allowed to be optimistic.
#
# Bounded retry, in the shape ``atomic.replace_with_retry`` established, but NOT
# its schedule. That one is tuned for a Windows handle race that clears in
# milliseconds: 8 attempts doubling from 8ms, about a second in total. This is
# defending against a rate-limited network filesystem whose own client has
# already exhausted its internal retries before the error reaches us, and a
# second of backoff against a per-second object quota is not a retry, it is a
# formality. Five attempts doubling from 0.5s is ~7.5s, which is long enough for
# a mutation quota to refill and short enough that a genuinely broken mount
# fails a beat instead of hanging it.
_COPY_ATTEMPTS = 5
_COPY_BACKOFF = 0.5

# Only these are worth a second go. EACCES/EPERM/EBUSY/EAGAIN are the shapes a
# throttled or momentarily-locked mount takes. ENOSPC is deliberately absent: a
# full disk does not un-fill itself, and retrying it just delays the report.
_RETRY_COPY_ERRNOS = frozenset({
    errno.EACCES, errno.EPERM, errno.EBUSY, errno.EAGAIN, errno.EIO,
})

# Renaming onto a gcsfuse mount is a server-side copy plus a delete, and this
# repo has already recorded it being refused outright -- see the note in
# ``director.normalize_clip`` about ``[Errno 1] Operation not permitted``. So
# whether ``os.replace`` works is a property of the destination's mount, learned
# once per directory rather than guessed.
_RENAME_OK: dict[str, bool] = {}


def _copy_with_retry(src: Path, dest: Path) -> None:
    """``shutil.copyfile``, retrying only the transient mount failures.

    ``copyfile`` and not ``copy2``: copy2 replays mode and mtime, and those are
    the metadata operations gcsfuse rejects. Nothing downstream reads either.
    """
    for attempt in range(_COPY_ATTEMPTS):
        try:
            shutil.copyfile(src, dest)
            return
        except OSError as exc:
            if exc.errno not in _RETRY_COPY_ERRNOS or attempt == _COPY_ATTEMPTS - 1:
                raise
            time.sleep(_COPY_BACKOFF * (2 ** attempt))


def _rename_supported(directory: Path) -> bool:
    """Whether ``os.replace`` works inside ``directory``, probed once and cached.

    Two empty temp files, one rename, per directory per process. That is a
    rounding error against one beat clip, and it is what stops a rename-hostile
    mount from paying for a full-size staged copy that it can only then discard.
    """
    key = str(directory)
    known = _RENAME_OK.get(key)
    if known is not None:
        return known
    ok = False
    a = directory / f".renameprobe.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    b = a.with_name(a.name + ".moved")
    try:
        a.touch()
        os.replace(a, b)
        ok = True
    except OSError:
        ok = False
    finally:
        with contextlib.suppress(OSError):
            a.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            b.unlink(missing_ok=True)
    _RENAME_OK[key] = ok
    return ok


def _rescue(src: Path, dest: Path) -> Path | None:
    """Move a render that could not be published somewhere it will not be swept.

    ``staged()`` removes its directory unconditionally, which is what keeps the
    working set bounded -- so a file that must survive has to leave first.
    """
    try:
        keep_dir = scratch_root() / "rescued"
        keep_dir.mkdir(parents=True, exist_ok=True)
        keep = keep_dir / f"{dest.stem}.{uuid.uuid4().hex[:8]}{dest.suffix}"
        shutil.move(str(src), str(keep))
        return keep
    except OSError:
        return None


# --- Decision 3: is the copy atomic from a reader's perspective? --------------
#
# Partly, and the boundary is worth being exact about, because a half-copied mp4
# visible to the studio would be a failure mode this change INTRODUCED.
#
# ``backend/atomic.py`` is the precedent and its shape is reused: write beside
# the destination under a unique temp name, then ``os.replace`` onto it. Its
# ``replace_with_retry`` is called directly. What does not carry over is the
# assumption that the replace is available at all -- atomic.py writes JSON to
# whatever filesystem it finds, this writes video to a FUSE mount that has been
# seen refusing rename. Hence the probe above.
#
# Where rename works, publication is atomic: a reader sees the old clip or the
# new one, never a prefix of the new one.
#
# Where it does not, the fallback copies straight onto the destination and that
# branch is NOT atomic -- an interrupted copy leaves a torn mp4 at the
# destination. It is still the right branch, because the alternative is refusing
# to publish a render that succeeded, and a torn destination is recoverable (the
# bytes are rescued, named, and can be re-published) where a discarded render is
# not. The failure is loud either way.
#
# In both branches the staging name is dot-prefixed and suffixed ``.part``, which
# is outside ``config.MEDIA_SUFFIXES``, so it can never be served by /media/ nor
# matched by the ``*.mp4`` globs and ``target.is_file()`` checks that decide
# whether a clip is finished. A publish in progress is invisible to every
# consumer of a render directory.
_STAGE_SUFFIX = ".part"


def publish(src: Path, dest: Path) -> Path:
    """Copy a finished render from local scratch to ``dest``, once.

    Returns ``dest``. Consumes ``src``: on success it is removed, on failure it
    is rescued and named in the raised :class:`PublishError`.
    """
    src, dest = Path(src), Path(dest)
    if not src.is_file():
        raise PublishError(f"nothing to publish: {src} does not exist")
    dest.parent.mkdir(parents=True, exist_ok=True)

    stage: Path | None = None
    try:
        if _rename_supported(dest.parent):
            stage = dest.parent / f".{dest.name}.{uuid.uuid4().hex[:8]}{_STAGE_SUFFIX}"
            _copy_with_retry(src, stage)
            replace_with_retry(stage, dest)
            stage = None
        else:
            _copy_with_retry(src, dest)
    except OSError as exc:
        if stage is not None:
            with contextlib.suppress(OSError):
                stage.unlink(missing_ok=True)
        kept = _rescue(src, dest)
        where = (f" The finished render is intact at {kept} — copy it to {dest} "
                 f"by hand rather than re-rendering." if kept else
                 " The finished render could not be rescued either.")
        raise PublishError(
            f"rendered {dest.name} but could not publish it to {dest}: {exc}.{where}",
            rescued=kept,
        ) from exc

    with contextlib.suppress(OSError):
        src.unlink(missing_ok=True)
    return dest


@contextlib.contextmanager
def staged(dest: Path, *, suffix: str | None = None):
    """A local path to render to, published to ``dest`` when the block exits cleanly.

    The yielded path is on local disk and is never inside ``dest``'s directory,
    which is the whole point: whatever ffmpeg does to it -- seek backwards, patch
    the mdat size, rewrite the moov atom -- costs a local write, not a bucket
    mutation. If the block raises, nothing is published and ``dest`` is left
    exactly as it was.
    """
    dest = Path(dest)
    work = Path(tempfile.mkdtemp(prefix="render_", dir=str(scratch_root())))
    local = work / f"{dest.stem}{suffix or dest.suffix}"
    try:
        yield local
        publish(local, dest)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def render_to(dest: Path, fn, *, suffix: str | None = None) -> Path:
    """``fn(local_path)`` renders locally; the result is published to ``dest``."""
    with staged(dest, suffix=suffix) as local:
        fn(local)
    return Path(dest)

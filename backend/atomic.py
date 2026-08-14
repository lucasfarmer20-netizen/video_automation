"""Durable JSON writes and reads, in one place.

This is the S2-03 fix extracted so it protects every record that matters rather
than only coverage plans. Three properties, and each was learned the hard way:

* **a unique temp file per write.** One fixed temp name per destination meant
  two writers shared it, and on Windows the second ``os.replace`` failed with
  WinError 32 -- seven of eight concurrent writers lost their write.
* **a lock per destination**, so two of *our* writers take turns. This is the
  half that actually fixes the collision; the unique name is defence for what
  the lock cannot cover.
* **bounded retry on the replace.** The lock cannot stop the operating system:
  another handle on the destination (indexer, scanner, a reader mid-open) makes
  Windows deny the replace transiently, which surfaced in 5 of 20 batches at 16
  writers. A denied replace is not a torn file, it is a *submitted write
  silently lost*, which is worse -- the caller goes on believing something it
  never persisted.

The read side is the mirror of the third property, and it was missing. The three
rules above protect writers from each other and nothing protected a *reader*
from a concurrent replace: on Windows, opening a destination while another
thread is mid-``os.replace`` on it is denied, and that denial reached the caller
as a ``PermissionError`` out of ``load_plan``. By this module's own standard
that is the worse half of the same bug -- a denied replace is a submitted write
silently lost, and a denied read is a *present record reported as unreadable*,
which fails a caller that had every right to succeed. So ``read_json`` exists
and every reader of a record ``write_json`` writes goes through it.

Deliberately NOT provided, and stated so nothing assumes otherwise: the lock is
per process, so two Cloud Run instances over one GCS mount are back to racing;
and nothing here prevents lost updates, because serialising writes makes each
one complete without making a read-modify-write pair correct.
"""

from __future__ import annotations

import errno as _errno
import json
import os
import tempfile
import threading
import time
from pathlib import Path

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

# ERROR_ACCESS_DENIED and ERROR_SHARING_VIOLATION. Only these are transient;
# anything else is a real problem and must fail on the first attempt.
_RETRY_WINERRNOS = frozenset({5, 32})

# The same two Windows conditions as seen from the READ side, and they do not
# look the same. os.replace raises through the Win32 wrapper, so the failure
# carries .winerror; open() goes through the C runtime, which has already mapped
# both ERROR_ACCESS_DENIED and ERROR_SHARING_VIOLATION to EACCES and left
# .winerror as None. Reusing _RETRY_WINERRNOS on a read would therefore compile,
# read plausibly, and retry nothing at all -- so the read predicate is keyed on
# the attribute the runtime actually sets. ENOENT is deliberately absent: a
# missing record is an answer, not a transient fault, and callers depend on
# getting it immediately.
_RETRY_READ_ERRNOS = frozenset({_errno.EACCES})

_ATTEMPTS = 8
_BACKOFF = 0.008          # doubles each attempt; ~1s worst case


def lock_for(dest: Path) -> threading.Lock:
    """The lock for one path, held by writers and by readers alike.

    Keyed by path, which is per project: two projects' identically-named files
    are different destinations and must not serialise against each other.
    """
    key = str(dest)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.Lock()
        return lock


def replace_with_retry(tmp: Path, dest: Path) -> None:
    """``os.replace``, retrying only the transient Windows denials.

    Bounded: the last attempt re-raises, so a genuine permissions problem fails
    rather than spinning.
    """
    for attempt in range(_ATTEMPTS):
        try:
            os.replace(tmp, dest)
            return
        except OSError as exc:
            retryable = getattr(exc, "winerror", None) in _RETRY_WINERRNOS
            if not retryable or attempt == _ATTEMPTS - 1:
                raise
            time.sleep(_BACKOFF * (2 ** attempt))


def write_json(dest: Path, payload, *, indent: int = 2) -> Path:
    """Serialise ``payload`` to ``dest`` atomically and safely against writers."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(payload, indent=indent)

    with lock_for(dest):
        # In the destination directory so the replace stays on one filesystem,
        # and uniquely named so no other writer can be holding it.
        fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent),
                                        prefix=f"{dest.name}.", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())
            replace_with_retry(tmp, dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    return dest


def _read_text_with_retry(src: Path) -> str:
    """``read_text``, retrying only the transient denials, same bound as the write.

    Bounded identically to ``replace_with_retry`` -- same attempt count, same
    doubling backoff -- because it is defending against the same window from the
    other side. The last attempt re-raises, so a file this process genuinely may
    not read fails instead of spinning for a second first.
    """
    for attempt in range(_ATTEMPTS):
        try:
            return src.read_text(encoding="utf-8")
        except OSError as exc:
            retryable = exc.errno in _RETRY_READ_ERRNOS
            if not retryable or attempt == _ATTEMPTS - 1:
                raise
            time.sleep(_BACKOFF * (2 ** attempt))
    raise AssertionError("unreachable")          # pragma: no cover


def read_json(src: Path):
    """Read a record ``write_json`` wrote, safely against concurrent writers.

    Two layers, the same two ``write_json`` uses and for the same reasons:

    * **the destination's lock**, so one of *our* readers cannot be inside
      ``open`` while one of *our* writers is inside ``os.replace``. That is the
      half that actually closes this race, because in the scenario that exposed
      it -- ``load_plan`` persisting a migration, so every reader is also a
      writer -- both sides are in this process and the lock covers both.
    * **bounded retry**, for what the lock cannot cover: another process, an
      indexer, a virus scanner, or an editor holding a handle. Exactly the
      argument the replace side already makes.

    Raises ``OSError`` if the record cannot be read and ``ValueError`` if it is
    not JSON -- the two cases callers already tell apart, kept distinct so a
    corrupt record is never retried as though it were a busy one, and never
    reported as a missing one.
    """
    src = Path(src)
    with lock_for(src):
        text = _read_text_with_retry(src)
    return json.loads(text)

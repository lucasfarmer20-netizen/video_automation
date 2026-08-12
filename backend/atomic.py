"""Durable JSON writes, in one place.

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

Deliberately NOT provided, and stated so nothing assumes otherwise: the lock is
per process, so two Cloud Run instances over one GCS mount are back to racing;
and nothing here prevents lost updates, because serialising writes makes each
one complete without making a read-modify-write pair correct.
"""

from __future__ import annotations

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
_ATTEMPTS = 8
_BACKOFF = 0.008          # doubles each attempt; ~1s worst case


def lock_for(dest: Path) -> threading.Lock:
    """The write lock for one destination path.

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

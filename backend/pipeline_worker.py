"""Background job manager for long-running pipeline tasks.

Handles voiceover synthesis, image/video generation, and DaVinci export
without blocking the main API thread. Captures standard output for real-time log polling.
"""

from __future__ import annotations

import io
import sys
import threading
import traceback
from typing import Callable, Dict, Any

# Global job registry
_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def log_job(name: str, msg: str):
    """Log a message to both stdout and the job's isolated log buffer."""
    line = f"[{name.upper()}] {msg}"
    print(line)
    with _jobs_lock:
        if name in _jobs:
            _jobs[name]["log"] = (_jobs[name]["log"] + line + "\n")[-10000:]


def start_job(name: str, fn: Callable[[], None]) -> bool:
    """Kick off a pipeline stage function in a background thread."""
    with _jobs_lock:
        if name in _jobs and _jobs[name]["status"] == "running":
            return False  # Already running
        
        _jobs[name] = {
            "status": "running",
            "log": "",
            "thread": None
        }

    def wrapper():
        log_job(name, "Starting background process...")
        try:
            fn()
            log_job(name, "Process completed successfully.")
            with _jobs_lock:
                _jobs[name]["status"] = "done"
        except Exception as e:
            # The full traceback goes to stdout (Cloud Logging), not into the job
            # buffer: GET /api/assemble/status is UNAUTHENTICATED -- the middleware
            # only checks X-Studio-Key on non-GET methods -- so anything put here
            # is public, and a traceback carries absolute container paths plus
            # whatever appears in the frames.
            #
            # The type and message stay, because a job that failed must still say
            # why. Surfacing that was the point of this buffer in the first place.
            print(f"[job:{name}] failed:")
            print(traceback.format_exc())
            log_job(name, f"Process failed with error: {type(e).__name__}: {e}")
            with _jobs_lock:
                _jobs[name]["status"] = "error"

    t = threading.Thread(target=wrapper, name=f"job-{name}")
    with _jobs_lock:
        _jobs[name]["thread"] = t
    t.start()
    return True


def get_jobs_status() -> dict:
    """Return the status and last 2000 characters of the log of all registered jobs."""
    with _jobs_lock:
        res = {}
        for k, v in _jobs.items():
            res[k] = {
                "status": v["status"],
                "log": v.get("log", "")[-2000:]
            }
        return res

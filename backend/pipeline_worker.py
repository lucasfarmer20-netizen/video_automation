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


class LogCapture(io.StringIO):
    """Buffer that captures stdout/stderr and forwards it to the original stream."""
    def __init__(self, original_stream):
        super().__init__()
        self.original_stream = original_stream

    def write(self, s):
        super().write(s)
        self.original_stream.write(s)
        self.original_stream.flush()


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
        # Capture standard output/error to show in the UI console log
        capture = LogCapture(sys.stdout)
        sys.stdout = capture
        sys.stderr = capture
        try:
            print(f"[{name.upper()}] Starting background process...")
            fn()
            print(f"[{name.upper()}] Process completed successfully.")
            with _jobs_lock:
                _jobs[name]["status"] = "done"
        except Exception as e:
            traceback.print_exc(file=sys.stdout)
            print(f"[{name.upper()}] Process failed with error: {e}")
            with _jobs_lock:
                _jobs[name]["status"] = "error"
        finally:
            sys.stdout = capture.original_stream
            sys.stderr = capture.original_stream
            # Save logs
            with _jobs_lock:
                _jobs[name]["log"] = capture.getvalue()

    t = threading.Thread(target=wrapper, name=f"job-{name}")
    with _jobs_lock:
        _jobs[name]["thread"] = t
    t.start()
    return True


def get_jobs_status() -> dict:
    """Return the status and last 1500 characters of the log of all registered jobs."""
    with _jobs_lock:
        res = {}
        for k, v in _jobs.items():
            # Get current log buffer if thread is running
            log_str = ""
            if v["status"] == "running" and hasattr(sys.stdout, "getvalue"):
                log_str = sys.stdout.getvalue()
            else:
                log_str = v.get("log", "")
            
            res[k] = {
                "status": v["status"],
                "log": log_str[-2000:]
            }
        return res

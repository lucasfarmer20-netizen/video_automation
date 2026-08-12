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

from . import projects

# The job registry. Keyed by (project_id, logical name) -- NOT by name alone.
#
# It used to be keyed by stage name only, which made the whole registry global
# across projects and produced two distinct failures at once: starting "render"
# for project B was refused because project A already had a job by that name,
# and B's status poll returned A's job, so B's studio showed A's progress and
# A's completion as though they were its own. Two films cannot be worked on at
# the same time if they share one namespace.
_jobs: Dict[tuple, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def _project_id() -> str:
    """Project id for the caller's bound context. Empty for unbound CLI runs."""
    ctx = projects.current()
    return ctx.project_id if ctx else ""


def _key(name: str, project_id: str | None = None) -> tuple:
    return (project_id if project_id is not None else _project_id(), name)


def log_job(name: str, msg: str, project_id: str | None = None):
    """Log to stdout and to this project's job buffer.

    Resolves the project from the caller's bound context. Inside a worker
    thread that is the context captured at enqueue, so a job's log lines always
    land in that job's buffer and never in a same-named job of another project.
    """
    line = f"[{name.upper()}] {msg}"
    print(line)
    key = _key(name, project_id)
    with _jobs_lock:
        if key in _jobs:
            _jobs[key]["log"] = (_jobs[key]["log"] + line + "\n")[-10000:]


def start_job(name: str, fn: Callable[[], None],
              ctx: "projects.ProjectContext | None" = None) -> bool:
    """Kick off a pipeline stage function in a background thread.

    The job's project identity is captured **here, at enqueue time**, and
    rebound inside the worker thread. It is never resolved from whatever project
    happens to be active when the thread gets around to running.

    That is contract §11.3, and the ordering is the whole point: a render queued
    against project A used to read the process globals when it ran, so selecting
    project B in the studio a second later sent A's render into B's directories.
    Nothing announced it -- the job reported success, against the wrong project.
    """
    job_ctx = ctx or projects.capture()
    pid = job_ctx.project_id if job_ctx else ""
    key = _key(name, pid)

    with _jobs_lock:
        # "Already running" is a question about THIS project. Another film
        # running the same stage is not a reason to refuse this one.
        if key in _jobs and _jobs[key]["status"] == "running":
            return False

        _jobs[key] = {
            "status": "running",
            "log": "",
            "thread": None,
            "project_id": pid or None,
            "name": name,
        }

    def wrapper():
        # Rebind the captured identity. A thread starts with an empty context,
        # so without this the job would fall through to the process global --
        # exactly the lookup this function exists to prevent.
        token = projects.bind(job_ctx) if job_ctx else None
        log_job(name, "Starting background process...", project_id=pid)
        try:
            fn()
            log_job(name, "Process completed successfully.", project_id=pid)
            with _jobs_lock:
                _jobs[key]["status"] = "done"
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
            log_job(name, f"Process failed with error: {type(e).__name__}: {e}",
                    project_id=pid)
            with _jobs_lock:
                _jobs[key]["status"] = "error"
        finally:
            if token is not None:
                projects.reset(token)

    t = threading.Thread(target=wrapper, name=f"job-{pid or '-'}-{name}")
    with _jobs_lock:
        _jobs[key]["thread"] = t
    t.start()
    return True


def get_jobs_status(project_id: str | None = None) -> dict:
    """Status and recent log for the jobs of ONE project.

    Filtered by the caller's bound context unless a project is named. An
    unfiltered view is not offered: this feeds the studio's job banners, and
    handing one project another's job state is how a user comes to believe
    someone else's render finished their film.
    """
    want = project_id if project_id is not None else _project_id()
    with _jobs_lock:
        return {
            name: {
                "status": v["status"],
                "log": v.get("log", "")[-2000:],
                "project_id": v.get("project_id"),
            }
            for (pid, name), v in _jobs.items()
            if pid == want
        }

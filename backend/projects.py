"""Per-request project identity.

There used to be exactly one active project **per process**. `set_active_manifest`
rebound module globals in `config`, `main` kept a `.active_project` pointer file,
and every endpoint resolved its target by asking "which project is active right
now?" — a question whose answer could change between the start and the end of a
request.

That is contract §11.3's failure mode in its purest form:

- a background job started against project A finished writing into project B,
  because it read the globals when it ran rather than when it was enqueued;
- two browser tabs on two projects shared one global, so whichever loaded last
  silently retargeted the other;
- `tests/conftest.py` exists solely to undo this leakage between tests, and its
  docstring records the order-dependent failures it caused.

The fix is that identity travels *with the work* instead of being looked up:

- a `ProjectContext` is immutable and carries every path it implies;
- it is bound to a `ContextVar` for the duration of one request or one job;
- **background work captures its context at enqueue time** and rebinds that exact
  context inside the worker thread. A job can never resolve its target from
  whatever happens to be active when it eventually runs.

The module-level globals in `config` remain for `pipeline.py`'s single-project
CLI runs, where one process really does mean one project. Anything served over
HTTP must go through a context.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path


class UnknownProject(ValueError):
    """A project id or manifest path that does not resolve to a real project."""


def project_id_for(path: str | Path) -> str:
    """Stable id for a manifest path.

    Same derivation the Firestore document id has always used, so ids minted
    here match every id already stored.
    """
    path_str = str(path).replace("\\", "/").strip("/")
    return "".join(c if c.isalnum() else "_" for c in path_str)


@dataclass(frozen=True)
class ProjectContext:
    """One project's identity and every path derived from it.

    Frozen on purpose. A context is captured by background jobs and compared
    against client-supplied ids; if it could be mutated after capture, the
    capture would prove nothing.
    """

    project_id: str
    manifest_path: Path

    # --- construction ---------------------------------------------------------
    @classmethod
    def from_manifest(cls, path: str | Path) -> "ProjectContext":
        p = Path(path).resolve()
        return cls(project_id=project_id_for(p), manifest_path=p)

    # --- derived paths --------------------------------------------------------
    @property
    def root(self) -> Path:
        return self.manifest_path.parent

    @property
    def assets(self) -> Path:
        return self.root / "assets"

    @property
    def references_dir(self) -> Path:
        return self.root / "references"

    @property
    def references_config(self) -> Path:
        return self.root / "references.json"

    @property
    def characters_config(self) -> Path:
        return self.root / "characters.json"

    @property
    def director_dir(self) -> Path:
        return self.root / "director"

    @property
    def render_dir(self) -> Path:
        return self.root / "render"

    @property
    def audio_dir(self) -> Path:
        return self.root / "audio"

    def exists(self) -> bool:
        return self.manifest_path.is_file()

    def matches(self, other_id: str | None) -> bool:
        """Whether a client-supplied id names this project.

        Used for stale-response rejection: a reply must be able to say which
        project it is about, so a client that has since switched can discard it.
        """
        return bool(other_id) and other_id == self.project_id


# The active context for the current request, task or job. Not a global: each
# asyncio task and each thread that binds one gets its own value.
_ACTIVE: ContextVar[ProjectContext | None] = ContextVar("active_project", default=None)

# Guards mutation of the process-wide fallback pointer only.
_FALLBACK_LOCK = threading.Lock()
_FALLBACK: ProjectContext | None = None


def bind(ctx: ProjectContext):
    """Make ``ctx`` the active project for this request/task. Returns a token."""
    return _ACTIVE.set(ctx)


def reset(token) -> None:
    _ACTIVE.reset(token)


def bound() -> ProjectContext | None:
    """The explicitly bound context, ignoring the process fallback.

    Path helpers in ``config`` resolve through this rather than :func:`current`
    so that legacy single-project callers -- ``pipeline.py`` and every test that
    monkeypatches ``config.ASSETS`` -- keep the exact behaviour they had. A
    context only takes precedence when something deliberately bound one, which
    web requests and background jobs do and CLI runs do not.
    """
    return _ACTIVE.get()


def current() -> ProjectContext | None:
    """The bound context, or the process fallback, or None.

    Callers that must not silently fall back should use :func:`require`.
    """
    ctx = _ACTIVE.get()
    if ctx is not None:
        return ctx
    with _FALLBACK_LOCK:
        return _FALLBACK


def require() -> ProjectContext:
    ctx = current()
    if ctx is None:
        raise UnknownProject(
            "No project context is bound. Web requests bind one per request; "
            "background jobs must capture one at enqueue time."
        )
    return ctx


def set_fallback(ctx: ProjectContext | None) -> None:
    """Set the process-wide fallback used by CLI runs and unbound callers.

    This is the legacy single-project behaviour, kept so ``pipeline.py`` and the
    existing tests keep working. HTTP handlers should never depend on it.
    """
    global _FALLBACK
    with _FALLBACK_LOCK:
        _FALLBACK = ctx


@contextmanager
def use(ctx: ProjectContext):
    """Bind ``ctx`` for the duration of a block, then restore the previous one."""
    token = bind(ctx)
    try:
        yield ctx
    finally:
        reset(token)


def capture() -> ProjectContext | None:
    """Snapshot the active context so it can be carried to another thread.

    The whole point of the C1 invariant: work that will run later records *now*
    which project it belongs to. See ``pipeline_worker.start_job``.
    """
    return current()

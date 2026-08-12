"""Project isolation and stale-response rejection (contract §11.3).

The failure this guards against is silent by nature: work targeted at project A
completes successfully against project B, and everything reports success. There
is no error to notice, so the only way to know it cannot happen is to assert it.

Every test here drives the real mechanism — a bound ``ProjectContext``, a real
background thread, a real TestClient request — rather than checking that some
function was called. The bug was never that a call was missing; it was that the
lookup happened at the wrong *time*.
"""

from __future__ import annotations

import sys
import threading
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

from backend import config, pipeline_worker, projects  # noqa: E402


def _project(tmp_path: Path, name: str) -> projects.ProjectContext:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    mf = d / "storyboard_manifest.json"
    mf.write_text('{"title": "%s", "shots": []}' % name, encoding="utf-8")
    return projects.ProjectContext.from_manifest(mf)


# --- identity -------------------------------------------------------------------

def test_a_context_is_immutable(tmp_path):
    """Capture proves nothing if the captured thing can change afterwards."""
    ctx = _project(tmp_path, "a")
    with pytest.raises(Exception):
        ctx.project_id = "something-else"  # type: ignore[misc]


def test_distinct_projects_get_distinct_ids(tmp_path):
    assert _project(tmp_path, "a").project_id != _project(tmp_path, "b").project_id


def test_the_id_is_stable_across_equivalent_paths(tmp_path):
    a = _project(tmp_path, "a")
    again = projects.ProjectContext.from_manifest(str(a.manifest_path))
    assert again.project_id == a.project_id


def test_every_derived_path_stays_inside_its_own_project(tmp_path):
    a, b = _project(tmp_path, "a"), _project(tmp_path, "b")
    for attr in ("assets", "references_dir", "director_dir", "render_dir", "audio_dir"):
        pa, pb = getattr(a, attr), getattr(b, attr)
        assert pa != pb
        assert a.root in pa.parents or pa == a.root
        assert b.root in pb.parents or pb == b.root


# --- binding --------------------------------------------------------------------

def test_binding_a_context_retargets_the_config_path_helpers(tmp_path):
    a = _project(tmp_path, "a")
    with projects.use(a):
        assert config.project_dir() == a.root
        assert config.assets_dir() == a.assets
        assert config.references_dir() == a.references_dir


def test_unbound_callers_still_read_the_process_globals(tmp_path, monkeypatch):
    """pipeline.py and the older tests must keep working exactly as before."""
    monkeypatch.setattr(config, "MANIFEST_PATH", tmp_path / "cli" / "m.json")
    assert projects.bound() is None
    assert config.project_dir() == tmp_path / "cli"


def test_a_binding_is_undone_when_the_block_exits(tmp_path):
    a = _project(tmp_path, "a")
    with projects.use(a):
        pass
    assert projects.bound() is None


def test_concurrent_threads_do_not_share_a_binding(tmp_path):
    """Two 'requests' at once must not see each other's project."""
    a, b = _project(tmp_path, "a"), _project(tmp_path, "b")
    seen: dict[str, Path] = {}
    entered = threading.Barrier(2, timeout=5)

    def run(ctx, key):
        with projects.use(ctx):
            entered.wait()          # both bound simultaneously
            seen[key] = config.project_dir()

    threads = [threading.Thread(target=run, args=(a, "a")),
               threading.Thread(target=run, args=(b, "b"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert seen["a"] == a.root
    assert seen["b"] == b.root


# --- the C1 invariant: jobs capture identity at ENQUEUE --------------------------

def test_a_job_uses_the_project_it_was_enqueued_against(tmp_path, monkeypatch):
    """The core regression.

    Enqueue against A, switch the process to B *before the job body runs*, and
    the job must still write into A. Previously the job read the globals when it
    ran, so this returned B's directory and the render landed in the wrong
    project while reporting success.
    """
    a, b = _project(tmp_path, "a"), _project(tmp_path, "b")
    release = threading.Event()
    done = threading.Event()
    observed: dict[str, Path] = {}

    def body():
        release.wait(timeout=5)      # the switch happens while we are queued
        observed["dir"] = config.project_dir()
        observed["ctx"] = projects.require().project_id
        done.set()

    with projects.use(a):
        assert pipeline_worker.start_job("isolation-probe", body) is True

    # The studio switches project while the job is still in flight.
    monkeypatch.setattr(config, "MANIFEST_PATH", b.manifest_path)
    projects.set_fallback(b)
    release.set()
    assert done.wait(timeout=5), "job did not run"

    assert observed["dir"] == a.root, "job followed the process, not its own project"
    assert observed["ctx"] == a.project_id


def test_job_status_reports_the_project_it_belongs_to(tmp_path):
    a = _project(tmp_path, "a")
    finished = threading.Event()
    with projects.use(a):
        pipeline_worker.start_job("status-probe", finished.set)
    assert finished.wait(timeout=5)
    with projects.use(a):
        assert pipeline_worker.get_jobs_status()["status-probe"]["project_id"] == a.project_id


def test_a_job_enqueued_with_no_context_does_not_crash(tmp_path):
    """CLI runs enqueue without a bound context; that must stay legal."""
    ran = threading.Event()
    assert pipeline_worker.start_job("no-ctx-probe", ran.set) is True
    assert ran.wait(timeout=5)


# --- HTTP: stale-response rejection ---------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient
    from backend import main as M

    a = _project(tmp_path, "alpha")
    monkeypatch.setattr(config, "MANIFEST_PATH", a.manifest_path)
    monkeypatch.setattr(M, "get_active_manifest_path", lambda: str(a.manifest_path))
    return TestClient(M.app, raise_server_exceptions=False), a


def test_every_response_names_the_project_it_is_about(client):
    """Without this header a client cannot tell a stale reply from a fresh one."""
    c, a = client
    r = c.get("/api/stages")
    assert r.headers.get("X-Project-Id") == a.project_id


def test_a_request_naming_an_unknown_project_is_refused(client):
    """Refusing is the only answer that cannot corrupt the wrong project."""
    c, _ = client
    r = c.get("/api/stages", headers={"X-Project-Id": "no_such_project"})
    assert r.status_code == 404
    assert "Unknown project" in r.json()["error"]


def test_an_unknown_project_is_never_silently_served_the_active_one(client):
    c, a = client
    r = c.get("/api/stages", headers={"X-Project-Id": "no_such_project"})
    assert r.headers.get("X-Project-Id") != a.project_id
    assert r.json().get("stages") is None


def test_naming_the_active_project_explicitly_is_accepted(client):
    c, a = client
    r = c.get("/api/stages", headers={"X-Project-Id": a.project_id})
    assert r.status_code == 200
    assert r.headers.get("X-Project-Id") == a.project_id


def test_the_query_parameter_targets_a_project_too(client):
    """Media and download URLs cannot set headers, so both forms must work."""
    c, a = client
    r = c.get(f"/api/stages?project_id={a.project_id}")
    assert r.status_code == 200
    assert r.headers.get("X-Project-Id") == a.project_id


def test_a_bound_request_does_not_repoint_the_process_globals(client, tmp_path):
    """A read must never mutate what a concurrent request would resolve."""
    c, a = client
    before = config.MANIFEST_PATH
    c.get("/api/stages", headers={"X-Project-Id": a.project_id})
    assert config.MANIFEST_PATH == before


def test_manifest_save_defaults_to_the_bound_project(tmp_path):
    """The worst version of §11.3: a whole storyboard written over the wrong one."""
    from backend import manifest
    a, b = _project(tmp_path, "a"), _project(tmp_path, "b")
    import backend.config as cfg
    original = cfg.MANIFEST_PATH
    try:
        cfg.MANIFEST_PATH = b.manifest_path          # process points at B
        sb = manifest.Storyboard(title="belongs-to-a")
        with projects.use(a):                        # this request is about A
            manifest.save(sb)
        assert manifest.load(a.manifest_path).title == "belongs-to-a"
        assert manifest.load(b.manifest_path).title != "belongs-to-a"
    finally:
        cfg.MANIFEST_PATH = original


def test_manifest_load_defaults_to_the_bound_project(tmp_path):
    from backend import manifest
    a, b = _project(tmp_path, "a"), _project(tmp_path, "b")
    manifest.save(manifest.Storyboard(title="A-side"), a.manifest_path)
    manifest.save(manifest.Storyboard(title="B-side"), b.manifest_path)
    import backend.config as cfg
    original = cfg.MANIFEST_PATH
    try:
        cfg.MANIFEST_PATH = b.manifest_path
        with projects.use(a):
            assert manifest.load().title == "A-side"
    finally:
        cfg.MANIFEST_PATH = original


# --- F-02: the job registry is per project, not global --------------------------

def test_two_projects_can_run_the_same_stage_at_once(tmp_path):
    """A shared job namespace meant one film blocked another's identical stage."""
    a, b = _project(tmp_path, "a"), _project(tmp_path, "b")
    release = threading.Event()
    started = {"a": threading.Event(), "b": threading.Event()}

    def body(which):
        started[which].set()
        release.wait(timeout=5)

    with projects.use(a):
        assert pipeline_worker.start_job("shared-stage", lambda: body("a")) is True
    with projects.use(b):
        assert pipeline_worker.start_job("shared-stage", lambda: body("b")) is True, \
            "project B was refused because A owned the global job name"

    assert started["a"].wait(timeout=5) and started["b"].wait(timeout=5)
    release.set()


def test_a_project_only_sees_its_own_jobs(tmp_path):
    """B's studio must never be shown A's progress as though it were B's."""
    a, b = _project(tmp_path, "a"), _project(tmp_path, "b")
    done = threading.Event()
    with projects.use(a):
        pipeline_worker.start_job("a-only-stage", done.set)
    assert done.wait(timeout=5)

    with projects.use(b):
        assert "a-only-stage" not in pipeline_worker.get_jobs_status()
    with projects.use(a):
        assert "a-only-stage" in pipeline_worker.get_jobs_status()


def test_same_named_jobs_do_not_share_a_log_buffer(tmp_path):
    a, b = _project(tmp_path, "a"), _project(tmp_path, "b")
    release = threading.Event()
    for ctx, word in ((a, "alpha"), (b, "bravo")):
        with projects.use(ctx):
            pipeline_worker.start_job(
                "logged-stage",
                lambda w=word: (pipeline_worker.log_job("logged-stage", w),
                                release.wait(timeout=5)))
    import time
    for _ in range(50):
        with projects.use(a):
            log_a = pipeline_worker.get_jobs_status().get("logged-stage", {}).get("log", "")
        with projects.use(b):
            log_b = pipeline_worker.get_jobs_status().get("logged-stage", {}).get("log", "")
        if "alpha" in log_a and "bravo" in log_b:
            break
        time.sleep(0.02)
    release.set()
    assert "alpha" in log_a and "bravo" not in log_a
    assert "bravo" in log_b and "alpha" not in log_b


# --- F-01: the application save wrapper, not just the primitive -----------------

def test_save_current_project_writes_to_the_bound_project(tmp_path, monkeypatch):
    """F-01. The wrapper used to pass the ACTIVE path explicitly, overriding the
    safe default -- so working on B overwrote A's whole manifest."""
    from backend import main as M, manifest
    a, b = _project(tmp_path, "a"), _project(tmp_path, "b")
    manifest.save(manifest.Storyboard(title="A-original"), a.manifest_path)
    manifest.save(manifest.Storyboard(title="B-original"), b.manifest_path)

    monkeypatch.setattr(M.config, "MANIFEST_PATH", a.manifest_path)
    monkeypatch.setattr(M, "get_active_manifest_path", lambda: str(a.manifest_path))
    monkeypatch.setattr(M.manifest, "save_project", lambda sb: None)

    before = a.manifest_path.read_bytes()
    with projects.use(b):
        M.save_current_project(manifest.Storyboard(title="B-mutated"))

    assert a.manifest_path.read_bytes() == before, "bound B overwrote active A"
    assert manifest.load(b.manifest_path).title == "B-mutated"


def test_save_current_project_stamps_the_bound_project_id(tmp_path, monkeypatch):
    """The document id and the file must not describe different projects."""
    from backend import main as M, manifest
    a, b = _project(tmp_path, "a"), _project(tmp_path, "b")
    monkeypatch.setattr(M.config, "MANIFEST_PATH", a.manifest_path)
    monkeypatch.setattr(M, "get_active_manifest_path", lambda: str(a.manifest_path))
    seen = {}
    monkeypatch.setattr(M.manifest, "save_project", lambda sb: seen.update(id=sb.id))

    with projects.use(b):
        M.save_current_project(manifest.Storyboard(title="x", id="stale-id"))
    assert seen["id"] == b.project_id


def test_reference_registry_follows_the_bound_project(tmp_path, monkeypatch):
    """PF-01. A reference chosen for B must not land in A's registry."""
    from backend import config as cfg
    a, b = _project(tmp_path, "a"), _project(tmp_path, "b")
    monkeypatch.setattr(cfg, "REFERENCES_CONFIG", a.references_config)
    monkeypatch.setattr(cfg, "CHARACTERS_CONFIG", a.characters_config)
    with projects.use(b):
        assert cfg.references_config() == b.references_config
        assert cfg.characters_config() == b.characters_config
    assert cfg.references_config() == a.references_config

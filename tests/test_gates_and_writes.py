"""Guards found missing by the end-to-end review (round 1).

Each test here failed before its fix; the fixes are one-liners whose absence was
invisible precisely because nothing raised.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for m in ("anthropic", "fal_client", "librosa", "elevenlabs"):
    sys.modules.setdefault(m, types.ModuleType(m))

from backend import audio  # noqa: E402


# --- narration must never leave a poison file -------------------------------

class _Boom:
    """A voice stream that dies partway, like a container reclaim mid-TTS."""
    def __iter__(self):
        yield b"\x49\x44\x33partial"
        raise ConnectionError("stream died")


def test_a_failed_narration_stream_leaves_nothing_behind(tmp_path):
    """Streaming into the final path left a truncated mp3 under the exact name
    every later run tests with .exists(), so the beat was skipped forever."""
    dest = tmp_path / "s001.mp3"
    with pytest.raises(ConnectionError):
        audio._write_stream(_Boom(), dest)
    assert not dest.exists(), "a half-written take must not occupy the real name"
    assert list(tmp_path.iterdir()) == [], "and must not leave a .partial either"


def test_an_empty_stream_is_an_error_not_a_zero_byte_file(tmp_path):
    dest = tmp_path / "s001.mp3"
    with pytest.raises(RuntimeError, match="no audio"):
        audio._write_stream(iter([]), dest)
    assert not dest.exists()


def test_a_good_stream_still_lands(tmp_path):
    dest = tmp_path / "s001.mp3"
    out = audio._write_stream(iter([b"abc", b"def"]), dest)
    assert out == dest and dest.read_bytes() == b"abcdef"
    assert not (tmp_path / "s001.mp3.partial").exists()


def test_zero_byte_narration_does_not_count_as_present(tmp_path):
    """`dest.exists()` is what made a poison file permanent."""
    empty, real = tmp_path / "a.mp3", tmp_path / "b.mp3"
    empty.touch()
    real.write_bytes(b"xx")
    assert audio._has_audio(empty) is False
    assert audio._has_audio(real) is True
    assert audio._has_audio(tmp_path / "missing.mp3") is False


# --- endpoint-level: the gates, exercised through the real app ----------------
# These need the FastAPI app to import, which needs the optional runtime deps.
# Skipped rather than failed where they are absent, so CI without them stays green.

fastapi_testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend import main as M
    monkeypatch.setattr(M, "ACTIVE_PROJECT_FILE", tmp_path / ".active_project")
    return TestClient(M.app), M


def test_paid_video_route_refuses_an_unapproved_storyboard(client):
    """Gate 1's only unenforced path. Five of the ten review lenses found it."""
    from backend.manifest import Camera, Shot, Storyboard
    tc, M = client
    sb = Storyboard(title="T", storyboard_approved=False,
                    shots=[Shot(scene_id="s001", narration="n", prompt="p",
                                camera=Camera(move="static", duration=6.0))])
    with pytest.raises(Exception) as exc:
        M.require_paid_gate(sb, "render")
    assert "Approve the storyboard first" in str(exc.value)

    sb.storyboard_approved = True
    M.require_paid_gate(sb, "render")          # approved -> silent


def test_project_switch_is_refused_while_a_job_runs(client, monkeypatch, tmp_path):
    """Switching projects while a job runs is refused.

    The switch target must be a REAL manifest inside the workspace root, or the
    request fails the containment check before it ever reaches the guard --
    which would make this test pass whether the guard exists or not.

    The workspace root is pointed at tmp_path rather than the checkout. This
    used to create ``chan/GuardTest`` inside the real repository and rmtree it
    in a finally, so an interrupted run left directories in the working tree,
    and a test that writes into the repo it is testing is one bad path
    expression away from deleting something that matters.
    """
    tc, M = client
    monkeypatch.setattr(M, "WORKSPACE_ROOT", tmp_path)
    man = tmp_path / "chan" / "GuardTest" / "storyboard_manifest.json"
    man.parent.mkdir(parents=True)
    man.write_text("{}", encoding="utf-8")

    # Control: with nothing running, this target is accepted.
    monkeypatch.setattr(M, "get_jobs_status", lambda *a, **k: {})
    assert tc.post("/api/project/select", json={"rel": str(man)}).status_code == 200

    # With a job running, the same request must be refused.
    monkeypatch.setattr(M, "get_jobs_status",
                        lambda *a, **k: {"render": {"status": "running", "log": ""}})
    r = tc.post("/api/project/select", json={"rel": str(man)})
    assert r.status_code == 409, r.status_code
    assert "still running" in r.json()["detail"]


def test_the_switch_guard_reads_the_real_job_registry(client, monkeypatch, tmp_path):
    """The same guard, without stubbing the thing under test.

    The test above replaces get_jobs_status wholesale, so it proves the endpoint
    calls the guard but says nothing about whether the guard can actually see a
    job. Now that the registry is keyed per project, "is anything running?" is a
    question about a specific project, and a guard that asked it of the wrong one
    would look identical from outside.
    """
    import threading
    from backend import config, pipeline_worker, projects

    tc, M = client
    monkeypatch.setattr(M, "WORKSPACE_ROOT", tmp_path)

    source = tmp_path / "chan" / "Source" / "storyboard_manifest.json"
    source.parent.mkdir(parents=True)
    source.write_text("{}", encoding="utf-8")
    target = tmp_path / "chan" / "Target" / "storyboard_manifest.json"
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")

    # The request binds the currently active project, so the job has to belong
    # to that same project for the guard to be asking about the right one.
    monkeypatch.setattr(config, "MANIFEST_PATH", source)
    ctx = projects.ProjectContext.from_manifest(source)

    release = threading.Event()
    running = threading.Event()
    try:
        with projects.use(ctx):
            pipeline_worker.start_job(
                "guard-probe",
                lambda: (running.set(), release.wait(timeout=5)))
        assert running.wait(timeout=5)

        r = tc.post("/api/project/select", json={"rel": str(target)})
        assert r.status_code == 409, r.status_code
        assert "guard-probe" in r.json()["detail"]
    finally:
        release.set()


def test_setting_the_active_project_repoints_the_process_config(tmp_path, monkeypatch):
    """The pointer file and config.MANIFEST_PATH were set by different functions,
    so they diverged until some later request happened to sync them."""
    from backend import config
    from backend import main as M
    monkeypatch.setattr(M, "ACTIVE_PROJECT_FILE", tmp_path / ".active_project")
    proj = tmp_path / "chan" / "Ep"
    proj.mkdir(parents=True)
    man = proj / "storyboard_manifest.json"
    man.write_text("{}", encoding="utf-8")

    M.set_active_manifest_path(str(man))
    assert config.MANIFEST_PATH == man.resolve()
    assert config.CHARACTERS_CONFIG == proj.resolve() / "characters.json"
    assert config.ASSETS == proj.resolve() / "assets"


# --- paid assets: never re-bill, never ship a deleted take --------------------

def test_deleting_the_active_take_replaces_the_clip_in_the_cut(tmp_path, monkeypatch):
    """Repointing shot.video_clip left render/<scene>.mp4 holding the DELETED
    take's pixels, so preview, FCPXML and master all kept playing the rejected
    take -- and the re-bill guard then refused to re-render over it."""
    from backend import config
    from backend import main as M
    from backend.manifest import Camera, MotionType, Shot, Storyboard

    assets = tmp_path / "assets" / "s001"
    render = tmp_path / "render"
    assets.mkdir(parents=True); render.mkdir(parents=True)
    take_a, take_b = assets / "v0.mp4", assets / "v1.mp4"
    take_a.write_bytes(b"AAAA"); take_b.write_bytes(b"BBBB")
    placed = render / "s001.mp4"
    placed.write_bytes(b"AAAA")                       # take A is in the cut

    shot = Shot(scene_id="s001", narration="n", prompt="p",
                camera=Camera(move="static", duration=6.0),
                motion_type=MotionType.AI_VIDEO)
    shot.video_variations = [str(take_a), str(take_b)]
    shot.video_clip = str(take_a)
    sb = Storyboard(title="T", shots=[shot], storyboard_approved=True)

    monkeypatch.setattr(M, "get_current_project", lambda: sb)
    monkeypatch.setattr(M, "save_current_project", lambda _sb: None)
    monkeypatch.setattr(M.config, "episode_paths",
                        lambda title: {"render": render, "narration": tmp_path,
                                       "sfx": tmp_path, "root": tmp_path})
    monkeypatch.setattr(M.config, "resolve_media",
                        lambda rel, scene_id=None: Path(rel) if Path(rel).is_file() else None)
    monkeypatch.setattr(M, "log_job", lambda *a, **k: None)

    res = M.delete_video_variation("s001", 0)
    assert res["ok"] and res["now_active"] == str(take_b)
    assert not take_a.exists(), "the rejected take's file is gone"
    assert placed.read_bytes() == b"BBBB", "the cut must play the surviving take"


def test_deleting_the_last_take_clears_the_placed_clip(tmp_path, monkeypatch):
    from backend import main as M
    from backend.manifest import Camera, MotionType, Shot, Storyboard

    assets = tmp_path / "assets" / "s001"; render = tmp_path / "render"
    assets.mkdir(parents=True); render.mkdir(parents=True)
    only = assets / "v0.mp4"; only.write_bytes(b"AAAA")
    placed = render / "s001.mp4"; placed.write_bytes(b"AAAA")

    shot = Shot(scene_id="s001", narration="n", prompt="p",
                camera=Camera(move="static", duration=6.0),
                motion_type=MotionType.AI_VIDEO)
    shot.video_variations = [str(only)]
    shot.video_clip = str(only)
    sb = Storyboard(title="T", shots=[shot], storyboard_approved=True)

    monkeypatch.setattr(M, "get_current_project", lambda: sb)
    monkeypatch.setattr(M, "save_current_project", lambda _sb: None)
    monkeypatch.setattr(M.config, "episode_paths",
                        lambda title: {"render": render, "narration": tmp_path,
                                       "sfx": tmp_path, "root": tmp_path})
    monkeypatch.setattr(M.config, "resolve_media",
                        lambda rel, scene_id=None: Path(rel) if Path(rel).is_file() else None)

    res = M.delete_video_variation("s001", 0)
    assert res["ok"] and res["now_active"] is None
    assert shot.video_clip is None
    assert not placed.exists(), "no take left, so nothing may remain in the cut"


# --- moving a project on a mount that rejects metadata ------------------------

def _mount_like_gcsfuse(monkeypatch):
    """Make copystat/chmod/utime raise the way gcsfuse does.

    Without this the tests pass on a normal filesystem no matter what the code
    does -- which is exactly why the first version of this fix looked correct and
    changed nothing.
    """
    import shutil as _sh
    import os as _os

    def denied(*a, **k):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(_sh, "copystat", denied)
    monkeypatch.setattr(_os, "chmod", denied)
    monkeypatch.setattr(_os, "utime", denied)
    # Directory renames are unsupported on the mount, which is what sends
    # shutil.move down the copytree path in the first place.
    real_rename = _os.rename
    def rename(src, dst, *a, **k):
        if Path(src).is_dir():
            raise OSError(18, "Invalid cross-device link")
        return real_rename(src, dst, *a, **k)
    monkeypatch.setattr(_os, "rename", rename)


def _project(root: Path) -> Path:
    p = root / "chan" / "Ep"
    (p / "assets" / "s001").mkdir(parents=True)
    (p / "render").mkdir()
    (p / "storyboard_manifest.json").write_text("{}", encoding="utf-8")
    (p / "assets" / "s001" / "v0.png").write_bytes(b"img")
    (p / "render" / "s001.mp4").write_bytes(b"vid")
    return p


def test_plain_shutil_move_really_does_fail_on_this_mount(tmp_path, monkeypatch):
    """The premise. If this ever stops failing, the fix below is unnecessary."""
    import shutil as _sh
    _mount_like_gcsfuse(monkeypatch)
    src = _project(tmp_path)
    with pytest.raises(Exception):
        _sh.move(str(src), str(tmp_path / "_trash" / "Ep"))


def test_copy_function_copyfile_is_not_enough(tmp_path, monkeypatch):
    """The mistake this fix corrects: copy_function governs FILE copies only, and
    shutil._copytree copystat()s every directory regardless."""
    import shutil as _sh
    _mount_like_gcsfuse(monkeypatch)
    src = _project(tmp_path)
    with pytest.raises(Exception):
        _sh.move(str(src), str(tmp_path / "_trash" / "Ep"),
                 copy_function=_sh.copyfile)


def test_move_tree_moves_everything_and_removes_the_source(tmp_path, monkeypatch):
    from backend import main as M
    _mount_like_gcsfuse(monkeypatch)
    src = _project(tmp_path)
    dst = tmp_path / "_trash" / "Ep"

    M._move_tree(src, dst)

    assert not src.exists(), "the source must be gone after a move"
    assert (dst / "storyboard_manifest.json").read_text() == "{}"
    assert (dst / "assets" / "s001" / "v0.png").read_bytes() == b"img"
    assert (dst / "render" / "s001.mp4").read_bytes() == b"vid"


def test_move_tree_keeps_the_source_when_the_copy_is_short(tmp_path, monkeypatch):
    """A half-finished move must never delete the original -- the whole failure
    being replaced is one where an incomplete move reported success."""
    import shutil as _sh
    from backend import main as M
    _mount_like_gcsfuse(monkeypatch)
    src = _project(tmp_path)

    real_copyfile = _sh.copyfile
    def flaky(a, b, *args, **kw):
        if str(a).endswith("s001.mp4"):
            return b        # silently copies nothing
        return real_copyfile(a, b, *args, **kw)
    monkeypatch.setattr(M.shutil, "copyfile", flaky)

    # The destruction first, the refusal second. Wrapped in pytest.raises, a
    # _move_tree that wrongly SUCCEEDS fails on "DID NOT RAISE" and the line
    # below never runs -- so under exactly the regression this guards, the suite
    # reddens without ever asserting that the original film is still there.
    try:
        M._move_tree(src, tmp_path / "_trash" / "Ep")
        refused = None
    except BaseException as exc:  # noqa: BLE001 -- classified below
        refused = exc

    assert src.exists() and (src / "render" / "s001.mp4").is_file(), \
        "a short copy deleted the original"
    assert isinstance(refused, RuntimeError) and "refusing to delete" in str(refused), \
        f"the short copy was accepted; got {refused!r}"


def test_another_project_s_job_does_not_block_this_switch(client, monkeypatch, tmp_path):
    """The registry is per project, so the guard asks about THIS one.

    Under the old global registry any film's running stage blocked every other
    film's project switch. That is not a safety property, it is a collision:
    the job it was protecting belongs to a different project and now carries its
    own context, so it cannot be redirected by this switch at all.
    """
    import threading
    from backend import config, pipeline_worker, projects

    tc, M = client
    monkeypatch.setattr(M, "WORKSPACE_ROOT", tmp_path)

    here = tmp_path / "chan" / "Here" / "storyboard_manifest.json"
    here.parent.mkdir(parents=True)
    here.write_text("{}", encoding="utf-8")
    elsewhere = tmp_path / "chan" / "Elsewhere" / "storyboard_manifest.json"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(config, "MANIFEST_PATH", here)
    other = projects.ProjectContext.from_manifest(elsewhere)

    release = threading.Event()
    running = threading.Event()
    try:
        with projects.use(other):
            pipeline_worker.start_job(
                "elsewhere-probe",
                lambda: (running.set(), release.wait(timeout=5)))
        assert running.wait(timeout=5)

        r = tc.post("/api/project/select", json={"rel": str(elsewhere)})
        assert r.status_code == 200, r.json()
    finally:
        release.set()

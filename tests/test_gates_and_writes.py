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
    """config paths are process globals read by worker threads on one instance, so
    switching mid-render redirects paid output into the other project.

    The switch target must be a REAL manifest inside the workspace, or the request
    404s on validation before it ever reaches the guard -- which would make this
    test pass whether the guard exists or not.
    """
    tc, M = client
    proj = M.WORKSPACE_ROOT / "chan" / "GuardTest"
    proj.mkdir(parents=True, exist_ok=True)
    man = proj / "storyboard_manifest.json"
    man.write_text("{}", encoding="utf-8")
    try:
        # Control: with nothing running, this target is accepted.
        monkeypatch.setattr(M, "get_jobs_status", lambda: {})
        assert tc.post("/api/project/select", json={"rel": str(man)}).status_code == 200

        # With a job running, the same request must be refused.
        monkeypatch.setattr(M, "get_jobs_status",
                            lambda: {"render": {"status": "running", "log": ""}})
        r = tc.post("/api/project/select", json={"rel": str(man)})
        assert r.status_code == 409, r.status_code
        assert "still running" in r.json()["detail"]
    finally:
        import shutil as _sh
        _sh.rmtree(proj, ignore_errors=True)


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

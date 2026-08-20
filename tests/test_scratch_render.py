"""ffmpeg must encode to local disk and reach the bucket as one copy.

Three properties, and the order of the classes in this file is the order of
their importance, not the order the code executes them.

FIRST, because it protects work a human has already paid for: **a copy that
fails must not lose a render that succeeded.** The render is the expensive half
-- frames warped, a still that may have been bought from fal underneath it --
and losing those bytes to a failed copy would be worse than the behaviour this
change replaces. Every other property here is an efficiency argument; this one
is not.

SECOND, the defect itself: a render whose output path is the destination rather
than local scratch. That is what ffmpeg was doing to ``/gcs`` --

    ffmpeg ... -i - -an -vcodec libx264 ... /gcs/calluses/TestRun/render/s003/s003.06.mp4

-- and it is what the Cloud Run logs of 2026-08-18 caught being rate-limited:
82 ``ComposeObject`` 429s on one beat clip, and a rate-limited ``s001.mp4``
whose ten shots were every one of them skipped, so no frame was encoded at all.

THIRD: a partially copied file must never be visible as a finished clip. That is
a failure mode this change could INTRODUCE, so it is tested even though nothing
has ever exhibited it.

Inside each test the assertion that proves the defect comes before any
supporting assertion, so none of them can pass by accident on the way to
something else.
"""

from __future__ import annotations

import errno
import shutil
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

from backend import scratch_render  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_scratch(tmp_path, monkeypatch):
    """Scratch under tmp_path, and no cached rename verdict from another test."""
    monkeypatch.setenv("RENDER_SCRATCH_DIR", str(tmp_path / "scratch"))
    monkeypatch.setattr(scratch_render, "_RENAME_OK", {})
    # The retry schedule is real seconds. Nothing here is testing the sleep.
    monkeypatch.setattr(scratch_render, "_COPY_BACKOFF", 0.0)


def _bucket(tmp_path) -> Path:
    """A stand-in for a render directory on the GCS-FUSE mount."""
    d = tmp_path / "gcs" / "proj" / "render"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------- #
# 1. A failed copy must not lose a render that succeeded.
# --------------------------------------------------------------------------- #

class TestAFailedCopyKeepsTheRender:

    def test_failed_publish_rescues_the_bytes_and_names_where_they_are(
            self, tmp_path, monkeypatch):
        """The whole point. The render worked; the mount did not.

        A ``PublishError`` that does not carry the surviving bytes is the same
        outcome as deleting them, because nothing else knows where to look.
        """
        dest = _bucket(tmp_path) / "s003.06.mp4"

        def _always_denied(src, dst):
            raise OSError(errno.EACCES, "the mount said no")

        monkeypatch.setattr(scratch_render.shutil, "copyfile", _always_denied)

        with pytest.raises(scratch_render.PublishError) as caught:
            scratch_render.render_to(dest, lambda local: local.write_bytes(b"finished render"))

        # THE assertion: the finished bytes still exist somewhere real.
        rescued = caught.value.rescued
        assert rescued is not None, "a failed copy discarded a successful render"
        assert rescued.is_file(), f"PublishError named {rescued}, which does not exist"
        assert rescued.read_bytes() == b"finished render"

        # ...and the human is told where, in the message they will actually see.
        assert str(rescued) in str(caught.value)
        # Nothing half-done was left at the destination.
        assert not dest.exists()

    def test_rescue_survives_the_scratch_directory_being_swept(
            self, tmp_path, monkeypatch):
        """``staged`` removes its directory unconditionally -- that is what bounds
        scratch usage. So the rescue has to move the file OUT of that directory,
        not merely decline to delete it."""
        dest = _bucket(tmp_path) / "s005.05.mp4"
        monkeypatch.setattr(scratch_render.shutil, "copyfile",
                            lambda *_: (_ for _ in ()).throw(OSError(errno.EIO, "io")))

        with pytest.raises(scratch_render.PublishError) as caught:
            scratch_render.render_to(dest, lambda local: local.write_bytes(b"paid work"))

        rescued = caught.value.rescued
        assert rescued.is_file(), "the rescued render was swept with the scratch dir"
        assert scratch_render.scratch_root() in rescued.parents
        # It is not inside a `render_*` staging directory, which is what gets removed.
        assert not any(p.name.startswith("render_") for p in rescued.parents)

    def test_a_transient_denial_is_retried_rather_than_losing_the_render(
            self, tmp_path, monkeypatch):
        """Bounded retry, in ``atomic.replace_with_retry``'s shape. A mount that
        refuses once and then works must not cost a beat."""
        dest = _bucket(tmp_path) / "s001.mp4"
        calls = {"n": 0}
        real = shutil.copyfile

        def _flaky(src, dst, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(errno.EAGAIN, "throttled")
            return real(src, dst, **kw)

        monkeypatch.setattr(scratch_render.shutil, "copyfile", _flaky)
        scratch_render.render_to(dest, lambda local: local.write_bytes(b"beat clip"))

        assert dest.read_bytes() == b"beat clip"
        assert calls["n"] == 2, "the transient denial was not retried"

    def test_a_permanent_failure_stops_instead_of_spinning(self, tmp_path, monkeypatch):
        """The bound is the other half of the retry. ENOSPC is not transient and
        must fail on the first attempt rather than sleeping through five."""
        dest = _bucket(tmp_path) / "s002.mp4"
        calls = {"n": 0}

        def _full(src, dst, **kw):
            calls["n"] += 1
            raise OSError(errno.ENOSPC, "no space left on device")

        monkeypatch.setattr(scratch_render.shutil, "copyfile", _full)
        with pytest.raises(scratch_render.PublishError):
            scratch_render.render_to(dest, lambda local: local.write_bytes(b"x"))
        assert calls["n"] == 1, "a full disk was retried as though it were transient"

    def test_a_render_that_raises_publishes_nothing_and_leaves_dest_alone(
            self, tmp_path):
        """The complement: a FAILED render must not replace a good clip."""
        dest = _bucket(tmp_path) / "s004.mp4"
        dest.write_bytes(b"the previous good clip")

        with pytest.raises(RuntimeError):
            def _dies(local):
                local.write_bytes(b"half an encode")
                raise RuntimeError("ffmpeg died")
            scratch_render.render_to(dest, _dies)

        assert dest.read_bytes() == b"the previous good clip"


# --------------------------------------------------------------------------- #
# 2. The defect: encoding straight onto the bucket.
# --------------------------------------------------------------------------- #

class TestTheEncodeHappensOnLocalScratch:

    def test_the_path_handed_to_the_encoder_is_scratch_not_the_destination(
            self, tmp_path):
        """The faithful mutation of this fix is ``render_to(dest, fn)`` calling
        ``fn(dest)``. This is the assertion that fails when it does."""
        dest = _bucket(tmp_path) / "s003.06.mp4"
        seen: list[Path] = []

        scratch_render.render_to(dest, lambda local: (seen.append(local),
                                                      local.write_bytes(b"v")))
        encoded_at = seen[0]

        # THE assertion: not the destination, and not anywhere in its directory.
        assert encoded_at != dest
        assert dest.parent not in encoded_at.parents, (
            f"the encoder wrote {encoded_at}, inside the destination directory "
            f"{dest.parent} — on Cloud Run that is the bucket")
        assert scratch_render.scratch_root() in encoded_at.parents
        assert dest.read_bytes() == b"v"

    def test_scratch_is_removed_once_the_clip_is_published(self, tmp_path):
        """What bounds scratch usage: one clip in flight, never an episode's worth."""
        dest = _bucket(tmp_path) / "s007.mp4"
        seen: list[Path] = []
        scratch_render.render_to(dest, lambda local: (seen.append(local),
                                                      local.write_bytes(b"x" * 512)))
        assert not seen[0].exists(), "scratch survived a successful publish"
        assert not seen[0].parent.exists()

    def test_scratch_root_honours_the_env_override(self, tmp_path, monkeypatch):
        """RENDER_SCRATCH_DIR is the escape hatch for Cloud Run's /tmp being
        memory-backed. If it is ignored, an operator's mounted disk is too."""
        elsewhere = tmp_path / "big_disk"
        monkeypatch.setenv("RENDER_SCRATCH_DIR", str(elsewhere))
        dest = _bucket(tmp_path) / "s008.mp4"
        seen: list[Path] = []
        scratch_render.render_to(dest, lambda local: (seen.append(local),
                                                      local.write_bytes(b"x")))
        assert elsewhere.resolve() in seen[0].resolve().parents

    def test_motion_render_shot_does_not_hand_the_bucket_to_the_encoder(
            self, tmp_path, monkeypatch):
        """The same property at the call site the observed ffmpeg command came
        from. Asserted against ``motion``, not against ``scratch_render``, because
        a helper nobody calls fixes nothing."""
        from backend import depth as depthmod
        from backend import motion
        from backend.manifest import Camera, MotionType, Shot

        import numpy as np

        out_dir = _bucket(tmp_path)
        still = tmp_path / "still.png"
        still.write_bytes(b"not really a png")

        monkeypatch.setattr(motion.config, "resolve_media", lambda *a, **k: still)
        monkeypatch.setattr(depthmod, "load_rgb",
                            lambda *_: np.zeros((16, 32, 3), dtype=np.uint8))

        opened: list[Path] = []

        class _Writer:
            def append_data(self, frame):
                pass

            def close(self):
                Path(opened[-1]).write_bytes(b"encoded")

        def _get_writer(path, **kw):
            opened.append(Path(path))
            return _Writer()

        monkeypatch.setattr(motion.imageio, "get_writer", _get_writer)

        shot = Shot(scene_id="s003.06", narration="n", prompt="p",
                    motion_type=MotionType.STATIC,
                    camera=Camera(move="static", duration=0.2))
        shot.draft_image = "still.png"

        out = motion.render_shot(shot, fps=4, height=9, out_dir=out_dir)

        # THE assertion: the writer was never pointed at the render directory.
        assert opened, "render_shot never opened a writer"
        assert out_dir not in opened[0].parents, (
            f"motion.render_shot encoded straight into {opened[0]}")
        assert scratch_render.scratch_root() in opened[0].parents
        assert out == out_dir / "s003.06.mp4"
        assert out.read_bytes() == b"encoded"

    def test_director_concat_builds_off_the_bucket(self, tmp_path, monkeypatch):
        """s001.mp4 on 2026-08-18 was rate-limited with every shot skipped: the
        concat's own writes were the entire mutation load. Both the build file
        and the concat listing must leave the destination directory."""
        from backend import director

        render_dir = _bucket(tmp_path)
        clips = []
        for n in (1, 2):
            c = render_dir / f"s001.0{n}.mp4"
            c.write_bytes(b"clip")
            clips.append(c)
        dest = render_dir / "s001.mp4"

        wrote: list[Path] = []

        def _fake_run(cmd, **kw):
            out = Path(cmd[-1])
            listing = Path(cmd[cmd.index("-i") + 1])
            wrote.append(out)
            wrote.append(listing)
            out.write_bytes(b"beat clip")
            return types.SimpleNamespace(returncode=0)

        monkeypatch.setattr(director.subprocess, "run", _fake_run)
        monkeypatch.setattr(director, "_ffmpeg", lambda: "ffmpeg")

        director.concat(clips, dest, log=lambda *_: None)

        # THE assertion: nothing ffmpeg touched lived in the render directory.
        offenders = [p for p in wrote if p.parent == render_dir]
        assert not offenders, (
            f"concat wrote {offenders} into the render directory — on Cloud Run "
            f"each one is a rate-limited bucket object")
        assert dest.read_bytes() == b"beat clip"


# --------------------------------------------------------------------------- #
# 3. A partial copy must never look like a finished clip.
# --------------------------------------------------------------------------- #

class TestAPartialCopyIsNeverVisible:

    def test_publication_is_atomic_where_the_mount_supports_rename(self, tmp_path):
        """tmp_path is an ordinary filesystem, so the staged-then-replaced path
        is the one under test. A reader sees the old clip or the new one."""
        dest = _bucket(tmp_path) / "s006.mp4"
        dest.write_bytes(b"old clip")

        observed: list[bytes] = []
        real = shutil.copyfile

        def _watching(src, dst, **kw):
            # What a reader would see mid-copy.
            observed.append(dest.read_bytes() if dest.exists() else b"")
            return real(src, dst, **kw)

        import unittest.mock as mock
        with mock.patch.object(scratch_render.shutil, "copyfile", _watching):
            scratch_render.render_to(dest, lambda local: local.write_bytes(b"new clip"))

        # THE assertion: at no point was a prefix of the new clip at `dest`.
        assert observed == [b"old clip"], (
            f"a reader could see {observed} at the destination mid-publish")
        assert dest.read_bytes() == b"new clip"

    def test_the_staging_file_can_never_be_served_as_media(self, tmp_path, monkeypatch):
        """Belt and braces for the branch where rename is refused and the copy is
        NOT atomic: whatever is staged must at least be unservable and invisible
        to the ``*.mp4`` checks that decide a clip is finished."""
        from backend import config

        dest = _bucket(tmp_path) / "s009.mp4"
        staged: list[Path] = []
        real = shutil.copyfile

        def _capture(src, dst, **kw):
            staged.append(Path(dst))
            return real(src, dst, **kw)

        monkeypatch.setattr(scratch_render.shutil, "copyfile", _capture)
        scratch_render.render_to(dest, lambda local: local.write_bytes(b"clip"))

        stage = staged[-1]
        # THE assertion: the in-flight name is not a media file by any test the
        # codebase applies to one.
        assert stage.suffix not in config.MEDIA_SUFFIXES
        assert not config.is_within_media_roots(stage)
        assert stage.name.startswith("."), "the staging file is not hidden"
        assert dest.is_file(), "sanity: the clip was published"
        assert stage != dest

    def test_a_rename_hostile_mount_still_publishes_rather_than_refusing(
            self, tmp_path, monkeypatch):
        """gcsfuse has been recorded refusing rename outright (see
        ``director.normalize_clip``). Refusing to publish would strand a finished
        render on scratch that dies with the container, which is worse than a
        non-atomic copy. So the fallback must actually run."""
        dest = _bucket(tmp_path) / "s010.mp4"
        monkeypatch.setattr(scratch_render, "_rename_supported", lambda _d: False)

        targets: list[Path] = []
        real = shutil.copyfile

        def _capture(src, dst, **kw):
            targets.append(Path(dst))
            return real(src, dst, **kw)

        monkeypatch.setattr(scratch_render.shutil, "copyfile", _capture)
        scratch_render.render_to(dest, lambda local: local.write_bytes(b"clip"))

        assert dest.read_bytes() == b"clip"
        # One write to the bucket, straight to the destination: no staged copy,
        # because staging then renaming is what this mount cannot do, and paying
        # for the staged bytes anyway would double the mutation cost.
        assert targets == [dest]

    def test_rename_support_is_probed_once_per_directory(self, tmp_path, monkeypatch):
        """The probe costs two empty files. Per publish that would be silly; per
        directory per process it is a rounding error against one beat clip."""
        render_dir = _bucket(tmp_path)
        probes = {"n": 0}
        real = scratch_render.os.replace

        def _counting(a, b):
            if ".renameprobe." in str(a):
                probes["n"] += 1
            return real(a, b)

        monkeypatch.setattr(scratch_render.os, "replace", _counting)
        for n in range(3):
            scratch_render.render_to(render_dir / f"s0{n}.mp4",
                                     lambda local: local.write_bytes(b"c"))
        assert probes["n"] == 1, f"probed {probes['n']} times for one directory"
        assert not list(render_dir.glob(".renameprobe*")), "the probe left litter"

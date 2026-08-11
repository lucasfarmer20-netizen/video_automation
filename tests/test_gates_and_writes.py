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

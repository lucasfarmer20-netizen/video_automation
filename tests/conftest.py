"""Test isolation for the module-level config globals.

backend/config.py keeps MANIFEST_PATH, ASSETS, REFERENCES_DIR, REFERENCES_CONFIG
and CHARACTERS_CONFIG as process globals that set_active_manifest rebinds. Tests
monkeypatch them to tmp_path; monkeypatch restores the ones it set, but any code
under test that calls set_active_manifest rebinds them for real, and that
survives the test.

The symptom is order-dependent failures: tests/test_director.py's gestural cases
passed alone and failed in the full suite, because an earlier test had left the
globals pointing into a tmp directory that no longer existed. An order-dependent
suite cannot tell you whether a change is safe, which is the only thing a suite
is for.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

_GLOBALS = ("MANIFEST_PATH", "ASSETS", "REFERENCES_DIR", "REFERENCES_CONFIG",
            "CHARACTERS_CONFIG", "AUDIO_DIR", "AUDIO_POOL", "ROOT")


@pytest.fixture(autouse=True)
def _restore_config_globals():
    from backend import config
    before = {k: getattr(config, k) for k in _GLOBALS if hasattr(config, k)}
    yield
    for k, v in before.items():
        setattr(config, k, v)

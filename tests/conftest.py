"""Test isolation for the module-level config globals, plus loud skip accounting.

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

The second concern here is the skip count. pytest prints it in the status line,
where it is easy to read past: a run reporting "105 passed" looked green while
12 ffmpeg-dependent tests had silently not run, and an earlier handoff recorded
that as "117/117 passing". pytest_terminal_summary below reprints the skips as
their own banner, grouped by reason, so an incomplete run cannot read as a
clean one.

Expected failures are counted the same way and for the same reason. An xfail is
a test whose assertions did not run, but pytest files it under stats["xfailed"]
rather than stats["skipped"], so a banner that counted only skips would report
"0 skipped - full suite ran" over a permanently-red test — the same false green
in a different column.
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


def _report_xfails(terminalreporter):
    """List xfails and xpasses. Same argument as the skip banner: a test that is
    expected to fail did not run its assertions either, and pytest files it
    under stats["xfailed"], not stats["skipped"] — so counting only skips would
    let a permanently-red test hide behind "0 skipped - full suite ran", which
    is the exact blind spot this module exists to close.

    xpassed is listed too, for the non-strict markers. A *strict* xfail that
    starts passing is filed under stats["failed"], not stats["xpassed"] --
    verified, not assumed -- so pytest already fails the run and names it, and
    nothing here needs to catch it. A non-strict one is the quiet case: it
    passes, says XPASS in the status line and nothing else, and the gap its
    reason describes has in fact closed and the marker should come off."""
    xfailed = terminalreporter.stats.get("xfailed", [])
    xpassed = terminalreporter.stats.get("xpassed", [])
    if not xfailed and not xpassed:
        return

    terminalreporter.write_sep("-", f"{len(xfailed) + len(xpassed)} KNOWN-GAP "
                                    f"TEST(S) DID NOT ASSERT", yellow=True)
    for report in sorted(xfailed, key=lambda r: r.nodeid):
        reason = str(getattr(report, "wasxfail", "") or "no reason given")
        terminalreporter.write_line(f"  xfail: {report.nodeid}")
        terminalreporter.write_line(f"         {reason}")
    for report in sorted(xpassed, key=lambda r: r.nodeid):
        terminalreporter.write_line(
            f"  XPASS: {report.nodeid} - the gap this marker tracks looks "
            f"closed; remove the xfail.", yellow=True)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Reprint skips as their own banner — the status line hides them too well."""
    skipped = terminalreporter.stats.get("skipped", [])
    passed = len(terminalreporter.stats.get("passed", []))
    deferred = (len(terminalreporter.stats.get("xfailed", []))
                + len(terminalreporter.stats.get("xpassed", [])))
    if not skipped:
        tail = f", {deferred} known-gap" if deferred else ""
        terminalreporter.write_sep("=", f"coverage: {passed} passed, 0 skipped"
                                        f"{tail} - full suite ran",
                                   green=not deferred, yellow=bool(deferred))
        _report_xfails(terminalreporter)
        return

    reasons: dict[str, list[str]] = {}
    for report in skipped:
        # longrepr for a skip is (path, lineno, "Skipped: <reason>")
        reason = "unknown"
        lr = getattr(report, "longrepr", None)
        if isinstance(lr, tuple) and len(lr) == 3:
            reason = str(lr[2]).removeprefix("Skipped: ").strip() or "unknown"
        reasons.setdefault(reason, []).append(report.nodeid)

    terminalreporter.write_sep("=", f"{len(skipped)} TEST(S) DID NOT RUN", yellow=True)
    for reason, nodeids in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
        terminalreporter.write_line(f"  {len(nodeids):>3} skipped: {reason}", yellow=True)
        for nodeid in sorted(nodeids):
            terminalreporter.write_line(f"        {nodeid}")
    terminalreporter.write_line(
        f"  {passed} passed is NOT a full pass - {len(skipped)} test(s) above were "
        f"never executed.", yellow=True)
    _report_xfails(terminalreporter)

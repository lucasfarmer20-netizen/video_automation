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

Expected failures are counted for a related reason, though not the same one. An
xfail *does* run and *does* execute its assertions -- they fail, which is the
point -- so it is not a did-not-run at all. What it has in common with a skip is
that it protects nothing today, and that pytest files it in a quiet counter of
its own (stats["xfailed"], not stats["skipped"]). A banner that counted only
skips therefore printed "0 skipped - full suite ran" over a permanently-red
test: the same false green in a different column.

The banner also has to be honest about what it did not run. `-k`, `-m`, a
`--lf` with a stored cache and a collection error all produce a partial run, so
deselected/error counts are reported and the words "full suite ran" are held
back unless the run was genuinely complete.
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
    """List xfails and xpasses with their reasons.

    These tests run and their assertions execute; the assertions fail, on
    purpose. So this is not a did-not-run report -- it is a "protects nothing
    yet" report, which is why the banner says EXPECTED FAILURES rather than
    anything about not asserting. What it shares with the skip banner is the
    failure mode: pytest keeps them in a counter of their own, and a summary
    that watched only stats["skipped"] would call a run clean while a
    permanently-red test sat in it.

    xpassed is listed for the non-strict markers. A *strict* xfail that starts
    passing is filed under stats["failed"], not stats["xpassed"] -- verified,
    not assumed -- so pytest already fails the run and names it, and nothing
    here needs to catch it. A non-strict one is the quiet case: it passes, says
    XPASS in the status line and nothing else, and the gap its reason describes
    has in fact closed and the marker should come off."""
    xfailed = terminalreporter.stats.get("xfailed", [])
    xpassed = terminalreporter.stats.get("xpassed", [])
    if not xfailed and not xpassed:
        return

    terminalreporter.write_sep("-", f"{len(xfailed) + len(xpassed)} EXPECTED "
                                    f"FAILURE(S) / KNOWN GAPS", yellow=True)
    for report in sorted(xfailed, key=lambda r: r.nodeid):
        reason = str(getattr(report, "wasxfail", "") or "no reason given")
        terminalreporter.write_line(f"  xfail: {report.nodeid}")
        terminalreporter.write_line(f"         {reason}")
    for report in sorted(xpassed, key=lambda r: r.nodeid):
        terminalreporter.write_line(
            f"  XPASS: {report.nodeid} - the gap this marker tracks looks "
            f"closed; remove the xfail.", yellow=True)


_INTERRUPT: dict[str, str] = {}


def pytest_keyboard_interrupt(excinfo):
    """Record that the session was aborted, and why.

    ``wrap_session`` (main.py:336-345) fires this for BOTH KeyboardInterrupt
    and ``pytest.exit()`` -- they share an except arm -- and it fires *before*
    an exit status is chosen. That ordering is the point: it is the one signal
    that survives ``pytest.exit(returncode=N)``, where the exit status is
    whatever the caller asked for and no longer says "interrupted".
    """
    value = getattr(excinfo, "value", None)
    message = str(getattr(value, "msg", "") or value or "").strip()
    kind = type(value).__name__ if value is not None else "interrupt"
    _INTERRUPT["reason"] = f"{kind}: {message}" if message else kind


def _early_stop_reason(terminalreporter, exitstatus=None) -> str:
    """Why the session stopped early, or "" if it ran to the end.

    Four arms, in this order: ``shouldfail``, ``shouldstop``, the recorded
    ``_INTERRUPT`` reason, and ``exitstatus == INTERRUPTED`` as the catch-all.
    The first two carry a reason string of their own; the third reconstructs
    one; the last only knows that something happened.

    The first three conditions are pytest's own, but NOT in pytest's order --
    ``terminal.py``
    tests INTERRUPTED before ``shouldstop``, and copying it verbatim mislabels
    ``--stepwise``. ``Interrupted`` subclasses ``KeyboardInterrupt``, so a
    stepwise stop raises it, ``wrap_session`` catches it in the same arm as
    Ctrl-C, and the session ends with BOTH ``shouldstop`` set and
    ``exitstatus == INTERRUPTED``. Measured under pytest's order: a stepwise
    stop reported "interrupted (pytest.exit() or KeyboardInterrupt)" when the
    true reason was "Test failed, continuing from this test next run."
    Reordered, it reports the latter.

    The orders differ because the jobs differ. pytest is choosing which *report*
    to print and prefers the keyboard-interrupt traceback; this function is
    choosing which *reason string* to show, so the specific one has to win over
    the generic one. INTERRUPTED is last precisely because it is the arm that
    cannot say why.

    DO NOT "simplify" this to ``shouldstop`` alone. That is the obvious-looking
    attribute and it is the wrong one; it was probed and rejected. Measured on
    pytest 9.1.1:

        run                    shouldstop   shouldfail   exitstatus
        -x, 1 of 2 failed      False        'stopping…'  TESTS_FAILED
        --maxfail=1            False        'stopping…'  TESTS_FAILED
        --stepwise, 1 failed   'Test fail…' False        INTERRUPTED
        pytest.exit() mid-run  False        False        INTERRUPTED
        completed normally     False        False        OK

    ``-x`` and ``--maxfail=N`` are one signal, not two: ``-x`` is registered
    with ``const=1, dest="maxfail"``, so both rows above are the same code path
    (``main.py:703``) shown twice.

    The abort arms exist because an earlier revision of this docstring claimed
    ``shouldstop`` covered ``pytest.exit()`` and internal aborts. It does not,
    and that claim was the justification for an arm that therefore caught
    nothing. Across all of ``_pytest`` exactly two lines assign either flag --
    ``main.py:703`` (shouldfail) and ``stepwise.py:187`` (shouldstop) -- and
    ``pytest.exit()`` raises ``Exit``, which ``wrap_session`` catches at
    ``main.py:336-345``, setting ``session.exitstatus`` and firing
    ``pytest_keyboard_interrupt`` without touching either flag. KeyboardInterrupt
    takes the same branch. Measured before the fix: a probe calling
    ``pytest.exit()`` after the first of three tests printed
    "1 passed, 0 skipped - full suite ran".

    ``shouldstop`` keeps its place with a reason that is actually true:
    ``--stepwise`` sets it, and third-party plugins (pytest-xdist,
    pytest-timeout) set it too.

    The fourth arm, ``_INTERRUPT``, exists because the exitstatus comparison
    alone leaves a hole and the mapping behind it is not something to lean on.
    ``pytest.exit(returncode=N)`` puts N in the exit status, so INTERRUPTED
    never matches. Measured:

        call                     exitstatus at the hook   without _INTERRUPT
        pytest.exit("x")         2 (INTERRUPTED)          caught
        pytest.exit(returncode=0) 0                       "full suite ran"
        pytest.exit(returncode=1) 1                       "full suite ran"
        pytest.exit(returncode=3) 1  (not 3)              "full suite ran"

    The last row is the reason this is a hook and not a wider comparison: 3
    arrives as 1, so no set of exit codes written here would be reliable.
    ``pytest_keyboard_interrupt`` fires before any of that mapping happens, for
    both KeyboardInterrupt and Exit, so it catches all four rows. The exitstatus
    arm is kept underneath it as a cheap backstop.

    Everything is read through getattr defaults because these are internals. A
    summary hook that raised would be a worse failure than one that
    under-reports -- the whole point of this module is that the run's shape
    stays legible, and an exception here would take the skip and known-gap
    banners down with it.
    """
    session = getattr(terminalreporter, "_session", None)

    for attr in ("shouldfail", "shouldstop"):
        value = getattr(session, attr, False)
        if value:
            return value if isinstance(value, str) else f"session.{attr}"

    if _INTERRUPT.get("reason"):
        return _INTERRUPT["reason"]

    if exitstatus is not None and exitstatus == pytest.ExitCode.INTERRUPTED:
        return "interrupted"

    return ""


def _coverage_line(stats, stop_reason: str = "") -> tuple[str, bool]:
    """The banner text, and whether the run was actually the whole suite.

    "full suite ran" is a claim, and it was being printed over runs that were
    nothing of the kind: `-k` matching nothing, a collection error, or `-x`
    aborting the session all used to produce "... - full suite ran". Pytest's
    own output still showed the deselection, the error or the `!!! stopping`
    line, so nothing was hidden -- but a banner added specifically to stop a
    partial run reading as a clean one must not be the thing asserting it. The
    phrase is held back unless the run is genuinely complete, and whatever
    prevented that is named.
    """
    def n(key: str) -> int:
        return len(stats.get(key, []))

    gaps = n("xfailed") + n("xpassed")
    parts = [f"{n('passed')} passed"]
    if n("failed"):
        parts.append(f"{n('failed')} failed")
    if n("error"):
        parts.append(f"{n('error')} error")
    parts.append(f"{n('skipped')} skipped")
    if gaps:
        parts.append(f"{gaps} known-gap")
    if n("deselected"):
        parts.append(f"{n('deselected')} deselected")

    # Only things that stopped a test from running make the run partial. A
    # failure is a complete run with a bad result, which is a different claim
    # and one pytest already makes loudly.
    #
    # There are four ways a run ends up short, and they are listed here rather
    # than inferred, because each was missed in turn: deselection (-k/-m),
    # skips, collection or setup errors, and the session stopping early. That
    # last one is why `stop_reason` is a parameter -- it is the only one of the
    # four that leaves no trace in `stats`, so counting reports alone will call
    # an aborted run complete.
    partial = []
    if n("deselected"):
        partial.append(f"{n('deselected')} deselected")
    if n("skipped"):
        partial.append(f"{n('skipped')} skipped")
    if n("error"):
        partial.append(f"{n('error')} collection/setup error(s)")
    if stop_reason:
        partial.append(f"stopped early ({stop_reason})")
    if not (n("passed") or n("failed") or n("skipped") or gaps):
        partial.append("nothing ran")

    tail = "full suite ran" if not partial else "PARTIAL RUN: " + ", ".join(partial)
    return "coverage: " + ", ".join(parts) + " - " + tail, not partial


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Reprint skips as their own banner — the status line hides them too well."""
    stats = terminalreporter.stats
    skipped = stats.get("skipped", [])
    passed = len(stats.get("passed", []))
    line, complete = _coverage_line(
        stats, _early_stop_reason(terminalreporter, exitstatus))
    # Green means "nothing here needs your attention", and a known gap does.
    #
    # `complete` and `clean` are deliberately different questions. A run with
    # xfails IS complete -- an xfail executes and its assertions run -- so it is
    # not a PARTIAL RUN and the tail still says "full suite ran". But it is not
    # clean either: each gap is a hazard someone decided to defer, and this
    # module exists because a compromised run that reads fine at a glance is how
    # 12 unrun tests once got recorded as "117/117 passing". Colour is the part
    # read at a glance, so gaps take it to yellow even though the text already
    # says "3 known-gap" and the EXPECTED FAILURE(S) block follows.
    gaps = len(stats.get("xfailed", [])) + len(stats.get("xpassed", []))
    clean = (complete and not stats.get("failed") and not stats.get("error")
             and not gaps)
    terminalreporter.write_sep("=", line, green=clean, yellow=not clean)

    if not skipped:
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

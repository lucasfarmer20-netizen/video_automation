"""Mutation-sensitivity check for the LOCK & GENERATE COVERAGE refusal path.

A test that passes proves nothing on its own; what it must do is FAIL when the
fix it guards is taken away. Each entry below is a faithful mutation -- the shape
a future edit would actually take -- and the run is only meaningful if every one
is KILLED.

DYING IS NOT ENOUGH; IT HAS TO DIE FOR THE RIGHT REASON.

The defect this stands for is that a refused lock reached the human as nothing at
all, so the assertion that has to do the killing is the one that reads the
server's own sentence out of the DOM. A mutation killed by a missing element, or
by a state check that happened to run first, has not proved that anybody was
told anything. So a mutation may declare:

  expect_fail    test names that MUST be among the failures
  expect_output  a substring that MUST appear in the run's output -- the
                 defect-proving assertion's own sentinel, which can only be
                 printed if that assertion actually ran and evaluated

M1 is the original defect, restored: the handler exactly as it was before this
round, with no try/catch, no busy state and no read-back. Everything else is a
way of half-doing the fix.

DO NOT EDIT THE TARGET FILES WHILE THIS RUNS. It mutates
`DirectorWorkspace.tsx` and `directorApi.ts` in place and restores them after
each case, so a concurrent edit is reverted without a word. It now refuses to
restore a file that changed under it and says so, but the only safe way to run
it is on a committed tree with nothing else touching those two files.

Run from the repo root:  python scratch/mutate_lock_refusal.py
Or one at a time:        python scratch/mutate_lock_refusal.py M27 M35
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
WS = ROOT / "frontend/src/components/DirectorWorkspace.tsx"
API = ROOT / "frontend/src/lib/directorApi.ts"

TARGETS = [
    "src/lib/directorApi.lock.test.ts",
    "src/components/DirectorWorkspace.lock.test.tsx",
    "src/components/DirectorWorkspace.replan.test.tsx",
]

# The defect-proving assertion's failure message, defined in the component test.
# Its presence in the output is proof the assertion was reached and evaluated.
REFUSAL_NEVER_REACHED_THE_HUMAN = (
    "the lock was refused and the server's sentence never reached the screen: "
    "the click is indistinguishable from a dead button"
)
# The same, at the API seam.
SENTENCE_LOST = "the server's own sentence did not survive setCoverageStatus"
# And for the re-plan controls, where the defect is that a running job looked
# exactly like a button that did nothing.
LOOKED_LIKE_NOTHING_HAPPENED = (
    "a re-plan job is running and the control says nothing about it: "
    "indistinguishable from a button that did nothing"
)

# The handler as it stood at 082f67b. Restored by M1 via a span replacement, so
# the reproduction is the real one and not a transcription of it: the harness
# cuts from the first marker to the second and drops these two lines in.
ORIGINAL_HANDLER_BODY = """    await setCoverageStatus(beatsToLock, shouldLock);
    setCoveragePlan({ ...coveragePlan, status: shouldLock ? "locked" : "draft" });
"""

# (name, file, old, new, expect_fail, expect_output)
# `old` may be a (start, end) pair, meaning "everything from start through end".
MUTATIONS = [
    # --- the original defect ------------------------------------------------
    ("M1  the handler as it was: no catch, no busy state, no read-back", WS,
     ('    setLockBusy(shouldLock ? "locking" : "unlocking");',
      "    } finally {\n      setLockBusy(null);\n    }\n  };"),
     ORIGINAL_HANDLER_BODY + "  };",
     ["undecided findings", "the auth 401", "the catch-all 400",
      "the control says so, and a second click sends nothing"],
     REFUSAL_NEVER_REACHED_THE_HUMAN),

    # --- the refusal never reaches the DOM ----------------------------------
    ("M2  catch the rejection, set the state, render nothing", WS,
     "      {lockProblem && (",
     "      {false && (",
     ["undecided findings", "the auth 401"], REFUSAL_NEVER_REACHED_THE_HUMAN),
    ("M3  swallow the refusal in the catch, as handleRecheckCritique once did", WS,
     "      const changed = err?.changed || [];",
     "      if (true) return;\n      const changed = err?.changed || [];",
     ["undecided findings", "the auth 401"], REFUSAL_NEVER_REACHED_THE_HUMAN),
    ("M4  keep the headline, drop the per-beat sentences", WS,
     "              {lockProblem.problems.length > 0 && (",
     "              {false && (",
     ["undecided findings", "every beat that refused is listed"],
     REFUSAL_NEVER_REACHED_THE_HUMAN),
    ("M5  collapse `problems` into nothing on the way in", WS,
     "        problems: err?.problems || [],",
     "        problems: [],",
     ["undecided findings", "every beat that refused is listed"],
     REFUSAL_NEVER_REACHED_THE_HUMAN),
    ("M6  render the list but drop the headline it explains", WS,
     "              <span>{lockProblem.message}</span>",
     "              <span />",
     ["the auth 401", "the catch-all 400"], REFUSAL_NEVER_REACHED_THE_HUMAN),
    ("M7  a refusal with no message at all becomes an empty box", WS,
     "          err?.message ||\n          `${shouldLock ? \"Locking\" : \"Unlocking\"} ${named} failed before the server answered.`,",
     "          err?.message || \"\",",
     ["a transport failure with no server sentence"],
     REFUSAL_NEVER_REACHED_THE_HUMAN),

    # --- false success ------------------------------------------------------
    ("M8  claim locked because the promise resolved", WS,
     "      const took = shouldLock\n"
     "        ? fresh.status === \"locked\" || fresh.status === \"compiled\"\n"
     "        : fresh.status === \"draft\";",
     "      const took = true;",
     ["the saved plan still says draft"], ""),
    ("M9  write the status locally instead of reading it back", WS,
     "      const fresh = await fetchCoveragePlan(beatsToLock);\n      setCoveragePlan(fresh);",
     "      const fresh = { ...coveragePlan, status: shouldLock ? \"locked\" : \"draft\" };",
     ["carries the signature the compile gate has to send",
      "reads the plan back"], ""),
    ("M10 report an accepted lock whose read-back failed as a refusal", WS,
     "      if (accepted) {",
     "      if (false) {",
     ["is not reported as a refusal"], ""),

    # --- in flight ----------------------------------------------------------
    # BOTH halves, in one mutation, and deliberately so. A click while the
    # request is in flight is stopped twice over -- the button is disabled and
    # the handler returns early -- and either alone is sufficient, so removing
    # only one is a mutation nothing can detect. Splitting them would produce
    # two survivors that mean "this defence is redundant", not "this defence is
    # untested". What has to be killed is the loss of in-flight protection.
    ("M11 no in-flight protection at all: a second click fires a second request", WS,
     [("    if (!coveragePlan || lockBusy) return;",
       "    if (!coveragePlan) return;"),
      ("            disabled={Boolean(lockBusy)}",
       "            disabled={false}")],
     "",
     ["a second click sends nothing"], ""),
    ("M12 the button greys out but keeps its old label", WS,
     "            {lockBusy ? (\n              <>\n                <Sparkles className=\"w-4 h-4 animate-spin\" />\n"
     "                <span>{lockBusy === \"locking\" ? \"LOCKING…\" : \"UNLOCKING…\"}</span>\n              </>\n            ) : isLocked ? (",
     "            {isLocked ? (",
     ["the control says so", "an unlock in flight says UNLOCKING"], ""),
    ("M13 leave the control disabled after a refusal, so it cannot be retried", WS,
     "    } finally {\n      setLockBusy(null);\n    }",
     "    }",
     ["the control comes back after a refusal"], ""),

    # --- the rejected design: guess the answer client-side -------------------
    ("M14 disable the button while findings are outstanding", WS,
     "            disabled={Boolean(lockBusy)}",
     "            disabled={Boolean(lockBusy) || unresolvedWarnings.length > 0}",
     ["never disable the button"], ""),
    ("M15 point at the queue after a refused UNLOCK too", WS,
     "        changed,\n        wasLocking: shouldLock,",
     "        changed,\n        wasLocking: true,",
     ["a refused UNLOCK does not offer it"], ""),
    # No sentinel: the refusal still reaches the DOM here, which is the point.
    # What is removed is the way to act on it, so the failure that has to do the
    # killing is the absent control, and that is what `expect_fail` names.
    ("M16 drop the pointer to the queue entirely", WS,
     "            {lockProblem.wasLocking && unresolvedWarnings.length > 0 && (\n"
     "              <button\n                data-testid=\"lock-open-queue\"",
     "            {false && (\n"
     "              <button\n                data-testid=\"lock-open-queue\"",
     ["offers the queue"], ""),

    # --- the asymmetry between locking and unlocking ------------------------
    ("M17 a part-done unlock does not say what it already changed", WS,
     "        problems: err?.problems || [],\n        changed,",
     "        problems: err?.problems || [],\n        changed: [],",
     ["a part-done unlock says which beats"], ""),
    ("M18 a part-done unlock leaves the stale status badge on screen", WS,
     "      if (changed.length > 0) {",
     "      if (false) {",
     ["a part-done unlock says which beats"], ""),

    # --- the message belongs to the scene it happened on --------------------
    ("M19 the refusal follows the human to the next scene", WS,
     "    setLockDone(null);\n    setLockProblem(null);",
     "    setLockDone(null);",
     ["is gone when s002 is opened"], ""),

    # --- the API seam -------------------------------------------------------
    ("M20 drop `problems` at the seam, as `new Error(data.error)` did", API,
     "  if (Array.isArray(data.problems) && data.problems.length > 0) {",
     "  if (false) {",
     ["undecided findings", "every beat that failed is reported"], SENTENCE_LOST),
    ("M21 read only `error`, losing the HTTPException's `detail`", API,
     "  const said = data.error || data.detail;",
     "  const said = data.error;",
     ["beats[] is required"], SENTENCE_LOST),
    ("M22 report only the first beat that refused to unlock", API,
     "    const bad = results.filter((r) => !r.ok);",
     "    const bad = results.filter((r) => !r.ok).slice(0, 1);",
     ["several refusals are all reported"], SENTENCE_LOST),
    ("M23 lose which beats a part-done unlock already changed", API,
     "      err.changed = results.filter((r) => r.ok).map((r) => r.beat);",
     "      err.changed = [];",
     ["the beats that DID unlock are reported"], SENTENCE_LOST),
    ("M24 promote one beat's sentence to the headline when several failed", API,
     "        bad.length === 1 ? bad[0].data : {},",
     "        bad[0].data,",
     ["several refusals are all reported"], ""),
    # The next two carry no sentinel, for the same reason and on purpose. Under
    # each of them the refusal does not merely lose a sentence -- it stops being
    # reported as a refusal at all (M25 resolves; M26 drops the findings), so
    # the assertion that kills them is not the one the sentinel is attached to.
    # Requiring it here would demand that a test fail in a way it correctly
    # does not.
    ("M25 a body that would not parse is treated as a success", API,
     "        return { beat: b, ok: r.ok && d.ok === true, status: r.status, data: d };",
     "        return { beat: b, ok: d.ok !== false, status: r.status, data: d };",
     ["no readable body"], ""),
    ("M26 drop the per-beat route's `warnings` payload", API,
     "  if (Array.isArray(data.warnings) && data.warnings.length > 0) {",
     "  if (false) {",
     ["the undecided findings come back with the finding list"], ""),

    # --- the re-plan controls (second round) --------------------------------
    # The human's second sentence: "The re-plan button also should provide some
    # feedback that it is working, like at least greying out." M27 is that
    # defect restored -- the banner's button exactly as it was, with no state of
    # any kind.
    ("M27 the banner's Re-plan button as it was: no disabled, no label, no spinner", WS,
     ('          <button\n            data-testid="replan-stale"',
      '                  : "Re-plan Scene"}\n            </span>\n          </button>'),
     # The testid stays. It is a test hook, not behaviour, and the defect is the
     # absent state -- with the hook removed the tests fail on a missing element
     # before they can evaluate anything about what the control says, which is
     # the trap this harness exists to catch.
     '          <button\n            data-testid="replan-stale"\n'
     "            onClick={handleRedirectScene}\n"
     '            className="px-3 py-1 bg-amber-500/20 hover:bg-amber-500/30 border '
     'border-amber-500/40 text-amber-200 rounded text-[11px] font-bold transition-colors"\n'
     "          >\n            Re-plan Scene\n          </button>",
     # NOT the double click: `handleRedirectScene` already guarded that with
     # `if (redirecting) return`, and that guard is not what this round changed.
     # M35 is what stands for it.
     ["says a job is running", "on a locked plan it says what to do"],
     LOOKED_LIKE_NOTHING_HAPPENED),
    ("M28 REDIRECT SCENE keeps its label while the planner runs", WS,
     "            <span>\n              {redirecting\n"
     "                ? `RE-PLANNING (${sceneBeatsLabel})…`\n"
     "                : isLocked\n                  ? \"UNLOCK TO REDIRECT\"\n"
     "                  : \"REDIRECT SCENE\"}\n            </span>",
     "            <span>REDIRECT SCENE</span>",
     ["the label changes", "a multi-beat scene names every beat"],
     LOOKED_LIKE_NOTHING_HAPPENED),
    # M29/M30 target the workspace panel (a <div>, six-space indent). The
    # unplanned-beat view has its own running line (a <p>, eight spaces) and it
    # is a different surface with a different test -- M43. They were one anchor
    # for a while, which quietly moved M30 onto the other one and let it survive.
    ("M29 the running panel waits for a log, as the old one did", WS,
     '      {redirecting && (\n        <div\n          data-testid="redirect-running"',
     '      {redirecting && redirectLog && (\n        <div\n          data-testid="redirect-running"',
     ["before the server has logged anything"], LOOKED_LIKE_NOTHING_HAPPENED),
    ("M30 drop the running panel entirely", WS,
     '      {redirecting && (\n        <div\n          data-testid="redirect-running"',
     '      {false && (\n        <div\n          data-testid="redirect-running"',
     ["before the server has logged anything", "says a job is running"],
     LOOKED_LIKE_NOTHING_HAPPENED),
    ("M43 drop the running line from the unplanned-beat view", WS,
     '        {redirecting && (\n          <p\n            data-testid="redirect-running"',
     '        {false && (\n          <p\n            data-testid="redirect-running"',
     ["the first plan says it is running"], LOOKED_LIKE_NOTHING_HAPPENED),
    ("M31 the locked plan's button goes dead again, with no sentence", WS,
     '        {isLocked && !redirecting && (\n          <p\n            data-testid="redirect-locked-note"',
     '        {false && (\n          <p\n            data-testid="redirect-locked-note"',
     ["the button names the action, and the note names the button",
      "a compiled plan is locked for this purpose too"], ""),
    ("M32 the locked button reads REDIRECT SCENE while doing nothing", WS,
     '                : isLocked\n                  ? "UNLOCK TO REDIRECT"\n'
     '                  : "REDIRECT SCENE"}',
     '                : "REDIRECT SCENE"}',
     ["the button names the action, and the note names the button"], ""),
    ("M33 the banner's button re-enabled on a locked plan", WS,
     '            data-testid="replan-stale"\n            onClick={handleRedirectScene}\n'
     "            disabled={isLocked || redirecting}",
     '            data-testid="replan-stale"\n            onClick={handleRedirectScene}\n'
     "            disabled={redirecting}",
     ["on a locked plan it says what to do"], ""),
    # Both halves again, for the reason M11 gives: the handler's early return
    # and the disabled attribute each stop the second request on their own, so a
    # mutation of one alone is undetectable and would only prove the pair is
    # redundant.
    ("M35 no in-flight protection on the re-plan job either", WS,
     [("    if (redirecting) return;\n    setRedirecting(true);",
       "    setRedirecting(true);"),
      ('            data-testid="replan-stale"\n            onClick={handleRedirectScene}\n'
       "            disabled={isLocked || redirecting}",
       '            data-testid="replan-stale"\n            onClick={handleRedirectScene}\n'
       "            disabled={false}")],
     "",
     ["a second click cannot start a second planning job"], ""),
    # --- the outcome must outlive the box that asked for it (third round) ----
    # The banner renders under `isSnapshotStale`, so a successful re-plan
    # unmounts the control that was clicked. M36 puts the outcome back inside
    # it, which is the shape a well-meaning edit would actually take.
    ("M36 render the outcome inside the banner that a success unmounts", WS,
     [("      {redirectOutcome && (\n        <div\n"
       '          data-testid="redirect-outcome"',
       "      {redirectOutcome && isSnapshotStale && (\n        <div\n"
       '          data-testid="redirect-outcome"')],
     "",
     ["success is stated, even though the banner it was clicked in is gone"],
     LOOKED_LIKE_NOTHING_HAPPENED),
    ("M37 say nothing at all when the re-plan succeeds", WS,
     '        setRedirectOutcome({\n          kind: "done",',
     '        if (false) setRedirectOutcome({\n          kind: "done",',
     ["success is stated, even though the banner it was clicked in is gone",
      "the success line is the server's numbers"],
     LOOKED_LIKE_NOTHING_HAPPENED),
    ("M38 report the failure through `error`, blanking the workspace again", WS,
     "            setRedirectOutcome({\n              kind: \"failed\",\n"
     "              message:\n                done.status === \"timeout\"",
     "            setError(\n              done.status === \"timeout\"\n"
     "                ? `x` : `y`);\n            if (false) setRedirectOutcome({\n"
     "              kind: \"failed\",\n              message:\n"
     "                done.status === \"timeout\"",
     ["a failure keeps the plan on screen and does not call the beat unplanned",
      "a job still running at the timeout is unfinished"], ""),
    ("M39 the running panel goes back inside the vanishing banner", WS,
     '      {redirecting && (\n        <div\n          data-testid="redirect-running"',
     '      {redirecting && isSnapshotStale && (\n        <div\n          data-testid="redirect-running"',
     # Killed by the NON-stale case, not by the structural check beside it: this
     # mutation moves nothing in the DOM, it narrows the condition, so
     # `banner.contains(running)` is false either way. It survived until a test
     # existed for a re-plan on a plan that is not stale.
     ["a re-plan on a plan that is NOT stale still reports itself"],
     LOOKED_LIKE_NOTHING_HAPPENED),
    ("M42 tie the OUTCOME to the stale flag, so a revision reports nothing", WS,
     "      {redirectOutcome && (\n        <div\n"
     '          data-testid="redirect-outcome"\n          data-kind={redirectOutcome.kind}',
     "      {redirectOutcome && isSnapshotStale && (\n        <div\n"
     '          data-testid="redirect-outcome"\n          data-kind={redirectOutcome.kind}',
     ["a re-plan on a plan that is NOT stale still reports itself"],
     LOOKED_LIKE_NOTHING_HAPPENED),
    ("M40 claim a plan is standing after a first plan fails", WS,
     '    const planStands = coveragePlan\n      ? "The plan below is unchanged."',
     '    const planStands = true\n      ? "The plan below is unchanged."',
     ["does not claim a plan is standing"], ""),
    ("M41 the outcome follows the human to the next scene", WS,
     "    setLockProblem(null);\n    setRedirectOutcome(null);",
     "    setLockProblem(null);",
     ["the outcome does not follow the human to the next scene"], ""),
    ("M34 the unplanned-beat button repeats its route while planning", WS,
     "            {redirecting\n              ? `PLANNING (${sceneId})…`\n"
     "              : `POST /api/director/plan (${sceneId})`}",
     "            {`POST /api/director/plan (${sceneId})`}",
     ["says it is planning rather than repeating the route"],
     LOOKED_LIKE_NOTHING_HAPPENED),
]


def apply(src: str, old, new: str) -> tuple[str | None, str]:
    """The mutated source, or (None, why) if an anchor does not identify a span.

    `old` is a literal, a (start, end) pair meaning "everything between these
    inclusive", or a list of (old, new) pairs applied in order -- the last for a
    defence held up by two independent halves, where removing one alone changes
    nothing observable.
    """
    if isinstance(old, list):
        for i, (o, n) in enumerate(old):
            src, why = apply(src, o, n)
            if src is None:
                return None, f"edit {i + 1}: {why}"
        return src, ""
    if isinstance(old, tuple):
        start, end = old
        if src.count(start) != 1:
            return None, f"start anchor matched {src.count(start)} times"
        if src.count(end) != 1:
            return None, f"end anchor matched {src.count(end)} times"
        i = src.index(start)
        j = src.index(end, i)
        if j < i:
            return None, "end anchor precedes start anchor"
        return src[:i] + new + src[j + len(end):], ""
    if src.count(old) != 1:
        return None, f"anchor matched {src.count(old)} times"
    return src.replace(old, new), ""


def run_suite() -> tuple[int, str]:
    proc = subprocess.run(
        ["npx.cmd" if sys.platform == "win32" else "npx", "vitest", "run", *TARGETS],
        cwd=ROOT / "frontend", capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    # Optional ids on the command line re-run a subset: `… mutate.py M11 M16`.
    # A full run is ~25 vitest boots, so iterating on one mutation is worth the
    # argument -- but a subset run is not a result, and only the full one is
    # reportable.
    wanted = [a.upper() for a in sys.argv[1:]]
    problems: list[str] = []
    for name, path, old, new, expect_fail, expect_output in MUTATIONS:
        if wanted and name.split()[0].upper() not in wanted:
            continue
        src = path.read_text(encoding="utf-8")
        mutated, why = apply(src, old, new)
        if mutated is None:
            print(f"STALE    {name}: {why}")
            problems.append(f"{name}: stale anchor ({why})")
            continue
        try:
            path.write_text(mutated, encoding="utf-8")
            code, out = run_suite()
        finally:
            # Restore ONLY what this harness wrote. It edits real source files in
            # place, so anything that touches them while a run is in flight is
            # silently reverted by this line -- which is exactly what happened
            # once: a run left in the background reverted two hand edits made
            # during it, and left the last mutation applied when it was killed.
            # Neither loss announced itself.
            now = path.read_text(encoding="utf-8")
            if now == mutated:
                path.write_text(src, encoding="utf-8")
            else:
                print(f"REFUSED to restore {path.name}: it changed under the run. "
                      f"The mutation was NOT reverted -- check `git diff` before "
                      f"trusting this result.")
                problems.append(f"{name}: file changed under the run; not restored")

        totals = [l.strip() for l in out.splitlines() if l.strip().startswith("Tests ")]
        failed = [l.strip() for l in out.splitlines() if l.strip().startswith(("x ", "×"))]
        verdict = "KILLED  " if code != 0 else "SURVIVED"

        unmet = [w for w in expect_fail if not any(w in f for f in failed)]
        if code == 0:
            problems.append(f"{name}: survived")
        elif unmet:
            verdict = "MISFIRED"
            problems.append(f"{name}: killed, but not by {unmet}")
        elif expect_output and expect_output not in out:
            verdict = "MISFIRED"
            problems.append(
                f"{name}: killed without ever evaluating its own assertion "
                f"(sentinel absent from the output)")

        print(f"{verdict} {name}")
        for line in totals:
            print("         ", line)
        for line in failed[:8]:
            print("          -", line[:120])
        if expect_output:
            print(f"          reached its own assertion: "
                  f"{'yes' if expect_output in out else 'NO'}")

    print()
    if problems:
        print(f"{len(problems)} mutation(s) did not prove what they stand for:")
        for p in problems:
            print("  -", p)
        return 1
    ran = len(wanted) if wanted else len(MUTATIONS)
    print(f"All {ran} mutations killed, each by the assertion it stands for."
          + (" (SUBSET RUN — not a result)" if wanted else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

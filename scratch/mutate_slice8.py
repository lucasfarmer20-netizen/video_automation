"""Mutation harness for issues #7 and #8 — project identity on create and delete.

Guardrails require a test that fails under a faithful mutation of the change,
plus a mutation that genuinely reproduces the original defect. Each mutation
below is a list of edits applied together, the relevant suite run, then every
touched file restored — so the run reports both directions: mutated (must fail)
and restored (must pass).

Two things it inherits from `mutate_5b.py`, both of which were learned the hard
way there:

  * restoration snapshots EVERY path a mutation names, not just the first, and
    the tree is verified byte-identical at the end by hash. A green suite is not
    evidence the tree is clean;
  * a mutation that reorders or renames without changing behaviour "passes"
    meaninglessly. Each one here is annotated with the behaviour it removes.

What is new: these mutations span two suites. A frontend mutation is judged by
vitest and a backend one by pytest, because the frontend's fake server is a
model of the middleware, not the middleware — mutating `backend/main.py` cannot
fail a vitest test, and a harness that ran only vitest would report every
backend mutation as SURVIVED for the wrong reason.

    python scratch/mutate_slice8.py
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
PAGE = FRONTEND / "src" / "app" / "page.tsx"
MAIN = ROOT / "backend" / "main.py"


@dataclass
class Mutation:
    name: str
    clause: str
    suite: str                      # "frontend" | "python"
    edits: list[tuple[Path, str, str]]


# --- issue #7: deleting the ACTIVE project --------------------------------------

DELETE_CLEARS = (
    "      if (res.was_active) {\n"
    "        projectIdRef.current = \"\";\n"
    "        setActiveProjectId(\"\");\n"
    "      }"
)
DELETE_RECOVERS = (
    "      await fetchProjects();\n"
    "      const installed = await fetchActiveProject();\n"
    "      if (!installed && res.was_active) {"
)
# Removing the loading screen means swapping `whileLoading(fn)` for an immediately
# INVOKED async function — `await (async () => {...})` merely awaits the function
# object, so the body never runs and the mutation would delete the whole recovery
# rather than just the screen. It "killed" four tests that way, which is a
# mutation hiding the thing it was meant to isolate, not evidence.
DELETE_HIDES_WORKSPACE_OPEN = (
    "    await whileLoading(async () => {\n      if (res.was_active) {"
)
DELETE_HIDES_WORKSPACE_CLOSE = (
    "              + \"switched to. Reload to pick it up.\");\n"
    "      }\n"
    "    });"
)

# --- issue #8: creating a project -----------------------------------------------

CREATE_RETARGETS = (
    "      projectIdRef.current = data.project_id || \"\";\n"
    "      setActiveProjectId(data.project_id || \"\");\n"
    "      opened = await fetchActiveProject();"
)
# Anchored on the alert text immediately above it, because the rollback block
# ITSELF is byte-identical in handleSelectProject — same `if (!opened)`, same two
# lines, same `whileLoading` tail. The first version of this mutation anchored on
# the block alone, so it silently mutated the SWITCH handler and reported five of
# slice 5b's tests failing as though they were evidence about create. `apply()`
# now refuses an ambiguous anchor outright, which is what caught it.
CREATE_ROLLS_BACK = (
    "pick the new one up.\");\n"
    "    } finally {\n"
    "      if (!opened) {"
)


MUTATIONS = [
    # ---- reproductions of the original defects ---------------------------------
    Mutation(
        # The mechanism of #7 exactly: the ref goes on naming a project that is
        # now in _trash, which _scan_projects skips, so bind_project_context
        # answers 404 to this load and every request after it.
        "ORIGINAL DEFECT #7: the deleted project's id is never dropped", "§11.3",
        "frontend",
        [(PAGE, DELETE_CLEARS, DELETE_CLEARS.replace("if (res.was_active)", "if (false)"))],
    ),
    Mutation(
        # handleDeleteProject as it stands on main: identity untouched AND the
        # reloads fired and forgotten, so the 404 never reaches the screen
        # either. This is the whole original handler, not just its mechanism.
        "ORIGINAL DEFECT #7 (whole handler): no retarget, no check, no recovery",
        "§11.3", "frontend",
        [(PAGE, DELETE_CLEARS, DELETE_CLEARS.replace("if (res.was_active)", "if (false)")),
         (PAGE, DELETE_RECOVERS,
          "      fetchProjects();\n"
          "      const installed = await fetchActiveProject();\n"
          "      if (false && !installed && res.was_active) {")],
    ),
    Mutation(
        # #8 exactly: the load goes out with the PREVIOUS X-Project-Id still set,
        # the middleware honours the header over the pointer, and the server
        # answers about the previous film — so data.project_id comes back as the
        # previous film and the ref never moves.
        "ORIGINAL DEFECT #8: the load still carries the previous project's id",
        "§11.3", "frontend",
        [(PAGE, CREATE_RETARGETS, "      opened = await fetchActiveProject();")],
    ),
    Mutation(
        # The subtler shape of the same defect, and the reason the retarget is
        # deliberately BEFORE the load: retargeting after it means the load is
        # answered about the old film while identity moves to the new one —
        # the previous film on screen, the new one in every header.
        "ORIGINAL DEFECT #8 (reordered): identity moves after the load, not before",
        "§11.3", "frontend",
        [(PAGE, CREATE_RETARGETS,
          "      opened = await fetchActiveProject();\n"
          "      projectIdRef.current = data.project_id || \"\";\n"
          "      setActiveProjectId(data.project_id || \"\");")],
    ),
    Mutation(
        # The id is taken from somewhere that is not the server's answer.
        "ORIGINAL DEFECT #8 (variant): the create keeps naming the previous film",
        "§11.3", "frontend",
        [(PAGE, CREATE_RETARGETS,
          "      projectIdRef.current = previousId;\n"
          "      setActiveProjectId(previousId);\n"
          "      opened = await fetchActiveProject();")],
    ),

    # ---- faithful mutations of the fix -----------------------------------------
    Mutation(
        # Half the identity moved. The page ref is cleared but directorApi keeps
        # its own copy, so director calls go on naming the deleted film and its
        # staleness check judges every reply against a project in _trash.
        "delete clears the page ref but not the director module's copy", "§11.3",
        "frontend",
        [(PAGE, DELETE_CLEARS, DELETE_CLEARS.replace("      setActiveProjectId(\"\");\n", ""))],
    ),
    Mutation(
        # The gate removed: deleting ANY project drops the studio's identity, so
        # the next request is answered by the active pointer — which is free to
        # name a film the user is not looking at. The fix causing the defect.
        "delete drops identity even when the deleted project was not active",
        "§11.3", "frontend",
        [(PAGE, DELETE_CLEARS, DELETE_CLEARS.replace("if (res.was_active)", "if (true)"))],
    ),
    Mutation(
        # The failure path substitutes instead of stating the block: the film in
        # _trash stays on screen, inviting an edit on a project that is gone.
        "a delete whose reload failed keeps showing the deleted film", "§11.4",
        "frontend",
        [(PAGE, "      setActiveProject(null);\n", "")],
    ),
    Mutation(
        # The workspace stays on screen while identity is dropped and the
        # replacement is still in flight — so a control clicked in that window
        # writes to whichever film the server's pointer picked, under a screen
        # still showing the deleted one.
        "the workspace stays clickable while the studio is between projects",
        "§11.4", "frontend",
        [(PAGE, DELETE_HIDES_WORKSPACE_OPEN,
          "    await (async () => {\n      if (res.was_active) {"),
         (PAGE, DELETE_HIDES_WORKSPACE_CLOSE,
          DELETE_HIDES_WORKSPACE_CLOSE.replace("    });", "    })();"))],
    ),
    Mutation(
        # Identity is committed before the load confirms it, so the path where
        # the load fails must put it back. Without this the studio shows the
        # previous film while every request names the new one.
        "a create whose load failed never puts the identity back", "§11.4",
        "frontend",
        [(PAGE, CREATE_ROLLS_BACK, CREATE_ROLLS_BACK.replace("if (!opened)", "if (false)"))],
    ),

    # ---- backend: the additive field and the flag the client keys on -----------
    Mutation(
        # The field this slice added. Removed, the studio falls back to a
        # header-less read — which still works, but the request names no project
        # for one round trip, which is the window the field exists to close.
        "the create response stops reporting the id it created", "§11.3",
        "python",
        [(MAIN, "            \"project_id\": f_id,\n", "")],
    ),
    Mutation(
        # A unique id is not the same as the id the middleware binds. This one
        # looks right in the response and 404s the moment a client names it.
        "the create reports an id the middleware will not resolve", "§11.3",
        "python",
        [(MAIN, "            \"project_id\": f_id,", "            \"project_id\": f_id + \"-new\",")],
    ),
    Mutation(
        # `was_active` is the only signal the client gets that the pointer moved.
        # Always-false leaves the studio naming a project in _trash — issue #7,
        # reached from the server side.
        "the delete never admits it repointed the active project", "§11.3",
        "python",
        [(MAIN, "        was_active = config.MANIFEST_PATH.resolve() == p",
                "        was_active = False")],
    ),
    Mutation(
        # Always-true makes every delete look like it moved the studio, so
        # deleting a bystander drops a perfectly good identity.
        "the delete claims it repointed when it did not", "§11.3",
        "python",
        [(MAIN, "        was_active = config.MANIFEST_PATH.resolve() == p",
                "        was_active = True")],
    ),
]


def run_frontend() -> tuple[bool, str]:
    proc = subprocess.run(
        ["npm", "test", "--silent"], cwd=FRONTEND, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        shell=(sys.platform == "win32"),
    )
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def run_python() -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


SUITES = {"frontend": run_frontend, "python": run_python}


def failing_tests(out: str) -> list[str]:
    named = {
        line.strip().lstrip("×").strip()
        for line in out.splitlines() if line.strip().startswith("×")
    }
    named |= {
        line.split(" ", 1)[1].strip()
        for line in out.splitlines() if line.startswith("FAILED ")
    }
    return sorted(named)[:6]


def apply(path: Path, find: str, replace: str) -> None:
    """Apply one edit, refusing anything but an exactly-one-site anchor.

    The ambiguity check is not defensive tidiness. `str.replace(find, x, 1)`
    silently takes the FIRST match, so an anchor that also appears in a sibling
    handler mutates the sibling — and the run then reports OTHER tests failing
    with total confidence, as evidence about code it never touched. That is
    worse than a mutation that survives, because a survivor is at least visible
    as a gap. Ambiguity is a harness bug and it stops the run.
    """
    text = path.read_text(encoding="utf-8")
    hits = text.count(find)
    if hits == 0:
        raise SystemExit(f"anchor not found in {path.name}:\n{find}")
    if hits > 1:
        raise SystemExit(
            f"anchor matches {hits} sites in {path.name} — it would mutate the "
            f"first, which is not necessarily the one meant. Narrow it:\n{find}"
        )
    path.write_text(text.replace(find, replace, 1), encoding="utf-8")


def digest(paths) -> dict:
    return {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def main() -> int:
    touched = sorted({p for m in MUTATIONS for p, _, _ in m.edits})
    pristine = {p: p.read_text(encoding="utf-8") for p in touched}
    before = digest(touched)

    for suite in ("python", "frontend"):
        ok, out = run_suite_named(suite)
        print(f"baseline {suite}: {'PASS' if ok else 'FAIL'}")
        if not ok:
            print(out[-4000:])
            return 1

    survived = []
    for mut in MUTATIONS:
        snapshot = {p: p.read_text(encoding="utf-8") for p, _, _ in mut.edits}
        try:
            for path, find, replace in mut.edits:
                apply(path, find, replace)
            ok, out = run_suite_named(mut.suite)
        finally:
            for path, text in snapshot.items():
                path.write_text(text, encoding="utf-8")

        if ok:
            survived.append(mut.name)
        print(f"\n[{mut.suite}] {mut.clause} {mut.name}: {'SURVIVED' if ok else 'killed'}")
        for t in failing_tests(out):
            print(f"    x {t}")

    after = digest(touched)
    dirty = [p.name for p in touched if before[p] != after[p]]
    for p in touched:
        if before[p] != after[p]:
            p.write_text(pristine[p], encoding="utf-8")

    ok_all = True
    for suite in ("python", "frontend"):
        ok, _ = run_suite_named(suite)
        ok_all = ok_all and ok
        print(f"restored {suite}: {'PASS' if ok else 'FAIL'}")
    if dirty:
        print(f"  TREE NOT RESTORED — still modified: {', '.join(dirty)}")
        print("  git checkout -- <file> before trusting any result above.")
    if survived:
        print("\nmutations that survived:")
        for name in survived:
            print(f"  - {name}")
    return 1 if survived or dirty or not ok_all else 0


def run_suite_named(suite: str) -> tuple[bool, str]:
    return SUITES[suite]()


if __name__ == "__main__":
    raise SystemExit(main())

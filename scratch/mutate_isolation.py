"""Mutation harness for the three irreversible outcomes promoted into scope.

    python scratch/mutate_isolation.py
    python scratch/mutate_isolation.py --discover   # probes only, no pytest

The slice 8 retrofit was scoped to safeguards whose failure costs money or
admits unapproved work, with one stated exception: **anything guarding an
irreversible outcome that is neither — data loss, an overwrite, a destroyed
record — is promoted rather than deferred.** The inventory
(`docs/audits/slice8_deferred_safeguard_inventory.md`) turned up three, and a
review pass turned up a fourth that had been deferred for a reason about the
round rather than about the safeguard. This is all four.

Money can be spent again. None of these can be undone by retrying, because in
each case the thing that would have been used to recover is what was destroyed:

  * **P1 — `save_current_project` writes to the BOUND context.** Audit Pass F.
    Working on project B overwrote project A's *whole manifest* — every
    approval, every take selection, every timing — and reported success.
    ``manifest.save``'s default was already bound-aware; its main caller passed
    the active path explicitly and overrode it, so hardening the primitive
    achieved nothing. Both halves are mutated here, plus the id stamp that keeps
    the Firestore document and the JSON file describing the same film.
  * **P2 — an unknown ``X-Project-Id`` is REFUSED.** The read case is a display
    defect. The write case is P1 by another route: a client naming a project the
    server cannot resolve, answered about whichever film happens to be active,
    writes into it.
  * **P3 — ``_move_tree`` keeps the source when the copy is short.** Project
    delete on the gcsfuse mount is copy-then-delete, because directory rename is
    unsupported there. A half-finished copy that reported success and then ran
    ``rmtree`` destroys a film. The guard is a file count and a ``RuntimeError``.
  * **P4 — ``/api/project/delete`` refuses a path outside the workspace.** The
    handler takes a caller-supplied path and ``purge: true`` is a real
    ``shutil.rmtree``, not a move to ``_trash``. The containment check is the
    only guard between the two, and it had neither a mutation nor a test.

P2's mutation is the one worth reading twice. Refusing GETs and falling back on
everything else leaves every read assertion in the suite passing, because they
are all reads -- and ``/api/approve`` sets ``approved`` on every beat and
``storyboard_approved`` on the episode, so a POST naming a project the server
cannot resolve does not merely write into the active film. It clears Gate 1 on
one nobody named.

All three are reachable on Cloud Run and none of them is exotic: P1 fires
whenever two projects are open in one browser session, P2 whenever a switch
races a request, and P3 on every project delete, because the GCS mount is the
normal deployed filesystem.

Same obligations as the rest of these harnesses. Every mutation is killed or
reported, every mutation declares a probe signature that must appear under it
and be absent pristine, and the signatures are about the destroyed bytes — "was
project A's manifest still project A's afterwards" — not about status codes.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "backend" / "main.py"
MANIFEST = ROOT / "backend" / "manifest.py"


# --------------------------------------------------------------------------- #
# probes
# --------------------------------------------------------------------------- #

_PRELUDE = '''
import json, sys, tempfile, types
from pathlib import Path
sys.path.insert(0, r"__ROOT__")
for _m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

from backend import config, manifest, projects
from backend.manifest import Shot, Storyboard

tmp = Path(tempfile.mkdtemp())


def project(name, title):
    """A real project directory with a recognisable storyboard in it."""
    d = tmp / "bestiary" / name
    d.mkdir(parents=True, exist_ok=True)
    mf = d / "storyboard_manifest.json"
    manifest.save(Storyboard(title=title, channel="bestiary",
                             shots=[Shot(scene_id="s001", narration=title)]), mf)
    return projects.ProjectContext.from_manifest(mf), mf
'''

# P1. Two films open; a request bound to BRAVO saves. The question is not
# whether bravo got its save -- it is whether alpha is still alpha afterwards.
PROBE_OVERWRITE = _PRELUDE + '''
import backend.main as M

a, a_mf = project("alpha", "ALPHA")
b, b_mf = project("bravo", "BRAVO")

# ALPHA is the globally active project: the pointer file names it, and
# config.MANIFEST_PATH is where a caller that ignores the binding would write.
config.MANIFEST_PATH = a_mf
M.get_active_manifest_path = lambda: str(a_mf)

before = a_mf.read_bytes()

sb = manifest.load(b_mf)
sb.title = "BRAVO-EDITED"
# A deliberately WRONG id on the object in hand. Without this the stamp has
# nothing to correct: manifest.load() derives an id from the path it read, which
# is already bravo's, so removing the stamp changes no byte and the mutation
# reads as a no-op. A storyboard reconstructed from a Firestore document, or
# built fresh by a handler, is exactly this shape.
sb.id = "an-id-from-some-other-film"
with projects.use(b):
    try:
        M.save_current_project(sb)
        outcome = "returned"
    except Exception as exc:
        outcome = "raised:" + type(exc).__name__

after_a = json.loads(a_mf.read_text(encoding="utf-8"))
after_b = json.loads(b_mf.read_text(encoding="utf-8"))

print("PROBE_OVERWRITE_SAVE=" + outcome)
print("PROBE_OVERWRITE_BRAVO_TITLE=" + str(after_b.get("title")))
print("PROBE_OVERWRITE_ALPHA_TITLE=" + str(after_a.get("title")))
# The whole finding in one line: another film's manifest was replaced.
print("PROBE_OVERWRITE_ALPHA_DESTROYED="
      + ("true" if a_mf.read_bytes() != before else "false"))
print("PROBE_OVERWRITE_STAMPED_ID=" + str(after_b.get("id") == b.project_id))
'''

# P1c, one layer down: manifest.save's own default, with no caller involved.
PROBE_DEFAULT = _PRELUDE + '''
a, a_mf = project("alpha", "ALPHA")
b, b_mf = project("bravo", "BRAVO")
config.MANIFEST_PATH = a_mf

before = a_mf.read_bytes()
with projects.use(b):
    manifest.save(Storyboard(title="WRITTEN-UNDER-BRAVO", channel="bestiary"))

print("PROBE_DEFAULT_ALPHA_TITLE="
      + str(json.loads(a_mf.read_text(encoding="utf-8")).get("title")))
print("PROBE_DEFAULT_BRAVO_TITLE="
      + str(json.loads(b_mf.read_text(encoding="utf-8")).get("title")))
print("PROBE_DEFAULT_ALPHA_DESTROYED="
      + ("true" if a_mf.read_bytes() != before else "false"))
'''

# P2. A client names a project the server cannot resolve.
PROBE_UNKNOWN = _PRELUDE + '''
from fastapi.testclient import TestClient
import backend.main as M

a, a_mf = project("alpha", "ALPHA")
config.MANIFEST_PATH = a_mf
M.get_active_manifest_path = lambda: str(a_mf)
M.WORKSPACE_ROOT = tmp

client = TestClient(M.app, raise_server_exceptions=False)
r = client.get("/api/project/active",
               headers={"X-Project-Id": "a-project-that-does-not-exist"})

print("PROBE_UNKNOWN_STATUS=%d" % r.status_code)
print("PROBE_UNKNOWN_SERVED_ACTIVE="
      + ("true" if "ALPHA" in r.text else "false"))
print("PROBE_UNKNOWN_ECHOED_ID="
      + str(r.headers.get("X-Project-Id", "'absent'")))
# The control: naming nothing at all must still work, or the mutation would
# look like a fix for a problem it created.
plain = client.get("/api/project/active")
print("PROBE_UNKNOWN_PLAIN_STATUS=%d" % plain.status_code)
'''

# P3. A project move whose copy silently drops a file.
PROBE_MOVE = _PRELUDE + '''
import shutil as _sh
import backend.main as M

src = tmp / "bestiary" / "Doomed"
(src / "assets" / "s001").mkdir(parents=True)
(src / "render").mkdir(parents=True)
(src / "storyboard_manifest.json").write_text('{"title": "Doomed"}', encoding="utf-8")
(src / "assets" / "s001" / "v0.png").write_bytes(b"an approved still")
(src / "render" / "s001.mp4").write_bytes(b"a clip that was paid for")

real_copyfile = _sh.copyfile

def flaky(a, b, *args, **kw):
    # The mount accepted the call and wrote nothing. This is the shape the
    # guard exists for: no exception, no bytes.
    if str(a).endswith("s001.mp4"):
        return b
    return real_copyfile(a, b, *args, **kw)

M.shutil.copyfile = flaky
dst = tmp / "_trash" / "Doomed"
# Counted from the tree, not written down. The literal 3 that used to be on the
# PROBE_MOVE_FILES_DESTROYED line was the size of the fixture thirty lines above
# with nothing tying the two together: adding a file to the fixture would have
# made a correct mutation report MISSING, and the run would have failed saying
# the defect was not demonstrated when the real fault was arithmetic here. This
# is the same rglob count _move_tree itself uses for `expected`.
had = sum(1 for f in src.rglob("*") if f.is_file())
try:
    M._move_tree(src, dst)
    outcome = "returned"
except Exception as exc:
    outcome = "raised:" + type(exc).__name__
M.shutil.copyfile = real_copyfile

survived = src.exists()
clip = src / "render" / "s001.mp4"
print("PROBE_MOVE_OUTCOME=" + outcome)
print("PROBE_MOVE_SOURCE_SURVIVED=" + ("true" if survived else "false"))
print("PROBE_MOVE_ORIGINAL_CLIP_STILL_THERE="
      + ("true" if clip.is_file() else "false"))
landed = sorted(p.name for p in dst.rglob("*") if p.is_file()) if dst.exists() else []
print("PROBE_MOVE_LANDED=" + json.dumps(landed))
# The bytes that exist nowhere any more. Zero is the only acceptable number.
print("PROBE_MOVE_FILES_IN_SOURCE=%d" % had)
lost = 0 if survived else max(0, had - len(landed))
print("PROBE_MOVE_FILES_DESTROYED=%d" % lost)
'''

# P2's WRITE case, which is the one it was promoted for. PROBE_UNKNOWN above
# sends a GET; refusing GETs and falling back on everything else preserves every
# read assertion while letting a POST that named an unresolvable project land in
# whichever film happens to be active. /api/approve is the sharpest version of
# that: it sets approved on every beat and storyboard_approved on the episode, so
# the fallback does not merely write -- it CLEARS GATE 1 on a film nobody named.
PROBE_WRITE_UNKNOWN = _PRELUDE + '''
from fastapi.testclient import TestClient
import backend.main as M

a, a_mf = project("alpha", "ALPHA")
config.MANIFEST_PATH = a_mf
M.get_active_manifest_path = lambda: str(a_mf)
M.WORKSPACE_ROOT = tmp

# A beat carrying a chosen image, so /api/approve would really succeed if the
# request were allowed through. Without it the endpoint refuses on "no beats to
# approve" and the probe would read clean under the mutation for the wrong
# reason.
sb = manifest.load(a_mf)
sb.shots[0].draft_image = "assets/s001/v0.png"
sb.shots[0].motion_type = "ai_video"
sb.shots[0].video_model = "seedance_2_0"
manifest.save(sb, a_mf)
before = a_mf.read_bytes()

client = TestClient(M.app, raise_server_exceptions=False)
r = client.post("/api/approve",
                headers={"X-Project-Id": "a-project-that-does-not-exist"})

after = json.loads(a_mf.read_text(encoding="utf-8"))
print("PROBE_WRITE_UNKNOWN_STATUS=%d" % r.status_code)
print("PROBE_WRITE_UNKNOWN_ACTIVE_CHANGED="
      + ("true" if a_mf.read_bytes() != before else "false"))
print("PROBE_WRITE_UNKNOWN_ACTIVE_APPROVED="
      + str(bool(after.get("storyboard_approved"))))
print("PROBE_WRITE_UNKNOWN_BEAT_APPROVED="
      + str(bool((after.get("shots") or [{}])[0].get("approved"))))

# The control, run last because it really does approve alpha: a write that names
# nothing must still work, or a mutation that refused every POST would look like
# a fix.
plain = client.post("/api/approve")
print("PROBE_WRITE_NAMED_NOTHING_STATUS=%d" % plain.status_code)
'''

# P4. The containment check on /api/project/delete: the only guard between a
# caller-supplied path and rmtree, and `purge: true` is a real rmtree.
PROBE_CONTAINMENT = _PRELUDE + '''
import tempfile as _tf
from fastapi.testclient import TestClient
import backend.main as M

a, a_mf = project("alpha", "ALPHA")
config.MANIFEST_PATH = a_mf
M.get_active_manifest_path = lambda: str(a_mf)
# The workspace is `tmp`; the decoy below is deliberately somewhere else.
M.WORKSPACE_ROOT = tmp

outside = Path(_tf.mkdtemp()) / "NotOurs"
outside.mkdir(parents=True)
(outside / "storyboard_manifest.json").write_text(
    '{"title": "NotOurs"}', encoding="utf-8")
(outside / "keepme.txt").write_text("bytes that exist nowhere else",
                                    encoding="utf-8")

client = TestClient(M.app, raise_server_exceptions=False)
# The typed confirmation is supplied CORRECTLY, on purpose. Every later guard in
# the handler -- protected directories, nested projects, the typed name -- would
# also refuse this request, so a probe that let one of those answer first would
# read clean with containment removed. This gets past all of them and is stopped
# by containment or not at all.
r = client.post("/api/project/delete", json={
    "rel": str(outside / "storyboard_manifest.json"),
    "confirm": "notours", "purge": True})

print("PROBE_CONTAIN_STATUS=%d" % r.status_code)
print("PROBE_CONTAIN_REFUSED_FOR_CONTAINMENT="
      + ("true" if "outside the workspace" in r.text else "false"))
print("PROBE_CONTAIN_OUTSIDE_SURVIVED="
      + ("true" if outside.is_dir() else "false"))
print("PROBE_CONTAIN_FILE_SURVIVED="
      + ("true" if (outside / "keepme.txt").is_file() else "false"))
'''

PROBES = {
    "overwrite": PROBE_OVERWRITE,
    "default": PROBE_DEFAULT,
    "unknown": PROBE_UNKNOWN,
    "write_unknown": PROBE_WRITE_UNKNOWN,
    "containment": PROBE_CONTAINMENT,
    "move": PROBE_MOVE,
}


@dataclass
class Mutation:
    name: str
    clause: str
    removes: str
    edits: list[tuple[Path, str, str]]
    probes: list[tuple[str, str]] = field(default_factory=list)
    defect: bool = False
    expect: list[str] = field(default_factory=list)
    # The reachability obligation. `expect` proves the right TEST died; these
    # prove the right ASSERTION inside it ran. A test that dies on
    # `pytest.raises(...)` not raising has demonstrated nothing about the bytes
    # it was protecting -- test_move_tree_keeps_the_source_when_the_copy_is_short
    # was exactly that shape until this harness was written against it.
    #
    # `proves_note` is required when `proves` is empty, and main() refuses to run
    # without one, so "the probe carries this" is a recorded decision.
    proves: list[str] = field(default_factory=list)
    not_proves: list[str] = field(default_factory=list)
    proves_note: str = ""


# --- anchors ------------------------------------------------------------------

SAVE_NO_EXPLICIT_PATH = (
    "    # Save back to local/GCS JSON for CLI & local sync. No explicit path: the\n"
    "    # default already resolves the bound project, and naming one here is exactly\n"
    "    # how this went wrong before.\n"
    "    manifest.save(sb)"
)

SAVE_STAMPS_ID = (
    "    ctx = projects.bound()\n"
    "    if ctx is not None:\n"
    "        # Keep the document id and the file in agreement with the bound project.\n"
    "        sb.id = ctx.project_id"
)

# `path = Path(path) if path is not None else config.manifest_path()` appears in
# BOTH load() and save(), so the anchor carries save()'s next line with it.
MANIFEST_SAVE_DEFAULT = (
    "    path = Path(path) if path is not None else config.manifest_path()\n"
    "    path.parent.mkdir(parents=True, exist_ok=True)"
)

MIDDLEWARE_REFUSES = (
    "    try:\n"
    "        ctx = _context_for(requested)\n"
    "    except projects.UnknownProject as exc:\n"
    "        return JSONResponse(status_code=404,\n"
    '                            content={"ok": False, "error": str(exc),\n'
    '                                     "project_id": requested})'
)

DELETE_CONTAINMENT = (
    "        root = next((r for r in roots if r in p.parents), None)\n"
    "        if root is None:\n"
    '            raise HTTPException(status_code=400, detail="Project path is outside the workspace")'
)

MOVE_REFUSES = (
    "    if landed < expected:\n"
    "        raise RuntimeError(\n"
    '            f"refusing to delete {src.name}: copied {landed} of {expected} file(s) "\n'
    '            f"to {dst}. The source is untouched."\n'
    "        )"
)

MOVE_COUNTS_ARRIVALS = (
    '    landed = sum(1 for f in dst.rglob("*") if f.is_file())'
)


MUTATIONS = [
    # ---- P1: the whole-manifest overwrite ---------------------------------------
    Mutation(
        "DEFECT Pass F: the save names the active project's path explicitly",
        "§11.3",
        "the exact pre-fix line — save_current_project passes "
        "get_active_manifest_path(), stepping over manifest.save's bound-aware "
        "default, so a request working on B writes B's storyboard over A's "
        "manifest and answers 200. Every approval, take selection and timing in "
        "A, replaced, with nothing anywhere holding the old bytes",
        [(MAIN, SAVE_NO_EXPLICIT_PATH,
          "    manifest.save(sb, get_active_manifest_path())  # MUTANT")],
        probes=[("overwrite", "PROBE_OVERWRITE_ALPHA_DESTROYED=true"),
                ("overwrite", "PROBE_OVERWRITE_ALPHA_TITLE=BRAVO-EDITED")],
        defect=True,
        expect=["test_save_current_project_writes_to_the_bound_project"],
        proves=["bound B overwrote active A"],
    ),
    Mutation(
        "manifest.save's default follows the process, not the binding", "§11.3",
        "the primitive's own half — the same overwrite arriving through every "
        "caller that trusts the default, which after the fix above is all of them",
        [(MANIFEST, MANIFEST_SAVE_DEFAULT,
          "    path = Path(path) if path is not None else config.MANIFEST_PATH  # MUTANT\n"
          "    path = Path(path)\n"
          "    path.parent.mkdir(parents=True, exist_ok=True)")],
        probes=[("default", "PROBE_DEFAULT_ALPHA_DESTROYED=true"),
                ("default", "PROBE_DEFAULT_ALPHA_TITLE=WRITTEN-UNDER-BRAVO")],
        expect=["test_manifest_save_defaults_to_the_bound_project"],
        proves=['assert manifest.load(a.manifest_path).title == "belongs-to-a"'],
    ),
    Mutation(
        "the saved document is not stamped with the project it belongs to",
        "§11.3",
        "the id that keeps the Firestore document and the JSON file describing "
        "the same film — without it a save can file one project's storyboard "
        "under another's document id, which is the same overwrite in the durable "
        "store rather than on disk",
        [(MAIN, SAVE_STAMPS_ID, "    ctx = projects.bound()  # MUTANT: id not stamped")],
        probes=[("overwrite", "PROBE_OVERWRITE_STAMPED_ID=False")],
        expect=["test_save_current_project_stamps_the_bound_project_id"],
        proves=['assert seen["id"] == b.project_id'],
    ),

    # ---- P2: an unknown project is refused, not substituted ----------------------
    Mutation(
        "an unresolvable project id is served the active project instead",
        "§11.3",
        "the refusal — a client naming a project the server cannot resolve is "
        "quietly answered about whichever film is active, under the id it asked "
        "for. Reads mislead; writes are P1 by another route",
        [(MAIN, MIDDLEWARE_REFUSES,
          "    try:  # MUTANT\n"
          "        ctx = _context_for(requested)\n"
          "    except projects.UnknownProject:\n"
          '        ctx = _context_for("")')],
        probes=[("unknown", "PROBE_UNKNOWN_STATUS=200"),
                ("unknown", "PROBE_UNKNOWN_SERVED_ACTIVE=true")],
        defect=True,
        expect=["test_a_request_naming_an_unknown_project_is_refused",
                "test_an_unknown_project_is_never_silently_served_the_active_one"],
        proves=["assert r.headers.get(\"X-Project-Id\") != a.project_id"],
    ),

    Mutation(
        "only GETs are refused; a write for an unknown project falls back",
        "§11.3",
        "the half of the refusal that P2 exists for. Reads that name an "
        "unresolvable project are still refused, so every read assertion in the "
        "suite still passes -- and a POST is answered about whichever film is "
        "active. /api/approve makes that concrete: it sets approved on every "
        "beat and storyboard_approved on the episode, so a request naming a "
        "project that does not exist clears Gate 1 on one that does",
        [(MAIN, MIDDLEWARE_REFUSES,
          "    try:  # MUTANT: only GETs are refused\n"
          "        ctx = _context_for(requested)\n"
          "    except projects.UnknownProject as exc:\n"
          '        if request.method == "GET":\n'
          "            return JSONResponse(status_code=404,\n"
          '                                content={"ok": False, "error": str(exc),\n'
          '                                         "project_id": requested})\n'
          '        ctx = _context_for("")')],
        probes=[("write_unknown", "PROBE_WRITE_UNKNOWN_STATUS=200"),
                ("write_unknown", "PROBE_WRITE_UNKNOWN_ACTIVE_CHANGED=true"),
                ("write_unknown", "PROBE_WRITE_UNKNOWN_ACTIVE_APPROVED=True")],
        defect=True,
        expect=["test_a_write_naming_an_unknown_project_never_touches_the_active_one"],
        proves=["a write naming an unknown project changed the active project"],
    ),

    # ---- P4: a delete that escapes the workspace ---------------------------------
    Mutation(
        "a project path outside the workspace is deleted anyway", "§11.3",
        "the containment check on /api/project/delete — the only guard between a "
        "caller-supplied path and `shutil.rmtree`, because `purge: true` is a "
        "real unlink rather than a move to _trash. Promoted on its own terms: "
        "the bytes are not in Firestore, not in _trash and not anywhere else",
        [(MAIN, DELETE_CONTAINMENT,
          "        root = next((r for r in roots if r in p.parents), roots[0])  # MUTANT")],
        probes=[("containment", "PROBE_CONTAIN_OUTSIDE_SURVIVED=false"),
                ("containment", "PROBE_CONTAIN_FILE_SURVIVED=false"),
                ("containment", "PROBE_CONTAIN_STATUS=200")],
        defect=True,
        expect=["test_a_delete_outside_the_workspace_is_refused"],
        proves=["a path outside the workspace was deleted"],
    ),

    # ---- P3: a project destroyed by a short copy ---------------------------------
    Mutation(
        "a half-finished move deletes the original anyway", "§11.3",
        "the refusal that stands between a silent short copy and rmtree — the "
        "failure being replaced is precisely one where an incomplete move "
        "reported success, and on the gcsfuse mount a copyfile that writes "
        "nothing raises nothing",
        [(MAIN, MOVE_REFUSES,
          "    if False:  # MUTANT: a short copy is not refused\n"
          '        raise RuntimeError("unreachable")')],
        probes=[("move", "PROBE_MOVE_SOURCE_SURVIVED=false"),
                ("move", "PROBE_MOVE_OUTCOME=returned"),
                ("move", "PROBE_MOVE_FILES_DESTROYED=1")],
        defect=True,
        expect=["test_move_tree_keeps_the_source_when_the_copy_is_short"],
        proves=["a short copy deleted the original"],
    ),
    Mutation(
        "the move counts the copies it attempted, not the files that arrived",
        "§11.3",
        "the difference between calling copyfile and the bytes being there — a "
        "mount that accepts the call and writes nothing satisfies a counter of "
        "attempts perfectly, which is the whole shape of this failure",
        [(MAIN, MOVE_COUNTS_ARRIVALS,
          "    landed = copied  # MUTANT: attempts, not arrivals")],
        probes=[("move", "PROBE_MOVE_SOURCE_SURVIVED=false"),
                ("move", "PROBE_MOVE_FILES_DESTROYED=1")],
        expect=["test_move_tree_keeps_the_source_when_the_copy_is_short"],
        proves=["a short copy deleted the original"],
    ),
]


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #

_ENV = {k: v for k, v in os.environ.items()
        if k not in ("FAL_KEY", "ELEVENLABS_API_KEY", "ANTHROPIC_API_KEY")}
_ENV.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})


def run_suite() -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q"], cwd=ROOT, env=_ENV,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def run_probe(name: str) -> tuple[bool, str]:
    script = PROBES[name].replace("__ROOT__", str(ROOT))
    proc = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, env=_ENV,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def probe_lines(out: str) -> list[str]:
    return [ln.strip() for ln in out.splitlines() if ln.strip().startswith("PROBE_")]


def shows(out: str, signature: str) -> bool:
    """Whether a probe printed exactly this line. Equality, not ``in``."""
    return signature in probe_lines(out)


def failure_lines(out: str) -> str:
    """Only the lines pytest marks as the FAILING statement and its explanation.

    Matching `proves` / `not_proves` against the whole suite output does not
    work, and the first run with these fields enforced is what showed it.
    pytest's long traceback prints the failing test's source from the def down
    to the failure: the failing statement is prefixed ``>``, its explanation
    ``E``, and every earlier line -- including earlier assertions and their
    message strings -- is printed UNPREFIXED as context.

    So a substring search over the raw output cannot tell "this assertion
    failed" from "this assertion sits above the one that did". It reported three
    not_proves violations in mutate_paid_path.py that were nothing of the kind:
    the money assertion had passed, and its source was merely visible above the
    refusal check that failed. Restricting the search to ``>``/``E`` lines is
    what makes the distinction the field was added to draw.
    """
    return "\n".join(ln for ln in out.splitlines()
                      if ln.lstrip().startswith(("E ", "> ")))


def failing_tests(out: str) -> list[str]:
    return sorted({line.split(" ", 1)[1].strip()
                   for line in out.splitlines() if line.startswith("FAILED ")})


def _read(path: Path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _purge_pycache(path: Path) -> None:
    """Drop any cached bytecode for ``path``.

    CPython invalidates a ``.pyc`` on (mtime, size) of the source, at one-second
    mtime resolution. Two mutations of one file whose replacements happen to be
    the same length, applied within the same second, let the second run import
    the first one's bytecode — a result reported with total confidence about
    code that never executed. It happened in ``mutate_paid_path.py --discover``;
    see the longer note there.
    """
    cache = path.parent / "__pycache__"
    if cache.is_dir():
        for pyc in cache.glob(f"{path.stem}.*.pyc"):
            try:
                pyc.unlink()
            except OSError:
                pass


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    _purge_pycache(path)


def apply(path: Path, find: str, replace: str) -> None:
    """One edit, refusing anything but an exactly-one-site anchor."""
    text = _read(path)
    if "\r\n" in text:
        find, replace = find.replace("\n", "\r\n"), replace.replace("\n", "\r\n")
    hits = text.count(find)
    if hits == 0:
        raise SystemExit(f"anchor not found in {path.name}:\n{find}")
    if hits > 1:
        raise SystemExit(
            f"anchor matches {hits} sites in {path.name} — narrow it:\n{find}")
    _write(path, text.replace(find, replace, 1))


def digest(paths) -> dict:
    return {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def discover() -> int:
    """Apply each mutation, run only its probes, print everything, restore."""
    touched = sorted({p for m in MUTATIONS for p, _, _ in m.edits})
    pristine = {p: _read(p) for p in touched}
    before = digest(touched)
    for name in sorted(PROBES):
        ok, out = run_probe(name)
        print(f"\n=== PRISTINE probe {name}: {'ran' if ok else 'ERRORED'}")
        for ln in probe_lines(out):
            print(f"    {ln}")
        if not ok:
            print(out[-2500:])
    for mut in MUTATIONS:
        snapshot = {p: _read(p) for p, _, _ in mut.edits}
        outs: dict[str, str] = {}
        try:
            for path, find, replace in mut.edits:
                apply(path, find, replace)
            for name in sorted({n for n, _ in mut.probes} or PROBES):
                outs[name] = run_probe(name)[1]
        finally:
            for path, text in snapshot.items():
                _write(path, text)
        print(f"\n=== MUTATED: {mut.name}")
        for name, out in outs.items():
            print(f"  probe {name}:")
            for ln in probe_lines(out):
                print(f"    {ln}")
            if not probe_lines(out):
                print(f"    (no PROBE_ output)\n{out[-1200:]}")
    after = digest(touched)
    dirty = [p.name for p in touched if before[p] != after[p]]
    for p in touched:
        if before[p] != after[p]:
            _write(p, pristine[p])
    if dirty:
        print(f"\nTREE NOT RESTORED — was still modified: {', '.join(dirty)}")
    return 1 if dirty else 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--discover", action="store_true",
                    help="apply each mutation and print its probe output; no pytest")
    args = ap.parse_args()

    naked = [m.name for m in MUTATIONS if not m.probes]
    if naked:
        print("mutations with no probe signature:")
        for n in naked:
            print(f"  - {n}")
        return 1

    silent = [m.name for m in MUTATIONS if not m.proves and not m.proves_note]
    if silent:
        print("mutations with neither `proves` nor a written `proves_note`:")
        for n in silent:
            print(f"  - {n}")
        return 1

    if args.discover:
        return discover()

    touched = sorted({p for m in MUTATIONS for p, _, _ in m.edits})
    pristine = {p: _read(p) for p in touched}
    before = digest(touched)

    ok, out = run_suite()
    print(f"baseline suite: {'PASS' if ok else 'FAIL'}")
    if not ok:
        print(out[-4000:])
        return 1

    clean_probes: dict[str, str] = {}
    for name in sorted(PROBES):
        pok, pout = run_probe(name)
        clean_probes[name] = pout
        print(f"baseline probe {name}: {'ran' if pok else 'ERRORED'}")
        for ln in probe_lines(pout):
            print(f"    {ln}")
        if not pok:
            print(pout[-2500:])
            return 1

    stale = [f"{m.name}: {sig} is present PRISTINE in probe {probe}"
             for m in MUTATIONS for probe, sig in m.probes
             if shows(clean_probes[probe], sig)]

    survived, unproven = [], list(stale)
    for mut in MUTATIONS:
        snapshot = {p: _read(p) for p, _, _ in mut.edits}
        probe_out: dict[str, str] = {}
        try:
            for path, find, replace in mut.edits:
                apply(path, find, replace)
            suite_ok, suite_out = run_suite()
            for name in {n for n, _ in mut.probes}:
                probe_out[name] = run_probe(name)[1]
        finally:
            for path, text in snapshot.items():
                _write(path, text)

        killed = not suite_ok
        print(f"\n[{mut.clause}] {mut.name}")
        print(f"    removes: {mut.removes}")
        print(f"    suite: {'killed' if killed else 'SURVIVED'}")
        failed = failing_tests(suite_out)
        for t in failed[:6]:
            print(f"      x {t}")
        if len(failed) > 6:
            print(f"      ... and {len(failed) - 6} more")
        if not killed:
            survived.append(mut.name)

        for want in mut.expect:
            if not any(want in t for t in failed):
                unproven.append(f"{mut.name}: expected {want} to fail")
        failed_asserts = failure_lines(suite_out)
        for phrase in mut.proves:
            shown = phrase in failed_asserts
            print(f"    assertion {'FIRED' if shown else 'NEVER RAN'}: {phrase!r}")
            if not shown:
                unproven.append(
                    f"{mut.name}: no failing assertion said {phrase!r} — the test "
                    f"died on a cheaper assertion first, so the defect was never "
                    f"demonstrated")
        for phrase in mut.not_proves:
            if phrase in failed_asserts:
                print(f"    assertion UNEXPECTEDLY FIRED: {phrase!r}")
                unproven.append(
                    f"{mut.name}: {phrase!r} also failed — the test may be red "
                    f"for a reason other than the one named")
        if mut.proves_note and not mut.proves:
            print(f"    no assertion pinned: {mut.proves_note}")

        for name in sorted({n for n, _ in mut.probes}):
            lines = probe_lines(probe_out.get(name, ""))
            print(f"    probe {name}:")
            for ln in lines:
                print(f"      {ln}")
            if not lines:
                print("      (no PROBE_ output — probe crashed under the mutation)")
        for name, sig in mut.probes:
            shown = shows(probe_out.get(name, ""), sig)
            print(f"    signature {'SHOWN' if shown else 'MISSING'}: {sig}")
            if not shown:
                unproven.append(
                    f"{mut.name}: probe {name} did NOT show {sig} — killed, but "
                    f"the behaviour it names was not demonstrated")

    after = digest(touched)
    dirty = [p.name for p in touched if before[p] != after[p]]
    for p in touched:
        if before[p] != after[p]:
            _write(p, pristine[p])

    ok, _ = run_suite()
    print(f"\nrestored suite: {'PASS' if ok else 'FAIL'}")
    print(f"mutations: {len(MUTATIONS)} defined, {len(survived)} survived, "
          f"{len(MUTATIONS) - len(survived)} killed")
    if dirty:
        print(f"  TREE NOT RESTORED — was still modified: {', '.join(dirty)}")
    if survived:
        print("\nmutations that SURVIVED (unprotected safeguards):")
        for name in survived:
            print(f"  - {name}")
    if unproven:
        print("\nnot proven:")
        for line in unproven:
            print(f"  - {line}")
    return 1 if survived or unproven or dirty or not ok else 0


if __name__ == "__main__":
    raise SystemExit(main())

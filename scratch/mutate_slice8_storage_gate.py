"""Mutation harness for V1 slice 8 — the storage gate (stated, not substituted).

    python scratch/mutate_slice8_storage_gate.py

Same shape and the same two obligations as ``mutate_slice8_spend.py``:

  * **a test that fails under a faithful mutation of the fix** — every mutation
    below must be killed. A survivor is a safeguard no test protects;
  * **a mutation that genuinely reproduces the defect** — the known failure mode
    is a mutation that "passes" because it accidentally hid the bug it was
    restoring.

And the reason this defect in particular needs probe signatures rather than a
red suite: it is silent by construction. The broken code raises nothing, logs a
warning nobody reads, and answers **200** with a complete, well-formed
storyboard. The only difference between healthy and broken is *where the
storyboard came from* — and on Cloud Run the wrong source is ephemeral, so the
evidence disappears at the next cold start.

So every mutation declares (probe, signature) pairs. Each signature must appear
under the mutation and must NOT appear pristine. The signatures that matter
most are the pair::

    PROBE_ACTIVE_STATUS=200
    PROBE_ACTIVE_SERVED_DISK_TITLE=ON-DISK-ONLY

which together are the production symptom itself: a healthy 200 carrying state
that came off local disk while the durable store was unreachable.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "backend" / "manifest.py"
MAIN = ROOT / "backend" / "main.py"


# --------------------------------------------------------------------------- #
# probes: the real endpoints and the real loader, read under the mutation
# --------------------------------------------------------------------------- #

_PRELUDE = '''
import sys, tempfile, types
from pathlib import Path
sys.path.insert(0, r"__ROOT__")
for _m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

from fastapi.testclient import TestClient
from backend import config, manifest, projects
from backend import main as M

tmp = Path(tempfile.mkdtemp())
root = tmp / "bestiary" / "leshy"
(root / "assets").mkdir(parents=True)
(root / "references").mkdir(parents=True)
MF = root / "storyboard_manifest.json"

# The disk copy holds recognisable state. On Cloud Run this is frequently the
# repo baked into the image, i.e. ephemeral -- so this title appearing in a 200
# during an outage IS the defect, not a symptom of it.
manifest.save(manifest.Storyboard(
    title="ON-DISK-ONLY", channel="bestiary",
    shots=[manifest.Shot(scene_id="s001", narration="from disk")]), MF)

config.MANIFEST_PATH = MF
M.get_active_manifest_path = lambda: str(MF)
CTX = projects.ProjectContext.from_manifest(MF)
client = TestClient(M.app, raise_server_exceptions=False)


class Snap:
    def __init__(self, d): self._d = d
    def to_dict(self): return self._d


class Unreachable:
    """Every read raises, as an unreachable/absent/denied backend does."""
    def collection(self, n): return self
    def document(self, d): return self
    def get(self): raise RuntimeError("404 The database (default) does not exist")


class DeadBeats:
    """The project document reads; the beats stream dies mid-iteration."""
    exists = True
    def collection(self, n): return self
    def document(self, d): return self
    def get(self): return self
    def to_dict(self): return {"title": "FROM-STORE", "channel": "bestiary"}
    def order_by(self, f): return self
    def stream(self):
        def gen():
            yield Snap({"scene_id": "s001", "narration": "kept"})
            yield Snap({"scene_id": "s002", "narration": "kept"})
            raise RuntimeError("503 Deadline exceeded reading beats")
        return gen()


class Empty:
    """The store answers, and holds no document for this project."""
    exists = False
    def collection(self, n): return self
    def document(self, d): return self
    def get(self): return self


def call(fn, *a):
    try:
        return "returned", fn(*a)
    except Exception as e:
        return "raised", e


def loaded(kind, val):
    if kind == "raised":
        return "raised:" + type(val).__name__
    if val is None:
        return "returned:None"
    return "returned:Storyboard"


def served(r):
    return "ON-DISK-ONLY" if "ON-DISK-ONLY" in r.text else "'absent'"


def gate(r):
    try:
        detail = r.json().get("detail")
    except Exception:
        return "'absent'"
    if isinstance(detail, dict) and detail.get("storage_gate"):
        return detail["storage_gate"]
    return "'absent'"
'''


# The headline condition: the durable store cannot answer at all.
PROBE_UNAVAILABLE = _PRELUDE + '''
manifest.db = Unreachable()

kind, val = call(manifest.load_project, "leshy")
print("PROBE_LOAD_PROJECT=" + loaded(kind, val))

r = client.get("/api/project/active")
print("PROBE_ACTIVE_STATUS=%d" % r.status_code)
print("PROBE_ACTIVE_GATE_KEY=" + gate(r))
print("PROBE_ACTIVE_SERVED_DISK_TITLE=" + served(r))
'''

# The partial read: project document fine, subcollection dies half way.
PROBE_PARTIAL = _PRELUDE + '''
manifest.db = DeadBeats()

kind, val = call(manifest.load_project, "leshy")
if kind == "raised":
    print("PROBE_BEATS=raised:" + type(val).__name__)
else:
    print("PROBE_BEATS=returned:%dbeats" % len(val.shots))

r = client.get("/api/project/active")
print("PROBE_PARTIAL_STATUS=%d" % r.status_code)
print("PROBE_PARTIAL_SERVED_DISK_TITLE=" + served(r))
'''

# The legitimate answer: the store is fine and simply has no record yet.
PROBE_NOTFOUND = _PRELUDE + '''
manifest.db = Empty()

kind, val = call(manifest.load_project, "leshy")
print("PROBE_NOT_FOUND=" + loaded(kind, val))

r = client.get("/api/project/active")
print("PROBE_NOTFOUND_STATUS=%d" % r.status_code)
title = ""
try:
    title = (r.json().get("project") or {}).get("title", "")
except Exception:
    pass
print("PROBE_NOTFOUND_TITLE=" + (title or "'absent'"))
'''

# Local development: Firestore is legitimately absent and JSON is the store of
# record. Every one of the 627 tests runs in exactly this mode.
PROBE_LOCAL = _PRELUDE + '''
manifest.db = None

kind, val = call(manifest.load_project, "leshy")
print("PROBE_LOCAL_LOAD=" + loaded(kind, val))

sb = manifest.load(MF)
sb.title = "EDIT"
sb.id = CTX.project_id
with projects.use(CTX):
    kind2, val2 = call(M.save_current_project, sb)
if kind2 == "returned":
    print("PROBE_LOCAL_SAVE=ok")
else:
    code = getattr(val2, "status_code", None)
    print("PROBE_LOCAL_SAVE=raised:" + ("HTTP%d" % code if code else type(val2).__name__))
print("PROBE_LOCAL_SAVED_TITLE=" + manifest.load(MF).title)
'''

# The write side of the same defect.
PROBE_WRITE = _PRELUDE + '''
manifest.db = Unreachable()

before = MF.read_text(encoding="utf-8")
sb = manifest.load(MF)
sb.title = "EDIT"
sb.id = CTX.project_id
with projects.use(CTX):
    kind, val = call(M.save_current_project, sb)
if kind == "returned":
    print("PROBE_SAVE=reported:ok")
else:
    code = getattr(val, "status_code", None)
    print("PROBE_SAVE=raised:" + ("HTTP%d" % code if code else type(val).__name__))
print("PROBE_SAVE_MIRROR_CHANGED="
      + ("true" if MF.read_text(encoding="utf-8") != before else "false"))
'''

PROBES = {
    "unavailable": PROBE_UNAVAILABLE,
    "partial": PROBE_PARTIAL,
    "notfound": PROBE_NOTFOUND,
    "local": PROBE_LOCAL,
    "write": PROBE_WRITE,
}


@dataclass
class Mutation:
    name: str
    clause: str
    removes: str                       # the behaviour this deletes, in one line
    edits: list[tuple[Path, str, str]]
    probes: list[tuple[str, str]] = field(default_factory=list)
    defect: bool = False               # restores a defect that really shipped
    expect: list[str] = field(default_factory=list)


# --- anchors ----------------------------------------------------------------------

LOAD_DB_NONE = (
    "    if db is None:\n"
    "        # Firestore was never configured here -- decided at import above, and"
)

LOAD_GET_GUARD = (
    "    try:\n"
    "        p_ref = db.collection(\"projects\").document(project_id)\n"
    "        p_doc = p_ref.get()\n"
    "    except Exception as exc:  # noqa: BLE001 -- classified, not swallowed"
)
LOAD_GET_RAISE = (
    "        raise StorageUnavailable(\n"
    "            f\"could not read project {project_id!r} from the durable store: \"\n"
    "            f\"{exc.__class__.__name__}: {exc}. Refusing to answer from local \"\n"
    "            f\"disk as though it were authoritative -- on Cloud Run that copy \"\n"
    "            f\"may be the image's ephemeral one.\"\n"
    "        ) from exc"
)

NOT_FOUND = "    if not p_doc.exists:\n        return None"

BEATS_GUARD = (
    "    try:\n"
    "        shots_ref = p_ref.collection(\"beats\").order_by(\"scene_id\").stream()\n"
    "        shots_list = [shot.to_dict() for shot in shots_ref]\n"
    "    except Exception as exc:  # noqa: BLE001 -- classified, not swallowed"
)
BEATS_RAISE = (
    "        raise StorageUnavailable(\n"
    "            f\"read project {project_id!r} from the durable store but could not \"\n"
    "            f\"read its beats: {exc.__class__.__name__}: {exc}. A partial \"\n"
    "            f\"storyboard is not a shorter one.\"\n"
    "        ) from exc"
)

SAVE_DB_NONE = "    if db is None:\n        return\n\n    try:\n        _save_project_to_firestore(sb)"

MAIN_LOAD = (
    "    try:\n"
    "        sb = manifest.load_project(f_id)\n"
    "    except manifest.StorageUnavailable as exc:\n"
    "        print(f\"Storage gate: {exc}\")\n"
    "        raise storage_gate_unavailable(exc, f_id) from exc"
)
MAIN_SAVE = (
    "    try:\n"
    "        manifest.save_project(sb)\n"
    "    except manifest.StorageUnavailable as exc:\n"
    "        print(f\"Storage gate: {exc}\")\n"
    "        raise storage_gate_unavailable(exc, sb.id) from exc"
)
GATE_KEY = '            "storage_gate": "unavailable",'
GATE_STATUS = "        status_code=503,"
ACTIVE_RERAISE = (
    "    except HTTPException:\n"
    "        raise\n"
    "    except Exception as e:\n"
    "        return JSONResponse(status_code=500, content={\"ok\": False, \"error\": str(e)})\n"
    "\n"
    "\n"
    "@app.get(\"/api/stages\")"
)

# The pre-fix loader body, used by the full reproduction.
PREFIX_GET = (
    "    p_ref = db.collection(\"projects\").document(project_id)  # MUTANT\n"
    "    p_doc = p_ref.get()"
)
PREFIX_BEATS = (
    "    shots_ref = p_ref.collection(\"beats\").order_by(\"scene_id\").stream()  # MUTANT\n"
    "    shots_list = [shot.to_dict() for shot in shots_ref]"
)
PREFIX_CALLER = (
    "    try:\n"
    "        sb = manifest.load_project(f_id)\n"
    "    except Exception as fe:  # MUTANT\n"
    "        print(f\"Warning: Firestore load_project failed: {fe}\")"
)


MUTATIONS = [
    # ---- reproductions of a defect that really shipped -------------------------
    Mutation(
        "DEFECT: the caller swallows every failure and falls through to disk",
        "CLAUDE.md gates / §11.4",
        "the caller's half — `except Exception` around load_project again, so an "
        "unreachable store and an absent record both reach the disk manifest and "
        "answer 200",
        [(MAIN, MAIN_LOAD, PREFIX_CALLER)],
        probes=[("unavailable", "PROBE_ACTIVE_STATUS=200"),
                ("unavailable", "PROBE_ACTIVE_SERVED_DISK_TITLE=ON-DISK-ONLY"),
                ("unavailable", "PROBE_ACTIVE_GATE_KEY='absent'"),
                ("partial", "PROBE_PARTIAL_STATUS=200"),
                ("partial", "PROBE_PARTIAL_SERVED_DISK_TITLE=ON-DISK-ONLY")],
        defect=True,
        expect=["test_get_current_project_refuses_rather_than_serving_the_disk_copy",
                "test_the_studio_boot_read_refuses_instead_of_answering_200",
                "test_a_second_endpoint_refuses_the_same_way"],
    ),
    Mutation(
        "DEFECT: the whole fix at once — nothing classifies and nothing refuses",
        "CLAUDE.md gates / §11.4",
        "the exact pre-fix code on both sides: load_project lets the raw "
        "exception escape and the caller swallows it, which is what shipped and "
        "what logged `Firestore load_project failed: 404` on every "
        "/api/roughcut/plan call while returning 200",
        [(MANIFEST, LOAD_GET_GUARD + "\n", ""),
         (MANIFEST, LOAD_GET_RAISE, PREFIX_GET),
         (MANIFEST, BEATS_GUARD + "\n", ""),
         (MANIFEST, BEATS_RAISE, PREFIX_BEATS),
         (MAIN, MAIN_LOAD, PREFIX_CALLER)],
        probes=[("unavailable", "PROBE_LOAD_PROJECT=raised:RuntimeError"),
                ("unavailable", "PROBE_ACTIVE_STATUS=200"),
                ("unavailable", "PROBE_ACTIVE_SERVED_DISK_TITLE=ON-DISK-ONLY"),
                ("partial", "PROBE_BEATS=raised:RuntimeError"),
                ("partial", "PROBE_PARTIAL_SERVED_DISK_TITLE=ON-DISK-ONLY")],
        defect=True,
        expect=["test_an_unreachable_store_raises_rather_than_reporting_no_record",
                "test_a_dead_beats_stream_is_unavailable_not_a_shorter_storyboard",
                "test_get_current_project_refuses_rather_than_serving_the_disk_copy",
                "test_the_studio_boot_read_refuses_instead_of_answering_200"],
    ),
    Mutation(
        "DEFECT: a save that never reached the store is reported as a save",
        "CLAUDE.md gates / §11.4",
        "the write side — a warning and carry on, so the studio answered 200 to "
        "every edit while nothing was persisted anywhere that survives a cold "
        "start",
        [(MAIN, MAIN_SAVE,
          "    try:\n"
          "        manifest.save_project(sb)\n"
          "    except Exception as fe:  # MUTANT\n"
          "        print(f\"Warning: Firestore save_project failed: {fe}\")")],
        probes=[("write", "PROBE_SAVE=reported:ok"),
                ("write", "PROBE_SAVE_MIRROR_CHANGED=true")],
        defect=True,
        expect=["test_a_save_that_never_reached_the_store_is_not_reported_as_a_save"],
    ),

    # ---- faithful mutations of the fix ------------------------------------------
    Mutation(
        "a beats stream that dies returns the beats it managed to read",
        "CLAUDE.md gates",
        "the partial-read arm — a storyboard that LOST beats becomes "
        "indistinguishable from one that never had them, and save_project() "
        "deletes every beat absent from what it writes, so the truncation "
        "becomes durable",
        [(MANIFEST, BEATS_GUARD,
          "    shots_list = []  # MUTANT\n"
          "    try:\n"
          "        for shot in p_ref.collection(\"beats\").order_by(\"scene_id\").stream():\n"
          "            shots_list.append(shot.to_dict())\n"
          "    except Exception as exc:"),
         (MANIFEST, BEATS_RAISE, "        pass  # MUTANT")],
        probes=[("partial", "PROBE_BEATS=returned:2beats"),
                ("partial", "PROBE_PARTIAL_STATUS=200")],
        expect=["test_a_dead_beats_stream_is_unavailable_not_a_shorter_storyboard"],
    ),
    Mutation(
        "an unconfigured Firestore is treated as an outage", "CLAUDE.md runtime",
        "the local-development path — `db is None` is decided once at import and "
        "means JSON is the store of record, not that the store is down; raising "
        "here takes every workstation checkout offline",
        [(MANIFEST, LOAD_DB_NONE,
          "    if db is None:  # MUTANT\n"
          "        raise StorageUnavailable(\"no Firestore client\")\n"
          "    if False:\n"
          "        # Firestore was never configured here -- decided at import above, and")],
        probes=[("local", "PROBE_LOCAL_LOAD=raised:StorageUnavailable")],
        expect=["test_an_unconfigured_firestore_is_not_an_outage"],
    ),
    Mutation(
        "an absent record is treated as an outage", "CLAUDE.md gates",
        "the over-correction — not-found is legitimate and is how every project "
        "starts; refusing on it means no project can ever be read before its "
        "first Firestore write",
        [(MANIFEST, NOT_FOUND,
          "    if not p_doc.exists:  # MUTANT\n"
          "        raise StorageUnavailable(\"no document\")")],
        probes=[("notfound", "PROBE_NOT_FOUND=raised:StorageUnavailable"),
                ("notfound", "PROBE_NOTFOUND_STATUS=503"),
                ("notfound", "PROBE_NOTFOUND_TITLE='absent'")],
        expect=["test_no_document_is_still_none",
                "test_no_record_still_falls_back_to_the_disk_manifest",
                "test_an_ordinary_request_is_unaffected_when_the_store_has_no_record"],
    ),
    Mutation(
        "the refusal no longer names the storage gate", "CLAUDE.md gates",
        "the machine-readable cause — without it a client cannot tell 'the "
        "durable store is unreachable' from any other failure, which is exactly "
        "the collapse this slice removes, relocated to the error path",
        [(MAIN, GATE_KEY, "  # MUTANT: gate not named")],
        probes=[("unavailable", "PROBE_ACTIVE_GATE_KEY='absent'")],
        expect=["test_the_studio_boot_read_refuses_instead_of_answering_200",
                "test_a_second_endpoint_refuses_the_same_way"],
    ),
    Mutation(
        "the refusal is a server fault rather than a retryable one",
        "CLAUDE.md gates",
        "503 -> 500 — the studio can no longer offer 'try again' instead of "
        "'this project is broken', and a transient outage reads as data loss",
        [(MAIN, GATE_STATUS, "        status_code=500,  # MUTANT")],
        probes=[("unavailable", "PROBE_ACTIVE_STATUS=500")],
        expect=["test_get_current_project_refuses_rather_than_serving_the_disk_copy",
                "test_the_studio_boot_read_refuses_instead_of_answering_200",
                "test_a_second_endpoint_refuses_the_same_way"],
    ),
    Mutation(
        "the studio's boot read flattens the refusal into a 500",
        "CLAUDE.md gates",
        "the handler's `except HTTPException: raise` on /api/project/active — "
        "the generic arm rewrites the 503 as a 500 whose body is the string "
        "\"503: {...}\", so the gate is unreachable by the one endpoint the "
        "studio boots on",
        [(MAIN, ACTIVE_RERAISE,
          "    except Exception as e:  # MUTANT\n"
          "        return JSONResponse(status_code=500, content={\"ok\": False, \"error\": str(e)})\n"
          "\n"
          "\n"
          "@app.get(\"/api/stages\")")],
        probes=[("unavailable", "PROBE_ACTIVE_STATUS=500"),
                ("unavailable", "PROBE_ACTIVE_GATE_KEY='absent'")],
        expect=["test_the_studio_boot_read_refuses_instead_of_answering_200"],
    ),
    Mutation(
        "saving explodes when no Firestore is configured", "CLAUDE.md runtime",
        "save_project's local-development guard — every write on a workstation "
        "becomes a 503 about a store that was never meant to be there",
        [(MANIFEST, SAVE_DB_NONE,
          "    if False:  # MUTANT\n"
          "        return\n"
          "\n"
          "    try:\n"
          "        _save_project_to_firestore(sb)")],
        probes=[("local", "PROBE_LOCAL_SAVE=raised:HTTP503")],
        expect=["test_saving_locally_still_works_with_no_firestore_configured"],
    ),
    Mutation(
        "the local mirror is written anyway before refusing", "CLAUDE.md gates",
        "the all-or-nothing write — half a save leaves the durable store and the "
        "JSON mirror disagreeing about a project nobody has been told is in "
        "trouble, and the mirror is what the next bootstrap copies UP",
        [(MAIN, MAIN_SAVE,
          "    try:\n"
          "        manifest.save_project(sb)\n"
          "    except manifest.StorageUnavailable as exc:\n"
          "        manifest.save(sb)  # MUTANT\n"
          "        raise storage_gate_unavailable(exc, sb.id) from exc")],
        probes=[("write", "PROBE_SAVE_MIRROR_CHANGED=true")],
        expect=["test_a_save_that_never_reached_the_store_is_not_reported_as_a_save"],
    ),
]


# --------------------------------------------------------------------------- #
# harness (same as mutate_slice8_spend.py — see its notes)
# --------------------------------------------------------------------------- #

_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


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


def failing_tests(out: str) -> list[str]:
    return sorted({line.split(" ", 1)[1].strip()
                   for line in out.splitlines() if line.startswith("FAILED ")})


def _read(path: Path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


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


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

    naked = [m.name for m in MUTATIONS if not m.probes]
    if naked:
        print("mutations with no probe signature:")
        for n in naked:
            print(f"  - {n}")
        return 1

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

        for name in sorted({n for n, _ in mut.probes}):
            print(f"    probe {name}:")
            for ln in probe_lines(probe_out.get(name, "")):
                print(f"      {ln}")
            if not probe_lines(probe_out.get(name, "")):
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

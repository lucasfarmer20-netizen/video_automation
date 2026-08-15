"""Mutation harness for V1 slice 8 item 2 — TG-S4-04, TG-S4-05, TG-S4-06.

    python scratch/mutate_slice8_tg_s4.py

Same shape as ``mutate_slice8_spend.py``. The difference is what is being
proved. Those items were defects, so a mutation restored a bug that had shipped.
These are **test gaps**: the safeguards already work and the auditor reproduced
each twice. So the question here is not "did the fix work" but the sharper one
the guardrails put:

    would this test notice if the safeguard were removed?

A test that passes with and without the safeguard is worse than no test, because
it claims coverage that does not exist. Slice 7 shipped three of those and the
reviewer caught them, so every mutation below declares a probe signature that
must appear UNDER the mutation and must NOT appear pristine. Killing the suite
without showing the named behaviour proves the tests are sensitive to something;
it does not prove they guard the right thing.

Two mutations per gap, deliberately:

  * one that **removes the safeguard** outright, and
  * one **faithful mutation** of a different clause on the same path, so a test
    that happens to be sensitive to the first cannot pass the second by luck.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GEN = ROOT / "backend" / "generation.py"
WORKER = ROOT / "backend" / "pipeline_worker.py"
MAIN = ROOT / "backend" / "main.py"

# Paths inside the repository that a MUTATED run can write a real project's
# records to, because ROOT is itself a project (config.MANIFEST_PATH defaults to
# ROOT/storyboard_manifest.json). Neither is gitignored and s011 is a live beat
# id, so these are never assumed to be scratch: the run refuses to start if
# either already exists, and only removes what it created.
ROOT_ARTIFACTS = (ROOT / "generation" / "s011.json",
                  ROOT / "director" / "s011.json")


# --------------------------------------------------------------------------- #
# probes: the production paths, driven for real and printed
# --------------------------------------------------------------------------- #

_PRELUDE = """
import json, sys, tempfile, threading, types
from pathlib import Path
sys.path.insert(0, r"__ROOT__")
for _m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(_m, types.ModuleType(_m))
from backend import atomic, config, generation

tmp = Path(tempfile.mkdtemp())
config.MANIFEST_PATH = tmp / "storyboard_manifest.json"

def write_raw(beat_id, doc):
    p = generation.ledger_path(beat_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_json(p, doc)

def join_jobs():
    me = threading.current_thread()
    for t in threading.enumerate():
        if t is not me and t.name.startswith("job-"):
            t.join(timeout=15)
"""

# TG-S4-04. Two real HTTP compiles for one beat, fired together, with the
# compile held open so the first job is genuinely still running when the second
# request arrives. Prints the three numbers the audit recorded.
PROBE_RACE = _PRELUDE + """
from fastapi.testclient import TestClient
from backend import director, main as M
from backend.manifest import Camera, Shot, Storyboard

sb = Storyboard(title="T", script_locked=True, storyboard_approved=True,
                shots=[Shot(scene_id="s001", narration="n", prompt="p",
                            camera=Camera(move="static", duration=12.0))])
M.get_current_project = lambda: sb
M.get_active_manifest_path = lambda: str(config.MANIFEST_PATH)

plan = director.CoveragePlan(beat_id="s001", beat_duration=12.0, status="locked")
plan.coverage = [
    director.DirectorShot(id="s001.01", beat_id="s001", motion_type="ai_video",
                          prompt="a", camera=Camera(move="static", duration=6.0)),
    director.DirectorShot(id="s001.02", beat_id="s001", motion_type="parallax",
                          prompt="b", camera=Camera(move="static", duration=6.0)),
]
director.approve(plan)
director.save_plan(plan)

calls = {"n": 0}
counter = threading.Lock()
entered = threading.Event()
proceed = threading.Event()

def held(plan_, sb_, render_dir, log=print):
    with counter:
        calls["n"] += 1
    entered.set()
    proceed.wait(timeout=10)
    return Path(render_dir) / "s001.mp4"

M.director.compile_coverage = held

client = TestClient(M.app, raise_server_exceptions=False)
line = threading.Barrier(2, timeout=10)
codes = [0, 0]

def fire(i):
    line.wait()
    codes[i] = client.post("/api/director/compile/s001").status_code

threads = [threading.Thread(target=fire, args=(i,)) for i in range(2)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=20)
entered.wait(timeout=10)
proceed.set()
join_jobs()

n = calls["n"]
accepted = sum(1 for c in codes if c == 200)
print("PROBE_RACE_CODES=" + json.dumps(sorted(codes)))
print("PROBE_RACE_COMPILE_CALLS=" + str(n))
# Two dispatches for one beat: the second one re-buys its ai_video shots.
print("PROBE_RACE_DOUBLE_DISPATCH=" + ("true" if n > 1 else "false"))
# The other shape: a caller told 200/started while nothing was handed to a
# worker, so the beat is silently dropped.
print("PROBE_RACE_LIED_ABOUT_STARTING=" + ("true" if accepted > n else "false"))
"""

# TG-S4-05. Compile on project A through the real endpoint, switch the whole
# process to B while the job is queued, then read the ledger from both sides.
PROBE_IDENTITY = _PRELUDE + """
from fastapi.testclient import TestClient
from backend import director, main as M, projects
from backend.manifest import Camera, Shot, Storyboard

def project(name):
    d = tmp / name
    d.mkdir(parents=True, exist_ok=True)
    mf = d / "storyboard_manifest.json"
    mf.write_text('{"title": "' + name + '", "shots": []}', encoding="utf-8")
    return projects.ProjectContext.from_manifest(mf)

a, b = project("alpha"), project("bravo")
config.MANIFEST_PATH = a.manifest_path

# The third place a ledger can land. backend/config.py calls set_active_manifest
# at import, which points projects' process fallback at the REPOSITORY's own
# manifest -- so a job that resolves identity late binds that and writes here.
#
# These paths are NOT scratch. The repository root is itself a project, both
# directories exist there, neither is gitignored, and s011 is a real beat id --
# backend/main.py names s011 and s017 as the beats that were silently dropped.
# A file here can be a real generation ledger: the record of what was paid.
#
# So this probe deletes by OWNERSHIP, never by existence. It refuses to run at
# all if either path is already present, rather than clearing it -- clearing was
# the previous behaviour and it would have destroyed a money record without a
# prompt or a backup. Refusing also protects the file from being *appended to*:
# begin() loads an existing ledger and writes the list back, so a pre-existing
# s011 ledger would be rewritten, not merely read.
STRAY_LEDGER = Path(r"__ROOT__") / "generation" / "s011.json"
STRAY_PLAN = Path(r"__ROOT__") / "director" / "s011.json"
OCCUPIED = [p for p in (STRAY_LEDGER, STRAY_PLAN) if p.exists()]
if OCCUPIED:
    print("PROBE_IDENTITY_REFUSED_TO_RUN="
          + json.dumps([str(p) for p in OCCUPIED]))
    print("PROBE_IDENTITY_REFUSED_REASON=these exist and were not written by "
          "this probe; a generation ledger is a money record. Move them aside "
          "and re-run.")
    raise SystemExit(3)

def board():
    return Storyboard(title="T", script_locked=True, storyboard_approved=True,
                      shots=[Shot(scene_id="s011", narration="n", prompt="p",
                                  camera=Camera(move="static", duration=5.0))])

M.get_current_project = lambda: board()
M.get_active_manifest_path = lambda: str(config.MANIFEST_PATH)

plan = director.CoveragePlan(beat_id="s011", beat_duration=5.0, status="locked")
ds = director.DirectorShot(id="s011.01", beat_id="s011", motion_type="ai_video",
                           camera=Camera(move="static", duration=5.0),
                           prompt="a hand on drill steel", subject="single jack",
                           shot_size="m", purpose="master")
ds.draft_variations = ["a.png"]
ds.chosen_variation = 0
plan.coverage = [ds]
director.approve(plan)
with projects.use(a):
    director.save_plan(plan)

def fake_paid(ds_, synth, sb_, out_dir, log=print):
    target = Path(out_dir) / (ds_.id + ".mp4")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"PAID-BYTES")
    return target

director.generate_paid_clip = fake_paid
director.normalize_clip = lambda *x, **k: None
director.fit_clip = lambda *x, **k: None
director.concat = lambda *x, **k: Path("beat.mp4")
director.probe_seconds = lambda *x, **k: 5.0

real_compile = director.compile_coverage
gate = threading.Event()
go = threading.Event()

def held(*x, **k):
    gate.set()
    go.wait(timeout=10)
    return real_compile(*x, **k)

M.director.compile_coverage = held

client = TestClient(M.app, raise_server_exceptions=False)
r = client.post("/api/director/compile/s011", headers={"X-Project-Id": a.project_id})
print("PROBE_IDENTITY_ENQUEUED=" + str(r.status_code))
gate.wait(timeout=10)

# The studio switches film while the job is still queued.
config.MANIFEST_PATH = b.manifest_path
projects.set_fallback(b)
go.set()
join_jobs()

in_a = (a.root / "generation" / "s011.json").is_file()
in_b = (b.root / "generation" / "s011.json").is_file()
with projects.use(a):
    spend_a = generation.spend("s011")
with projects.use(b):
    spend_b = generation.spend("s011")

print("PROBE_IDENTITY_LEDGER_IN_A=" + ("true" if in_a else "false"))
print("PROBE_IDENTITY_LEDGER_IN_B=" + ("true" if in_b else "false"))
print("PROBE_IDENTITY_SPEND_A=" + repr(spend_a.get("spent")))
print("PROBE_IDENTITY_SPEND_B=" + repr(spend_b.get("spent")))

# WHERE it went, not merely whether it went to B. A mutation is free to file the
# ledger somewhere neither project names -- backend/config.py sets a process
# fallback at import, pointing at the repo's own manifest, so a job that resolves
# identity late binds THAT and writes into the repository. Declaring "it lands in
# B" would then read as MISSING against a mutation that had in fact reproduced
# the defect somewhere worse, which is the wrong lesson to draw from a probe.
in_repo = STRAY_LEDGER.is_file()
if in_a and not (in_b or in_repo):
    where = "alpha"
elif in_b and not (in_a or in_repo):
    where = "bravo"
elif in_repo and not (in_a or in_b):
    where = "repository"
elif in_a or in_b or in_repo:
    where = "split"
else:
    where = "nowhere"
print("PROBE_IDENTITY_LEDGER_LOCATION=" + where)

# Leave no artifact behind -- but only OUR artifact. Both paths were verified
# absent at the top of this probe, so anything at them now was written by this
# run and nothing else. That check is what makes the unlink safe; without it
# this line is a delete of a money record on a path the probe never wrote.
for stray in (STRAY_LEDGER, STRAY_PLAN):
    if stray.is_file():
        stray.unlink()

# The whole finding in one line: the film that paid is not the film that holds
# the record of paying.
print("PROBE_IDENTITY_BILLED_THE_WRONG_FILM="
      + ("true" if not (in_a and not in_b) else "false"))
"""

# TG-S4-06. The audit's own shape -- right syntax, wrong types, a cost that would
# reach a sum -- put in front of every public entry point in turn.
PROBE_CONTRACT = _PRELUDE + """
GOOD = {"id": "s001.01.a1", "shot_id": "s001.01", "beat_id": "s001",
        "attempt": 1, "status": "succeeded", "paid": True, "cost": "0.60",
        "output": "a.mp4"}
DOC = {"beat_id": "s001", "attempts": [GOOD]}

CALLERS = {
    "load_attempts": lambda: generation.load_attempts("s001"),
    "for_shot": lambda: generation.for_shot("s001", "s001.01"),
    "history": lambda: generation.history("s001", "s001.01"),
    "find_by_key": lambda: generation.find_by_key("s001", "req-1"),
    "reusable": lambda: generation.reusable("s001", "s001.01", "sig-a"),
    "spend": lambda: generation.spend("s001"),
    "begin": lambda: generation.begin(beat_id="s001", shot_id="s001.01",
                                      signature="sig-a", paid=True,
                                      idempotency_key="k1"),
    "succeed": lambda: generation.succeed("s001", "s001.01.a1", "a.mp4", cost=0.6),
    "fail": lambda: generation.fail("s001", "s001.01.a1", "boom"),
    "in_doubt": lambda: generation.in_doubt("s001", "s001.01.a1", "no answer"),
    "abandon": lambda: generation.abandon("s001", "s001.01.a1", "wrote it off"),
}

escaped = False
overwrote = False
for name in sorted(CALLERS):
    write_raw("s001", DOC)
    before = generation.ledger_path("s001").read_bytes()
    try:
        CALLERS[name]()
        outcome = "returned"
    except generation.LedgerUnreadable:
        outcome = "unreadable"
    except Exception as exc:
        outcome = "raised:" + type(exc).__name__
    if outcome != "unreadable":
        escaped = True
    if generation.ledger_path("s001").read_bytes() != before:
        outcome += "+overwrote"
        overwrote = True
    print("PROBE_CONTRACT_" + name + "=" + outcome)

# The promise the audit said was incomplete: callers are told LedgerUnreadable
# and may get something else.
print("PROBE_CONTRACT_ESCAPED=" + ("true" if escaped else "false"))
# Worse than escaping: a caller that could not read the file wrote over it.
print("PROBE_CONTRACT_OVERWROTE=" + ("true" if overwrote else "false"))

# The gate's own blind spot: it is exactly as complete as _SHAPE, and nothing
# makes _SHAPE follow the dataclass.
missing = sorted(set(generation.GenerationAttempt.__dataclass_fields__)
                 - set(generation._SHAPE))
print("PROBE_SHAPE_UNVALIDATED=" + json.dumps(missing))
print("PROBE_SHAPE_COVERS_EVERY_FIELD=" + ("true" if not missing else "false"))
"""

PROBES = {
    "race": PROBE_RACE,
    "identity": PROBE_IDENTITY,
    "contract": PROBE_CONTRACT,
}


@dataclass
class Mutation:
    name: str
    gap: str
    removes: str                       # the behaviour this deletes, in one line
    edits: list[tuple[Path, str, str]]
    # (probe, signature) pairs. The signature must appear under the mutation and
    # must NOT appear pristine. Required for every mutation.
    probes: list[tuple[str, str]] = field(default_factory=list)
    expect: list[str] = field(default_factory=list)
    # Assertion messages that must appear in the mutated suite's output.
    #
    # `expect` proves the right test died. This proves the right ASSERTION
    # inside it ran. The two are not the same, and the gap between them is how
    # this round's finding survived every earlier check: the concurrency tests
    # asserted the status split before the dispatch count, so under the
    # regression they failed on the status and aborted, and the money assertion
    # -- the whole point of TG-S4-04 -- was never evaluated. The test died, the
    # table went red, and nothing had been demonstrated.
    proves: list[str] = field(default_factory=list)


# --- anchors ----------------------------------------------------------------------

JOB_DEDUPE = ('        if key in _jobs and _jobs[key]["status"] == "running":\n'
              "            return False")
JOB_REBIND = "        token = projects.bind(job_ctx) if job_ctx else None"
JOB_CAPTURE = "    job_ctx = ctx or projects.capture()"
ENDPOINT_409 = ("        if not start_job(job, fn):\n"
                "            return JSONResponse(status_code=409, content={\n"
                '                "ok": False,\n'
                '                "error": f"a compile for {beat_id} is already running",\n'
                "            })")
GEN_DIR = ("def generation_dir() -> Path:\n"
           '    return config.project_dir() / "generation"')
ROW_TYPES = "        if key in _SHAPE and not _valid(_SHAPE[key], value):"
SHAPE_COST = '    "cost": float, "estimated_cost": float,'


MUTATIONS = [
    # ---- TG-S4-04: one dispatch per beat, however many requests arrive --------
    Mutation(
        "TG-S4-04 removal: the job registry stops refusing a running key",
        "TG-S4-04",
        "the safeguard itself — two concurrent compiles for one beat both reach "
        "a worker, and every ai_video shot in that beat is bought twice",
        [(WORKER, JOB_DEDUPE, "        if False:  # MUTANT\n            return False")],
        probes=[("race", "PROBE_RACE_DOUBLE_DISPATCH=true"),
                ("race", "PROBE_RACE_COMPILE_CALLS=2"),
                ("race", "PROBE_RACE_CODES=[200, 200]")],
        expect=["two_concurrent_compiles_dispatch_exactly_one"],
        # The dispatch count, not the status split. Before the assertions were
        # reordered this phrase never appeared: the test aborted on [200, 200]
        # and the money assertion below it never ran.
        proves=["compiles were dispatched for one beat"],
    ),
    Mutation(
        "TG-S4-04 faithful: the endpoint stops honouring the refusal",
        "TG-S4-04",
        "the caller's half — start_job still refuses, but the route reports "
        "{\"started\": true} anyway, so the beat is dropped while the human is "
        "told it began (this exact shape shipped, for s011 and s017)",
        [(MAIN, ENDPOINT_409, "        start_job(job, fn)  # MUTANT")],
        probes=[("race", "PROBE_RACE_LIED_ABOUT_STARTING=true"),
                ("race", "PROBE_RACE_CODES=[200, 200]")],
        expect=["two_concurrent_compiles_dispatch_exactly_one",
                "the_refused_caller_is_never_told_its_compile_started"],
        # Asserted over every body, so it survives there being no 409 to index
        # by. The old `bodies[codes.index(409)]` raised ValueError here -- an
        # error, not a failure, reporting nothing about the two callers who were
        # both told their compile had started.
        proves=["were told their compile started"],
    ),

    # ---- TG-S4-05: the ledger belongs to the project that paid ----------------
    Mutation(
        "TG-S4-05 removal: the worker thread stops rebinding its captured project",
        "TG-S4-05",
        "the safeguard itself — the job falls through to whatever project the "
        "process is on when it runs, so the generation ledger is filed against "
        "a film that never bought the clip",
        [(WORKER, JOB_REBIND, "        token = None  # MUTANT: no rebind")],
        probes=[("identity", "PROBE_IDENTITY_BILLED_THE_WRONG_FILM=true"),
                ("identity", "PROBE_IDENTITY_LEDGER_LOCATION=bravo"),
                ("identity", "PROBE_IDENTITY_LEDGER_IN_A=false"),
                ("identity", "PROBE_IDENTITY_LEDGER_IN_B=true"),
                ("identity", "PROBE_IDENTITY_SPEND_A=0.0"),
                ("identity", "PROBE_IDENTITY_SPEND_B=0.6")],
        expect=["a_background_generation_ledger_lands_in_the_captured_project",
                "the_project_that_paid_is_the_project_that_reports_the_spend"],
        # The money sentence. Before the reorder, `len(rows) == 1` was checked
        # against A first, failed there, and this never ran -- so the suite
        # reported that A had no rows and never once said another film had been
        # charged $0.60.
        proves=["for another film's clip",
                "another film was billed for a clip it did not buy"],
    ),
    Mutation(
        "TG-S4-05 faithful: identity is resolved when the job RUNS, not at enqueue",
        "TG-S4-05",
        "the ordering §11.3 is entirely about — the capture still happens and is "
        "then ignored. Measured, not assumed: this does NOT file the ledger under "
        "B. It binds whatever the process fallback names when the thread starts, "
        "which config.py seeds at import with the repository's own manifest — so "
        "the money lands in a project neither request ever mentioned.",
        [(WORKER, JOB_REBIND,
          "        _late = projects.current()  # MUTANT: resolved at run time\n"
          "        token = projects.bind(_late) if _late else None")],
        probes=[("identity", "PROBE_IDENTITY_BILLED_THE_WRONG_FILM=true"),
                ("identity", "PROBE_IDENTITY_LEDGER_LOCATION=repository"),
                ("identity", "PROBE_IDENTITY_LEDGER_IN_A=false"),
                ("identity", "PROBE_IDENTITY_SPEND_A=0.0")],
        expect=["a_background_generation_ledger_lands_in_the_captured_project",
                "the_project_that_paid_is_the_project_that_reports_the_spend"],
    ),
    Mutation(
        "TG-S4-05 faithful: the ledger path reads the process global",
        "TG-S4-05",
        "the other end of the same path — the job binds correctly and the "
        "LEDGER still follows the process, which is the half a start_job test "
        "cannot see",
        [(GEN, GEN_DIR,
          "def generation_dir() -> Path:  # MUTANT\n"
          '    return config.MANIFEST_PATH.parent / "generation"')],
        probes=[("identity", "PROBE_IDENTITY_BILLED_THE_WRONG_FILM=true"),
                ("identity", "PROBE_IDENTITY_LEDGER_LOCATION=bravo"),
                ("identity", "PROBE_IDENTITY_LEDGER_IN_A=false"),
                ("identity", "PROBE_IDENTITY_LEDGER_IN_B=true")],
        expect=["a_background_generation_ledger_lands_in_the_captured_project",
                "the_project_that_paid_is_the_project_that_reports_the_spend"],
    ),

    # ---- TG-S4-06: one exception type, whoever asks ---------------------------
    Mutation(
        "TG-S4-06 removal: a row's field types are no longer checked",
        "TG-S4-06",
        "the schema gate — the exact state the audit recorded, where a "
        "structurally invalid ledger raises an incidental TypeError instead of "
        "the LedgerUnreadable its callers are promised",
        [(GEN, ROW_TYPES, "        if False:  # MUTANT")],
        probes=[("contract", "PROBE_CONTRACT_ESCAPED=true"),
                ("contract", "PROBE_CONTRACT_OVERWROTE=true"),
                ("contract", "PROBE_CONTRACT_begin=returned+overwrote"),
                ("contract", "PROBE_CONTRACT_spend=raised:TypeError")],
        # The overwrite test is named here deliberately. Before it stopped
        # wrapping the call in pytest.raises it COULD NOT fail for the right
        # reason under this mutation -- begin() returns rather than raising, so
        # the wrapper failed with DID NOT RAISE and read_bytes() was never
        # compared. Naming it is what proves the fix: the harness fails the run
        # if this specific test does not die here.
        expect=["every_entry_point_answers_a_broken_ledger_with_the_promised_error",
                "no_entry_point_overwrites_the_ledger_it_could_not_read"],
    ),
    Mutation(
        "TG-S4-06 faithful: one money field drops out of the shape table",
        "TG-S4-06",
        "the gate's own blind spot — _SHAPE stops naming `cost`, so the gate "
        "silently stops covering it while every other clause still reads as "
        "complete. This is what adding a field and forgetting the table does.",
        [(GEN, SHAPE_COST, '    "estimated_cost": float,  # MUTANT: cost dropped')],
        probes=[("contract", "PROBE_SHAPE_COVERS_EVERY_FIELD=false"),
                ("contract", 'PROBE_SHAPE_UNVALIDATED=["cost"]'),
                ("contract", "PROBE_CONTRACT_spend=raised:TypeError")],
        expect=["the_gate_validates_every_field_an_attempt_can_carry",
                "every_entry_point_answers_a_broken_ledger_with_the_promised_error"],
    ),
]


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #

_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}

# How many times each ROOT project file was written by a mutated run and taken
# back. Reported in the summary rather than swept quietly.
_RECLAIMED: dict[Path, int] = {}


def run_suite() -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q"], cwd=ROOT, env=_ENV,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def reclaim_root_artifacts() -> list[Path]:
    """Remove ROOT project files THIS RUN created. Returns what it removed.

    Ownership is established once, per harness run, by the check at the top of
    ``main()``: both paths were verified absent before anything ran, so anything
    at them afterwards was written by this harness -- and under a mutation it is
    not only the probe that writes them, it is the mutated *suite*, which runs
    first. That ordering is why this is not folded into the probe: a probe that
    refuses on existence would refuse on the file the suite just left, and four
    signatures would go missing for a mutation that had reproduced its defect
    perfectly.

    MUST NOT be called before that start-up check. Everything protecting a real
    generation ledger from this function is that one precondition.
    """
    removed = [p for p in ROOT_ARTIFACTS if p.is_file()]
    for p in removed:
        p.unlink()
        # Counted, not silently swept. That a mutated run writes a generation
        # ledger into the repository is a fact about the mutation worth stating
        # -- it is the defect, seen from the filesystem -- and reporting it is
        # what keeps the cleanup from reading as "nothing happened here".
        _RECLAIMED[p] = _RECLAIMED.get(p, 0) + 1
    return removed


def run_probe(name: str) -> tuple[bool, str]:
    # Clear first: a mutated suite may have left this run's artifact at ROOT,
    # and the probe refuses to start on a ROOT file it did not write.
    reclaim_root_artifacts()
    script = PROBES[name].replace("__ROOT__", str(ROOT))
    proc = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, env=_ENV,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def probe_lines(out: str) -> list[str]:
    return [ln.strip() for ln in out.splitlines() if ln.strip().startswith("PROBE_")]


def shows(out: str, signature: str) -> bool:
    """Whether a probe printed exactly this line.

    Equality, not ``in``: a substring signature silently matches a different
    value, which is the accidental-hiding failure arriving through the check
    rather than through the mutation.
    """
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

    # The repository root is itself a project, and under a mutation this harness
    # really does write a generation ledger into it. Refuse to start if either
    # path is already occupied, rather than clearing it: these are not scratch
    # paths -- neither is gitignored, s011 is a real beat id, and a generation
    # ledger is the record of what was paid. Deleting one to make room for a
    # test artifact is the failure backend/generation.py's own docstring argues
    # against, and it is not a decision a harness may make unattended.
    occupied = [p for p in ROOT_ARTIFACTS if p.exists()]
    if occupied:
        print("refusing to run: these exist and are not this harness's to touch:")
        for p in occupied:
            print(f"  - {p.relative_to(ROOT)}")
        print("A generation ledger is a money record. Move them aside, confirm "
              "they are not real, and re-run.")
        return 1

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
        print(f"\n[{mut.gap}] {mut.name}")
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

        # And that the assertion which NAMES the defect is the one that ran.
        for phrase in mut.proves:
            shown = phrase in suite_out
            print(f"    assertion {'FIRED' if shown else 'NEVER RAN'}: {phrase!r}")
            if not shown:
                unproven.append(
                    f"{mut.name}: no failing assertion said {phrase!r} — the test "
                    f"died on a cheaper assertion first, so the defect was never "
                    f"demonstrated")

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

    # A mutated run really can write a ledger into the repository itself (see the
    # late-resolve mutation). The probe removes its own, but a crash between the
    # write and the cleanup would leave one behind, so the run says so rather
    # than leaving it to be found by `git status`.
    #
    # Safe to unlink only because every one of these paths was verified ABSENT
    # before the first suite ran: anything here now was written by this harness.
    # Ownership, not existence -- the check above is what earns this line.
    strays = reclaim_root_artifacts()
    if strays:
        dirty += [str(p.relative_to(ROOT)) + " (written by a mutated run, removed)"
                  for p in strays]

    ok, _ = run_suite()
    print(f"\nrestored suite: {'PASS' if ok else 'FAIL'}")
    print(f"mutations: {len(MUTATIONS)} defined, {len(survived)} survived, "
          f"{len(MUTATIONS) - len(survived)} killed")
    for p, n in sorted(_RECLAIMED.items(), key=lambda kv: str(kv[0])):
        print(f"  reclaimed {p.relative_to(ROOT)} {n}x — a mutated run wrote a "
              f"real project's records into the repository; removed, and only "
              f"because this run created them")
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

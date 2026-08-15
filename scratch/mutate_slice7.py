"""Mutation harness for V1 slice 7 — frozen export (§9.1, §9.2, §11.7).

    python scratch/mutate_slice7.py

Guardrails require both directions, and this slice is the one where the
distinction matters most, because a §11.7 defect leaves both artifacts present
and openable. So:

  * **a test that fails under a faithful mutation of the fix** — every mutation
    below must be killed. A survivor is a safeguard that is not actually
    protected by a test;
  * **a mutation that genuinely reproduces the defect it guards** — and the known
    failure mode there is a mutation that "passes" because it accidentally hid the
    bug it was restoring. A killed test is not evidence the defect came back; it is
    evidence *something* broke. So the mutations marked ``DEFECT`` also run a
    **probe**: a real export, executed under the mutation, whose output is checked
    for the defect's own signature. The probe must show the signature under the
    mutation and must NOT show it pristine. Both halves are asserted, not eyeballed.

Inherited from ``mutate_slice8.py`` and ``mutate_5b.py``, both learned the hard
way there: an anchor that matches more than one site stops the run rather than
mutating the first match, and the tree is verified byte-identical by hash at the
end, because a green suite is not evidence the tree is clean.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXPORTS = ROOT / "backend" / "exports.py"
BUNDLE = ROOT / "backend" / "bundle.py"


# --------------------------------------------------------------------------- #
# probes: a real export run under the mutation, reporting the defect's signature
# --------------------------------------------------------------------------- #

_PROBE_PRELUDE = """
import json, sys, tempfile, types
from pathlib import Path
import xml.etree.ElementTree as ET
sys.path.insert(0, r"__ROOT__")
for _m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(_m, types.ModuleType(_m))
from backend import config, exports, slots, timeline
from backend.manifest import Camera, Shot, Storyboard
from backend.manifest import save as save_manifest

tmp = Path(tempfile.mkdtemp())
config.MANIFEST_PATH = tmp / "storyboard_manifest.json"

def sb_of(*beats):
    return Storyboard(title="Frozen Cut", storyboard_approved=True, shots=[
        Shot(scene_id=b, narration="line " + b, approved=True,
             camera=Camera(move="static", duration=d)) for b, d in beats])

def renders(*ids):
    rd = tmp / "render"
    rd.mkdir(parents=True, exist_ok=True)
    for i in ids:
        (rd / (i + ".mp4")).write_bytes(b"\\0")

def spine(path):
    root = ET.parse(path).getroot().find(".//spine")
    return [(c.get("name"), c.get("offset"), c.get("duration")) for c in root]
"""

# The §11.7 probe. A beat is retimed 4s -> 20s and a third beat appears, in the
# window between the master render and the FCPXML write. The defect's signature is
# the retimed beat reaching the FCPXML: "20/1s" in the spine. A correct export
# cannot produce it, because it never looks at live state after the freeze.
PROBE_EQUIVALENCE = _PROBE_PRELUDE + """
sb = sb_of(("s001", 6.0), ("s002", 4.0))
renders("s001", "s002", "s003")

def fake_master(storyboard=None, render_dir=None, out=None, height=480):
    out = Path(out); out.parent.mkdir(parents=True, exist_ok=True)
    beats = [{"scene_id": s.scene_id, "duration": float(s.camera.duration)}
             for s in storyboard.shots]
    out.write_text(json.dumps({"beats": beats}), encoding="utf-8")
    sb.shots[1].camera.duration = 20.0
    sb.shots.append(Shot(scene_id="s003", narration="late",
                         camera=Camera(move="static", duration=9.0)))
    save_manifest(sb)
    return out, sum(b["duration"] for b in beats)

timeline.build_preview = fake_master
r = exports.run(sb, log=lambda m: None)
vdir = exports.version_dir(r["version"])
got = spine(vdir / (exports.STEM + ".fcpxml"))
master = json.loads((vdir / exports.MASTER_NAME).read_text(encoding="utf-8"))["beats"]
print("PROBE_SPINE=" + json.dumps(got))
print("PROBE_MASTER=" + json.dumps([(b["scene_id"], b["duration"]) for b in master]))
print("PROBE_FCPXML_SAW_LIVE_RETIME=" + ("true" if any(d == "20/1s" for _n, _o, d in got) else "false"))
print("PROBE_FCPXML_SAW_LATE_BEAT=" + ("true" if any(n == "s003" for n, _o, _d in got) else "false"))
"""

# §9.1's second sentence. Export v1, edit the cut, export again -- and report
# whether v1's master survived byte-identical. The defect is that it did not.
PROBE_OVERWRITE = _PROBE_PRELUDE + """
sb = sb_of(("s001", 6.0), ("s002", 4.0))
renders("s001", "s002")

def fake_master(storyboard=None, render_dir=None, out=None, height=480):
    out = Path(out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([[s.scene_id, float(s.camera.duration)]
                               for s in storyboard.shots]), encoding="utf-8")
    return out, 1.0

timeline.build_preview = fake_master
first = exports.run(sb, log=lambda m: None)
p1 = exports.version_dir(first["version"]) / exports.MASTER_NAME
before = p1.read_bytes()
sb.shots[1].camera.duration = 12.0
save_manifest(sb)
try:
    second = exports.run(sb, log=lambda m: None)
    second_version = second["version"]
except Exception as exc:
    second_version = "REFUSED:" + type(exc).__name__
after = p1.read_bytes() if p1.is_file() else b""
print("PROBE_FIRST_VERSION=" + first["version"])
print("PROBE_SECOND_VERSION=" + second_version)
print("PROBE_PRIOR_MASTER_OVERWRITTEN=" + ("true" if after != before else "false"))
print("PROBE_PRIOR_MASTER_NOW=" + after.decode("utf-8", "replace")[:80])
"""

# The migration constraint. A pre-snapshot FCPXML is entered in history; the
# defect is that its row carries provenance -- specifically the id of whatever
# state happens to be live now, which asserts an equivalence nobody recorded.
PROBE_LEGACY = _PROBE_PRELUDE + """
sb = sb_of(("s001", 6.0))
slug = config.episode_paths(sb.title)["slug"]
(tmp / (slug + ".fcpxml")).write_text("<fcpxml/>", encoding="utf-8")
rows = exports.record_legacy(sb)
live = exports.snapshot_id(exports._frozen_state(sb))
ids = [r["snapshot_id"] for r in rows]
print("PROBE_LEGACY_ROWS=" + json.dumps([(r["type"], r["status"], r["snapshot_id"]) for r in rows]))
print("PROBE_LIVE_STATE_ID=" + live)
print("PROBE_LEGACY_FABRICATED=" + ("true" if any(i for i in ids) else "false"))
print("PROBE_LEGACY_CLAIMS_LIVE_STATE=" + ("true" if live in ids else "false"))
"""

# §9.2 append-only. Two rows recorded; the defect is that only one survives.
PROBE_APPEND = _PROBE_PRELUDE + """
exports.record("v1", "master", "succeeded", snapshot="sha256:aaa")
exports.record("v1", "fcpxml", "succeeded", snapshot="sha256:aaa")
rows = exports.history()
print("PROBE_HISTORY_LEN=" + str(len(rows)))
print("PROBE_HISTORY_TYPES=" + json.dumps([r["type"] for r in rows]))
print("PROBE_FIRST_ROW_LOST=" + ("true" if not any(r["type"] == "master" for r in rows) else "false"))
"""

PROBES = {
    "equivalence": PROBE_EQUIVALENCE,
    "overwrite": PROBE_OVERWRITE,
    "legacy": PROBE_LEGACY,
    "append": PROBE_APPEND,
}


@dataclass
class Mutation:
    name: str
    clause: str
    removes: str                     # the behaviour this deletes, in one line
    edits: list[tuple[Path, str, str]]
    # Set for the mutations that restore a real defect. ``probe`` names the script
    # and ``signature`` is the line that must appear under the mutation and must
    # NOT appear pristine -- which is what distinguishes "the defect came back"
    # from "something broke".
    probe: str = ""
    signature: str = ""
    defect: bool = False
    expect: list[str] = field(default_factory=list)


# --- anchors --------------------------------------------------------------------

RESTORE_ONCE = "    frozen = restore(snapshot)"

BUILD_FROM_FROZEN = (
    "        otio_path, fcpxml_path, tl_runtime = timeline.build(\n"
    "            frozen, out_dir=vdir, out_stem=STEM)"
)

REFUSE_EXISTING = "    if vdir.exists():"
NEXT_VERSION = "    return f\"v{highest + 1}\""
APPEND_ROW = "        entries.append(entry)"
LEGACY_ROW = "            \"status\": \"legacy\",\n            \"snapshot_id\": \"\","
LEGACY_ARM = "    if str(version).startswith(\"legacy:\"):"
ID_COVERS_STATE = "    return digest({\"snapshot_version\": SNAPSHOT_VERSION, \"frozen\": frozen})"
FROZEN_SLOTS = "        \"timeline_slots\": [asdict(s) for s in slots.load()],"
NOW_DEF = "def _now() -> str:"
GRADE_PER_BEAT = "            \"beats\": {s.get(\"scene_id\"): s.get(\"grade\") for s in shots},"
# The three the review's own mutations found. Each field IS in the snapshot; what
# was missing was a test that fails when it is taken out again.
AGGREGATE_APPROVAL = "            \"storyboard_approved\": sb.get(\"storyboard_approved\"),"
SFX_LAYERS_IN_AUDIO = "                \"sfx_layers\": s.get(\"sfx_layers\"),"
TIMELINE_FULL_SLOTS = "        \"timeline\": digest(slots),"
PLAN_VERSION_IN_DIGEST = (
    "        \"director_plan\": digest({\n"
    "            \"plan_version\": frozen.get(\"director_plan_version\"),\n"
    "            \"plans\": plans,\n"
    "        }),"
)
SCRIPT_NARRATION = "                \"narration\": s.get(\"narration\"),"
MISSING_XML_RECORDED = (
    "        record(version, \"fcpxml\", \"failed\", preset=preset, snapshot=sid,\n"
    "               note=\"the fcpx_xml adapter failed; the .otio is still valid\")"
)
VERSION_VALIDATION = "    if not _VERSION_RE.match(v):"
BUNDLE_NO_VERSION = "    if not version:"
RESTORE_SHOTS = (
    "    sb = Storyboard.from_dict(project_id, sb_dict, sb_dict.get(\"shots\") or [])"
)
RECORD_FAILURE_GUARD = (
    "    try:\n"
    "        record(version, kind, \"failed\", preset=preset, snapshot=sid,\n"
    "               note=f\"{type(exc).__name__}: {exc}\")\n"
    "    except Exception as write_exc:  # noqa: BLE001 — must not mask `exc`\n"
    "        print(f\"export: FAILED to record the {kind} failure for {version} \"\n"
    "              f\"({write_exc}); the export error itself was: {exc}\")"
)


MUTATIONS = [
    # ---- reproductions of a real defect ----------------------------------------
    Mutation(
        "DEFECT §11.7: both deliverables come from live state, not the snapshot",
        "§11.7",
        "the single restore() seam — nothing is produced from the snapshot at all",
        [(EXPORTS, RESTORE_ONCE, "    frozen = sb  # MUTANT: live state")],
        probe="equivalence", signature="PROBE_FCPXML_SAW_LIVE_RETIME=true",
        defect=True,
        expect=["describe_the_frozen_cut_when_live_state_changes",
                "reconstructed_exactly_once"],
    ),
    Mutation(
        "DEFECT §11.7: the FCPXML is regenerated from live state after the master",
        "§11.7",
        "the FCPXML's binding to the snapshot — this is the precise 'refreshed "
        "between them' case §11.7 names, and the shape of the pre-slice-7 route",
        [(EXPORTS, BUILD_FROM_FROZEN,
          "        from .manifest import load as manifest_load\n"
          "        otio_path, fcpxml_path, tl_runtime = timeline.build(\n"
          "            manifest_load(), out_dir=vdir, out_stem=STEM)")],
        probe="equivalence", signature="PROBE_FCPXML_SAW_LATE_BEAT=true",
        defect=True,
        expect=["a_manifest_rewritten_mid_export"],
    ),
    Mutation(
        "DEFECT §9.1: a later edit overwrites the prior final master",
        "§9.1",
        "both halves of the no-overwrite guarantee at once — version numbering "
        "stops advancing AND freeze stops refusing an existing version",
        [(EXPORTS, NEXT_VERSION, "    return \"v1\"  # MUTANT"),
         (EXPORTS, REFUSE_EXISTING, "    if False:  # MUTANT")],
        probe="overwrite", signature="PROBE_PRIOR_MASTER_OVERWRITTEN=true",
        defect=True,
        expect=["a_later_edit_creates_a_new_version"],
    ),
    Mutation(
        "DEFECT §9.2: history records the present instead of what happened",
        "§9.2",
        "the append — each row replaces the file rather than being added to it",
        [(EXPORTS, APPEND_ROW, "        entries = [entry]  # MUTANT")],
        probe="append", signature="PROBE_HISTORY_LEN=1",
        defect=True,
        expect=["history_is_append_only"],
    ),
    Mutation(
        "DEFECT migration: a legacy export is given fabricated provenance",
        "§9.1",
        "the refusal to invent a snapshot for an artifact nobody recorded one for",
        [(EXPORTS, LEGACY_ROW,
          "            \"status\": \"succeeded\",\n"
          "            \"snapshot_id\": snapshot_id(_frozen_state(sb)),")],
        probe="legacy", signature="PROBE_LEGACY_CLAIMS_LIVE_STATE=true",
        defect=True,
        expect=["recorded_legacy_with_no_snapshot_id"],
    ),

    # ---- faithful mutations of the fix ------------------------------------------
    Mutation(
        "freeze stops refusing a version that already exists", "§9.1",
        "the only place a prior master can be protected — the export can now be "
        "pointed at a directory that already holds one",
        [(EXPORTS, REFUSE_EXISTING, "    if False:  # MUTANT")],
        expect=["freeze_refuses_a_version_that_already_exists"],
    ),
    Mutation(
        "the snapshot id stops covering the frozen state", "§9.1",
        "the id's whole purpose: it becomes a constant, so it identifies the "
        "schema version and nothing about the cut",
        [(EXPORTS, ID_COVERS_STATE,
          "    return digest({\"snapshot_version\": SNAPSHOT_VERSION})  # MUTANT")],
        expect=["a_snapshot_edited_after_the_fact", "a_later_edit_creates_a_new_version"],
    ),
    Mutation(
        "an unreadable cut is frozen as an empty one", "§9.1",
        "the loud failure — a busy or corrupt slot file now yields a snapshot "
        "asserting a timeline nobody approved",
        [(EXPORTS, FROZEN_SLOTS, "        \"timeline_slots\": _tolerant_slots(slots),"),
         (EXPORTS, NOW_DEF,
          "def _tolerant_slots(slots):  # MUTANT\n"
          "    try:\n"
          "        return [asdict(s) for s in slots.load()]\n"
          "    except Exception:\n"
          "        return []\n"
          "\n"
          "\n"
          "def _now() -> str:")],
        expect=["an_unreadable_cut_stops_the_export"],
    ),
    Mutation(
        "the grade digest stops covering per-beat grade overrides", "§9.1",
        "coverage without breaking self-consistency — verify() still passes, and "
        "the digest no longer identifies the state §9.1 says it does",
        [(EXPORTS, GRADE_PER_BEAT, "            \"beats\": {},  # MUTANT")],
        expect=["each_state_digest_changes_when_its_own_state_changes"],
    ),
    Mutation(
        "the script/timing digest stops covering the narration text", "§9.1",
        "the same coverage gap on a different §9.1 item: a rewritten line at the "
        "same length no longer moves the snapshot",
        [(EXPORTS, SCRIPT_NARRATION, "                \"narration\": \"\",  # MUTANT")],
        expect=["each_state_digest_changes_when_its_own_state_changes"],
    ),
    Mutation(
        "an FCPXML the adapter could not write is silently not recorded", "§9.2",
        "the row that stops an export missing a required deliverable from reading "
        "as complete",
        [(EXPORTS, MISSING_XML_RECORDED,
          "        pass  # MUTANT: not recorded at all")],
        expect=["an_fcpxml_the_adapter_could_not_write_is_recorded_failed"],
    ),
    Mutation(
        "verification stops recognising a legacy row", "§9.1",
        "the honest answer for an unverifiable export — it now reports a missing "
        "snapshot as though it were a fault rather than the recorded truth",
        [(EXPORTS, LEGACY_ARM, "    if False:  # MUTANT")],
        expect=["no_snapshot_is_fabricated_for_a_legacy_export"],
    ),
    Mutation(
        "a version name is no longer validated before it reaches the filesystem",
        "§9.1",
        "containment: an export version can now be written outside exports/",
        [(EXPORTS, VERSION_VALIDATION, "    if False:  # MUTANT")],
        expect=["a_version_name_cannot_escape_the_exports_directory"],
    ),
    # --- the three the review found, now guarded -------------------------------
    #
    # These are not hypothetical shapes. The reviewer wrote them, they SURVIVED
    # the suite as it stood, and they were filed as test gaps rather than
    # findings, because the fields were in the snapshot the whole time and only
    # the guard was missing. They are kept here so removing one is caught the
    # next time rather than the round after.
    Mutation(
        "aggregate storyboard approval is dropped from the snapshot", "§9.1",
        "the episode-level approval — per-beat flags still move the digest, so a "
        "storyboard un-approved as a whole freezes as though nothing changed",
        [(EXPORTS, AGGREGATE_APPROVAL,
          "            \"storyboard_approved\": None,  # MUTANT")],
        expect=["each_state_digest_changes_when_its_own_state_changes"],
    ),
    Mutation(
        "layered SFX is dropped from the audio digest", "§9.1",
        "every layer of ambience on every beat — the bus levels still move the "
        "digest, so the loss hides behind the part that still works",
        [(EXPORTS, SFX_LAYERS_IN_AUDIO,
          "                \"sfx_layers\": None,  # MUTANT")],
        expect=["each_state_digest_changes_when_its_own_state_changes"],
    ),
    Mutation(
        "the timeline digest is reduced to slot ids", "§9.1",
        "everything a slot CONTAINS — trims, media, source attempt — while the "
        "digest still moves whenever the cut gains or loses a slot, which is why "
        "a one-change-per-key test could not see this",
        [(EXPORTS, TIMELINE_FULL_SLOTS,
          "        \"timeline\": digest([s.get(\"id\") for s in slots]),  # MUTANT")],
        expect=["each_state_digest_changes_when_its_own_state_changes"],
    ),
    Mutation(
        "the Director digest stops covering the plan schema version", "§9.1",
        "the literal words of §9.1 — 'Director plan version' — a schema bump now "
        "moves under a snapshot without the snapshot noticing",
        [(EXPORTS, PLAN_VERSION_IN_DIGEST,
          "        \"director_plan\": digest(plans),  # MUTANT")],
        expect=["director_plan_digest_covers_the_plan_schema_version"],
    ),
    Mutation(
        "restoring a snapshot silently drops per-beat grade", "§9.1",
        "the losslessness the binding rests on — the deliverables are produced "
        "from the RESTORED object, so a field the digests cover but restore "
        "defaults leaves the master describing something the snapshot does not",
        [(EXPORTS, RESTORE_SHOTS,
          "    for _s in (sb_dict.get(\"shots\") or []):  # MUTANT\n"
          "        _s.pop(\"grade\", None)\n"
          "    sb = Storyboard.from_dict(project_id, sb_dict, sb_dict.get(\"shots\") or [])")],
        expect=["restoring_a_snapshot_loses_nothing"],
    ),
    Mutation(
        "a failed history write replaces the export error that caused it", "§9.2",
        "the guard that keeps the real reason an export failed reachable — the "
        "operator now debugs the history write instead",
        [(EXPORTS, RECORD_FAILURE_GUARD,
          "    record(version, kind, \"failed\", preset=preset, snapshot=sid,  # MUTANT\n"
          "           note=f\"{type(exc).__name__}: {exc}\")")],
        expect=["a_history_write_that_fails_does_not_replace"],
    ),
    Mutation(
        "a bundle with no frozen export claims one anyway", "§9.2",
        "the honest 'provenance unverifiable' line — the ZIP a human carries to "
        "Resolve stops saying it cannot be verified",
        [(BUNDLE, BUNDLE_NO_VERSION, "    if False:  # MUTANT")],
        expect=["a_bundle_with_no_frozen_export_says_so"],
    ),
]


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #

def run_suite() -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def run_probe(name: str) -> tuple[bool, str]:
    # A token replace, not str.format: these scripts are full of JSON braces and
    # dict literals, and format() reads the first `{"scene_id"` as a field name.
    script = PROBES[name].replace("__ROOT__", str(ROOT))
    proc = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out


def probe_lines(out: str) -> list[str]:
    return [ln.strip() for ln in out.splitlines() if ln.strip().startswith("PROBE_")]


def failing_tests(out: str) -> list[str]:
    return sorted({line.split(" ", 1)[1].strip()
                   for line in out.splitlines() if line.startswith("FAILED ")})


def _read(path: Path) -> str:
    """Read without translating line endings.

    ``Path.read_text`` maps CRLF to LF and ``write_text`` maps it back to
    ``os.linesep``, so a read/write round trip REWRITES an LF-only file as CRLF
    on Windows. The first run of this harness restored ``exports.py`` that way and
    the byte check correctly reported the tree as not restored — restoration has
    to be byte-exact or the check is measuring the harness, not the mutations.
    """
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def apply(path: Path, find: str, replace: str) -> None:
    """One edit, refusing anything but an exactly-one-site anchor.

    ``str.replace`` silently takes the first match, so an ambiguous anchor
    mutates a site the run then reports results about with total confidence. That
    is worse than a survivor, because a survivor is at least visible as a gap.

    Anchors are written with plain ``\\n``, so on a CRLF file the search text is
    normalised to the file's own line endings before matching — otherwise every
    multi-line anchor is a spurious "anchor not found".
    """
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
    touched = sorted({p for m in MUTATIONS for p, _, _ in m.edits})
    pristine = {p: _read(p) for p in touched}
    before = digest(touched)

    ok, out = run_suite()
    print(f"baseline suite: {'PASS' if ok else 'FAIL'}")
    if not ok:
        print(out[-4000:])
        return 1

    # Every probe, pristine. The signature must be ABSENT here -- that is the
    # half that says the probe measures the defect rather than measuring noise.
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

    survived, unproven = [], []
    for mut in MUTATIONS:
        snapshot = {p: _read(p) for p, _, _ in mut.edits}
        try:
            for path, find, replace in mut.edits:
                apply(path, find, replace)
            suite_ok, suite_out = run_suite()
            probe_out = run_probe(mut.probe)[1] if mut.probe else ""
        finally:
            for path, text in snapshot.items():
                _write(path, text)

        killed = not suite_ok
        print(f"\n[{mut.clause}] {mut.name}")
        print(f"    removes: {mut.removes}")
        print(f"    suite: {'killed' if killed else 'SURVIVED'}")
        failed = failing_tests(suite_out)
        for t in failed[:8]:
            print(f"      x {t}")
        if len(failed) > 8:
            print(f"      ... and {len(failed) - 8} more")
        if not killed:
            survived.append(mut.name)

        # A named expectation is how a mutation is tied to the test that is
        # supposed to catch it. A mutation killed only by collateral damage
        # elsewhere is not evidence about this safeguard.
        for want in mut.expect:
            if not any(want in t for t in failed):
                unproven.append(f"{mut.name}: expected {want} to fail")

        if mut.probe:
            print(f"    probe {mut.probe}:")
            for ln in probe_lines(probe_out):
                print(f"      {ln}")
            if mut.defect:
                back = mut.signature in probe_out
                clean = mut.signature in clean_probes[mut.probe]
                print(f"    defect restored: {back} "
                      f"(signature {mut.signature!r}); present pristine: {clean}")
                if not back:
                    unproven.append(
                        f"{mut.name}: probe did NOT show the defect "
                        f"({mut.signature}) — the mutation may have hidden the "
                        f"bug it was restoring")
                if clean:
                    unproven.append(
                        f"{mut.name}: the signature {mut.signature} is present "
                        f"PRISTINE — the probe does not measure this defect")

    after = digest(touched)
    dirty = [p.name for p in touched if before[p] != after[p]]
    for p in touched:
        if before[p] != after[p]:
            _write(p, pristine[p])

    ok, _ = run_suite()
    print(f"\nrestored suite: {'PASS' if ok else 'FAIL'}")
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

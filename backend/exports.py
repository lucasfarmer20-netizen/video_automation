"""Frozen export: one immutable snapshot, and both deliverables derived from it.

Contract §9.1, §9.2 and §11.7. The invariant §11.7 states is short --

    Final master and FCPXML must reflect the same frozen timeline state.

-- and the reason it is called critical is that when it breaks, nothing looks
broken. Both files exist, both open, both play. The deliverable simply is not the
cut that was reviewed, and the only way to find out is to watch the whole thing
next to the approval it was supposedly given.

There are two ways to satisfy that clause and they are not equally strong.

The *procedural* way is to render the master, then write the FCPXML, and be
careful not to let anything change in between. That holds until someone adds a
refresh, a retry, a second entry point, or regenerates one artifact months later
from live state -- at which point the guarantee is gone and no test fails,
because the promise was never encoded anywhere.

The *structural* way is the one implemented here. Export freezes the state to a
snapshot, reconstructs a ``Storyboard`` from that snapshot **once**, and hands the
same object to the master renderer and to the FCPXML writer. Live state is not
read again after the freeze. Equivalence is then not a thing anybody has to
maintain: there is no code path that derives either deliverable from anything but
the snapshot, so the two cannot disagree without the snapshot itself being wrong.

Four decisions were settled before this was written (roadmap human gate 2 --
"decide it, do not discover it") and are implemented exactly as decided:

1. **FCPXML is generated FROM the snapshot at export time.** Not stored beside
   the master, not regenerated from live state. Two stored artifacts can drift.
2. **What is frozen is the state §9.1 enumerates**, not a rendering of it:
   project version, script/timing, Director plan version, approved shot state,
   selected outputs, timeline state, audio state, grade state.
3. **JSON under ``exports/<version>/``, through ``backend.atomic``.** Durability
   is not re-implemented here; ``atomic`` exists because a unique temp per write,
   a per-destination lock and bounded retry on the replace were each learned the
   hard way, and a denied replace is a submitted write silently lost.
4. **Export history is append-only**, retaining version, type, preset, timestamp,
   status and snapshot id per §9.2. A later edit makes a NEW version; a prior
   master is never overwritten, because the path it lives at is never targeted a
   second time.

What the snapshot deliberately does NOT freeze: the media bytes. Freezing the
state means both deliverables describe the same timeline -- the same beats, the
same durations, the same selected takes at the same paths. If a file on disk is
replaced underneath both of them, both still describe the frozen cut and the
pixels are whatever is on disk. That is the boundary of this clause, stated here
so nothing downstream assumes a stronger claim than the code makes.

What is NOT verifiable, and is recorded as such: exports made before snapshots
existed. They are entered in history with ``status="legacy"`` and an empty
``snapshot_id``, and ``verify()`` reports them unverifiable. There is deliberately
no code path that can produce a snapshot for an artifact it did not freeze --
fabricating one would assert an equivalence nobody recorded, which is the exact
failure this module exists to prevent.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from . import atomic, config
from .manifest import Storyboard

SNAPSHOT_VERSION = 1

SNAPSHOT_NAME = "snapshot.json"
HISTORY_NAME = "history.json"
MASTER_NAME = "master.mp4"
STEM = "master"

# The master is a real deliverable, not the 480p review proxy. Same server-side
# compositor either way: CLAUDE.md is explicit that there is deliberately no
# second compositor, because a second one drifts from the real renderer and
# cannot reproduce the depth-warp parallax.
MASTER_HEIGHT = 720

# A version name reaches the filesystem, and it arrives over HTTP. Anchored, no
# separators, no leading dot -- so no traversal, no absolute path, no hidden file.
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# The eight things §9.1 says a snapshot must identify. Held as a tuple so the
# set is a decision on the record rather than whatever _state_digests happens to
# return: a test asserts these exact keys are present in every snapshot, so
# dropping one fails rather than quietly narrowing what "frozen" means.
STATE_KEYS = (
    "project_version",
    "script_timing",
    "director_plan",
    "approved_shots",
    "selected_outputs",
    "timeline",
    "audio",
    "grade",
)


class ExportError(RuntimeError):
    """An export that must not proceed. Raised before anything is written."""


# --- paths ----------------------------------------------------------------------

def exports_dir() -> Path:
    return config.project_dir() / "exports"


def _check_version(version: str) -> str:
    v = str(version or "").strip()
    if not _VERSION_RE.match(v):
        raise ExportError(
            f"invalid export version {version!r}: expected letters, digits, "
            f"dot, dash or underscore, starting with a letter or digit"
        )
    return v


def version_dir(version: str) -> Path:
    return exports_dir() / _check_version(version)


def snapshot_path(version: str) -> Path:
    return version_dir(version) / SNAPSHOT_NAME


def history_path() -> Path:
    return exports_dir() / HISTORY_NAME


def next_version() -> str:
    """The next unused ``v<N>``.

    Reads BOTH the directories on disk and the history, and takes the highest it
    finds anywhere. Either source alone leaves a hole: a directory with no
    history entry (a crash between mkdir and the append) would be handed out
    again and overwrite a master, and a history entry whose directory was moved
    away would reuse a version name that history already claims belongs to
    something else.
    """
    highest = 0
    for entry in _read_history():
        m = re.fullmatch(r"v(\d+)", str(entry.get("version") or ""))
        if m:
            highest = max(highest, int(m.group(1)))
    root = exports_dir()
    if root.is_dir():
        for child in root.iterdir():
            m = re.fullmatch(r"v(\d+)", child.name)
            if m and child.is_dir():
                highest = max(highest, int(m.group(1)))
    return f"v{highest + 1}"


# --- digests --------------------------------------------------------------------

def _canonical(payload) -> str:
    """One encoding, everywhere.

    Sorted keys and fixed separators, so a digest computed at freeze time can
    never be compared against one computed a different way at verify time. The
    same discipline as ``director.SIGNATURE_VERSION`` guards one level down.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, default=str)


def digest(payload) -> str:
    return "sha256:" + hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


# --- what gets frozen -----------------------------------------------------------

def _state_digests(frozen: dict) -> dict:
    """A digest per §9.1 item, computed from the stored frozen state.

    The preimages are NOT stored alongside the digests. That is the point: each
    digest is recomputed at verify time from ``frozen``, the same state
    ``restore()`` rebuilds the export from, so a digest can only agree if it
    really does describe what the export was produced from. Storing the preimage
    too would be a second copy of the state, and two copies of anything in this
    codebase eventually disagree.
    """
    sb = frozen["storyboard"]
    project = frozen["project"]
    plans = frozen["director_plans"]
    slots = frozen["timeline_slots"]
    shots = sb.get("shots") or []

    def cam(shot: dict, key: str, default=None):
        return (shot.get("camera") or {}).get(key, default)

    return {
        "project_version": digest(project),
        "script_timing": digest({
            "script_locked": sb.get("script_locked"),
            "beats": [{
                "scene_id": s.get("scene_id"),
                "narration": s.get("narration"),
                "duration": cam(s, "duration"),
                "duration_locked": cam(s, "duration_locked"),
            } for s in shots],
        }),
        # §9.1 names "Director plan version", so the schema version is inside the
        # digest and not merely stored beside it. Each plan's own `version` rides
        # along in `plans`; PLAN_VERSION is the one that would otherwise move
        # under a snapshot without the snapshot noticing.
        "director_plan": digest({
            "plan_version": frozen.get("director_plan_version"),
            "plans": plans,
        }),
        "approved_shots": digest({
            "storyboard_approved": sb.get("storyboard_approved"),
            "beats": [{"scene_id": s.get("scene_id"),
                       "approved": s.get("approved"),
                       "motion_type": s.get("motion_type")} for s in shots],
        }),
        "selected_outputs": digest([{
            "scene_id": s.get("scene_id"),
            "chosen_variation": s.get("chosen_variation"),
            "draft_image": s.get("draft_image"),
            "video_clip": s.get("video_clip"),
        } for s in shots]),
        "timeline": digest(slots),
        "audio": digest({
            "music_track": sb.get("music_track"),
            "mix": sb.get("mix"),
            "voice_id": sb.get("voice_id"),
            "vo_profile": sb.get("vo_profile"),
            "narrator_name": sb.get("narrator_name"),
            "beats": [{
                "scene_id": s.get("scene_id"),
                "offset_narration": s.get("offset_narration"),
                "fade_in_narration": s.get("fade_in_narration"),
                "fade_out_narration": s.get("fade_out_narration"),
                "gain_narration": s.get("gain_narration"),
                "gain_sfx": s.get("gain_sfx"),
                "sfx": s.get("sfx"),
                "sfx_layers": s.get("sfx_layers"),
            } for s in shots],
        }),
        "grade": digest({
            "episode": sb.get("grade"),
            "beats": {s.get("scene_id"): s.get("grade") for s in shots},
        }),
    }


def _frozen_state(sb: Storyboard) -> dict:
    """The authoritative state this export is bound to, in restorable form.

    Read from the stores that OWN each piece rather than re-derived: Director
    plans from ``director/*.json``, the cut from ``timeline_slots.json``. A
    re-derivation would freeze what this module thinks those stages decided,
    which is not the same claim.

    An unreadable slot file raises rather than freezing an empty cut. ``slots``
    already makes that argument for its own callers -- a cut that cannot be read
    is not an empty cut -- and it is stronger here: an export that froze the cut
    as empty because the file was busy would produce a snapshot asserting a
    timeline nobody ever approved.
    """
    from . import director, slots

    sb_dict = sb.to_dict()
    sb_dict["shots"] = [asdict(s) for s in sb.shots]
    for s in sb_dict["shots"]:
        mt = s.get("motion_type")
        s["motion_type"] = getattr(mt, "value", mt)

    plans: dict = {}
    for beat in sb.shots:
        plan = director.load_plan(beat.scene_id)
        if plan is None:
            continue
        plans[beat.scene_id] = {
            "version": plan.version,
            "plan_id": plan.plan_id,
            "status": plan.status,
            "beat_signature": plan.beat_signature,
            "approved_signature": plan.approved_signature,
            "approved_at": plan.approved_at,
            "plan_signature": director.plan_signature(plan),
            "coverage": [{
                "id": ds.id,
                "motion_type": ds.motion_type,
                "duration": ds.duration,
                "clip": ds.clip,
                "selected_attempt": ds.selected_attempt,
                "chosen_variation": ds.chosen_variation,
            } for ds in plan.coverage],
        }

    return {
        "project": {
            "id": sb.id,
            "title": sb.title,
            "channel": sb.channel,
            "cultural_origin": sb.cultural_origin,
            "manifest_version": sb.version,
        },
        "storyboard": sb_dict,
        "director_plan_version": director.PLAN_VERSION,
        "director_plans": plans,
        "timeline_slots": [asdict(s) for s in slots.load()],
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --- freeze / restore -----------------------------------------------------------

def freeze(sb: Storyboard, *, version: str, preset: str = "",
           label: str = "") -> dict:
    """Write the immutable snapshot for one export version, and return it.

    Refuses a version whose directory already exists. That refusal is §9.1's
    "do not overwrite a prior final master", enforced at the only place that can
    enforce it: an export cannot overwrite a master if it can never be pointed at
    a directory that already holds one. Merging into an existing version instead
    would leave a directory whose snapshot describes one cut and whose master is
    another -- the §11.7 failure, produced by the very thing meant to prevent it.
    """
    version = _check_version(version)
    vdir = version_dir(version)
    if vdir.exists():
        raise ExportError(
            f"export version {version!r} already exists at {vdir.name}; a later "
            f"edit must create a NEW version (§9.1) -- a prior master is never "
            f"overwritten"
        )

    frozen = _frozen_state(sb)
    snapshot = {
        "snapshot_version": SNAPSHOT_VERSION,
        "snapshot_id": snapshot_id(frozen),
        "export_version": version,
        "label": label or f"{sb.title or 'Untitled'} Master {version}",
        "preset": preset,
        "created_at": _now(),
        "frozen": frozen,
        "state": _state_digests(frozen),
    }
    vdir.mkdir(parents=True, exist_ok=True)
    atomic.write_json(snapshot_path(version), snapshot)
    return snapshot


def snapshot_id(frozen: dict) -> str:
    """The identifier for a frozen state.

    A content digest, so it names the STATE and not the file: two exports of
    genuinely unchanged state share an id, which is true and worth being able to
    see. What distinguishes the two exports is the version, which is what history
    keys on. The alternative -- a random or timestamped id -- would be unique and
    would tell you nothing, and could not be checked against anything.
    """
    return digest({"snapshot_version": SNAPSHOT_VERSION, "frozen": frozen})


def load_snapshot(version: str) -> dict:
    p = snapshot_path(version)
    if not p.is_file():
        raise ExportError(f"no snapshot for export version {version!r}")
    return atomic.read_json(p)


def restore(snapshot: dict) -> Storyboard:
    """Rebuild the Storyboard this export is bound to, from the snapshot alone.

    The single seam that makes §11.7 structural. Every deliverable is produced
    from the object this returns, and ``run()`` calls it once -- so "the master
    and the FCPXML came from the same frozen state" is true by construction
    rather than by two call sites agreeing to be careful.
    """
    frozen = snapshot.get("frozen") or {}
    sb_dict = frozen.get("storyboard") or {}
    project_id = (frozen.get("project") or {}).get("id") or ""
    sb = Storyboard.from_dict(project_id, sb_dict, sb_dict.get("shots") or [])
    return sb


def verify(snapshot: dict) -> dict:
    """Recompute every digest from the stored state. Cheap, and it is the check.

    A snapshot that merely *claims* to identify eight things is worth nothing; a
    reader has to be able to establish that the claim is true of the state the
    export was actually produced from. So both the id and each §9.1 digest are
    recomputed here from ``frozen`` -- the same section ``restore()`` rebuilds
    from -- and every mismatch is named rather than summarised.
    """
    frozen = snapshot.get("frozen") or {}
    stored = snapshot.get("state") or {}
    mismatched = []
    if not frozen:
        return {"ok": False, "verifiable": False,
                "reason": "no frozen state recorded", "mismatched": []}

    want_id = snapshot_id(frozen)
    if snapshot.get("snapshot_id") != want_id:
        mismatched.append("snapshot_id")

    fresh = _state_digests(frozen)
    for key in STATE_KEYS:
        if key not in stored:
            mismatched.append(f"{key} (absent)")
        elif stored[key] != fresh[key]:
            mismatched.append(key)

    return {
        "ok": not mismatched,
        "verifiable": True,
        "snapshot_id": snapshot.get("snapshot_id", ""),
        "mismatched": mismatched,
        "reason": "" if not mismatched
                  else f"{len(mismatched)} part(s) do not match the frozen state",
    }


# --- history --------------------------------------------------------------------

def _read_history() -> list[dict]:
    p = history_path()
    if not p.is_file():
        return []
    raw = atomic.read_json(p)
    entries = raw.get("exports") if isinstance(raw, dict) else raw
    return list(entries or [])


# Guards the read-modify-write pair below, and nothing else.
#
# It is deliberately NOT ``atomic.lock_for(history_path())``. That lock is a
# plain threading.Lock and ``write_json`` takes it too, so holding it across the
# write would deadlock on our own lock -- and the alternative, inlining the write
# here, would be a second copy of the durability rules, which decision 3 exists
# to forbid. A separate lock composes: this one serialises the pair, ``atomic``
# keeps owning the write.
#
# ``atomic`` is explicit that it does not prevent lost updates, and the
# guardrails list lost-update prevention as out of scope. This closes the
# in-process case, which is the reachable one -- two appends from two request
# threads. Two Cloud Run instances over one GCS mount remain out of reach, as
# documented there; that limit is not silently narrowed here.
_APPEND_LOCK = threading.Lock()


def _append(entry: dict) -> dict:
    """Append one row. Append-only, and that is the whole contract of this file.

    Never mutates an existing row. A status is a second row, not an edit of the
    first: history is a record of what happened, and a row rewritten in place
    turns it into a record of the present, at which point a crash between the two
    writes leaves no trace instead of a ``started`` with no terminal row.
    """
    with _APPEND_LOCK:
        entries = _read_history()
        entries.append(entry)
        atomic.write_json(history_path(), {"exports": entries})
    return entry


def record(version: str, kind: str, status: str, *, preset: str = "",
           snapshot: str = "", path: str = "", note: str = "") -> dict:
    """One §9.2 history row: version, type, preset, timestamp, status, snapshot id."""
    return _append({
        "version": version,
        "type": kind,
        "preset": preset,
        "timestamp": _now(),
        "status": status,
        "snapshot_id": snapshot,
        "path": path,
        "note": note,
    })


def _record_failure(version: str, kind: str, preset: str, sid: str,
                    exc: BaseException) -> None:
    """Record a failed deliverable without ever replacing the reason it failed.

    ``record`` writes to disk, so it can fail too -- and on a failure path that
    matters more than usual: raising here would surface a history-write error in
    place of the export error that actually happened, and the operator would
    debug the wrong thing. The original exception is what the caller re-raises;
    a history write that cannot complete is reported to stdout and nothing else.
    """
    try:
        record(version, kind, "failed", preset=preset, snapshot=sid,
               note=f"{type(exc).__name__}: {exc}")
    except Exception as write_exc:  # noqa: BLE001 — must not mask `exc`
        print(f"export: FAILED to record the {kind} failure for {version} "
              f"({write_exc}); the export error itself was: {exc}")


def history() -> list[dict]:
    """Every recorded export, oldest first, exactly as recorded."""
    return _read_history()


def record_legacy(sb: Storyboard) -> list[dict]:
    """Enter pre-snapshot deliverables as legacy/unverifiable. Idempotent.

    These are exports whose provenance nobody recorded, and the approved
    migration constraint is unambiguous: never fabricate a snapshot for them.
    So each is entered with ``status="legacy"`` and ``snapshot_id=""`` -- present
    in history, honestly labelled, and reported unverifiable by
    ``verification()``. Manufacturing an id from whatever state happens to be
    live now would assert that this artifact came from that state, which is
    precisely the equivalence claim nobody is in a position to make.

    Idempotent by version key, so calling it on every history read does not grow
    the file. Append-only is preserved: an already-recorded artifact is skipped,
    never rewritten.
    """
    slug = config.episode_paths(sb.title)["slug"]
    proj = config.project_dir()
    candidates = [
        (proj / f"{slug}.fcpxml", "fcpxml"),
        (proj / f"{slug}.otio", "otio"),
        (proj / f"{slug}_bundle.zip", "bundle"),
    ]
    known = {str(e.get("version") or "") for e in _read_history()}
    added = []
    for path, kind in candidates:
        if not path.is_file():
            continue
        version = f"legacy:{path.name}"
        if version in known:
            continue
        added.append(_append({
            "version": version,
            "type": kind,
            "preset": "",
            "timestamp": datetime.fromtimestamp(
                path.stat().st_mtime, timezone.utc
            ).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "status": "legacy",
            "snapshot_id": "",
            "path": config.rel_media_path(path),
            "note": "predates export snapshots; provenance unverifiable",
        }))
        known.add(version)
    return added


def verification(version: str) -> dict:
    """Whether one export version's deliverables can be checked against a snapshot."""
    if str(version).startswith("legacy:"):
        return {"version": version, "ok": False, "verifiable": False,
                "reason": "no snapshot provenance recorded; legacy export",
                "mismatched": []}
    try:
        snapshot = load_snapshot(version)
    except (ExportError, OSError, ValueError) as exc:
        return {"version": version, "ok": False, "verifiable": False,
                "reason": f"snapshot unreadable: {exc}", "mismatched": []}
    result = verify(snapshot)
    result["version"] = version
    return result


# --- the export itself ----------------------------------------------------------

def _render_master(sb: Storyboard, dest: Path, log=print) -> tuple[Path, float]:
    """Render the master from ``sb`` and nothing else.

    ``timeline.build_preview`` is the server-side compositor, used here at full
    height with an explicit destination. It is deliberately the same one the
    review proxy uses: CLAUDE.md states there is no second compositor because a
    second one drifts from the real renderer and cannot reproduce the depth-warp
    parallax, and a master produced by a different mixer than the one the
    reviewer watched would break the equivalence this module exists to hold.
    """
    from . import timeline

    dest.parent.mkdir(parents=True, exist_ok=True)
    out, runtime = timeline.build_preview(sb, out=dest, height=MASTER_HEIGHT)
    log(f"export: master rendered ({runtime:.1f}s runtime) -> {Path(out).name}")
    return Path(out), runtime


def run(sb: Storyboard, *, preset: str = "", label: str = "",
        version: str | None = None, log=print) -> dict:
    """Freeze, then produce every deliverable from the frozen state.

    The ordering is the design, not an implementation detail:

    1. freeze the live state to ``exports/<version>/snapshot.json``;
    2. reconstruct the Storyboard from that snapshot, **once**;
    3. render the master from it;
    4. write the OTIO + FCPXML from the *same object*.

    ``sb`` is not read after step 1. That is what makes the §11.7 test possible:
    live state can be mutated between steps 3 and 4 and both deliverables still
    describe the frozen cut, because neither of them is looking at live state.

    A failure in step 3 or 4 appends a ``failed`` row and re-raises. It does not
    retry, and it does not fall back to live state -- a deliverable produced from
    something other than the snapshot is the defect, so the honest outcome of a
    broken export is a missing artifact and a recorded failure, never an
    unverifiable one.
    """
    version = _check_version(version or next_version())
    snapshot = freeze(sb, version=version, preset=preset, label=label)
    sid = snapshot["snapshot_id"]
    vdir = version_dir(version)
    record(version, "snapshot", "started", preset=preset, snapshot=sid,
           path=config.rel_media_path(snapshot_path(version)))
    log(f"export: froze {version} ({snapshot['label']}) as {sid[:19]}...")

    # ONE reconstruction. Both deliverables below come from this object and
    # nothing re-reads `sb`. Two calls to restore() would be two objects and
    # would reopen exactly the drift this closes.
    frozen = restore(snapshot)

    artifacts: dict[str, str] = {"snapshot": config.rel_media_path(snapshot_path(version))}

    try:
        master, runtime = _render_master(frozen, vdir / MASTER_NAME, log=log)
    except Exception as exc:
        _record_failure(version, "master", preset, sid, exc)
        raise
    artifacts["master"] = config.rel_media_path(master)
    record(version, "master", "succeeded", preset=preset, snapshot=sid,
           path=artifacts["master"])

    try:
        from . import timeline
        otio_path, fcpxml_path, tl_runtime = timeline.build(
            frozen, out_dir=vdir, out_stem=STEM)
    except Exception as exc:
        _record_failure(version, "fcpxml", preset, sid, exc)
        raise
    artifacts["otio"] = config.rel_media_path(otio_path)
    record(version, "otio", "succeeded", preset=preset, snapshot=sid,
           path=artifacts["otio"])
    if fcpxml_path is not None:
        artifacts["fcpxml"] = config.rel_media_path(fcpxml_path)
        record(version, "fcpxml", "succeeded", preset=preset, snapshot=sid,
               path=artifacts["fcpxml"])
    else:
        # The adapter failed and said so. Recorded as failed rather than omitted:
        # §9.2 names FCPXML as a required deliverable, and an export missing one
        # must not read as complete.
        record(version, "fcpxml", "failed", preset=preset, snapshot=sid,
               note="the fcpx_xml adapter failed; the .otio is still valid")

    log(f"export: {version} complete -- master {runtime:.1f}s, timeline "
        f"{tl_runtime:.1f}s, snapshot {sid[:19]}...")
    return {
        "version": version,
        "label": snapshot["label"],
        "preset": preset,
        "snapshot_id": sid,
        "runtime": round(runtime, 3),
        "artifacts": artifacts,
    }

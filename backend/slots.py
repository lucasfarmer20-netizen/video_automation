"""Timeline slots: a cut made of positions, not of file paths.

Contract §7.1 is the whole design: *a timeline visual clip is fundamentally a
slot tied to a DirectorShot, not merely a file path.*

    Timeline Slot -> Shot 3C -> Selected Output Take B

The difference shows the moment anything changes. When a cut is a list of file
paths, choosing a different take means rebuilding the list, and everything the
editor did to those clips — trims, order, what sits either side — is rebuilt
with it, which is to say lost. When a cut is a list of slots, a new take is a
new *value* in a slot that already exists, and the edit survives because the
edit was never made of files.

The same reasoning covers placeholders (§6.2). A missing shot is not an absence,
it is a slot whose media has not arrived: it holds the shot it belongs to, the
duration it is owed, the media type it expects, and the beat it came from. That
is why a later output can drop into it without reconstructing the edit — the
placeholder was already the edit. A placeholder that is only a grey rectangle on
screen looks the same and carries none of that, which is the failure this model
exists to prevent.

What a slot deliberately does NOT own: the creative intent (Director's), the
attempts that produced its media (Generate's), or approval. It references them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import atomic, config

VIDEO = "video"
STILL = "still"


@dataclass
class TimelineSlot:
    """One visual position in the cut."""

    id: str                       # stable across take swaps and re-plans
    beat_id: str                  # the narration beat this covers
    shot_id: str = ""             # the DirectorShot; "" for a whole-beat clip
    index: int = 0                # position in the cut
    intended_duration: float = 0.0
    expected_media: str = VIDEO
    media: str = ""               # media-root-relative; "" while a placeholder
    source_attempt: str = ""      # the GenerationAttempt that produced `media`
    trim_in: float = 0.0
    trim_out: float = 0.0         # 0 means "to the end of the media"
    note: str = ""

    @property
    def placeholder(self) -> bool:
        """A slot is a placeholder exactly when it has no media yet.

        Derived, not stored, so the two can never disagree — a stored flag left
        true after media arrived is precisely how a filled slot goes on
        rendering as a placeholder.
        """
        return not self.media

    @property
    def duration(self) -> float:
        """What this slot occupies in the cut.

        The intended duration governs until a trim says otherwise. A placeholder
        therefore holds its full length, which is what keeps everything after it
        in the right place while its media is still missing.
        """
        if self.trim_out and self.trim_out > self.trim_in:
            return round(self.trim_out - self.trim_in, 3)
        return round(max(0.0, self.intended_duration - self.trim_in), 3)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["placeholder"] = self.placeholder
        d["duration"] = self.duration
        return d


# --- identity -------------------------------------------------------------------

def slot_id(beat_id: str, shot_id: str = "") -> str:
    """Stable slot identity, derived from what the slot covers.

    Derived rather than random so a slot rebuilt from the same plan is the SAME
    slot and keeps its trims. Random ids would make every rebuild a new cut.
    """
    return f"{beat_id}::{shot_id}" if shot_id else f"{beat_id}::beat"


# --- storage --------------------------------------------------------------------

def slots_path() -> Path:
    return config.project_dir() / "timeline_slots.json"


def load() -> list[TimelineSlot]:
    p = slots_path()
    if not p.is_file():
        return []
    try:
        raw = atomic.read_json(p)
    except (OSError, ValueError):
        # A cut that cannot be read is not an empty cut. Callers rebuild from
        # the plan instead, which loses trims but never silently ships a film
        # with everything after the unreadable point in the wrong place.
        raise
    known = set(TimelineSlot.__dataclass_fields__)
    return [TimelineSlot(**{k: v for k, v in s.items() if k in known})
            for s in (raw.get("slots") or [])]


def save(slots: list[TimelineSlot]) -> Path:
    return atomic.write_json(slots_path(),
                             {"slots": [asdict(s) for s in slots]})


# --- building and reconciling -----------------------------------------------------

def build(sb, coverage_for=None, media_for=None) -> list[TimelineSlot]:
    """The slots a storyboard implies, in cut order.

    ``coverage_for(beat_id)`` returns that beat's DirectorShots, or None when
    the beat has no coverage plan and is therefore one whole-beat slot.
    ``media_for(beat_id, shot_id)`` returns ``(media, attempt_id)`` or None.

    Both are injected so this module never reaches into the director's or the
    generation ledger's storage, and so a caller that already has plans loaded
    does not pay to read them again.
    """
    out: list[TimelineSlot] = []
    for beat in sb.shots:
        shots = (coverage_for(beat.scene_id) if coverage_for else None) or []
        if shots:
            for ds in shots:
                media, attempt = (media_for(beat.scene_id, ds.id)
                                  if media_for else (None, None)) or ("", "")
                out.append(TimelineSlot(
                    id=slot_id(beat.scene_id, ds.id),
                    beat_id=beat.scene_id, shot_id=ds.id, index=len(out),
                    intended_duration=round(float(ds.duration), 3),
                    expected_media=STILL if ds.motion_type == "static" else VIDEO,
                    media=media or "", source_attempt=attempt or "",
                ))
        else:
            media, attempt = (media_for(beat.scene_id, "")
                              if media_for else (None, None)) or ("", "")
            dur = float(beat.camera.duration) if beat.camera else 0.0
            out.append(TimelineSlot(
                id=slot_id(beat.scene_id), beat_id=beat.scene_id, index=len(out),
                intended_duration=round(dur, 3), expected_media=VIDEO,
                media=media or "", source_attempt=attempt or "",
            ))
    return out


def reconcile(existing: list[TimelineSlot],
              rebuilt: list[TimelineSlot]) -> list[TimelineSlot]:
    """Fold a rebuilt slot list onto the edit that already exists.

    Slots that still exist keep their trims and their note — that is the editing
    work, and a re-plan of one beat must not discard it everywhere. Slots whose
    shot no longer exists are dropped; new ones arrive empty.

    A trim is kept only while it still fits the slot's intended duration. When
    Director shortens a shot below an existing trim, the trim is dropped rather
    than silently retimed, because quietly reinterpreting an editor's decision
    is worse than making them redo it: §7.1 forbids hiding a plan change by
    stretching the cut around it.
    """
    prior = {s.id: s for s in existing}
    out: list[TimelineSlot] = []
    for i, fresh in enumerate(rebuilt):
        old = prior.get(fresh.id)
        if old is not None:
            fresh.note = old.note
            fits = (old.trim_in < fresh.intended_duration and
                    (not old.trim_out or old.trim_out <= fresh.intended_duration))
            if fits:
                fresh.trim_in, fresh.trim_out = old.trim_in, old.trim_out
            else:
                fresh.note = (old.note + " " if old.note else "") + (
                    f"trim dropped: the shot is now {fresh.intended_duration:.2f}s")
            # An existing slot keeps media the caller did not supply, so a
            # rebuild that cannot see the ledger does not blank the cut.
            if not fresh.media and old.media:
                fresh.media, fresh.source_attempt = old.media, old.source_attempt
        fresh.index = i
        out.append(fresh)
    return out


def fill(slots: list[TimelineSlot], shot_id: str, media: str,
         attempt: str = "", beat_id: str = "") -> TimelineSlot | None:
    """Put media into the slot that already exists. §7.1's core operation.

    Selecting a different take must not recreate the edit, so this changes one
    field and touches nothing else: the slot keeps its id, its index, its trims
    and everything around it.
    """
    for s in slots:
        if s.shot_id == shot_id and (not beat_id or s.beat_id == beat_id):
            s.media = media
            s.source_attempt = attempt
            return s
    return None


def coverage(slots: list[TimelineSlot]) -> dict:
    """How complete the cut is, in the words §6.2 asks for."""
    total = len(slots)
    ready = sum(1 for s in slots if not s.placeholder)
    missing = total - ready
    return {
        "slots": total,
        "ready": ready,
        "placeholders": missing,
        "runtime": round(sum(s.duration for s in slots), 3),
        "summary": (f"{ready}/{total} visuals ready"
                    + (f" · Draft 1 will use {missing} placeholder"
                       f"{'' if missing == 1 else 's'}" if missing else "")),
    }

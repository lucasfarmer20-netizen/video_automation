"""Generation lineage: every attempt to turn an approved shot into media.

Contract §6 makes Generate the lineage stage, and §11.1/§11.6 make two demands
that pull in opposite directions unless attempts are first-class:

* a retry must **never overwrite** a prior attempt, because a failure that
  disappears takes the reason with it;
* a retry must **never double-charge**, no matter how it arrives -- a duplicate
  click, a UI retry, a worker restart, a network retry that the client thinks
  failed and the server thinks succeeded.

``DirectorShot`` cannot answer either. It holds one clip, one signature and one
error, so attempt two erases attempt one by construction. Attempts live here
instead, append-only, and the shot keeps a *reference* to the one that was
selected (``selected_attempt``) rather than owning the history.

Two different guards, deliberately, because they answer different questions:

* ``idempotency_key`` -- "is this the same REQUEST as one I already handled?"
  Two clicks on one button, or a retried HTTP call, carry the same key and must
  produce one attempt.
* ``signature`` -- "are these the same INPUTS I already bought?" A genuinely new
  request for a shot whose still, prompt, length and model are unchanged must
  reuse the media already paid for.

Neither subsumes the other: the first stops a duplicate request becoming a
second charge, the second stops a *new* request re-buying an unchanged shot.
"""

from __future__ import annotations

import datetime as _dt
import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import atomic, config

RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


@dataclass
class GenerationAttempt:
    """One attempt to produce media for one director shot. Never mutated after
    it reaches a terminal state, and never removed."""

    id: str
    shot_id: str
    beat_id: str
    attempt: int                    # 1-based, monotonic per shot
    parent_attempt: str = ""        # the attempt this one retries; "" for the first
    status: str = RUNNING
    kind: str = "video"             # video|still|parallax
    backend: str = ""
    paid: bool = False
    cost: float = 0.0
    idempotency_key: str = ""
    signature: str = ""             # the inputs this attempt was made for
    output: str = ""                # media-root-relative path when it succeeded
    error: str = ""
    started_at: str = field(default_factory=_now)
    finished_at: str = ""

    @property
    def terminal(self) -> bool:
        return self.status in (SUCCEEDED, FAILED)


# --- storage --------------------------------------------------------------------
#
# One file per beat, mirroring how coverage plans are stored, so a beat's whole
# lineage moves with the beat. Writes go through backend.atomic, which is where
# the durability rules live.

_MUTATE_LOCK = threading.Lock()


def generation_dir() -> Path:
    return config.project_dir() / "generation"


def ledger_path(beat_id: str) -> Path:
    return generation_dir() / f"{beat_id}.json"


def load_attempts(beat_id: str) -> list[GenerationAttempt]:
    p = ledger_path(beat_id)
    if not p.is_file():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    known = set(GenerationAttempt.__dataclass_fields__)
    return [GenerationAttempt(**{k: v for k, v in a.items() if k in known})
            for a in (raw.get("attempts") or [])]


def _save_attempts(beat_id: str, attempts: list[GenerationAttempt]) -> Path:
    return atomic.write_json(ledger_path(beat_id),
                             {"beat_id": beat_id,
                              "attempts": [asdict(a) for a in attempts]})


def for_shot(beat_id: str, shot_id: str) -> list[GenerationAttempt]:
    return [a for a in load_attempts(beat_id) if a.shot_id == shot_id]


def history(beat_id: str, shot_id: str) -> list[dict]:
    """The shot's lineage, oldest first, for display."""
    return [asdict(a) for a in for_shot(beat_id, shot_id)]


# --- the guarantees ---------------------------------------------------------------

def find_by_key(beat_id: str, key: str) -> GenerationAttempt | None:
    """The attempt already created for this request, if any."""
    if not key:
        return None
    return next((a for a in load_attempts(beat_id) if a.idempotency_key == key), None)


def reusable(beat_id: str, shot_id: str, signature: str,
             exists=None) -> GenerationAttempt | None:
    """A succeeded, paid attempt whose inputs match and whose media is still there.

    ``exists`` checks that the output is genuinely on disk. A recorded success
    whose file has since been deleted is not reusable -- claiming otherwise is
    how a shot ends up referencing media that is not there, which reads as a
    render bug long after the deletion.
    """
    if not signature:
        return None
    for a in for_shot(beat_id, shot_id):
        if a.status != SUCCEEDED or a.signature != signature or not a.output:
            continue
        if exists is not None and not exists(a.output):
            continue
        return a
    return None


def begin(*, beat_id: str, shot_id: str, signature: str, idempotency_key: str = "",
          kind: str = "video", backend: str = "", paid: bool = False,
          exists=None) -> tuple[GenerationAttempt, str]:
    """Open an attempt, or hand back one that already covers this request.

    Returns ``(attempt, disposition)`` where disposition is:

    * ``"created"``  -- a new attempt; the caller should generate.
    * ``"duplicate"`` -- this exact request was already handled; do NOT generate.
    * ``"reused"``   -- different request, identical inputs, media already
      bought; do NOT generate.

    The caller must not spend money unless it got ``"created"``. That is the
    whole contract of this function, and it is enforced in one place so no call
    site has to remember the rule.
    """
    with _MUTATE_LOCK:
        attempts = load_attempts(beat_id)

        if idempotency_key:
            dup = next((a for a in attempts
                        if a.idempotency_key == idempotency_key), None)
            if dup is not None:
                return dup, "duplicate"

        mine = [a for a in attempts if a.shot_id == shot_id]
        if signature:
            for a in mine:
                if (a.status == SUCCEEDED and a.signature == signature and a.output
                        and (exists is None or exists(a.output))):
                    return a, "reused"

        # A retry branches from the most recent attempt for this shot, so the
        # chain records what was tried before rather than a flat list.
        parent = mine[-1].id if mine else ""
        n = len(mine) + 1
        att = GenerationAttempt(
            id=f"{shot_id}#{n}:{uuid.uuid4().hex[:8]}",
            shot_id=shot_id, beat_id=beat_id, attempt=n, parent_attempt=parent,
            kind=kind, backend=backend, paid=paid, signature=signature,
            idempotency_key=idempotency_key,
        )
        attempts.append(att)
        _save_attempts(beat_id, attempts)
        return att, "created"


def _finish(beat_id: str, attempt_id: str, **changes) -> GenerationAttempt | None:
    with _MUTATE_LOCK:
        attempts = load_attempts(beat_id)
        found = None
        for a in attempts:
            if a.id == attempt_id:
                for k, v in changes.items():
                    setattr(a, k, v)
                a.finished_at = _now()
                found = a
                break
        if found is not None:
            _save_attempts(beat_id, attempts)
        return found


def succeed(beat_id: str, attempt_id: str, output: str,
            cost: float = 0.0) -> GenerationAttempt | None:
    return _finish(beat_id, attempt_id, status=SUCCEEDED, output=output, cost=cost)


def fail(beat_id: str, attempt_id: str, error: str) -> GenerationAttempt | None:
    """Record a failure. The attempt stays in the lineage, attached to its shot.

    §6: "A failed generation must remain attached to the intended shot." Deleting
    it would leave the next person looking at a shot that simply has no media and
    no reason why.
    """
    return _finish(beat_id, attempt_id, status=FAILED, error=str(error)[:2000])


def spend(beat_id: str, shot_id: str | None = None) -> dict:
    """What has actually been billed, per §6.1. Only paid, succeeded attempts."""
    rows = [a for a in load_attempts(beat_id)
            if (shot_id is None or a.shot_id == shot_id)]
    billed = [a for a in rows if a.paid and a.status == SUCCEEDED]
    return {
        "attempts": len(rows),
        "failed": sum(1 for a in rows if a.status == FAILED),
        "paid_attempts": len(billed),
        "spent": round(sum(a.cost for a in billed), 4),
    }

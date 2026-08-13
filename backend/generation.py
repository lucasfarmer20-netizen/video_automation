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


class LedgerUnreadable(RuntimeError):
    """The lineage exists but could not be read. NOT the same as no lineage."""


def load_attempts(beat_id: str) -> list[GenerationAttempt]:
    """Every attempt recorded for a beat, oldest first.

    Raises LedgerUnreadable when a ledger is present but unreadable or corrupt.

    That distinction is the whole point. This used to swallow every OSError and
    return [], which is indistinguishable from "this beat has no history" -- so
    one transient read fault on the GCS mount made begin() treat a paid,
    succeeded attempt as nonexistent, permit a second charge, and then write the
    new list over the real one. The atomic write preserved the wrong answer
    perfectly. Failing closed costs a retry; failing open costs money and the
    record that would have prevented the next one.
    """
    p = ledger_path(beat_id)
    try:
        if not p.is_file():
            return []
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise LedgerUnreadable(
            f"could not read the generation ledger for {beat_id}: {exc}. "
            f"Refusing to treat it as empty -- that would permit a second charge "
            f"and overwrite the record of the first.") from exc
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise LedgerUnreadable(
            f"the generation ledger for {beat_id} is corrupt: {exc}. "
            f"It has NOT been overwritten; inspect it before generating again."
        ) from exc
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
    * ``"in_flight"`` -- an attempt for these inputs is still running, or died
      without recording its outcome; do NOT generate, and surface it for a human
      to resolve with :func:`abandon`.

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

        # An attempt for these same inputs is still RUNNING. Either it is live,
        # or it died after reaching the provider and before recording success --
        # and nothing here can tell which. Creating another is how the crash
        # window double-charges, so refuse and make a human resolve it with
        # abandon(). Blindly retrying is not idempotency, it is a second bill.
        if signature:
            stuck = next((a for a in mine
                          if a.status == RUNNING and a.signature == signature), None)
            if stuck is not None:
                return stuck, "in_flight"

        # A retry branches from the most recent attempt for this shot, so the
        # chain records what was tried before rather than a flat list.
        parent = mine[-1].id if mine else ""
        n = len(mine) + 1
        att = GenerationAttempt(
            # URL-safe on purpose: this id appears in a path segment on the
            # recovery route, and the original "shot#n:uuid" form truncated at
            # the "#" -- the browser treated the rest as a fragment, so the
            # abandon endpoint was simply unreachable and answered 405.
            id=f"{shot_id}.a{n}.{uuid.uuid4().hex[:8]}",
            shot_id=shot_id, beat_id=beat_id, attempt=n, parent_attempt=parent,
            kind=kind, backend=backend, paid=paid, signature=signature,
            idempotency_key=idempotency_key,
        )
        attempts.append(att)
        _save_attempts(beat_id, attempts)
        return att, "created"


class TerminalConflict(RuntimeError):
    """An attempt that already finished was told it finished differently."""


def _finish(beat_id: str, attempt_id: str, *, status: str,
            **changes) -> GenerationAttempt | None:
    """Move a running attempt to exactly one terminal state.

    A terminal record is billing truth. This used to apply whatever it was
    given, so a late or duplicated callback could turn a succeeded, paid attempt
    into a failed one -- the output and cost stayed on the record while spend()
    stopped counting it, and the reported bill silently dropped to zero.

    Repeating the SAME completion is a no-op, because a retried callback is not
    a new fact. A conflicting one raises and leaves the stored bytes untouched.
    """
    with _MUTATE_LOCK:
        attempts = load_attempts(beat_id)
        target = next((a for a in attempts if a.id == attempt_id), None)
        if target is None:
            return None
        if target.terminal:
            # An exact replay is a no-op. Anything else is a different claim
            # about what happened, and must be refused rather than dropped.
            # Comparing status alone was not enough: succeed(first.mp4, $0.60)
            # followed by succeed(other.mp4, $99) returned successfully and
            # silently kept the first, so a caller believed a cost was recorded
            # that never was -- and fail("provider failed") racing
            # abandon("human abandoned") let both callers think they had written
            # the reason while only one did.
            differing = {k: (getattr(target, k, None), v)
                         for k, v in changes.items() if getattr(target, k, None) != v}
            if target.status == status and not differing:
                return target
            detail = (f" as {status}" if target.status != status
                      else f" with different {', '.join(sorted(differing))}")
            raise TerminalConflict(
                f"{attempt_id} already finished as {target.status}; refusing to "
                f"rewrite it{detail}. The first terminal result stands.")
        target.status = status
        for k, v in changes.items():
            setattr(target, k, v)
        target.finished_at = _now()
        _save_attempts(beat_id, attempts)
        return target


def succeed(beat_id: str, attempt_id: str, output: str,
            cost: float = 0.0) -> GenerationAttempt | None:
    return _finish(beat_id, attempt_id, status=SUCCEEDED, output=output, cost=cost)


def in_doubt(beat_id: str, attempt_id: str, reason: str) -> GenerationAttempt | None:
    """Record why an attempt is unresolved WITHOUT closing it.

    Used when generation raised after the provider was already called. Marking
    it failed there would be a claim nobody can support -- the money may have
    been spent -- and the retry that follows a "failed" would buy the clip
    again. It stays running, which makes the next request see it as in_flight
    and refuse, until a human resolves it with abandon().
    """
    with _MUTATE_LOCK:
        attempts = load_attempts(beat_id)
        target = next((a for a in attempts if a.id == attempt_id), None)
        if target is None or target.terminal:
            return target
        target.error = str(reason)[:2000]
        _save_attempts(beat_id, attempts)
        return target


def abandon(beat_id: str, attempt_id: str,
            reason: str = "outcome unknown") -> GenerationAttempt | None:
    """Close a running attempt whose provider outcome nobody knows.

    The explicit recovery for a process that died mid-generation. It is a human
    decision precisely because the money may already have been spent: the
    machine cannot tell, so it must not choose. Recorded as a failure with the
    reason, which keeps it in the lineage and unblocks a retry.
    """
    return _finish(beat_id, attempt_id, status=FAILED, error=f"abandoned: {reason}")


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

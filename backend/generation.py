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
import math
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import atomic, config

RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"

# The marker abandon() writes into `error`. It is a sentinel, not prose: a
# FAILED attempt carrying it was closed by a human who did NOT know what the
# provider did, which is a different money fact from an ordinary fail().
ABANDONED = "abandoned: "

# The value `cost_source` takes when `cost` is fal's own figure for the request
# rather than this repo's estimate. Re-exported from backend.fal_billing so the
# two modules cannot drift to different spellings of the same claim.
MEASURED = "measured"
_COST_SOURCES = ("", MEASURED)


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
    # Where `cost` came from. Empty means nobody said -- which is every row this
    # repo wrote before fal's billed quantity was read back, and every row whose
    # provider does not report one. MEASURED is a positive claim, set only by
    # succeed() from a `measurement` that backend.fal_billing obtained from fal
    # itself, and it is what lets spend() report which money was measured and
    # which was inferred. Deliberately not derived from `cost > 0`: an estimate
    # written into `cost` looks identical to a bill, and this codebase already
    # learned (see `outcome_unknown`) that money must be classified by a
    # recorded fact rather than by inference.
    cost_source: str = ""
    # fal's own count of what it billed this request for, and the unit it
    # counted in ("seconds", "megapixels", "compute seconds"). Kept beside the
    # money so the figure is auditable: a human comparing this ledger against
    # their fal dashboard needs the quantity and the request id, not just a
    # total that either matches or does not.
    billable_units: float = 0.0
    billing_unit: str = ""
    provider_request_id: str = ""
    # What this attempt was expected to cost, recorded BEFORE the provider was
    # called. It is the only figure available for an attempt whose outcome
    # nobody ever recorded -- and reporting the price we were about to pay is
    # the whole difference between "$0.60 may have gone" and a silent $0.00.
    #
    # It is kept even once a measured cost arrives, rather than overwritten:
    # the two disagreeing is the useful signal. The first measured pair had wan
    # v2.7 asked for 4s and billed for 6, so the estimate was 33% under.
    estimated_cost: float = 0.0
    # The provider was called and never told us what it did. Written by
    # in_doubt() and abandon(), which are the two ways that happens. Kept as a
    # recorded fact rather than inferred from an error string, because money
    # must not be classified by parsing prose.
    outcome_unknown: bool = False
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


# --- schema ---------------------------------------------------------------------
#
# A ledger is accounting data, so "JSON parsed" is not the same as "readable".
# `{}` is valid JSON and used to yield an empty attempt list, indistinguishable
# from a beat that never generated anything -- and once spend() started reporting
# `spend_is_certain`, that shape stopped being merely wrong and became a
# CONFIDENT zero on data nobody could read. A file on persistent storage that was
# truncated, hand-repaired or half-migrated is syntactically valid and reaches
# exactly this path.
#
# So every structural and type failure below raises LedgerUnreadable, which
# callers already handle: begin() refuses to spend, and the routes answer 409.
# Failing closed costs a retry. Failing open prices a bill nobody can check.
#
# (This closes the core of TG-S4-06, which recorded these shapes raising
# incidental AttributeError/TypeError instead of LedgerUnreadable.)

_STATUSES = (RUNNING, SUCCEEDED, FAILED)

# Every field a row may carry, and the JSON type it must hold. Money and status
# are here for the same reason the ids are: a total computed over a cost of
# "0.60" is not a smaller number, it is a wrong bill or a TypeError.
_SHAPE: dict[str, type] = {
    "id": str, "shot_id": str, "beat_id": str, "parent_attempt": str,
    "status": str, "kind": str, "backend": str, "signature": str,
    "idempotency_key": str, "output": str, "error": str,
    "started_at": str, "finished_at": str,
    "cost_source": str, "billing_unit": str, "provider_request_id": str,
    "attempt": int, "paid": bool, "outcome_unknown": bool,
    "cost": float, "estimated_cost": float, "billable_units": float,
}
_REQUIRED = ("id", "shot_id", "beat_id", "attempt")


def _valid(kind: type, value) -> bool:
    if kind is bool:
        return isinstance(value, bool)
    if kind is int:
        # bool is an int in Python, and paid=True must not pass as attempt=1.
        return isinstance(value, int) and not isinstance(value, bool)
    if kind is float:
        # NaN and Infinity survive json.loads and would poison every total they
        # touch -- silently, since NaN compares false against every threshold.
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0)
    return isinstance(value, str)


def _row(beat_id: str, index: int, raw) -> GenerationAttempt:
    """One validated row, or LedgerUnreadable. Never an incidental TypeError."""
    where = f"attempt {index} in the generation ledger for {beat_id}"
    if not isinstance(raw, dict):
        raise LedgerUnreadable(
            f"{where} is {type(raw).__name__}, not an object. Refusing to "
            f"report spend from a ledger whose rows are not attempts.")
    for key in _REQUIRED:
        if key not in raw:
            raise LedgerUnreadable(
                f"{where} has no {key!r}. A row that cannot be identified "
                f"cannot be reconciled against a bill.")
    for key, value in raw.items():
        if key in _SHAPE and not _valid(_SHAPE[key], value):
            raise LedgerUnreadable(
                f"{where}: {key}={value!r} is not a valid "
                f"{_SHAPE[key].__name__}. Inspect the file; it has NOT been "
                f"overwritten.")
    if raw.get("cost_source", "") not in _COST_SOURCES:
        # Same reasoning as the status check below. `cost_source` decides whether
        # a figure is reported as measured or as inferred, so a value neither
        # branch recognises would fall to "inferred" and quietly demote a real
        # bill -- or, worse under a future spelling, promote an estimate.
        raise LedgerUnreadable(
            f"{where} has cost_source {raw.get('cost_source')!r}, which is "
            f"neither empty nor {MEASURED!r}. Refusing to guess whether that "
            f"figure was measured or estimated.")
    if raw.get("status", RUNNING) not in _STATUSES:
        raise LedgerUnreadable(
            f"{where} has status {raw.get('status')!r}, which no guard here "
            f"recognises -- it would count as neither billed, nor at risk, nor "
            f"in flight, and so would drop out of every check at once.")
    if raw["attempt"] < 1:
        raise LedgerUnreadable(f"{where} is numbered {raw['attempt']}; "
                               f"attempts are 1-based.")
    if raw["beat_id"] != beat_id:
        raise LedgerUnreadable(
            f"{where} belongs to beat {raw['beat_id']!r}. Counting it here "
            f"would bill this beat for another one's generation.")
    known = set(GenerationAttempt.__dataclass_fields__)
    return GenerationAttempt(**{k: v for k, v in raw.items() if k in known})


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

    Rows written before ``outcome_unknown`` existed are migrated here, once, on
    the way in: an ``ABANDONED`` reason already MEANT the outcome was unknown, so
    setting the field states what the row always said rather than adding to it.
    Doing it here and not in the money code matters twice over. It keeps spend
    classification reading one recorded field instead of a field plus a sniff at
    prose; and it keeps a legacy row's shape identical to a new one, which is
    what stops ``_finish`` seeing a replayed ``abandon()`` as a conflicting claim
    and refusing a recovery it used to accept.
    """
    p = ledger_path(beat_id)
    try:
        if not p.is_file():
            return []
        raw = atomic.read_json(p)
    except OSError as exc:
        raise LedgerUnreadable(
            f"could not read the generation ledger for {beat_id}: {exc}. "
            f"Refusing to treat it as empty -- that would permit a second charge "
            f"and overwrite the record of the first.") from exc
    except ValueError as exc:
        raise LedgerUnreadable(
            f"the generation ledger for {beat_id} is corrupt: {exc}. "
            f"It has NOT been overwritten; inspect it before generating again."
        ) from exc
    if not isinstance(raw, dict):
        raise LedgerUnreadable(
            f"the generation ledger for {beat_id} is a "
            f"{type(raw).__name__}, not an object. It has NOT been "
            f"overwritten; inspect it before generating again.")
    if raw.get("beat_id") != beat_id:
        raise LedgerUnreadable(
            f"the generation ledger at {ledger_path(beat_id).name} says it "
            f"belongs to beat {raw.get('beat_id')!r}. Refusing to report one "
            f"beat's spend from another's lineage.")
    listed = raw.get("attempts")
    if not isinstance(listed, list):
        # The reachable shape: `{}`, or a file truncated to its header. It used
        # to read as an empty list, which is what "this beat never generated
        # anything" looks like -- a certain $0.00 on unreadable accounting.
        raise LedgerUnreadable(
            f"the generation ledger for {beat_id} has no attempts list "
            f"({type(listed).__name__}). Refusing to treat it as an empty "
            f"lineage -- that reports $0.00 as a settled fact about a file "
            f"nobody could read.")
    rows = [_row(beat_id, i, a) for i, a in enumerate(listed)]
    for a in rows:
        if a.error.startswith(ABANDONED):
            a.outcome_unknown = True
    return rows


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
          estimated_cost: float = 0.0,
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
            # Recorded here, before the provider is reachable, because after a
            # crash there is no other moment left to record it.
            estimated_cost=float(estimated_cost or 0.0),
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
            cost: float = 0.0,
            estimated_cost: float | None = None,
            measurement: dict | None = None) -> GenerationAttempt | None:
    """Close an attempt as succeeded.

    ``measurement`` is the ONLY way an attempt comes to claim a measured cost.
    Pass what :func:`backend.fal_billing.measure` returned -- fal's own billed
    quantity for the request, times fal's own live unit price -- and this
    records the amount, the quantity, the unit and the provider request id, and
    marks ``cost_source`` as measured. Pass ``None``, which is what happens
    whenever fal will not say, and the attempt keeps reporting its estimate,
    labelled as one.

    ``cost`` is a bare figure with no provenance. It still counts toward the
    total, because money that was spent is money that was spent, but it does NOT
    make the attempt measured: only a ``measurement`` does that. The distinction
    exists because callers used to pass their own estimate here, which made
    every attempt read as a confirmed invoice.

    ``estimated_cost`` revises OUR figure when the true scope is only known once
    the call returns -- a draft batch whose variation count decides the price.
    Still an estimate; still not a bill.
    """
    changes: dict = {"output": output, "cost": cost}
    if estimated_cost is not None:
        changes["estimated_cost"] = float(estimated_cost)
    if measurement is not None:
        changes.update(_measured_changes(measurement))
    return _finish(beat_id, attempt_id, status=SUCCEEDED, **changes)


def _number(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def _measured_changes(measurement: dict) -> dict:
    """The fields a fal measurement writes, or nothing if it is not one.

    Validated here rather than trusted, because ``measurement`` crosses a
    network boundary before it gets this far. A malformed one must leave the
    attempt estimated -- reporting an unusable payload as measured would be the
    exact lie this whole change exists to remove -- so anything that does not
    parse yields ``{}`` and the estimate stands.

    A measurement carrying units but no cost writes the QUANTITY and stops
    there. That case is not a degraded nothing: the billed quantity is the half
    this repo could never see, and the half that turned out to be wrong -- a 4
    second wan request billing 6 units is what ``capabilities.BILLED_UNITS`` is
    made of. Recording it without setting ``cost_source`` keeps the money
    honest (the attempt still reports its estimate, still labelled an estimate)
    while banking the observation that corrects the next quote.
    """
    if not isinstance(measurement, dict):
        return {}
    units = measurement.get("units")
    if not _number(units):
        return {}
    observed = {
        "billable_units": float(units),
        "billing_unit": str(measurement.get("unit") or ""),
        "provider_request_id": str(measurement.get("request_id") or ""),
    }
    amount = measurement.get("cost")
    if not _number(amount):
        return observed
    return {**observed, "cost": float(amount), "cost_source": MEASURED}


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
        # The provider was reached. Whatever it did was never recorded, so this
        # attempt's price is money that may already have gone -- and spend()
        # must be able to say so without re-reading the reason text.
        target.outcome_unknown = True
        _save_attempts(beat_id, attempts)
        return target


def abandon(beat_id: str, attempt_id: str,
            reason: str = "outcome unknown") -> GenerationAttempt | None:
    """Close a running attempt whose provider outcome nobody knows.

    The explicit recovery for a process that died mid-generation. It is a human
    decision precisely because the money may already have been spent: the
    machine cannot tell, so it must not choose. Recorded as a failure with the
    reason, which keeps it in the lineage and unblocks a retry.

    Closing it does not settle the bill. ``outcome_unknown`` stays on the record
    so :func:`spend` keeps reporting the money as at risk: abandoning is a
    decision to stop waiting, not evidence that nothing was charged.
    """
    return _finish(beat_id, attempt_id, status=FAILED,
                   error=f"{ABANDONED}{reason}", outcome_unknown=True)


def fail(beat_id: str, attempt_id: str, error: str) -> GenerationAttempt | None:
    """Record a failure. The attempt stays in the lineage, attached to its shot.

    §6: "A failed generation must remain attached to the intended shot." Deleting
    it would leave the next person looking at a shot that simply has no media and
    no reason why.
    """
    return _finish(beat_id, attempt_id, status=FAILED, error=str(error)[:2000])


def amount(a: GenerationAttempt) -> float:
    """The best figure this record carries for what it cost.

    The real one when the provider reported it, otherwise the price the attempt
    was opened for. Never zero merely because nobody wrote the invoice down.

    Public (it was ``_amount``) because ``backend.reconcile`` totals the same
    rows against fal's invoice and must use the same rule. A second
    implementation of "what did this attempt cost" is exactly how the quote and
    the ledger came to disagree in the first place.

    A MEASURED cost is used even when it is 0.00, and that is the point of
    checking ``cost_source`` first rather than truthiness. fal answering "0
    billable units" is a fact about the bill; falling through to the estimate
    there would report a number we invented over a number fal gave us, on the
    one row where we actually know.
    """
    if measured(a):
        return a.cost
    return a.cost if a.cost else a.estimated_cost


def measured(a: GenerationAttempt) -> bool:
    """``cost`` is fal's own figure for this request, not this repo's estimate.

    A positive recorded claim, never inferred from the number being present.
    Rows written before fal's billed quantity was read back carry no
    ``cost_source`` and are therefore estimated -- which is what they always
    were. Nothing backfills them: absent means absent.
    """
    return a.cost_source == MEASURED


def billed(a: GenerationAttempt) -> bool:
    """The record already says the provider produced media for this attempt.

    Deliberately NOT "status == succeeded". A clip that was bought is billed
    however the attempt was later closed: an output path or a cost on the record
    is a provider success that somebody wrote down, and a local decision to
    abandon or doubt the attempt does not un-buy it (§6.1).
    """
    return a.paid and (a.status == SUCCEEDED or bool(a.output) or a.cost > 0)


def at_risk(a: GenerationAttempt) -> bool:
    """Paid, not known to be billed, and nobody recorded what the provider did.

    Two shapes, both meaning the same thing for money:

    * still RUNNING -- either live right now or killed before it could record an
      outcome; ``begin()`` cannot tell the two apart and neither can this;
    * ``outcome_unknown`` -- ``in_doubt()`` or ``abandon()`` said so explicitly,
      or ``load_attempts()`` migrated a row that said it in prose.

    A plain ``fail()`` is excluded on purpose: the module reserves it for
    failures it can support as unbilled, which is exactly why ``in_doubt()``
    exists beside it.
    """
    return a.paid and not billed(a) and (a.status == RUNNING or a.outcome_unknown)


def spend_summary(spent: float, risk: float, at_risk_attempts: int,
                  measured_total: float, estimated_total: float,
                  estimated_attempts: int) -> str:
    """The one line a caller renders verbatim. Shared with planner._total_spend
    so a scene total cannot describe itself differently from a beat.

    Two questions, kept apart because they are different questions:

    * how much may have gone and nobody recorded -- ``at risk``;
    * how much of what DID go was measured against fal rather than inferred from
      our own price table.

    The second is new, and it is the reason this function exists. The line used
    to read "$1.20 billed" over two figures that fal was never asked about. It
    now says which part of a total is a measurement and which part is our
    arithmetic, and names the fal billing dashboard as the only invoice --
    because account discounts are not visible to this pipeline's key.
    """
    line = f"${spent:.2f} spent"
    if measured_total and estimated_total:
        line += (f" (${measured_total:.2f} measured against fal, "
                 f"${estimated_total:.2f} estimated on {estimated_attempts} "
                 f"attempt{'s' if estimated_attempts != 1 else ''} fal did not "
                 f"report a billed amount for)")
    elif estimated_total or estimated_attempts:
        line += (f" — ESTIMATED, not measured: fal reported no billed amount "
                 f"for any of the {estimated_attempts} paid attempt"
                 f"{'s' if estimated_attempts != 1 else ''}. Your fal billing "
                 f"dashboard is the only record of what was actually charged.")
    elif measured_total:
        line += " (measured against fal's own billed quantity)"
    if at_risk_attempts:
        line += (f" • ${risk:.2f} at risk on {at_risk_attempts} attempt"
                 f"{'s' if at_risk_attempts != 1 else ''} whose provider "
                 f"outcome was never recorded")
    return line


def spend(beat_id: str, shot_id: str | None = None) -> dict:
    """What has been billed, and what may have been, per §6.1.

    ``spent`` is money the ledger says actually left the account. ``at_risk`` is
    money that may have and whose outcome nobody recorded -- reported beside it,
    never folded into it, because a total that mixes certain with uncertain is
    accurate about nothing.

    ``spent`` splits again into ``measured`` and ``estimated``. That split is
    not decoration. Every figure this ledger held before fal's billed quantity
    was read back was OUR arithmetic -- our price table times the duration we
    asked for -- and the first two clips ever measured showed the estimate 33%
    under on one of them, because fal billed 6 seconds of a 4-second request.
    A caller that renders a total to a human can now say which half of it is a
    measurement.

    Two booleans, answering two questions that were previously conflated into
    one:

    * ``spend_is_certain`` -- is every attempt's provider OUTCOME recorded? It
      has always meant this and still does. It is NOT a claim that the amounts
      were measured; an all-estimated ledger with no unresolved attempts is
      certain about what happened and uncertain about what it cost.
    * ``spend_is_measured`` -- did every dollar in ``spent`` come from fal? This
      is the question ``spend_is_certain`` was being read as answering.

    This used to count paid AND succeeded only, so a clip that was bought and
    then locally abandoned -- or one still in doubt -- reported $0.00. Silence is
    the one answer money must never get.
    """
    rows = [a for a in load_attempts(beat_id)
            if (shot_id is None or a.shot_id == shot_id)]
    paid_for = [a for a in rows if billed(a)]
    unsettled = [a for a in rows if at_risk(a)]
    from_fal = [a for a in paid_for if measured(a)]
    inferred = [a for a in paid_for if not measured(a)]
    # float() because sum([]) is the integer 0, so an empty ledger reported
    # `"spent": 0` while a populated one reported `"spent": 0.6`. Harmless in
    # JSON and confusing everywhere else; a money field should have one type.
    measured_total = round(float(sum(amount(a) for a in from_fal)), 4)
    estimated_total = round(float(sum(amount(a) for a in inferred)), 4)
    spent = round(measured_total + estimated_total, 4)
    risk = round(float(sum(amount(a) for a in unsettled)), 4)
    return {
        "attempts": len(rows),
        "failed": sum(1 for a in rows if a.status == FAILED),
        "paid_attempts": len(paid_for),
        "spent": spent,
        "measured": measured_total,
        "estimated": estimated_total,
        "measured_attempts": len(from_fal),
        "estimated_attempts": len(inferred),
        "at_risk": risk,
        "at_risk_attempts": len(unsettled),
        "spend_is_certain": not unsettled,
        "spend_is_measured": not inferred,
        "summary": spend_summary(spent, risk, len(unsettled),
                                 measured_total, estimated_total, len(inferred)),
    }


def unknown_spend(reason: str) -> dict:
    """The honest answer when the ledger could not be read at all.

    The same shape as :func:`spend`, with ``None`` where every figure would be.
    Deliberately not zeros, and deliberately not an absent key: this is the one
    path where the exposure is LEAST known, and both of those render as $0.00 to
    a caller -- the first directly, the second through the ``?? 0`` that every
    client eventually writes. That is this PR's own defect, relocated to the
    error path.

    ``summary`` states the block rather than substituting a number for it, so a
    caller that renders the summary verbatim says what is wrong instead of
    quietly reporting nothing at stake.
    """
    return {
        "attempts": None,
        "failed": None,
        "paid_attempts": None,
        "spent": None,
        # None for the same reason as `spent`: a ledger nobody could read has
        # not measured $0.00 against fal, it has measured nothing at all. Zeros
        # here would render as "all of it measured, and it was free".
        "measured": None,
        "estimated": None,
        "measured_attempts": None,
        "estimated_attempts": None,
        "at_risk": None,
        "at_risk_attempts": None,
        "spend_is_certain": False,
        "spend_is_measured": False,
        "summary": f"spend is unknown: {reason}",
    }

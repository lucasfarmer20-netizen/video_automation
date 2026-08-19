"""What a beat-level paid video generation costs, and who agreed to pay it.

Everything built for cost consent -- ``capabilities.clip_price``, the quote and
the ledger sharing one function, the ``plan_signature`` that binds a quote to the
plan it was quoted for -- sat on the coverage-compile path. There is a second way
to buy paid video, and it went through none of it:

* the batch render loop (``main.generate_fal_and_render``), reachable from
  ``POST /api/assemble/render`` and the rough cut, and
* ``POST /api/shot/{scene_id}/generate_video``, the studio's per-beat
  "Generate Video" button.

Both resolved a model, built the fal arguments and dispatched. No price was
derived, none was shown, none was recorded. Gate 1 was checked -- approval is
where the *render budget* is allocated -- but approval names no number, so a
human clicking "Generate Video" was making a spending decision they were never
quoted for.

Two pieces live here, and the split is the point.

``quote()`` answers *what will this cost*, for the request as it will actually be
sent: this model, the seconds this model will actually be asked for, and the
audio flag this path will actually pass. Audio matters because these paths honour
the studio's audio toggle and ``veo_3_1`` is billed at 0.20/s silent and 0.40/s
with audio -- the toggle is a 2x price difference on that model, and it was
travelling to fal with nothing pricing it.

``Authorisation`` answers *may this be spent*, and it is deliberately attached to
the DISPATCH rather than to a route. Routes acquire the gate by having it written
into them and lose it by someone adding a fourth route; this codebase has already
paid for that lesson twice (``require_paid_gate``'s docstring, and the paid gate
that was missing from exactly one of four handlers). An ``Authorisation`` is
spent down one generation at a time, so a batch cannot dispatch more paid work
than the total a human accepted even if the estimate of *which* beats would buy
turns out to be wrong -- and the default, the one a caller gets by writing
nothing, authorises nothing at all.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import capabilities

# A tenth of a cent. Prices are rounded to four places and a batch total is a
# sum of them, so exact float equality would refuse a dispatch over 1e-16 of
# accumulated noise. Nothing at this scale is a real difference in money: the
# smallest figure the table can produce for one second is 0.056.
EPSILON = 0.0005

# A cent. What separates "the human was shown this number" from "the human was
# shown a different number": a button renders dollars and cents, so a difference
# below this one cannot have been visible to them, and a difference at or above
# it means they agreed to something else.
VISIBLE = 0.01


@dataclass(frozen=True)
class Quote:
    """The price of one paid generation, and the request it is the price of.

    Carries the inputs, not just the number. A bare float cannot be checked
    against the call that was made, and "what was this $1.51 for" is the question
    a human asks when reconciling against a fal invoice (contract §6.1).
    """

    backend: str
    generate_seconds: int
    generate_audio: bool
    price: float

    def as_dict(self) -> dict:
        return {
            "video_model": self.backend,
            "generate_seconds": self.generate_seconds,
            "generate_audio": self.generate_audio,
            "estimated_cost": self.price,
        }


def quote(key: str, target_seconds: float, *, generate_audio: bool) -> Quote:
    """Price the generation this path is about to request.

    ``generate_seconds`` comes from ``capabilities.clamp_duration``, which is the
    same figure ``video_arguments(..., cap_to_ceiling=True)`` puts on the wire --
    both take ``legal_durations`` and fall back to the model's ceiling. That
    identity is what makes this a price for the request being made rather than
    for the shot's editorial length, and it is asserted in the tests rather than
    trusted here: a 3.34s beat on kling is BILLED for kling's 5s minimum, and a
    20s beat on seedance is billed for seedance's 15s ceiling.
    """
    seconds = capabilities.clamp_duration(key, float(target_seconds))
    return Quote(
        backend=key,
        generate_seconds=seconds,
        generate_audio=bool(generate_audio),
        price=capabilities.clip_price(key, seconds,
                                      generate_audio=bool(generate_audio)),
    )


class Unauthorised(RuntimeError):
    """A paid generation was about to be dispatched against no accepted price."""


def is_a_number(value) -> bool:
    """True for a real, finite number -- and for nothing else.

    Type first, deliberately. An amount that arrives over HTTP is whatever the
    client sent, and a check written as a comparison (``value == quoted``) is
    satisfied by any object that chooses to compare equal, without ever being a
    number. ``bool`` is excluded explicitly because ``True == 1``: a JSON body of
    ``{"accepted_cost": true}`` would otherwise authorise a dollar.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


class Authorisation:
    """What a human agreed to spend, decremented at every paid dispatch.

    Constructed from an amount they were shown and confirmed. ``none()`` is the
    default and authorises nothing -- a caller that says nothing about consent
    has not obtained any, and the whole finding this module answers is a dispatch
    that happened because no one had written down that it needed permission.
    """

    def __init__(self, accepted: float | None, why: str = ""):
        self._accepted = None if accepted is None else float(accepted)
        self._remaining = 0.0 if accepted is None else float(accepted)
        self._why = why
        self.dispatched: list[Quote] = []

    @classmethod
    def none(cls, why: str = "no price was quoted or confirmed for this request"):
        return cls(None, why)

    @classmethod
    def accepting(cls, amount: float):
        return cls(float(amount))

    @property
    def accepted(self) -> float | None:
        return self._accepted

    @property
    def remaining(self) -> float:
        return round(self._remaining, 4)

    def refuses(self, q: Quote) -> str:
        """Why this generation may not be dispatched, or "" if it may."""
        if self._accepted is None:
            return (f"no confirmed price covers this ${q.price:.2f} generation "
                    f"({self._why}), so nothing was generated and nothing was "
                    f"charged")
        if q.price > self._remaining + EPSILON:
            return (f"this ${q.price:.2f} generation is not covered by the "
                    f"${self._accepted:.2f} that was confirmed "
                    f"(${self.remaining:.2f} of it is left), so nothing was "
                    f"generated and nothing was charged")
        return ""

    def spend(self, q: Quote, what: str = "") -> None:
        """Take this generation's price out of the authorisation, or refuse.

        Called immediately before the provider is reached, and after it returns
        the authorisation is that much smaller -- so a batch that was quoted for
        two paid beats cannot buy nine, however the loop came to visit them.
        """
        why = self.refuses(q)
        if why:
            raise Unauthorised(f"{what + ': ' if what else ''}{why}")
        self._remaining -= q.price
        self.dispatched.append(q)


def accepted_matches(accepted, quoted: float) -> bool:
    """Is this the number the human was shown?

    Not ``>=``. An accepted amount LARGER than the quote is as wrong as a smaller
    one: it means the figure on the button and the figure the server would charge
    for were computed from different states, and the honest answer to that is to
    re-quote rather than to spend the smaller of the two and call it generous.
    """
    if not is_a_number(accepted):
        return False
    return abs(float(accepted) - float(quoted)) < VISIBLE

"""What fal actually billed for one request, as opposed to what we guessed.

Every figure this pipeline has ever recorded for a paid generation came from
this repo: first a flat ``PAID_CLIP_COST``, then ``capabilities.clip_price()``
multiplying our own price table by the duration we *asked* for. Nothing ever
read a number back from fal, so ``spend()`` reported an estimate and the word
"billed" beside it.

fal does report one, on a surface ``fal_client`` throws away. The result fetch

    GET https://queue.fal.run/{owner}/{alias}/requests/{request_id}

answers with a ``x-fal-billable-units`` response header, and

    GET https://api.fal.ai/v1/models/pricing?endpoint_id=...

answers with that endpoint's ``unit_price`` and ``unit``. ``fal_client``'s
``subscribe()`` ends at ``response.json()``: the body is the model's output and
the headers are dropped on the floor, which is why the amount has always been
one HTTP GET away and never taken.

The gap this closes is not cosmetic. Measured against the first two clips the
human ever paid for:

    kling-video v2.1 standard   asked 5s   fal billed 5 units    x 0.056 = $0.28
    wan v2.7                    asked 4s   fal billed 6.0 units  x 0.10  = $0.60

The wan call was billed for 6 seconds of a 4-second request. Estimating from
the requested duration puts it at $0.40 -- 33% under, in the direction that
under-reports what a human has spent. No amount of care in the price table
finds that, because the table is right and the *quantity* is the thing we did
not know.

Two honesty limits, stated because this module is about not overclaiming:

* This is a MEASURED figure, not an invoice. The quantity is fal's own count of
  what it billed for the request; the rate is fal's live list price. It is not
  the line on the account, because account-level discounts are not visible
  here: ``GET /v1/models/usage``, the per-line-item billing API, answers 403 to
  the ``FAL_KEY`` this module holds -- it requires an admin key.

  An earlier version of this note went on to say that there was therefore "no
  after-the-fact reconciliation available to us". That was true of this key and
  false of the account: with ``FAL_ADMIN_KEY`` the same endpoint answers 200,
  and ``backend.fal_usage`` reads it. What reconciliation still cannot do is
  join a line item to a request -- fal's line items carry no request id -- so it
  is per-endpoint over a window and never per-attempt. See
  ``backend.reconcile``. That is a THIRD fact beside this one, not a correction
  of it: nothing there may overwrite what this module measured.
* Absent means absent. Every failure path here returns ``None`` rather than a
  number, and the caller then records nothing rather than promoting an estimate
  into a field that means "measured". A cost this module could not obtain must
  read as unmeasured, never as zero and never as our guess wearing fal's name.
"""

from __future__ import annotations

import os

# The header fal sets on a completed request's result. Named here rather than
# inline so the one string the whole feature depends on is greppable.
BILLABLE_UNITS_HEADER = "x-fal-billable-units"

QUEUE_HOST = "https://queue.fal.run"
PRICING_URL = "https://api.fal.ai/v1/models/pricing"

# What `cost_source` is set to on an attempt priced from these two calls.
MEASURED = "measured"

_TIMEOUT = 30.0


def _key() -> str:
    return os.environ.get("FAL_KEY") or os.environ.get("FAL_API_KEY") or ""


def result_url(endpoint: str, request_id: str) -> str:
    """The queue result URL for a completed request.

    Only the owner and alias appear -- ``fal-ai/kling-video/v2.1/standard/
    image-to-video`` is fetched from ``fal-ai/kling-video/requests/{id}``. That
    is not a simplification, it is what fal's queue actually serves; the path
    tail selects the variant on the way IN and is not part of the request's
    address on the way out. ``fal_client`` builds the same URL the same way.
    """
    parts = [p for p in endpoint.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"not an endpoint id: {endpoint!r}")
    return f"{QUEUE_HOST}/{parts[0]}/{parts[1]}/requests/{request_id}"


def _get(url: str, params: dict | None = None):
    import httpx

    return httpx.get(url, params=params, timeout=_TIMEOUT,
                     headers={"Authorization": f"Key {_key()}"})


def billable_units(endpoint: str, request_id: str, *, get=None) -> float | None:
    """How many billing units fal charged this request for, or None.

    Re-fetching a completed result costs nothing -- the work is done and stored,
    and this reads it back. It is deliberately a second call rather than a
    change to the generation call itself: nothing here can affect, delay or fail
    the thing that was actually paid for.
    """
    get = get or _get
    resp = get(result_url(endpoint, request_id))
    if getattr(resp, "status_code", 0) != 200:
        return None
    raw = resp.headers.get(BILLABLE_UNITS_HEADER)
    if raw is None:
        return None
    try:
        units = float(raw)
    except (TypeError, ValueError):
        return None
    # A negative or non-finite unit count is not a smaller bill, it is a header
    # this code does not understand. Refuse it rather than let it into a total.
    if units < 0 or units != units or units in (float("inf"), float("-inf")):
        return None
    return units


def unit_price(endpoint: str, *, get=None) -> dict | None:
    """fal's live list price for one billing unit of an endpoint, or None.

    Not the same figure as ``capabilities.cost_per_second``. That table is this
    repo's transcription of the published tariff, kept for *quoting* a plan
    before it runs; this is fal answering for itself, at the moment the bill was
    incurred, in whatever unit fal bills that endpoint in. They are not always
    the same unit: some endpoints bill in "seconds", some in "megapixels", some
    in "compute seconds" -- and a duration-based estimate cannot express the
    last of those at all.
    """
    get = get or _get
    resp = get(PRICING_URL, {"endpoint_id": endpoint})
    if getattr(resp, "status_code", 0) != 200:
        return None
    try:
        prices = (resp.json() or {}).get("prices") or []
    except Exception:  # noqa: BLE001 -- a non-JSON body is simply no price
        return None
    for row in prices:
        if not isinstance(row, dict) or row.get("endpoint_id") != endpoint:
            continue
        price = row.get("unit_price")
        if not isinstance(price, (int, float)) or isinstance(price, bool):
            return None
        if price < 0 or price != price:
            return None
        return {"unit_price": float(price),
                "unit": str(row.get("unit") or ""),
                "currency": str(row.get("currency") or "USD")}
    return None


def measure(endpoint: str, request_id: str, *, get=None) -> dict | None:
    """What fal billed for one completed request, or None if it will not say.

    ``None`` is a first-class answer and the common one for any endpoint that
    does not set the header. The caller records nothing in that case, which
    leaves the attempt reading as estimated -- correctly, because it is.
    """
    if not endpoint or not request_id or not _key():
        return None
    units = billable_units(endpoint, request_id, get=get)
    if units is None:
        return None
    priced = unit_price(endpoint, get=get)
    if priced is None:
        return None
    cost = round(units * priced["unit_price"], 6)
    return {
        "request_id": request_id,
        "endpoint": endpoint,
        "units": units,
        "unit": priced["unit"],
        "unit_price": priced["unit_price"],
        "currency": priced["currency"],
        "cost": cost,
        "source": MEASURED,
    }


def measure_quietly(endpoint: str, request_id: str, *, get=None, log=None):
    """:func:`measure`, but a failure here can never disturb the paid path.

    This runs AFTER the money has been spent and the media downloaded. Anything
    it raises -- a network fault, a fal outage, an httpx import problem -- would
    otherwise propagate into the caller's ``except`` and be recorded as a
    generation that may not have billed, which is the opposite of the truth: the
    clip is in hand. Not knowing the amount is a strictly smaller problem than
    the one a raised exception would manufacture.
    """
    try:
        return measure(endpoint, request_id, get=get)
    except Exception as exc:  # noqa: BLE001
        if log is not None:
            log(f"  (could not read fal's billed amount for {request_id}: {exc}"
                f" — the attempt will record our estimate instead)")
        return None

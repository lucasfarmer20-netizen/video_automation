"""fal's invoice, read from fal's own billing API. Needs the ADMIN key.

``backend.fal_billing`` answers "what did fal bill for THIS request?" from the
``x-fal-billable-units`` header on a completed result. That is a measurement,
and it is per-request. This module answers a different question — "what did fal
bill the ACCOUNT over this window?" — from the line items fal itself totals:

    GET https://api.fal.ai/v1/models/usage?start_time=...&end_time=...
    Authorization: Key <FAL_ADMIN_KEY>

``fal_billing``'s docstring used to say this endpoint was simply unavailable to
us. It answers **403 to the ordinary ``FAL_KEY``**; it answers 200 to an admin
key, which now exists in Secret Manager. So the after-the-fact reconciliation
that module called impossible is possible, and lives here.

WHAT THIS IS NOT: a per-request join. The line items carry an endpoint, a unit,
a quantity and a cost — and **no request id**. There is nothing to join a
``GenerationAttempt`` to. Reconciliation is therefore per-endpoint over a time
window and cannot be anything finer; see ``backend.reconcile``, which says the
same thing where it matters.

## The admin key

It is read here and nowhere else, only as ``os.environ.get("FAL_ADMIN_KEY")``,
and it must never leave this process. Two rules enforce that:

* it is sent in a **header**, never a query parameter, so it cannot appear in a
  URL that an exception, a redirect or an access log repeats;
* every string this module lets out — every ``reason``, every error text — goes
  through :func:`_redact` first.

That is not paranoia about a hypothetical. This service is deployed
``--allow-unauthenticated`` at the edge, and this codebase has already shipped
one leak of exactly this shape: the storage gate returned ``str(exc)`` to
unauthenticated GET callers and published the GCP project and database name
(see ``main.STORAGE_GATE_MESSAGE``). An admin key in an exception path is worse
than that one was.

## Absent means unavailable, not zero

Local development has no admin key and neither does the currently deployed
revision. Every function here answers with an ``available: False`` payload
naming the reason. It never raises for a missing key, and it never returns a
total of ``$0.00`` — a zero invoice is a claim that nothing was billed, which is
the one thing an unconfigured feature must not say.

## Cost and rate limits of calling it

The call itself is not billed: it is an account-management read, not a model
invocation. It is still not free to make — it is a whole-account billing query,
fal rate-limits its management API, and the answer changes at most once an hour
because the series is bucketed hourly. So nothing here is wired to a page load.
``main.py`` exposes it behind an explicit POST, caches the result, and serves
page loads the cached copy (see ``RECONCILE_TTL``).
"""

from __future__ import annotations

import datetime as _dt
import os

USAGE_URL = "https://api.fal.ai/v1/models/usage"

#: The one environment variable this module reads. Named once so it is greppable
#: and so no other module has a reason to spell it.
ADMIN_KEY_ENV = "FAL_ADMIN_KEY"

#: What :func:`_redact` substitutes. Deliberately not a length-preserving mask:
#: even the length of a credential is information nobody needs in a log.
REDACTED = "<FAL_ADMIN_KEY redacted>"

# Why the invoice could not be read. Machine-readable so a client branches on
# the cause instead of pattern-matching prose — the same lesson as
# `storage_gate: "unavailable"`. "Not configured" and "fal refused us" look
# identical in a rendered sentence and mean completely different things.
NO_KEY = "no_admin_key"
REFUSED = "refused"
UNREACHABLE = "unreachable"
MALFORMED = "malformed"

# The request parameter that carries `next_cursor` back. The RESPONSE field name
# is verified against a real 200; this REQUEST parameter name is not, and that
# asymmetry is handled rather than assumed: if it is wrong, fal ignores it and
# returns page one again, which the repeat-cursor guard in :func:`fetch` detects.
# The window is then reported `complete: False` rather than as a total — a
# silently truncated invoice would understate what fal billed, which is the
# direction that matters.
CURSOR_PARAM = "cursor"

# A backstop, not a limit anyone should reach: hourly buckets over a month is
# ~750 rows, and fal pages far coarser than that. Reaching it means the cursor
# is looping, and that is reported, never trimmed silently.
MAX_PAGES = 50

_TIMEOUT = 30.0


def _admin_key() -> str:
    return (os.environ.get(ADMIN_KEY_ENV) or "").strip()


def configured() -> bool:
    """Whether an admin key is present. Says nothing about whether it works."""
    return bool(_admin_key())


def _redact(text) -> str:
    """``text`` with the admin key removed, if it somehow got in.

    The empty-key guard is not defensive padding. ``"abc".replace("", "X")``
    returns ``"XaXbXcX"`` — so without it, the single most common configuration
    (no admin key at all) would splice the redaction marker between every
    character of every error message this module produces. The guard is the
    difference between this function being a safeguard and being a bug.
    """
    out = str(text)
    key = _admin_key()
    if not key:
        return out
    return out.replace(key, REDACTED)


def unavailable(reason: str, detail: str = "") -> dict:
    """The answer when there is no invoice to report. Never a zero total.

    ``cost`` and ``quantity`` are absent rather than 0.0 for the reason
    ``generation.unknown_spend`` gives: a zero renders as "nothing was billed",
    and the caller's inevitable ``?? 0`` turns a missing key into a clean bill
    of health.
    """
    return {
        "available": False,
        "reason": reason,
        "detail": _redact(detail),
        "rows": [],
        "total": None,
        "complete": False,
    }


def _get(url: str, params: dict):
    import httpx

    # Header, never a query parameter. A key in the URL survives in redirects,
    # proxy logs and the `request.url` that httpx puts in its own exceptions.
    return httpx.get(url, params=params, timeout=_TIMEOUT,
                     headers={"Authorization": f"Key {_admin_key()}"})


def _iso(when: _dt.datetime) -> str:
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    return when.astimezone(_dt.timezone.utc).isoformat(timespec="seconds")


def window(days: float = 7.0, *, now: _dt.datetime | None = None) -> tuple[str, str]:
    """A default ``(start, end)`` in the spelling the API wants."""
    end = now or _dt.datetime.now(_dt.timezone.utc)
    return _iso(end - _dt.timedelta(days=float(days))), _iso(end)


def _number(value) -> float | None:
    """A finite, non-negative number, or None. Booleans are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        return None
    return value


def _lines(body: dict) -> tuple[list[dict], int]:
    """Every line item on one page, plus the count that would not parse.

    A row whose cost or quantity is not a finite number is not a cheaper row, it
    is a row this code does not understand — the same rule
    ``fal_billing.billable_units`` applies to the header. It is dropped from the
    total AND counted, because a silently dropped invoice line is an
    understatement of the bill wearing the appearance of a complete answer.
    """
    rows: list[dict] = []
    unparsed = 0
    series = body.get("time_series")
    if not isinstance(series, list):
        return rows, unparsed
    for bucket in series:
        if not isinstance(bucket, dict):
            unparsed += 1
            continue
        results = bucket.get("results")
        # `results` is empty for a quiet hour. That is the normal shape of a
        # bucket, not a fault.
        if not isinstance(results, list):
            continue
        for item in results:
            if not isinstance(item, dict):
                unparsed += 1
                continue
            endpoint = str(item.get("endpoint_id") or "").strip()
            cost = _number(item.get("cost_total"))
            if cost is None:
                cost = _number(item.get("cost"))
            quantity = _number(item.get("quantity"))
            if not endpoint or cost is None or quantity is None:
                unparsed += 1
                continue
            rows.append({
                "bucket": str(bucket.get("bucket") or ""),
                "endpoint_id": endpoint,
                "unit": str(item.get("unit") or ""),
                "quantity": quantity,
                "unit_price": _number(item.get("unit_price")),
                "cost": cost,
                "currency": str(item.get("currency") or "USD"),
            })
    return rows, unparsed


def aggregate(lines: list[dict]) -> list[dict]:
    """Line items summed per (endpoint, unit), largest bill first.

    Keyed on the unit as well as the endpoint because they are not
    interchangeable: adding 12 "seconds" to 22 "images" produces a number that
    means nothing. An endpoint that reports two units gets two rows, which is
    the honest shape.
    """
    totals: dict[tuple[str, str], dict] = {}
    for line in lines:
        key = (line["endpoint_id"], line["unit"])
        row = totals.get(key)
        if row is None:
            row = totals[key] = {
                "endpoint_id": line["endpoint_id"],
                "unit": line["unit"],
                "quantity": 0.0,
                "cost": 0.0,
                "currency": line["currency"],
                "lines": 0,
            }
        row["quantity"] += line["quantity"]
        row["cost"] += line["cost"]
        row["lines"] += 1
    out = []
    for row in totals.values():
        row["quantity"] = round(row["quantity"], 6)
        row["cost"] = round(row["cost"], 6)
        # Derived, not read: the per-line `unit_price` is fal's rate for that
        # hour, and this is what the window actually averaged out at. They differ
        # if a rate moved mid-window, and that difference is the whole point of
        # watching for a SUSTAINED divergence.
        row["effective_unit_price"] = (round(row["cost"] / row["quantity"], 6)
                                       if row["quantity"] else None)
        out.append(row)
    out.sort(key=lambda r: (-r["cost"], r["endpoint_id"]))
    return out


def fetch(start: str, end: str, *, get=None) -> dict:
    """Every billed line item between ``start`` and ``end``, or why not.

    Pages until fal says it is done. ``has_more`` with a cursor that does not
    advance is treated as an incomplete answer and reported as one — never as a
    total, because a page silently dropped is money silently unbilled in the
    report, and that error points the wrong way.
    """
    if not _admin_key():
        return unavailable(
            NO_KEY,
            f"{ADMIN_KEY_ENV} is not set in this environment, so fal's invoice "
            f"cannot be read. The ordinary FAL_KEY is refused by this endpoint "
            f"(403) and is deliberately not tried.")

    get = get or _get
    lines: list[dict] = []
    unparsed = 0
    pages = 0
    complete = True
    note = ""
    cursor = None
    seen: set[str] = set()

    while pages < MAX_PAGES:
        params = {"start_time": start, "end_time": end}
        if cursor:
            params[CURSOR_PARAM] = cursor
        try:
            resp = get(USAGE_URL, params)
        except Exception as exc:  # noqa: BLE001 — httpx raises a family, not a type
            return unavailable(UNREACHABLE, f"{type(exc).__name__}: {exc}")

        status = getattr(resp, "status_code", 0)
        if status in (401, 403):
            return unavailable(
                REFUSED,
                f"fal answered HTTP {status} to the admin key. Either the key in "
                f"{ADMIN_KEY_ENV} is not an admin key, or it has been revoked.")
        if status != 200:
            return unavailable(UNREACHABLE, f"fal answered HTTP {status}.")

        try:
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            return unavailable(MALFORMED, f"the response was not JSON: "
                                          f"{type(exc).__name__}: {exc}")
        if not isinstance(body, dict):
            return unavailable(MALFORMED, f"the response was a "
                                          f"{type(body).__name__}, not an object.")

        pages += 1
        page_lines, page_unparsed = _lines(body)
        lines.extend(page_lines)
        unparsed += page_unparsed

        if not body.get("has_more"):
            break
        nxt = body.get("next_cursor")
        nxt = str(nxt).strip() if isinstance(nxt, (str, int)) else ""
        if not nxt or nxt in seen:
            complete = False
            note = (f"fal reported more pages after page {pages} but did not "
                    f"supply a new cursor, so this window is PARTIAL. The total "
                    f"below is a floor, not the bill.")
            break
        seen.add(nxt)
        cursor = nxt
    else:
        complete = False
        note = (f"stopped after {MAX_PAGES} pages with fal still reporting more. "
                f"This window is PARTIAL and the total below is a floor.")

    if unparsed:
        complete = False
        note = ((note + " ") if note else "") + (
            f"{unparsed} line item(s) could not be read and are excluded from "
            f"the total, which is therefore a floor.")

    rows = aggregate(lines)
    return {
        "available": True,
        "window": {"start": start, "end": end},
        "rows": rows,
        # float(): sum([]) is the integer 0, and a money field must not change
        # type with the data.
        "total": round(float(sum(r["cost"] for r in rows)), 6),
        "currency": (rows[0]["currency"] if rows else "USD"),
        "line_items": len(lines),
        "unparsed_line_items": unparsed,
        "pages": pages,
        "complete": complete,
        "note": note,
    }


def fetch_quietly(start: str, end: str, *, get=None) -> dict:
    """:func:`fetch` with no path out except an ``available: False`` payload.

    This is read-only reporting sitting next to a route. Anything it raises
    would become a 500 whose body is an exception string — and an exception
    string is the surface this module spends most of its length keeping the
    admin key out of. Catching here means there is exactly one way out.
    """
    try:
        return fetch(start, end, get=get)
    except Exception as exc:  # noqa: BLE001
        return unavailable(UNREACHABLE, f"{type(exc).__name__}: {exc}")

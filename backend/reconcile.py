"""What we recorded, what fal billed, and where the two differ.

A third fact, beside the estimate and the measurement — not a replacement for
either. ``backend.generation`` holds what this pipeline *recorded* spending;
``backend.fal_usage`` holds what fal *billed the account*. This module puts them
side by side over one window and reports the gap.

## What it deliberately does not do

* **It never writes.** Nothing here opens a ledger for writing, and no function
  returns something a caller is meant to store back onto an attempt. A recorded
  attempt's cost is what this pipeline observed at the time; an invoice line is
  a different observation of a different thing. Overwriting the first with the
  second destroys the only evidence a divergence ever existed.
* **It does not repair.** A difference is information. The whole value of the
  report is that it can be *read*, and a ledger quietly conformed to the invoice
  reads clean forever.
* **It cannot join per request.** fal's line items carry an endpoint, a unit, a
  quantity and a cost, and **no request id**. There is nothing to match a
  ``GenerationAttempt`` against. Reconciliation is per-endpoint over a window,
  full stop, and every field name here says so rather than implying a precision
  the data does not support.

## What a difference means

A single window will almost always show one, for reasons that are not errors:

* **Boundary.** A request made just before ``start`` is billed into a bucket
  that may fall inside it. Our side is filtered on when the attempt was opened.
* **Scope.** The invoice is the whole fal ACCOUNT. This pipeline's ledgers cover
  the projects handed to :func:`recorded`. Anything else on that account — a
  manual call, a spike script, another tool — is invoice-only by construction.
* **Unsettled money.** An attempt whose provider outcome nobody recorded may
  well be on the invoice (``at_risk``), and it is reported separately for
  exactly that reason.

What is worth acting on is a **sustained** divergence in the same direction on
the same endpoint. That is not noise: it means a rate moved, a billing rule
changed, or a tier we did not think we were requesting is the one being served.
That is the reason this exists — the same failure that had ``COST_PER_IMAGE``
sitting at $0.15 against a billed $0.0398, and the seedance row 3x under, for
months, with nothing in the system able to notice.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

from . import assets, config, fal_usage, generation, projects

#: Recorded spend whose endpoint could not be determined. Grouped under its own
#: heading rather than dropped: money we recorded and cannot attribute to a
#: model is a gap in the record, and hiding it would make the totals look like
#: they reconcile when part of one side was simply not shown.
UNATTRIBUTED = "(unattributed)"


def _parse(when: str) -> _dt.datetime | None:
    """An ISO timestamp as an aware UTC datetime, or None.

    A naive string is read as UTC, because every timestamp this codebase writes
    is UTC (``generation._now``) and reading one as local time would shift every
    attempt by the host's offset — silently, and differently on the developer's
    machine than on Cloud Run.
    """
    text = str(when or "").strip()
    if not text:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.astimezone(_dt.timezone.utc)


def endpoint_for(attempt) -> str:
    """The fal endpoint an attempt's charge would have landed on, or "".

    Routed on ``kind`` FIRST, and that is not incidental.
    ``assets.resolve_video_backend`` never returns None — it falls back to
    seedance for anything it does not recognise — so handing it an image
    backend would file a nano-banana charge under seedance and produce a
    divergence on two endpoints at once, neither of them real.
    """
    key = str(getattr(attempt, "backend", "") or "").strip()
    kind = str(getattr(attempt, "kind", "") or "").strip()
    if not key:
        return ""
    if kind == "image":
        return str((assets.IMAGE_BACKENDS.get(key) or {}).get("endpoint") or "")
    if kind == "video":
        return str((assets.resolve_video_backend(key) or {}).get("endpoint") or "")
    # parallax and anything else is a local tier. It costs nothing and fal never
    # sees it, so it has no endpoint — which is different from having an unknown
    # one, and `paid` is what actually decides whether it is counted at all.
    return ""


def _ledger_dirs(project_dirs) -> list[Path]:
    if project_dirs is None:
        return [config.project_dir()]
    return [Path(p) for p in project_dirs]


def recorded(start: str, end: str, *, project_dirs=None) -> dict:
    """What this pipeline recorded spending, per endpoint, in the window.

    Filtered on ``started_at`` — the moment the request went to fal — because
    that is when the charge is incurred. An attempt that is still running has no
    ``finished_at`` at all, and it is precisely the attempt whose money is most
    in question, so filtering on the finish would drop it.

    ``project_dirs`` defaults to the active project alone. The invoice is
    account-wide, so a caller comparing against it should pass every project it
    knows about; :func:`report` records which ones it was given so the scope of
    the comparison is stated rather than assumed.
    """
    lo, hi = _parse(start), _parse(end)
    rows: dict[str, dict] = {}
    unreadable: list[str] = []
    beats = 0

    for project_dir in _ledger_dirs(project_dirs):
        gen_dir = Path(project_dir) / "generation"
        if not gen_dir.is_dir():
            continue
        ctx = projects.ProjectContext.from_manifest(
            Path(project_dir) / "storyboard_manifest.json")
        for path in sorted(gen_dir.glob("*.json")):
            beat_id = path.stem
            # Bound the project so generation.ledger_path() resolves to THIS
            # project's beat rather than to whatever happens to be active.
            with projects.use(ctx):
                try:
                    attempts = generation.load_attempts(beat_id)
                except generation.LedgerUnreadable as exc:
                    # Named, not skipped. A ledger nobody can read is spend
                    # nobody can reconcile, and a report that quietly omitted it
                    # would show the invoice higher than the record and invite
                    # the wrong conclusion entirely.
                    unreadable.append(f"{ctx.project_id}/{beat_id}: {exc}")
                    continue
            beats += 1
            for att in attempts:
                if not att.paid:
                    continue
                opened = _parse(att.started_at)
                if opened is None:
                    continue
                if (lo and opened < lo) or (hi and opened >= hi):
                    continue
                key = endpoint_for(att) or UNATTRIBUTED
                row = rows.get(key)
                if row is None:
                    row = rows[key] = {
                        "endpoint_id": key, "cost": 0.0, "attempts": 0,
                        "measured": 0.0, "measured_attempts": 0,
                        "estimated": 0.0, "estimated_attempts": 0,
                        "at_risk": 0.0, "at_risk_attempts": 0,
                        "backends": set(),
                    }
                row["backends"].add(att.backend or "?")
                money = generation.amount(att)
                if generation.billed(att):
                    row["attempts"] += 1
                    row["cost"] += money
                    if generation.measured(att):
                        row["measured"] += money
                        row["measured_attempts"] += 1
                    else:
                        row["estimated"] += money
                        row["estimated_attempts"] += 1
                if generation.at_risk(att):
                    # Beside the total, never inside it — the same rule
                    # generation.spend() applies. Money that may have gone is
                    # not money the record says went, and folding the two
                    # together makes the comparison accurate about nothing.
                    row["at_risk"] += money
                    row["at_risk_attempts"] += 1

    for row in rows.values():
        row["backends"] = sorted(row["backends"])
        for field in ("cost", "measured", "estimated", "at_risk"):
            row[field] = round(row[field], 6)

    return {
        "rows": sorted(rows.values(), key=lambda r: (-r["cost"], r["endpoint_id"])),
        # float() because sum([]) is the integer 0, so an empty window
        # reported `"total": 0` while a populated one reported 0.6. A money
        # field has one type; generation.spend() learned the same thing.
        "total": round(float(sum(r["cost"] for r in rows.values())), 6),
        "at_risk": round(float(sum(r["at_risk"] for r in rows.values())), 6),
        "beats": beats,
        "unreadable": unreadable,
    }


def same_family(a: str, b: str) -> bool:
    """Whether two endpoint ids name the same model.

    Not string equality, because the two sides do not spell it the same way.
    fal's invoice bills ``fal-ai/kling-video/v2.1/standard``; the request this
    repo makes is to ``fal-ai/kling-video/v2.1/standard/image-to-video``. On
    exact matching those become two rows — one recorded with no bill, one billed
    with no record — which is a fabricated divergence on both sides at once, and
    the most misleading output this module could produce.

    Path-segment prefixes only. ``fal-ai/nano-banana`` is a family of
    ``fal-ai/nano-banana/edit``; it is NOT a family of
    ``fal-ai/nano-banana-pro``, and a bare ``startswith`` would say it was.
    """
    if not a or not b:
        return False
    if a == b:
        return True
    return a.startswith(b + "/") or b.startswith(a + "/")


def _group(endpoints: list[str]) -> list[list[str]]:
    """Endpoint ids partitioned into families, by the relation above.

    Transitive on purpose: if the invoice bills ``x/y`` and we called both
    ``x/y/a`` and ``x/y/b``, all three belong in one row. Splitting them would
    require attributing one bill across two recorded endpoints, and nothing in
    the data supports that split — see ``merged`` on the row, which says out
    loud that the row is a family total rather than a single endpoint.
    """
    groups: list[list[str]] = []
    for ep in sorted(set(endpoints)):
        hit = [g for g in groups if any(same_family(ep, m) for m in g)]
        if not hit:
            groups.append([ep])
            continue
        merged = [ep]
        for g in hit:
            merged.extend(g)
            groups.remove(g)
        groups.append(sorted(set(merged)))
    return groups


def compare(recorded_side: dict, invoice: dict) -> list[dict]:
    """One row per endpoint family, with both sides and the gap.

    ``difference`` is ``invoiced - recorded``: **positive means fal billed more
    than this pipeline recorded**, which is the direction that costs a human
    money they were not told about. It is stated here because a signed number
    whose sign is not defined is worse than no number.
    """
    rec_by_ep = {r["endpoint_id"]: r for r in recorded_side.get("rows") or []}
    inv_rows = invoice.get("rows") or []

    # UNATTRIBUTED is not an endpoint and must never be prefix-matched against
    # one; it gets its own row at the end.
    endpoints = [e for e in rec_by_ep if e != UNATTRIBUTED]
    endpoints += [r["endpoint_id"] for r in inv_rows]

    out: list[dict] = []
    for family in _group(endpoints):
        rec = [rec_by_ep[e] for e in family if e in rec_by_ep]
        inv = [r for r in inv_rows if r["endpoint_id"] in family]
        # float() throughout for the reason given in recorded(): an empty side
        # sums to the integer 0, and a money field must not change type with
        # the data.
        recorded_cost = round(float(sum(r["cost"] for r in rec)), 6)
        invoiced_cost = round(float(sum(r["cost"] for r in inv)), 6)
        invoiced_qty = float(sum(r["quantity"] for r in inv))
        out.append({
            "endpoints": family,
            # True when this row totals more than one endpoint id. The number is
            # then a family total, and a human reading it needs to know that
            # before concluding anything about a single model.
            "merged": len(family) > 1,
            "recorded": {
                "cost": recorded_cost,
                "attempts": sum(r["attempts"] for r in rec),
                "measured": round(float(sum(r["measured"] for r in rec)), 6),
                "estimated": round(float(sum(r["estimated"] for r in rec)), 6),
                "at_risk": round(float(sum(r["at_risk"] for r in rec)), 6),
                "at_risk_attempts": sum(r["at_risk_attempts"] for r in rec),
                "backends": sorted({b for r in rec for b in r["backends"]}),
            },
            "invoiced": {
                "cost": invoiced_cost,
                "quantity": round(invoiced_qty, 6),
                "units": sorted({r["unit"] for r in inv if r["unit"]}),
                "effective_unit_price": (round(invoiced_cost / invoiced_qty, 6)
                                         if invoiced_qty else None),
                "lines": sum(r["lines"] for r in inv),
            },
            "difference": round(invoiced_cost - recorded_cost, 6),
            "on_both_sides": bool(rec) and bool(inv),
            "invoice_only": not rec and bool(inv),
            "recorded_only": bool(rec) and not inv,
        })

    unattributed = rec_by_ep.get(UNATTRIBUTED)
    if unattributed:
        out.append({
            "endpoints": [UNATTRIBUTED],
            "merged": False,
            "recorded": {
                "cost": unattributed["cost"],
                "attempts": unattributed["attempts"],
                "measured": unattributed["measured"],
                "estimated": unattributed["estimated"],
                "at_risk": unattributed["at_risk"],
                "at_risk_attempts": unattributed["at_risk_attempts"],
                "backends": unattributed["backends"],
            },
            # Not zero. No invoice line was matched because no endpoint is
            # known, which is not the same claim as fal having billed nothing.
            "invoiced": {"cost": None, "quantity": None, "units": [],
                         "effective_unit_price": None, "lines": 0},
            "difference": None,
            "on_both_sides": False,
            "invoice_only": False,
            "recorded_only": True,
        })

    out.sort(key=lambda r: (r["difference"] is None,
                            -abs(r["difference"] or 0.0),
                            r["endpoints"][0]))
    return out


def report(start: str = "", end: str = "", *, project_dirs=None,
           days: float = 7.0, get=None) -> dict:
    """The whole comparison for one window.

    Returns a payload whose ``available`` is False, with a reason, whenever
    fal's side could not be read — including the ordinary case of no admin key
    configured. The recorded side is still reported in that payload: knowing
    what we spent is useful even when the invoice is unreachable, and it is the
    half that is never unavailable. What is NOT reported is a difference, because
    there is nothing to difference against, and a ``0.00`` gap would read as
    "reconciled".
    """
    if not start or not end:
        start, end = fal_usage.window(days)

    ours = recorded(start, end, project_dirs=project_dirs)
    invoice = fal_usage.fetch_quietly(start, end, get=get)

    scope = [Path(p).name for p in _ledger_dirs(project_dirs)]
    caveats = [
        "There is no request id on a fal line item, so nothing here is a "
        "per-attempt join. Every figure is a per-endpoint total over the window.",
        "fal's invoice covers the whole account; the recorded side covers "
        f"{len(scope)} project ledger(s): {', '.join(scope) or 'none'}. Anything "
        "else billed to this fal account is invoice-only by construction.",
        "Our side is filtered on when an attempt was opened; fal buckets by the "
        "hour it billed. A request near either edge of the window can land on "
        "one side and not the other.",
        "A single window's difference is not a finding. A SUSTAINED difference "
        "in the same direction on the same endpoint is — that is a rate that "
        "moved or a billing rule that changed.",
    ]
    if ours["at_risk"]:
        caveats.append(
            f"${ours['at_risk']:.2f} of recorded spend is at risk — attempts "
            f"whose provider outcome was never recorded. It is excluded from the "
            f"recorded total and may well be on the invoice.")
    if ours["unreadable"]:
        caveats.append(
            f"{len(ours['unreadable'])} generation ledger(s) could not be read, "
            f"so the recorded side is a floor, not a total.")

    base = {
        "window": {"start": start, "end": end},
        "scope": scope,
        "recorded": ours,
        "caveats": caveats,
    }

    if not invoice.get("available"):
        return {
            **base,
            "available": False,
            "reason": invoice.get("reason"),
            "detail": invoice.get("detail"),
            "invoice": None,
            "rows": [],
            # Explicitly None rather than 0.0. Nothing was compared, so nothing
            # reconciled, and a zero difference is the one answer that would
            # read as a clean bill of health on a feature that is switched off.
            "difference": None,
            "summary": _unavailable_summary(invoice, ours),
        }

    rows = compare(ours, invoice)
    difference = round(invoice["total"] - ours["total"], 6)
    if not invoice.get("complete"):
        caveats.append(
            "fal's side is PARTIAL for this window (see invoice.note), so the "
            "invoice total is a floor and the difference below understates it.")
    return {
        **base,
        "available": True,
        "invoice": invoice,
        "rows": rows,
        "difference": difference,
        "complete": bool(invoice.get("complete")) and not ours["unreadable"],
        "summary": _summary(ours, invoice, difference),
    }


def _unavailable_summary(invoice: dict, ours: dict) -> str:
    reasons = {
        fal_usage.NO_KEY: (f"reconciliation is unavailable: no "
                           f"{fal_usage.ADMIN_KEY_ENV} is configured, so fal's "
                           f"invoice cannot be read"),
        fal_usage.REFUSED: "reconciliation is unavailable: fal refused the admin key",
        fal_usage.UNREACHABLE: "reconciliation is unavailable: fal's billing API could not be reached",
        fal_usage.MALFORMED: "reconciliation is unavailable: fal's billing API answered something unreadable",
    }
    head = reasons.get(invoice.get("reason"), "reconciliation is unavailable")
    return (f"{head}. This pipeline recorded ${ours['total']:.2f} over the "
            f"window; nothing has been compared against it.")


def _summary(ours: dict, invoice: dict, difference: float) -> str:
    """The one line a caller renders verbatim.

    States the direction in words. "$3.20 vs $2.60" leaves the reader to work
    out which side is which, and half of them will get it wrong.
    """
    line = (f"recorded ${ours['total']:.2f}, fal billed ${invoice['total']:.2f} "
            f"over the window")
    if abs(difference) < 0.005:
        line += " — they agree to the cent"
    elif difference > 0:
        line += (f" — fal billed ${difference:.2f} MORE than this pipeline "
                 f"recorded")
    else:
        line += (f" — this pipeline recorded ${-difference:.2f} MORE than fal "
                 f"billed")
    if not invoice.get("complete"):
        line += " (fal's side is partial; the difference is a floor)"
    return line


# --- CLI ------------------------------------------------------------------------
#
# The studio routes are the surface in the product; this is the surface for the
# human holding the admin key on a workstation. It matters because the deployed
# revision does NOT have FAL_ADMIN_KEY wired, so until it does, this is the only
# way to actually run one.
#
#     python -m backend.reconcile --days 7
#     python -m backend.reconcile --start 2026-08-15T00:00:00+00:00 \
#                                 --end   2026-08-19T00:00:00+00:00
#
# One invocation is one call to fal's account-wide billing API. It is not billed,
# but it is rate-limited and hourly-bucketed, so running it in a loop learns
# nothing and is the reason the HTTP surface caches.

def _main() -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default="")
    ap.add_argument("--json", action="store_true", help="the whole payload")
    args = ap.parse_args()

    out = report(args.start, args.end, days=args.days)
    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return 0 if out.get("available") else 1

    print(f"window   {out['window']['start']} .. {out['window']['end']}")
    print(f"scope    {', '.join(out['scope']) or '(no project ledgers)'}")
    print(out["summary"])
    if not out.get("available"):
        # Not a zero and not a crash: the feature reports itself off. Exit 1 so a
        # script cannot mistake "could not read the invoice" for "reconciled".
        print(f"         reason: {out.get('reason')} -- {out.get('detail') or ''}")
        return 1
    print()
    print(f"{'endpoint':52} {'recorded':>10} {'invoiced':>10} {'diff':>10}")
    for row in out["rows"]:
        name = row["endpoints"][0] + (" (+family)" if row["merged"] else "")
        inv = row["invoiced"]["cost"]
        diff = row["difference"]
        print(f"{name[:52]:52} {row['recorded']['cost']:>10.4f} "
              f"{'n/a' if inv is None else format(inv, '.4f'):>10} "
              f"{'n/a' if diff is None else format(diff, '+.4f'):>10}")
    print()
    for caveat in out["caveats"]:
        print(f"  * {caveat}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(_main())

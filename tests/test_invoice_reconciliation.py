"""Recorded spend against fal's invoice, and the admin key that reads it.

Three things are under test here, in the order they matter.

**First and above everything: the admin key never gets out.** ``FAL_ADMIN_KEY``
is the credential that can read the whole account's billing. The service it
would live in is deployed ``--allow-unauthenticated``, and this codebase has
already shipped one leak of exactly this shape -- the storage gate returned
``str(exc)`` to unauthenticated GET callers and published the GCP project and
database name. So the leak tests are the first assertions in the file, and they
are written as a sweep over every string ``fal_usage`` can emit rather than as a
list of the paths someone thought of.

**Second: absent means unavailable, never zero.** Local development has no admin
key and neither does the deployed revision. A feature that is switched off must
say so; a $0.00 invoice is a claim that nothing was billed.

**Third: the comparison is honest.** Pagination cannot silently drop a page.
Nothing is written back to a generation ledger. And the two sides do not spell an
endpoint the same way -- fal bills ``fal-ai/kling-video/v2.1/standard`` for
requests this repo sends to ``fal-ai/kling-video/v2.1/standard/image-to-video``
-- so exact-string matching would manufacture a divergence on both sides at once.

Assertions inside each test are ordered so the DEFECT-PROVING one runs first.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

from backend import config, fal_usage, generation, reconcile  # noqa: E402

# Not a real credential. Long, distinctive and unlikely to occur by chance, so
# a substring search for it in an output string cannot pass by coincidence.
FAKE_ADMIN_KEY = "fal-admin-KEYLEAKCANARY-7f3a91d2c4e6b8a0"

NANO = "fal-ai/nano-banana"
KLING_BILLED = "fal-ai/kling-video/v2.1/standard"
KLING_CALLED = "fal-ai/kling-video/v2.1/standard/image-to-video"
WAN = "fal-ai/wan/v2.7/image-to-video"


class _Resp:
    def __init__(self, status_code=200, payload=None, raises=False):
        self.status_code = status_code
        self._payload = payload
        self._raises = raises

    def json(self):
        if self._raises:
            raise ValueError("not json")
        return self._payload


def _bucket(hour: str, *results) -> dict:
    return {"bucket": hour, "results": list(results)}


def _item(endpoint, unit, quantity, unit_price):
    cost = round(quantity * unit_price, 6)
    return {
        "endpoint_id": endpoint, "unit": unit, "quantity": quantity,
        "unit_price": unit_price, "percent_discount": None,
        "cost_subtotal": cost, "cost_discount": 0, "cost_total": cost,
        "cost": cost, "currency": "USD",
    }


# The whole account over 2026-08-15..19, transcribed from the real invoice.
ACCOUNT_PAGE = {
    "next_cursor": None,
    "has_more": False,
    "time_series": [
        _bucket("2026-08-15T06:00:00+00:00", _item(WAN, "seconds", 12.0, 0.10)),
        _bucket("2026-08-16T09:00:00+00:00"),  # a quiet hour: results is empty
        _bucket("2026-08-17T11:00:00+00:00", _item(KLING_BILLED, "seconds", 20.0, 0.056)),
        _bucket("2026-08-18T06:00:00+00:00", _item(NANO, "images", 22.0, 0.0398)),
    ],
}


def _one_page(page=ACCOUNT_PAGE):
    def get(url, params=None):
        return _Resp(200, page)
    return get


@pytest.fixture
def admin_key(monkeypatch):
    monkeypatch.setenv(fal_usage.ADMIN_KEY_ENV, FAKE_ADMIN_KEY)
    return FAKE_ADMIN_KEY


@pytest.fixture
def no_admin_key(monkeypatch):
    monkeypatch.delenv(fal_usage.ADMIN_KEY_ENV, raising=False)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MANIFEST_PATH", tmp_path / "storyboard_manifest.json")
    return tmp_path


def _strings(value):
    """Every string anywhere inside a nested payload, keys included."""
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for k, v in value.items():
            out.append(str(k))
            out.extend(_strings(v))
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            out.extend(_strings(v))
    else:
        out.append(str(value))
    return out


# --- THE CREDENTIAL. These run first because they are the ones that matter. ------

def test_the_admin_key_never_reaches_any_string_this_module_produces(admin_key, capsys):
    """The one test protecting the credential.

    A sweep, not a checklist. Every failure mode ``fal_usage`` has -- refusal,
    unreachable, malformed body, a raised transport exception, a plain success --
    is driven, and EVERY string in EVERY resulting payload is searched for the
    key, along with anything printed. Enumerating the paths a reviewer thought of
    is how the storage-gate leak survived review: the leak was in the arm nobody
    listed.
    """
    def refuse(url, params=None):
        return _Resp(403, {"detail": "forbidden"})

    def explode(url, params=None):
        # The realistic shape of the leak: a transport error whose text quotes
        # the request it failed on. httpx does not put headers in there -- but
        # this module must not be relying on that, because a redirect, a proxy
        # wrapper or a future httpx could.
        raise RuntimeError(
            f"connect failed for {fal_usage.USAGE_URL} "
            f"(Authorization: Key {FAKE_ADMIN_KEY})")

    def garbage(url, params=None):
        return _Resp(200, None, raises=True)

    payloads = [
        fal_usage.fetch("a", "b", get=refuse),
        fal_usage.fetch_quietly("a", "b", get=explode),
        fal_usage.fetch("a", "b", get=garbage),
        fal_usage.fetch("a", "b", get=_one_page()),
        fal_usage.unavailable("boom", f"raw text containing {FAKE_ADMIN_KEY}"),
    ]

    leaked = [text for p in payloads for text in _strings(p)
              if FAKE_ADMIN_KEY in text]
    assert not leaked, (
        f"the admin key appears in {len(leaked)} string(s) this module returned: "
        f"{leaked[:2]}. These payloads are served over an "
        f"--allow-unauthenticated GET; a credential in one of them is published.")

    printed = capsys.readouterr()
    assert FAKE_ADMIN_KEY not in printed.out + printed.err, (
        "the admin key was written to stdout/stderr, which on Cloud Run is "
        "Cloud Logging")

    # And the same sweep over the whole reconciliation payload, which is the
    # object an HTTP handler actually returns.
    report = reconcile.report("a", "b", get=refuse, project_dirs=[])
    assert not [t for t in _strings(report) if FAKE_ADMIN_KEY in t], (
        "the admin key reached the reconciliation payload")


def test_the_admin_key_is_sent_as_a_header_and_never_in_the_url(admin_key):
    """A key in a query string survives in redirects, proxy logs and
    ``request.url`` on the exception object. It must be a header."""
    seen: dict = {}

    def capture(url, params=None, timeout=None, headers=None):
        seen["url"] = url
        seen["params"] = params or {}
        seen["headers"] = headers or {}
        return _Resp(200, ACCOUNT_PAGE)

    import httpx
    original = httpx.get
    httpx.get = capture
    try:
        fal_usage.fetch("s", "e")
    finally:
        httpx.get = original

    assert FAKE_ADMIN_KEY not in str(seen["url"]), "the key is in the URL"
    assert FAKE_ADMIN_KEY not in json.dumps(seen["params"]), (
        "the key is in the query parameters")
    assert seen["headers"].get("Authorization") == f"Key {FAKE_ADMIN_KEY}", (
        "the key is not being sent at all, so nothing is being read")


def test_redaction_does_not_mangle_text_when_no_key_is_configured(no_admin_key):
    """``"abc".replace("", "X")`` is ``"XaXbXcX"``.

    Without the empty-key guard, the redactor would corrupt every message in the
    single most common configuration -- no admin key at all -- which is also the
    configuration in which the redactor has nothing to do.
    """
    text = "fal answered HTTP 500."
    assert fal_usage._redact(text) == text, (
        "the redactor rewrote a message that contained no key; the empty-key "
        "guard is missing and every error string is now interleaved with the "
        "redaction marker")


# --- absent key: unavailable, not zero, not a crash -----------------------------

def test_no_admin_key_reports_unavailable_rather_than_an_empty_invoice(no_admin_key):
    got = fal_usage.fetch("s", "e")

    assert got["total"] is None, (
        "a missing admin key produced a total. A number here reads as 'fal "
        "billed this much', and the number would be 0.00 -- an unconfigured "
        "feature certifying a clean bill")
    assert got["available"] is False
    assert got["reason"] == fal_usage.NO_KEY
    assert fal_usage.ADMIN_KEY_ENV in got["detail"]


def test_no_admin_key_still_reports_what_we_recorded_and_no_difference(
        no_admin_key, project):
    """Half an answer is available and useful. A difference is not, and must not
    be invented: a 0.00 gap reads as "reconciled"."""
    _attempt(project, cost=0.60, backend="wan_2_7", kind="video")

    got = reconcile.report("2026-08-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00",
                           project_dirs=[project])

    assert got["difference"] is None, (
        "a difference was reported with no invoice to difference against")
    assert got["available"] is False and got["reason"] == fal_usage.NO_KEY
    assert got["recorded"]["total"] == pytest.approx(0.60), (
        "the recorded side went missing too; it is the half that is never "
        "unavailable")
    assert "unavailable" in got["summary"]


def test_an_exception_inside_the_fetch_is_still_an_unavailable_payload(admin_key):
    def explode(url, params=None):
        raise RuntimeError("network is down")

    got = fal_usage.fetch_quietly("s", "e", get=explode)
    assert got["available"] is False and got["total"] is None
    assert got["reason"] == fal_usage.UNREACHABLE


def test_fal_refusing_the_key_is_not_the_same_answer_as_having_no_key(admin_key):
    """Two states that render identically in prose and mean opposite things:
    "you have not configured this" versus "your credential was rejected"."""
    got = fal_usage.fetch("s", "e", get=lambda u, p=None: _Resp(403, {}))
    assert got["reason"] == fal_usage.REFUSED, (
        f"a 403 was reported as {got['reason']!r}; a revoked admin key would "
        f"read to the operator as one that was never set")
    assert got["available"] is False and got["total"] is None


# --- pagination -----------------------------------------------------------------

def test_a_second_page_is_not_dropped(admin_key):
    """``has_more`` with a cursor means there is more invoice. Stopping at page
    one understates the bill, which is the direction that hides money."""
    page1 = {"next_cursor": "c2", "has_more": True,
             "time_series": [_bucket("h1", _item(NANO, "images", 10.0, 0.0398))]}
    page2 = {"next_cursor": None, "has_more": False,
             "time_series": [_bucket("h2", _item(NANO, "images", 12.0, 0.0398))]}
    pages = {None: page1, "c2": page2}
    asked: list = []

    def get(url, params=None):
        cursor = (params or {}).get(fal_usage.CURSOR_PARAM)
        asked.append(cursor)
        return _Resp(200, pages[cursor])

    got = fal_usage.fetch("s", "e", get=get)

    assert got["total"] == pytest.approx(22.0 * 0.0398, abs=1e-6), (
        f"page two was dropped: 22 images were billed across two pages and "
        f"only ${got['total']} was totalled")
    assert asked == [None, "c2"], f"pages requested: {asked}"
    assert got["pages"] == 2 and got["complete"] is True
    assert got["rows"][0]["quantity"] == pytest.approx(22.0)


def test_a_cursor_that_does_not_advance_is_reported_partial_not_totalled(admin_key):
    """The failure mode of guessing the request parameter name.

    If ``CURSOR_PARAM`` is wrong, fal ignores it and serves page one forever.
    The loop must notice and say the window is PARTIAL -- a total assembled from
    a repeated page, or one silently truncated, is a bill nobody can trust.
    """
    stuck = {"next_cursor": "same", "has_more": True,
             "time_series": [_bucket("h1", _item(NANO, "images", 10.0, 0.0398))]}
    calls = {"n": 0}

    def get(url, params=None):
        calls["n"] += 1
        return _Resp(200, stuck)

    got = fal_usage.fetch("s", "e", get=get)

    assert got["complete"] is False, (
        "a window fal said was incomplete was reported as complete; the total "
        "is a floor and nothing said so")
    assert calls["n"] <= fal_usage.MAX_PAGES, "the cursor loop did not terminate"
    assert "PARTIAL" in got["note"]
    assert got["available"] is True and got["total"] is not None


def test_a_line_item_that_cannot_be_read_is_counted_not_silently_dropped(admin_key):
    """A dropped line is an understatement of the bill wearing the appearance of
    a complete answer."""
    page = {"next_cursor": None, "has_more": False, "time_series": [
        _bucket("h1",
                _item(NANO, "images", 22.0, 0.0398),
                {"endpoint_id": NANO, "unit": "images", "quantity": "lots",
                 "cost": None})]}

    got = fal_usage.fetch("s", "e", get=_one_page(page))

    assert got["complete"] is False, (
        "an unreadable line item was excluded from the total and the total was "
        "still presented as complete")
    assert got["unparsed_line_items"] == 1
    assert got["total"] == pytest.approx(0.8756, abs=1e-6)


# --- the comparison -------------------------------------------------------------

def _attempt(project_dir, *, cost, backend, kind, beat="s001", shot="s001.01",
             started=None, measured=False, succeed=True, paid=True):
    """One recorded attempt on disk, written through the real ledger."""
    from backend import projects as projmod

    ctx = projmod.ProjectContext.from_manifest(
        Path(project_dir) / "storyboard_manifest.json")
    with projmod.use(ctx):
        att, how = generation.begin(beat_id=beat, shot_id=shot, signature="",
                                    kind=kind, backend=backend, paid=paid,
                                    estimated_cost=cost)
        assert how == "created"
        if started:
            rows = generation.load_attempts(beat)
            for r in rows:
                if r.id == att.id:
                    r.started_at = started
            generation._save_attempts(beat, rows)
        if succeed:
            measurement = None
            if measured:
                measurement = {"cost": cost, "units": 1.0, "unit": "seconds",
                               "request_id": "req-1"}
            generation.succeed(beat, att.id, output="x.mp4",
                               measurement=measurement)
    return att


def test_the_invoice_endpoint_and_the_one_we_call_are_matched_as_one_model(
        admin_key, project):
    """fal bills ``.../v2.1/standard``; we request ``.../v2.1/standard/image-to-video``.

    On exact matching that is two rows -- one recorded with no bill, one billed
    with no record -- which is a fabricated divergence on BOTH sides at once and
    the single most misleading thing this module could print.
    """
    _attempt(project, cost=1.12, backend="kling_2_1_standard", kind="video")

    got = reconcile.report("2026-08-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00",
                           get=_one_page(), project_dirs=[project])
    kling = [r for r in got["rows"] if any("kling" in e for e in r["endpoints"])]

    assert len(kling) == 1, (
        f"the same model was reported as {len(kling)} rows: "
        f"{[r['endpoints'] for r in kling]}. The invoice and the request spell "
        f"the endpoint differently and both rows are wrong.")
    assert kling[0]["on_both_sides"] is True
    assert kling[0]["recorded"]["cost"] == pytest.approx(1.12)
    assert kling[0]["invoiced"]["cost"] == pytest.approx(1.12)
    assert kling[0]["difference"] == pytest.approx(0.0, abs=1e-6)


def test_a_prefix_that_is_not_a_path_segment_is_not_the_same_model():
    """``fal-ai/nano-banana`` is a family of ``fal-ai/nano-banana/edit`` and is
    NOT a family of ``fal-ai/nano-banana-pro``. A bare ``startswith`` says it
    is, and would fold two models' bills into one row."""
    assert not reconcile.same_family(NANO, NANO + "-pro"), (
        "a bare string prefix matched two different models")
    assert reconcile.same_family(NANO, NANO + "/edit")
    assert reconcile.same_family(NANO, NANO)
    assert not reconcile.same_family(NANO, "")


def test_the_difference_is_reported_and_the_ledger_is_left_alone(admin_key, project):
    """The rule the whole module exists to keep: reconciliation is a third fact,
    not a correction. A ledger quietly conformed to the invoice reads clean
    forever and the divergence that mattered is gone."""
    _attempt(project, cost=0.15, backend="nano2", kind="image",
             beat="s001", shot="s001.01")
    before = _ledger_bytes(project)

    got = reconcile.report("2026-08-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00",
                           get=_one_page(), project_dirs=[project])

    assert _ledger_bytes(project) == before, (
        "the generation ledger changed during a reconciliation. Nothing here "
        "may write: an invoice figure over a recorded cost destroys the only "
        "evidence the two ever disagreed")
    nano = [r for r in got["rows"] if NANO in r["endpoints"]][0]
    assert nano["recorded"]["cost"] == pytest.approx(0.15)
    assert nano["invoiced"]["cost"] == pytest.approx(0.8756, abs=1e-6)
    assert nano["difference"] == pytest.approx(0.7256, abs=1e-6), (
        "difference must be invoiced minus recorded, positive when fal billed "
        "more than we recorded")


def _ledger_bytes(project_dir) -> bytes:
    gen = Path(project_dir) / "generation"
    return b"".join(sorted(p.read_bytes() for p in gen.glob("*.json")))


def test_an_attempt_outside_the_window_is_not_counted(admin_key, project):
    """A window that leaks means the recorded side includes spend the invoice
    window never covered, and every difference after it is noise."""
    _attempt(project, cost=0.60, backend="wan_2_7", kind="video",
             beat="s001", shot="s001.01", started="2020-01-01T00:00:00+00:00")
    _attempt(project, cost=1.12, backend="kling_2_1_standard", kind="video",
             beat="s002", shot="s002.01")

    got = reconcile.recorded("2026-08-01T00:00:00+00:00",
                             "2027-01-01T00:00:00+00:00", project_dirs=[project])

    assert got["total"] == pytest.approx(1.12), (
        f"recorded total is ${got['total']}, so the 2020 attempt was counted "
        f"inside a 2026 window")
    assert [r["endpoint_id"] for r in got["rows"]] == [
        "fal-ai/kling-video/v2.1/standard/image-to-video"]


def test_an_image_backend_is_not_filed_under_the_video_fallback(project):
    """``assets.resolve_video_backend`` never returns None -- it falls back to
    seedance. Routing an image attempt through it would file a nano-banana
    charge under seedance and invent a divergence on two endpoints at once."""
    img = _attempt(project, cost=0.0398, backend="nano2", kind="image")
    vid = _attempt(project, cost=0.60, backend="wan_2_7", kind="video",
                   beat="s002", shot="s002.01")

    assert reconcile.endpoint_for(img) == NANO, (
        f"a nano2 still resolved to {reconcile.endpoint_for(img)!r}")
    assert reconcile.endpoint_for(vid) == WAN


def test_recorded_spend_with_no_known_endpoint_is_shown_not_swallowed(
        admin_key, project):
    """Money we recorded and cannot attribute to a model is a gap in the record.
    Dropping it makes the totals look like they reconcile when half of one side
    was simply not displayed."""
    _attempt(project, cost=0.99, backend="", kind="video")

    got = reconcile.report("2026-08-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00",
                           get=_one_page(), project_dirs=[project])
    rows = [r for r in got["rows"] if reconcile.UNATTRIBUTED in r["endpoints"]]

    assert rows and rows[0]["recorded"]["cost"] == pytest.approx(0.99), (
        "unattributable recorded spend vanished from the report")
    assert rows[0]["invoiced"]["cost"] is None, (
        "an unattributable row claimed fal billed $0.00 against it; no invoice "
        "line was matched because no endpoint is known, which is a different "
        "fact entirely")
    assert rows[0]["difference"] is None


def test_at_risk_money_is_reported_beside_the_total_and_not_inside_it(
        admin_key, project):
    """An attempt whose provider outcome nobody recorded may well be on the
    invoice. Folding it into the recorded total hides a real divergence; hiding
    it entirely invents one."""
    from backend import projects as projmod

    ctx = projmod.ProjectContext.from_manifest(
        Path(project) / "storyboard_manifest.json")
    with projmod.use(ctx):
        att, _ = generation.begin(beat_id="s001", shot_id="s001.01", signature="",
                                  kind="video", backend="wan_2_7", paid=True,
                                  estimated_cost=0.60)
        generation.in_doubt("s001", att.id, "worker died mid-generation")

    got = reconcile.recorded("2026-08-01T00:00:00+00:00",
                             "2027-01-01T00:00:00+00:00", project_dirs=[project])

    assert got["total"] == pytest.approx(0.0), (
        f"${got['total']} of unsettled money was counted as recorded spend")
    assert got["at_risk"] == pytest.approx(0.60), (
        "at-risk money disappeared from the report entirely")
    assert any("at risk" in c for c in reconcile.report(
        "2026-08-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00",
        project_dirs=[project])["caveats"])


def test_the_report_says_it_cannot_join_a_line_item_to_an_attempt(
        admin_key, project):
    """fal's line items carry no request id. A report that did not say so would
    imply a precision the data has never supported -- and the next person would
    ask it which attempt caused a divergence."""
    got = reconcile.report("2026-08-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00",
                           get=_one_page(), project_dirs=[project])

    assert any("request id" in c for c in got["caveats"]), (
        f"the report does not state that it cannot join per request: "
        f"{got['caveats']}")
    assert any("account" in c for c in got["caveats"]), (
        "the report does not state that the invoice is account-wide while the "
        "recorded side is only the projects it was given")
    assert any("SUSTAINED" in c for c in got["caveats"])


def test_totals_agree_with_the_account_when_the_record_matches_it(
        admin_key, project):
    """The calibration case: record exactly what the account was billed and the
    difference is zero. A reconciliation that cannot reach zero on matching
    inputs is measuring something else."""
    _attempt(project, cost=1.20, backend="wan_2_7", kind="video",
             beat="s001", shot="s001.01")
    _attempt(project, cost=1.12, backend="kling_2_1_standard", kind="video",
             beat="s002", shot="s002.01")
    _attempt(project, cost=0.8756, backend="nano2", kind="image",
             beat="s003", shot="s003.01")

    got = reconcile.report("2026-08-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00",
                           get=_one_page(), project_dirs=[project])

    assert got["difference"] == pytest.approx(0.0, abs=1e-6), (
        f"difference is ${got['difference']} against an identical record; "
        f"summary was {got['summary']!r}")
    assert got["invoice"]["total"] == pytest.approx(3.1956, abs=1e-6)
    assert got["recorded"]["total"] == pytest.approx(3.1956, abs=1e-6)
    assert "agree to the cent" in got["summary"]


def test_the_summary_names_which_side_is_larger(admin_key, project):
    """"$3.20 vs $2.60" leaves the reader to work out which is which, and half
    of them get it wrong."""
    _attempt(project, cost=0.10, backend="nano2", kind="image")

    got = reconcile.report("2026-08-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00",
                           get=_one_page(), project_dirs=[project])

    assert "fal billed $3.10 MORE" in got["summary"], got["summary"]


def test_an_unreadable_generation_ledger_makes_the_recorded_side_a_floor(
        admin_key, project):
    """A ledger nobody can read is spend nobody can reconcile. Skipping it
    quietly shows the invoice above the record and invites the wrong
    conclusion."""
    gen = Path(project) / "generation"
    gen.mkdir(parents=True, exist_ok=True)
    (gen / "s009.json").write_text(json.dumps({"attempts": [{"nope": 1}]}),
                                   encoding="utf-8")

    got = reconcile.report("2026-08-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00",
                           get=_one_page(), project_dirs=[project])

    assert got["recorded"]["unreadable"], (
        "an unreadable ledger was skipped without a word")
    assert any("floor" in c for c in got["caveats"])
    assert got["complete"] is False


def test_a_default_window_is_a_trailing_span_ending_now():
    fixed = _dt.datetime(2026, 8, 18, 12, 0, tzinfo=_dt.timezone.utc)
    start, end = fal_usage.window(3.0, now=fixed)
    assert start == "2026-08-15T12:00:00+00:00" and end == "2026-08-18T12:00:00+00:00"


# --- the route ------------------------------------------------------------------
#
# Two endpoints, deliberately different methods. Running a reconciliation reaches
# fal's rate-limited, account-wide billing API, and `require_studio_key` only
# enforces X-Studio-Key on non-GET methods while the service is deployed
# --allow-unauthenticated. So the run is a POST and page loads read the GET,
# which never calls fal.

@pytest.fixture
def client(project, monkeypatch):
    from fastapi.testclient import TestClient
    from backend import main as M

    (Path(project) / "storyboard_manifest.json").write_text(
        json.dumps({"title": "T", "shots": []}), encoding="utf-8")
    monkeypatch.setattr(config, "MANIFEST_PATH", Path(project) / "storyboard_manifest.json")
    monkeypatch.setattr(M, "_scan_projects", lambda: [])
    monkeypatch.setattr(M, "_RECONCILIATION", None)
    return TestClient(M.app, raise_server_exceptions=False), M


def test_a_page_load_never_calls_fal_and_never_reports_zero(client, no_admin_key):
    """The GET must be free and must not answer a question nobody asked.

    "Never run" and "found no difference" are opposite facts and render
    identically the moment either is allowed back as zeros.
    """
    api, M = client
    called = {"n": 0}
    monkey = M.reconcile.report
    M.reconcile.report = lambda *a, **k: called.__setitem__("n", called["n"] + 1)
    try:
        body = api.get("/api/spend/reconcile").json()
    finally:
        M.reconcile.report = monkey

    assert called["n"] == 0, "the GET reached out to fal's billing API"
    assert body["ran"] is False and body["reason"] == "not_run"
    assert body.get("difference") is None and body.get("total") is None
    assert body["admin_key_configured"] is False


def test_running_a_reconciliation_is_gated_by_the_studio_key(client, monkeypatch):
    """It is a POST for this reason. As a GET it would be triggerable by anyone
    with the URL, on every page load, against a rate-limited account API."""
    api, M = client
    monkeypatch.setattr(M, "STUDIO_API_KEY", "s3cret")

    refused = api.post("/api/spend/reconcile", json={})

    assert refused.status_code == 401, (
        f"an unauthenticated caller ran a reconciliation (HTTP "
        f"{refused.status_code}); the studio-key middleware only covers non-GET "
        f"methods, so this must not be reachable by GET either")
    assert api.get("/api/spend/reconcile").status_code == 200


def test_the_route_caches_so_repeated_calls_do_not_re_ask_fal(client, admin_key):
    """The usage series is bucketed hourly: re-asking inside the TTL cannot
    learn anything, and fal rate-limits the management API."""
    api, M = client
    calls = {"n": 0}

    def counted(url, params=None):
        calls["n"] += 1
        return _Resp(200, ACCOUNT_PAGE)

    monkeypatch_get = M.reconcile.fal_usage._get
    M.reconcile.fal_usage._get = counted
    try:
        first = api.post("/api/spend/reconcile", json={}).json()
        second = api.post("/api/spend/reconcile", json={}).json()
        third = api.post("/api/spend/reconcile", json={"refresh": True}).json()
    finally:
        M.reconcile.fal_usage._get = monkeypatch_get

    assert calls["n"] == 2, (
        f"fal was called {calls['n']} times for three requests; the second was "
        f"inside the TTL and must have been served from cache, and the third "
        f"asked for a refresh")
    assert first["cached"] is False and second["cached"] is True
    assert third["cached"] is False
    assert second["invoice"]["total"] == pytest.approx(3.1956, abs=1e-6)


def test_the_route_never_puts_the_admin_key_in_a_response(client, admin_key):
    """Belt and braces over the module-level sweep: this is the actual body an
    unauthenticated network peer can receive."""
    api, M = client

    def refuse(url, params=None):
        raise RuntimeError(f"boom Authorization: Key {FAKE_ADMIN_KEY}")

    original = M.reconcile.fal_usage._get
    M.reconcile.fal_usage._get = refuse
    try:
        posted = api.post("/api/spend/reconcile", json={})
        got = api.get("/api/spend/reconcile")
    finally:
        M.reconcile.fal_usage._get = original

    for resp in (posted, got):
        assert FAKE_ADMIN_KEY not in resp.text, (
            "the admin key was served in an HTTP response body")
    assert posted.json()["available"] is False


def test_money_fields_do_not_change_type_with_the_data(admin_key, project):
    """``sum([])`` is the integer ``0``.

    An empty window therefore reported ``"total": 0`` while a populated one
    reported ``0.6`` -- harmless in JSON and confusing everywhere else. The same
    defect was fixed once already in ``generation.spend``; a second money module
    is exactly where it comes back.
    """
    got = reconcile.report("2026-08-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00",
                           get=_one_page(), project_dirs=[project])

    ints = []
    for row in got["rows"]:
        for side in ("recorded", "invoiced"):
            for field, value in row[side].items():
                if isinstance(value, int) and not isinstance(value, bool) \
                        and field in ("cost", "measured", "estimated", "at_risk",
                                      "quantity", "effective_unit_price"):
                    ints.append(f"{row['endpoints'][0]}.{side}.{field}")
    assert not ints, f"these money fields came back as integers: {ints}"
    assert isinstance(got["recorded"]["total"], float)
    assert isinstance(got["invoice"]["total"], float)

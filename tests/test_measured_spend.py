"""What fal billed, versus what we guessed it would (contract §6.1).

Every figure this ledger ever held for a paid generation was this repo's own
arithmetic -- a price table times the duration we ASKED for -- and ``spend()``
printed "billed" over it. fal does report a billed quantity, in the
``x-fal-billable-units`` header on the result fetch, and ``fal_client`` drops
the response headers so nothing ever read it.

The measured pair that motivated all of this:

    kling v2.1 standard   asked 5s   fal billed 5 units    x 0.056 = $0.28
    wan v2.7              asked 4s   fal billed 6.0 units  x 0.10  = $0.60

The wan clip was billed for 6 seconds of a 4-second request. That is why a
measurement is not just a better estimate: the estimate was 33% under, in the
direction that under-reports what a human has spent, and no amount of care in
the price table would have found it.

Assertions here are ordered so the DEFECT-PROVING one runs first. A test that
checks the shape of a dict before it checks whether an estimate is being sold
as a measurement goes red on the wrong line under exactly the regression it
exists to catch.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

from backend import config, fal_billing, generation  # noqa: E402

KLING = "fal-ai/kling-video/v2.1/standard/image-to-video"
WAN = "fal-ai/wan/v2.7/image-to-video"

# Transcribed from real responses, not invented. See the PR body for the request
# ids these came off.
WAN_REQUEST = "01a01871-5487-7162-bdb0-0cd41219c03e"
KLING_REQUEST = "01a01873-45d3-7ac0-a938-62fa337f7299"


class _Resp:
    def __init__(self, status_code=200, headers=None, payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def _fal(units, unit_price, unit="seconds", *, result_status=200,
         pricing_status=200, header=fal_billing.BILLABLE_UNITS_HEADER):
    """A stand-in for fal answering the two calls a measurement needs."""

    def get(url, params=None):
        if url == fal_billing.PRICING_URL:
            return _Resp(pricing_status, payload={"prices": [
                {"endpoint_id": (params or {}).get("endpoint_id"),
                 "unit_price": unit_price, "unit": unit, "currency": "USD"}]})
        headers = {} if units is None else {header: units}
        return _Resp(result_status, headers=headers, payload={"video": {"url": "x"}})

    return get


@pytest.fixture(autouse=True)
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MANIFEST_PATH", tmp_path / "storyboard_manifest.json")
    monkeypatch.setenv("FAL_KEY", "test-key")
    return tmp_path


def _paid(estimated_cost=0.40, shot_id="s001.01", signature="sig-a"):
    att, how = generation.begin(beat_id="s001", shot_id=shot_id, signature=signature,
                                paid=True, estimated_cost=estimated_cost)
    assert how == "created"
    return att


# --- what fal returns -------------------------------------------------------------

def test_the_billed_quantity_is_read_from_the_header_not_the_body():
    """The number is in ``x-fal-billable-units``; the body has no billing field.

    This is the whole finding. The body is what ``fal_client.subscribe`` returns
    and it is why nothing here ever read a cost: the amount was in the headers
    the client throws away.
    """
    got = fal_billing.measure(WAN, WAN_REQUEST, get=_fal("6.0", 0.1))

    assert got["cost"] == pytest.approx(0.60), (
        "fal billed 6.0 seconds at $0.10 — the measurement must be $0.60, not "
        "the $0.40 our own price table computes from the 4s we asked for")
    assert got["units"] == 6.0 and got["unit"] == "seconds"
    assert got["request_id"] == WAN_REQUEST


def test_a_four_second_request_billed_for_six_is_reported_as_six():
    """The regression in one line: the estimate is 33% under and the ledger
    would have reported the estimate as a bill."""
    from backend import capabilities

    estimate = capabilities.clip_price("wan_2_7", 4)
    measured = fal_billing.measure(WAN, WAN_REQUEST, get=_fal("6.0", 0.1))["cost"]

    assert measured > estimate, (
        f"fal billed ${measured:.2f} for a request we quoted at ${estimate:.2f}. "
        f"If the measurement does not exceed the estimate here, the test data "
        f"no longer reproduces the case that motivated measuring at all")
    assert estimate == pytest.approx(0.40) and measured == pytest.approx(0.60)


def test_a_kling_five_second_clip_measures_at_the_published_rate():
    got = fal_billing.measure(KLING, KLING_REQUEST, get=_fal("5", 0.056))

    assert got["cost"] == pytest.approx(0.28)
    assert got["units"] == 5.0


@pytest.mark.parametrize("get, why", [
    (_fal(None, 0.1), "the endpoint sets no billable-units header"),
    (_fal("6.0", 0.1, result_status=500), "the result fetch failed"),
    (_fal("6.0", 0.1, pricing_status=403), "the pricing call was refused"),
    (_fal("not-a-number", 0.1), "the header did not parse"),
    (_fal("-3", 0.1), "the header was negative"),
])
def test_a_measurement_that_cannot_be_obtained_is_none_never_a_number(get, why):
    """``None`` is the answer, so the attempt keeps its estimate labelled as one.

    A zero here would read as "fal charged nothing" and a fallback to our own
    figure would launder an estimate into a field that means measured. Both are
    the defect this module exists to remove, relocated to the error path.
    """
    assert fal_billing.measure(WAN, WAN_REQUEST, get=get) is None, why


def test_the_result_url_drops_the_variant_path():
    """fal's queue addresses a request by owner/alias only."""
    assert fal_billing.result_url(KLING, "abc") == (
        "https://queue.fal.run/fal-ai/kling-video/requests/abc")


def test_measure_quietly_never_raises_into_the_paid_path():
    """It runs after the money is gone. An exception here would be recorded as a
    generation that may not have billed -- the opposite of the truth."""

    def explode(url, params=None):
        raise OSError("connection reset")

    assert fal_billing.measure_quietly(WAN, WAN_REQUEST, get=explode) is None


def test_no_measurement_is_attempted_without_a_key():
    """No key, no measurement -- and no exception either."""
    import os

    os.environ.pop("FAL_KEY", None)
    os.environ.pop("FAL_API_KEY", None)
    assert fal_billing.measure(WAN, WAN_REQUEST, get=_fal("6.0", 0.1)) is None


# --- measured is never confused with estimated ------------------------------------

def test_an_estimated_cost_is_never_reported_as_a_measured_one():
    """The guardrail. A ledger with no measurement must report $0.00 measured.

    Ordered first on purpose: if ``measured`` ever picks up the estimate, this
    line is the one that names the money, and it has to be evaluated before any
    assertion about totals or key presence can skip it.
    """
    att = _paid(estimated_cost=0.40)
    generation.succeed("s001", att.id, "a.mp4")

    s = generation.spend("s001")
    assert s["measured"] == 0.0, (
        f"an estimate was reported as measured: ${s['measured']:.2f} of "
        f"${s['spent']:.2f} is claimed to have come from fal, and fal was "
        f"never asked")
    assert s["spend_is_measured"] is False
    assert s["estimated"] == pytest.approx(0.40)
    assert s["spent"] == pytest.approx(0.40)
    assert "ESTIMATED" in s["summary"] and "dashboard" in s["summary"]


def test_a_bare_cost_is_not_a_measurement():
    """``succeed(cost=...)`` still counts toward the total and still does not
    claim provenance. Only a ``measurement`` does.

    This is the shape the old code had: a figure this repo computed, handed to
    ``cost``, indistinguishable on the record from an invoice.
    """
    att = _paid(estimated_cost=0.40)
    generation.succeed("s001", att.id, "a.mp4", cost=0.60)

    s = generation.spend("s001")
    assert s["measured"] == 0.0, (
        "a bare cost= figure has no provenance and must not be reported as "
        "measured against fal")
    assert s["spent"] == pytest.approx(0.60)
    assert generation.for_shot("s001", "s001.01")[0].cost_source == ""


def test_a_measured_cost_is_authoritative_and_the_estimate_survives_beside_it():
    att = _paid(estimated_cost=0.40)
    generation.succeed("s001", att.id, "a.mp4",
                       measurement=fal_billing.measure(WAN, WAN_REQUEST,
                                                       get=_fal("6.0", 0.1)))

    s = generation.spend("s001")
    assert s["measured"] == pytest.approx(0.60), (
        "the measured figure must be what spend() reports, not the $0.40 "
        "estimate that sits beside it")
    assert s["spend_is_measured"] is True
    assert s["estimated"] == 0.0 and s["spent"] == pytest.approx(0.60)

    row = generation.for_shot("s001", "s001.01")[0]
    assert row.estimated_cost == pytest.approx(0.40), (
        "the estimate is kept, not overwritten — the two disagreeing is the "
        "signal that the estimate was wrong")
    assert row.cost_source == "measured"
    assert row.billable_units == 6.0 and row.billing_unit == "seconds"
    assert row.provider_request_id == WAN_REQUEST


def test_a_measured_zero_reports_zero_and_not_the_estimate():
    """fal answering "0 billable units" is a fact about the bill.

    The adversarial object for ``_amount``, which used to read ``cost if cost
    else estimated_cost`` and would therefore fall through a measured zero to a
    number we invented -- on the one row where we actually know.
    """
    att = _paid(estimated_cost=0.40)
    generation.succeed("s001", att.id, "a.mp4",
                       measurement=fal_billing.measure(WAN, WAN_REQUEST,
                                                       get=_fal("0", 0.1)))

    s = generation.spend("s001")
    assert s["measured"] == 0.0 and s["estimated"] == 0.0, (
        f"a measured $0.00 fell through to the estimate: "
        f"${s['estimated']:.2f} is being reported as spent on a request fal "
        f"said it billed nothing for")
    assert s["spend_is_measured"] is True
    assert s["spent"] == 0.0


def test_a_malformed_measurement_leaves_the_attempt_estimated():
    """``measurement`` crosses a network boundary. A payload that does not parse
    must not be recorded as a measured cost."""
    att = _paid(estimated_cost=0.40)
    generation.succeed("s001", att.id, "a.mp4",
                       measurement={"cost": "0.60", "units": "6"})

    s = generation.spend("s001")
    assert s["measured"] == 0.0, (
        "a measurement whose figures are strings was accepted as measured")
    assert s["estimated"] == pytest.approx(0.40)
    assert generation.for_shot("s001", "s001.01")[0].cost_source == ""


def test_a_mixed_ledger_states_both_halves_and_claims_neither_for_the_other():
    measured = _paid(estimated_cost=0.40, shot_id="s001.01", signature="sig-a")
    generation.succeed("s001", measured.id, "a.mp4",
                       measurement=fal_billing.measure(WAN, WAN_REQUEST,
                                                       get=_fal("6.0", 0.1)))
    guessed = _paid(estimated_cost=0.28, shot_id="s001.02", signature="sig-b")
    generation.succeed("s001", guessed.id, "b.mp4")

    s = generation.spend("s001")
    assert (s["measured"], s["estimated"]) == (pytest.approx(0.60),
                                               pytest.approx(0.28)), (
        "a mixed ledger must not report one figure over two different kinds of "
        "knowledge")
    assert s["spend_is_measured"] is False
    assert s["measured_attempts"] == 1 and s["estimated_attempts"] == 1
    assert s["spent"] == pytest.approx(0.88)
    assert "measured against fal" in s["summary"]


def test_certain_and_measured_answer_different_questions():
    """The conflation, named. An all-estimated ledger with nothing unresolved is
    certain about what HAPPENED and knows nothing about what it COST."""
    att = _paid(estimated_cost=0.40)
    generation.succeed("s001", att.id, "a.mp4")

    s = generation.spend("s001")
    assert s["spend_is_certain"] is True and s["spend_is_measured"] is False, (
        "spend_is_certain and spend_is_measured must be able to disagree; if "
        "one implies the other, one key is carrying both questions again")


# --- no backfill ------------------------------------------------------------------

def test_a_row_written_before_measuring_existed_reads_as_estimated():
    """Absent means absent. A historical row is a record of what was believed."""
    generation.generation_dir().mkdir(parents=True, exist_ok=True)
    generation.ledger_path("s001").write_text(
        '{"beat_id": "s001", "attempts": [{"id": "s001.01.a1", '
        '"shot_id": "s001.01", "beat_id": "s001", "attempt": 1, '
        '"status": "succeeded", "paid": true, "cost": 0.0, '
        '"estimated_cost": 0.6, "output": "a.mp4"}]}', encoding="utf-8")

    s = generation.spend("s001")
    assert s["measured"] == 0.0, (
        "a legacy row with no cost_source was counted as measured — an "
        "estimate was backfilled into a field that means actual")
    assert s["estimated"] == pytest.approx(0.60)
    assert generation.for_shot("s001", "s001.01")[0].cost_source == ""


def test_an_unrecognised_cost_source_is_refused_rather_than_guessed():
    """Neither branch is safe to default to, so the ledger refuses to read."""
    generation.generation_dir().mkdir(parents=True, exist_ok=True)
    generation.ledger_path("s001").write_text(
        '{"beat_id": "s001", "attempts": [{"id": "s001.01.a1", '
        '"shot_id": "s001.01", "beat_id": "s001", "attempt": 1, '
        '"cost_source": "probably"}]}', encoding="utf-8")

    with pytest.raises(generation.LedgerUnreadable):
        generation.spend("s001")


# --- the whole chain, end to end --------------------------------------------------
#
# The two tests above and below meet in the middle. A ledger that can HOLD a
# measured cost proves nothing on its own: the value has to travel from fal's
# header, through generate_paid_clip, to the row spend() reads. These check the
# two halves of that journey against the real code on each side, because a
# mutation table over generation.py alone cannot tell a wired feature from an
# unreachable one.

def test_the_real_generate_paid_clip_hands_the_measurement_to_its_caller(
        tmp_path, monkeypatch):
    """The producing half: the real function, with fal stubbed at the wire.

    ``generate_paid_clip`` is monkeypatched out of every other compile test, so
    this is the only place its own body is exercised. If it stopped capturing
    the request id, or stopped calling ``on_billed``, nothing else in the suite
    would notice.
    """
    from backend import assets, director
    from backend.manifest import Camera, Shot

    fal_client = sys.modules["fal_client"]
    monkeypatch.setattr(fal_client, "upload_file", lambda p: "https://x/in.png",
                        raising=False)

    def subscribe(endpoint, arguments=None, with_logs=False, on_enqueue=None):
        assert on_enqueue is not None, (
            "the request id was not captured — without it fal cannot be asked "
            "what it billed, and the human cannot find the line on their "
            "dashboard")
        on_enqueue(WAN_REQUEST)
        return {"video": {"url": "https://x/out.mp4"}}

    monkeypatch.setattr(fal_client, "subscribe", subscribe, raising=False)
    def download(url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"MP4")

    monkeypatch.setattr(assets, "_download", download)
    monkeypatch.setattr(fal_billing, "_get", _fal("6.0", 0.1))

    still = tmp_path / "a.png"
    still.write_bytes(b"PNG")
    monkeypatch.setattr(director.config, "resolve_media", lambda p, s=None: still)

    ds = director.DirectorShot(
        id="s001.01", beat_id="s001", motion_type="ai_video", backend="wan_2_7",
        camera=Camera(move="static", duration=4.0),
        prompt="a hand on drill steel", subject="single jack",
        shot_size="m", purpose="master")
    synth = Shot(scene_id="s001", narration="n", prompt="p",
                 camera=Camera(move="static", duration=4.0))
    synth.draft_image = "a.png"

    seen: list = []
    director.generate_paid_clip(ds, synth, None, tmp_path / "out",
                               log=lambda m: None, on_billed=seen.append)

    assert seen and seen[0] is not None, (
        "the paid path produced no measurement — fal's billed amount was read "
        "and then dropped, which is the defect this change exists to remove")
    assert seen[0]["cost"] == pytest.approx(0.60)
    assert seen[0]["request_id"] == WAN_REQUEST


def test_a_compiled_beat_records_the_measured_cost_in_its_ledger(
        tmp_path, monkeypatch):
    """The consuming half: compile_coverage stores what ``on_billed`` gave it.

    Asserted through ``generation.spend`` -- the surface the studio and the
    recovery routes actually read -- rather than by inspecting the row, so a
    measurement that lands on the record but never reaches the total still fails
    this test.
    """
    from backend import assets, director, motion
    from backend.manifest import Camera, Shot, Storyboard

    monkeypatch.setattr(director.config, "MANIFEST_PATH",
                        tmp_path / "storyboard_manifest.json")
    beat, shot_id = "s011", "s011.01"

    plan = director.CoveragePlan(beat_id=beat, beat_duration=4.0, status="locked")
    ds = director.DirectorShot(
        id=shot_id, beat_id=beat, motion_type="ai_video", backend="wan_2_7",
        camera=Camera(move="static", duration=4.0),
        prompt="a hand on drill steel", subject="single jack",
        shot_size="m", purpose="master")
    ds.draft_variations = ["a.png"]
    ds.chosen_variation = 0
    plan.coverage = [ds]
    director.save_plan(plan)

    sb = Storyboard(title="T", storyboard_approved=True,
                    shots=[Shot(scene_id=beat, narration="n", prompt="p",
                                camera=Camera(move="static", duration=4.0))])

    def fake_paid(ds_, synth, sb_, out_dir, log=print, on_billed=None):
        target = Path(out_dir) / f"{ds_.id}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"PAID-BYTES")
        if on_billed is not None:
            on_billed(fal_billing.measure(WAN, WAN_REQUEST, get=_fal("6.0", 0.1)))
        return target

    monkeypatch.setattr(director, "generate_paid_clip", fake_paid)
    monkeypatch.setattr(assets, "generate_for_shot", lambda *a, **k: None)
    monkeypatch.setattr(motion, "render_shot", lambda *a, **k: None)
    monkeypatch.setattr(director, "normalize_clip", lambda *a, **k: None)
    monkeypatch.setattr(director, "fit_clip", lambda *a, **k: None)
    monkeypatch.setattr(director, "concat", lambda *a, **k: Path("beat.mp4"))

    director.compile_coverage(director.load_plan(beat), sb, tmp_path / "render",
                              log=lambda m: None)

    s = generation.spend(beat)
    assert s["measured"] == pytest.approx(0.60), (
        f"a compile that measured $0.60 against fal reported "
        f"${s['measured']:.2f} measured — the value was obtained at the call "
        f"site and never reached the ledger")
    assert s["spend_is_measured"] is True
    assert generation.for_shot(beat, shot_id)[-1].provider_request_id == WAN_REQUEST


# --- the scene total ---------------------------------------------------------------

def _plan(director, beat_id):
    """A saved coverage plan, because scene_summary skips a beat without one."""
    from backend.manifest import Camera

    plan = director.CoveragePlan(beat_id=beat_id, beat_duration=4.0, status="locked")
    plan.coverage = [director.DirectorShot(
        id=f"{beat_id}.01", beat_id=beat_id, motion_type="ai_video",
        backend="wan_2_7", camera=Camera(move="static", duration=4.0),
        prompt="p", subject="s", shot_size="m", purpose="master")]
    director.save_plan(plan)

def test_a_scene_total_carries_the_split_it_aggregates(tmp_path, monkeypatch):
    """One measured beat plus one estimated beat is not one confident number.

    ``planner._total_spend`` re-derives the scene figures rather than calling
    ``spend()`` again, so it is a second place the split can be dropped — and
    dropping it there re-creates, one level up, exactly the defect the beat
    total just lost.
    """
    from backend import director, planner

    monkeypatch.setattr(director.config, "MANIFEST_PATH",
                        tmp_path / "storyboard_manifest.json")
    _plan(director, "s001")
    _plan(director, "s002")

    a, _ = generation.begin(beat_id="s001", shot_id="s001.01", signature="sig-a",
                            paid=True, estimated_cost=0.40)
    generation.succeed("s001", a.id, "a.mp4",
                       measurement=fal_billing.measure(WAN, WAN_REQUEST,
                                                       get=_fal("6.0", 0.1)))
    b, _ = generation.begin(beat_id="s002", shot_id="s002.01", signature="sig-b",
                            paid=True, estimated_cost=0.28)
    generation.succeed("s002", b.id, "b.mp4")

    total = planner.scene_summary(["s001", "s002"])["spend"]

    assert (total["measured"], total["estimated"]) == (pytest.approx(0.60),
                                                       pytest.approx(0.28)), (
        f"the scene reported ${total['measured']:.2f} measured and "
        f"${total['estimated']:.2f} estimated over one beat of each — the "
        f"split did not survive aggregation")
    assert total["spend_is_measured"] is False, (
        "a scene containing an estimated beat claimed its whole total was "
        "measured against fal")
    assert total["spent"] == pytest.approx(0.88)
    assert total["measured_attempts"] == 1 and total["estimated_attempts"] == 1


def test_an_unreadable_beat_makes_the_scene_measurement_unknown_not_zero(
        tmp_path, monkeypatch):
    """``unknown_spend`` has to answer the new keys too. Zeros there would read
    as "all of it measured, and it was free"."""
    from backend import director, planner

    monkeypatch.setattr(director.config, "MANIFEST_PATH",
                        tmp_path / "storyboard_manifest.json")
    _plan(director, "s001")
    generation.generation_dir().mkdir(parents=True, exist_ok=True)
    generation.ledger_path("s001").write_text("{}", encoding="utf-8")

    total = planner.scene_summary(["s001"])["spend"]

    assert total["measured"] is None and total["estimated"] is None, (
        "a ledger nobody could read reported a measured figure")
    assert total["spend_is_measured"] is False
    assert total["spent"] is None and "unknown" in total["summary"]

"""Paid video must not be dispatched against a price nobody was shown.

THE DEFECT, at the two places it lived:

    backend/main.py:984   generate_fal_and_render -- the batch render loop
    backend/main.py:4074  POST /api/shot/{scene_id}/generate_video

Both resolved a model, built the fal arguments and called
``fal_client.subscribe``. Neither derived a price. Everything built for cost
consent -- ``capabilities.clip_price``, the quote and the ledger sharing one
function, the ``plan_signature`` binding a quote to the plan it was quoted for --
was on the coverage-compile path, and these two went through none of it. Gate 1
was checked on both, and Gate 1 names no number: it says a render budget was
allocated, not that anybody agreed to THIS charge.

The amount is not small, and it moves with a switch in the UI. Both paths honour
the studio's audio toggle, and on ``veo_3_1`` audio is the price: $0.20/s silent,
$0.40/s with audio. An 8s beat is $1.60 or $3.20 depending on a checkbox, and
nothing between the checkbox and fal knew either figure.

WHAT EVERY TEST HERE ASSERTS FIRST, AND WHY
-------------------------------------------
``fal.subscribed == 0``. The defect is a DISPATCH -- money leaving -- not a
status code and not a helper returning a number. A test whose first assertion is
"the response was 400" fails, under the regression, with "expected 400, got 200"
and never mentions that a clip was bought; and a test that only checks
``paid_video.quote()`` arithmetic would pass against the original code, which
computed a perfectly good price nowhere and dispatched anyway. So the cheap
assertions come after the expensive one, in every case. This is the rule
``tests/test_compile_quote_binding.py`` and ``tests/test_gate1_strict_approval.py``
state at length, applied to the paths that did not have it.

WHAT MAKES THESE REACHABLE
--------------------------
``fal`` below is not a mock of the thing under test; it is the provider. It
replaces ``fal_client.subscribe`` at the module main.py actually calls, so a
dispatch counted here is a dispatch that would have been a charge, and
``test_the_provider_stub_is_actually_reached`` proves the harness can see one --
without it, every ``subscribed == 0`` above would be satisfied by a test that
simply never got as far as the paid branch.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

pytest.importorskip("fastapi.testclient")

from backend import capabilities, config, generation, paid_video  # noqa: E402
from backend.manifest import Camera, MotionType, Shot, Storyboard  # noqa: E402

BEAT = "s001"

# Transcribed, not derived. veo_3_1 is the row where the audio toggle is the
# price, and these are the two numbers an 8s beat costs on it:
#
#   $0.20/s silent, $0.40/s with audio, 720p/1080p
#   https://fal.ai/models/fal-ai/veo3.1/image-to-video  (read 2026-08-18)
#
# Written out rather than called out of capabilities, for the reason
# tests/test_fal_tariff.py exists: an expectation computed through the function
# under test passes for any implementation of it.
VEO_8S_SILENT = 1.60
VEO_8S_AUDIO = 3.20
STILL = 0.15


class Fal:
    """The provider. Counts what it was asked to buy, and for how long."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def subscribe(self, endpoint, arguments=None, with_logs=False):
        self.calls.append((endpoint, dict(arguments or {})))
        return {"video": {"url": "https://fal.example/clip.mp4"}}

    def upload_file(self, path):
        return f"https://fal.example/{Path(path).name}"

    @property
    def subscribed(self) -> int:
        return len(self.calls)


def _beat(duration: float = 8.0, model: str = "veo_3_1",
          audio: bool | None = None, scene_id: str = BEAT) -> Shot:
    return Shot(scene_id=scene_id, narration="n", prompt="p",
                motion_type=MotionType.AI_VIDEO, video_model=model,
                video_audio=audio,
                draft_image=f"assets/{scene_id}/still.png",
                camera=Camera(move="static", duration=duration))


@pytest.fixture
def studio(tmp_path, monkeypatch):
    """A real client over a real project, with fal replaced by a counter."""
    from fastapi.testclient import TestClient
    from backend import main as M

    manifest_path = tmp_path / "storyboard_manifest.json"
    monkeypatch.setattr(config, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(config, "ASSETS", tmp_path / "assets")
    monkeypatch.setattr(M, "get_active_manifest_path", lambda: str(manifest_path))
    monkeypatch.setenv("FAL_KEY", "test-key")

    shot = _beat()
    sb = Storyboard(title="T", script_locked=True, storyboard_approved=True,
                    shots=[shot])
    # video_audio defaults to on at the episode level, which is exactly the
    # configuration in which the toggle was reaching fal unpriced.
    sb.render.video_model = "veo_3_1"
    sb.render.video_audio = True
    monkeypatch.setattr(M, "get_current_project", lambda: sb)
    monkeypatch.setattr(M, "save_current_project", lambda _sb: None)
    monkeypatch.setattr(M, "save_shot_assets", lambda _s: None)

    still = tmp_path / "assets" / BEAT / "still.png"
    still.parent.mkdir(parents=True, exist_ok=True)
    still.write_bytes(b"PNG")

    fal = Fal()
    monkeypatch.setattr(M.fal_client, "subscribe", fal.subscribe, raising=False)
    monkeypatch.setattr(M.fal_client, "upload_file", fal.upload_file, raising=False)
    monkeypatch.setattr(M.assets, "_download",
                        lambda url, dest: Path(dest).write_bytes(b"MP4"))
    monkeypatch.setattr(M, "set_active_video_clip", lambda *a, **k: None)
    monkeypatch.setattr(M.assets, "extract_final_frame", lambda *a, **k: None)
    monkeypatch.setattr(M, "_pad_clip_to_beat", lambda *a, **k: None)

    yield TestClient(M.app, raise_server_exceptions=False), sb, shot, fal


def _attempts(beat_id: str = BEAT):
    return generation.load_attempts(beat_id)


def _video_attempts(beat_id: str = BEAT):
    return [a for a in _attempts(beat_id) if a.kind == "video"]


# --- the harness can see a dispatch ---------------------------------------------

def test_the_provider_stub_is_actually_reached(studio):
    """Without this, every `subscribed == 0` below could be vacuous.

    A refusal test proves nothing if the code never reached the paid branch for
    an unrelated reason -- a missing still, an unapproved storyboard, a 404 on
    the beat id. This is the same request as those, differing only in carrying
    the price, and it must buy exactly one clip.
    """
    client, sb, shot, fal = studio
    shot.video_audio = False

    r = client.post(f"/api/shot/{BEAT}/generate_video",
                    json={"accepted_cost": VEO_8S_SILENT})

    assert fal.subscribed == 1, (
        f"the harness never reached fal ({r.status_code}: {r.text[:300]}) -- so "
        f"nothing else in this file is testing a dispatch")
    assert r.json()["ok"] is True


# --- the defect: a dispatch with no quote ---------------------------------------

def test_a_beat_is_not_generated_without_a_price_anyone_confirmed(studio):
    """THE REPRODUCTION. Before the fix this bought a clip and returned ok.

    The studio's confirm dialog said "PAID: Video generation calls fal.ai.
    Continue?" and named no amount, and the request it sent carried none either.
    """
    client, sb, shot, fal = studio

    r = client.post(f"/api/shot/{BEAT}/generate_video", json={})

    assert fal.subscribed == 0, (
        "a paid generation was dispatched for a price nobody was shown or "
        "agreed to")
    assert r.status_code == 400
    body = r.json()
    assert body["cost_unconfirmed"] is True
    # The refusal has to be actionable: it is the only place the human learns
    # what the button they just pressed actually costs.
    assert body["quote"]["estimated_cost"] == VEO_8S_AUDIO
    assert f"{VEO_8S_AUDIO:.2f}" in body["error"]
    assert not _video_attempts(), (
        "nothing was generated, so nothing may be recorded as generated")


def test_a_price_quoted_before_the_audio_toggle_does_not_buy_the_dearer_clip(studio):
    """The specific way this path was wrong, not just that it was ungated.

    veo_3_1 at 8s is $1.60 silent and $3.20 with audio. A client that quoted the
    silent figure and posted it must not buy the audio one -- that is consent
    taken for one amount and another spent, which is the single thing the whole
    cost-gate design exists to prevent.
    """
    client, sb, shot, fal = studio
    shot.video_audio = True

    r = client.post(f"/api/shot/{BEAT}/generate_video",
                    json={"accepted_cost": VEO_8S_SILENT})

    assert fal.subscribed == 0, (
        f"a ${VEO_8S_AUDIO:.2f} generation was bought on consent given for "
        f"${VEO_8S_SILENT:.2f}")
    assert r.status_code == 409
    assert r.json()["quote"]["estimated_cost"] == VEO_8S_AUDIO


def test_a_price_larger_than_the_quote_is_also_a_re_quote(studio):
    """Not ``>=``. An over-payment is a disagreement, not a safety margin.

    A client confirming $3.20 for a call this server would price at $1.60 has
    been shown a figure computed from a different state -- most likely the audio
    toggle as it was a moment ago. Spending the smaller of the two and calling it
    generous still means the number on the button and the number in the ledger
    came from different places, which is the whole thing this gate exists to
    prevent.
    """
    client, sb, shot, fal = studio
    shot.video_audio = False

    r = client.post(f"/api/shot/{BEAT}/generate_video",
                    json={"accepted_cost": VEO_8S_AUDIO})

    assert fal.subscribed == 0, (
        "a generation ran on a confirmed price that does not match what this "
        "request costs")
    assert r.status_code == 409


# A confirmed price is a NUMBER. Every value here is something a client can put
# in a JSON body, and none of them is one.
#
# The truthy entries are the point: `if accepted_cost:` -- the obvious way to
# write this check, and the shape that has already cleared Gate 1 twice in this
# repo (see test_gate1_strict_approval.py) -- accepts all four of them and
# dispatches. `True` is the sharpest: `True == 1` in Python, so a check written
# as a comparison authorises a dollar for a JSON `true`.
_NOT_A_PRICE = [
    ("bool-true", True),
    ("string-of-the-right-number", str(VEO_8S_AUDIO)),
    ("nan", float("nan")),
    ("infinity", float("inf")),
    ("null", None),
    ("empty-string", ""),
    ("list", [VEO_8S_AUDIO]),
    ("dict", {"amount": VEO_8S_AUDIO}),
]


@pytest.mark.parametrize("label,value", _NOT_A_PRICE, ids=[c[0] for c in _NOT_A_PRICE])
def test_something_that_is_not_a_number_does_not_buy_a_clip(studio, label, value):
    client, sb, shot, fal = studio

    r = client.request(
        "POST", f"/api/shot/{BEAT}/generate_video",
        content=json.dumps({"accepted_cost": value}),
        headers={"content-type": "application/json"})

    assert fal.subscribed == 0, (
        f"accepted_cost={value!r} ({label}) bought a paid generation; it is not "
        f"a price, so nobody confirmed one")
    assert r.status_code in (400, 409)


class _EqualsAnything:
    """Compares equal to every value, and is not a number.

    The token list above closes named instances. This closes the CLASS: the
    input space of "things a client might send" is not enumerable, so the
    guarantee has to come from the shape of the check rather than from a longer
    table. Any gate written as ``accepted == quoted`` -- or ``accepted in
    {...}``, or ``float(accepted) == quoted`` in a try/except -- accepts this
    object. ``isinstance`` is the one test it cannot pass, which is why
    ``paid_video.is_a_number`` asks the type first and compares second.

    It is asserted against the predicate rather than posted through the client
    because it has no JSON encoding -- and the route reaches every accepted_cost
    through this one function, so the predicate is where the class is closed.
    """

    def __eq__(self, other):  # noqa: D105
        return True

    def __ne__(self, other):  # noqa: D105
        return False

    def __float__(self):  # a float() coercion must not rescue it either
        return VEO_8S_AUDIO


def test_an_object_that_compares_equal_to_the_quote_is_still_not_a_price():
    imposter = _EqualsAnything()
    assert imposter == VEO_8S_AUDIO, "the fixture must actually defeat =="
    assert not paid_video.is_a_number(imposter)
    assert not paid_video.accepted_matches(imposter, VEO_8S_AUDIO), (
        "an object that merely compares equal to the quote was accepted as the "
        "price a human confirmed")


# --- the confirmed price is the one that is spent and the one recorded ----------

@pytest.mark.parametrize("audio,expected", [(False, VEO_8S_SILENT),
                                            (True, VEO_8S_AUDIO)])
def test_the_confirmed_price_is_what_the_ledger_records(studio, audio, expected):
    """Both directions of the toggle, and the ledger agreeing with the button.

    Recording is the other half of §6.1. Stills bought on the way down already
    landed in the ledger; the clip -- the expensive half -- did not, so
    ``generation.spend()`` for a beat rendered this way reported the $0.15 still
    and none of the $3.20 video.
    """
    client, sb, shot, fal = studio
    shot.video_audio = audio

    r = client.post(f"/api/shot/{BEAT}/generate_video",
                    json={"accepted_cost": expected})

    assert fal.subscribed == 1
    assert r.json()["ok"] is True
    attempts = _video_attempts()
    assert len(attempts) == 1, "the paid clip was bought and not recorded"
    assert attempts[0].estimated_cost == expected, (
        f"the button said ${expected:.2f} and the ledger recorded "
        f"${attempts[0].estimated_cost:.2f}")
    assert attempts[0].paid is True
    assert attempts[0].backend == "veo_3_1"
    # And the request that was priced is the request that was sent.
    _, arguments = fal.calls[0]
    assert arguments["generate_audio"] is audio
    assert arguments["duration"] == "8s"


def test_the_quote_covers_the_still_this_route_also_buys(studio, tmp_path):
    """The route auto-generates drafts, and that is money too.

    A quote naming only the clip understates the button by ``_takes(sb)``
    images -- the same shape as the compile path quoting one still for a shot
    that bought four.
    """
    client, sb, shot, fal = studio
    shot.draft_image = ""
    shot.video_audio = False

    q = client.get(f"/api/shot/{BEAT}/video_quote").json()["quote"]
    assert q["still_cost"] > 0
    assert q["estimated_cost"] == pytest.approx(VEO_8S_SILENT + q["still_cost"])

    r = client.post(f"/api/shot/{BEAT}/generate_video",
                    json={"accepted_cost": VEO_8S_SILENT})

    assert fal.subscribed == 0, (
        "the clip was bought against a price that left out the stills the same "
        "request buys")
    assert r.status_code == 409


# --- the batch render loop ------------------------------------------------------

def _render_sb(monkeypatch, tmp_path, shots):
    from backend import main as M
    sb = Storyboard(title="T", script_locked=True, storyboard_approved=True,
                    shots=shots)
    sb.render.video_model = "veo_3_1"
    sb.render.video_audio = False
    monkeypatch.setattr(M, "get_current_project", lambda: sb)
    return sb


def test_the_batch_render_dispatches_nothing_paid_without_an_authorisation(
        studio, monkeypatch, tmp_path):
    """THE REPRODUCTION at main.py:984, called directly.

    ``generate_fal_and_render`` reached fal once per Tier-C beat with no price
    derived anywhere in the function. Called with no authorisation -- which is
    what every caller passed before this change, because there was no parameter
    to pass -- it must now buy nothing.
    """
    from backend import main as M
    client, sb, shot, fal = studio
    shot.video_audio = False

    M.generate_fal_and_render(sb, log=lambda m: None)

    assert fal.subscribed == 0, (
        "the batch render bought paid video without any confirmed price")
    assert not _video_attempts()


def test_the_batch_render_buys_the_beats_it_was_authorised_for(studio):
    from backend import main as M
    client, sb, shot, fal = studio
    shot.video_audio = False

    quote = M.batch_paid_video_quote(sb)
    assert quote["estimated_cost"] == VEO_8S_SILENT
    M.generate_fal_and_render(
        sb, log=lambda m: None,
        authorised=paid_video.Authorisation.accepting(quote["estimated_cost"]))

    assert fal.subscribed == 1
    assert [a.estimated_cost for a in _video_attempts()] == [VEO_8S_SILENT]


def test_a_beat_beyond_the_authorised_total_is_refused_not_bought(studio, tmp_path):
    """The reason the gate is on the DISPATCH and not on the route.

    Two Tier-C beats, an authorisation for one. The loop must buy one and refuse
    the other -- so a batch quoted for two paid beats cannot buy nine, however
    the loop came to visit them, and however wrong the estimate of which beats
    would buy turns out to be.
    """
    from backend import main as M
    client, sb, shot, fal = studio
    shot.video_audio = False

    second = _beat(scene_id="s002", audio=False)
    sb.shots.append(second)
    still = tmp_path / "assets" / "s002" / "still.png"
    still.parent.mkdir(parents=True, exist_ok=True)
    still.write_bytes(b"PNG")

    lines: list[str] = []
    M.generate_fal_and_render(
        sb, log=lines.append,
        authorised=paid_video.Authorisation.accepting(VEO_8S_SILENT))

    assert fal.subscribed == 1, (
        f"{fal.subscribed} paid generations ran against an authorisation for "
        f"one")
    assert not _video_attempts("s002"), (
        "the refused beat must not be recorded as a charge")
    assert any("s002" in ln and "not covered" in ln for ln in lines), (
        f"the refusal has to be said out loud, or it reads as a beat that "
        f"simply did not render: {lines}")


def test_a_render_quoted_as_free_refuses_a_beat_that_turns_out_to_buy(studio):
    """The case the whole dispatch-level design exists for.

    ``_will_buy_paid_video`` is an ESTIMATE: it reads the beat's tier and whether
    a clip is already on disk, at quote time, and the loop can find something
    different when it gets there -- a beat promoted to Tier C in another tab, a
    clip deleted, a duration re-fitted to its narration. Here the render is
    quoted as buying nothing, so no confirmation is asked for and none is given;
    the beat then turns out to be Tier C.

    An authorisation of "nothing" has to mean nothing. If the no-paid-beats case
    handed back a blank cheque instead, this beat would be bought against a
    number the human was never shown -- which is precisely the state this whole
    change is about, arrived at from the other direction.
    """
    from backend import main as M
    client, sb, shot, fal = studio

    shot.motion_type = MotionType.PARALLAX
    authorised, refusal = M.authorise_batch_render(sb, False, None)
    assert refusal is None, "an all-local render needs no price confirmed"

    shot.motion_type = MotionType.AI_VIDEO      # ...and the estimate is now wrong
    M.generate_fal_and_render(sb, log=lambda m: None, authorised=authorised)

    assert fal.subscribed == 0, (
        "a beat the quote did not predict was bought anyway, against a total "
        "nobody was shown")
    assert not _video_attempts()


def test_a_free_beat_still_renders_when_a_paid_one_is_refused(studio, monkeypatch):
    """A refusal is not an abort. The local tiers are free and must go through."""
    from backend import main as M
    client, sb, shot, fal = studio

    rendered: list[str] = []
    monkeypatch.setattr(M.motion, "render_shot",
                        lambda s, **k: rendered.append(s.scene_id))
    sb.shots.append(Shot(scene_id="s002", narration="n", prompt="p",
                         motion_type=MotionType.PARALLAX,
                         draft_image=f"assets/{BEAT}/still.png",
                         camera=Camera(move="push_in", duration=6.0)))

    M.generate_fal_and_render(sb, log=lambda m: None)

    assert fal.subscribed == 0
    assert rendered == ["s002"], (
        "refusing the unquoted paid beat took the free beats down with it")


# --- the batch route ------------------------------------------------------------

def _settle():
    """Join the job workers, so a dispatch in flight is not read as none.

    Without this a test asserting `subscribed == 0` could pass against a render
    that simply had not got there yet -- the worst possible false green for a
    test about money.
    """
    import threading
    me = threading.current_thread()
    for t in threading.enumerate():
        if t is not me and t.name.startswith("job-"):
            t.join(timeout=20)


def test_the_render_route_starts_nothing_paid_without_a_confirmed_total(studio):
    client, sb, shot, fal = studio
    shot.video_audio = False

    r = client.post("/api/assemble/render")
    _settle()

    assert fal.subscribed == 0, (
        "the render button bought paid video against a total nobody was shown")
    assert r.status_code == 400
    assert r.json()["quote"]["estimated_cost"] == VEO_8S_SILENT


def test_the_render_route_buys_on_the_total_it_quoted(studio):
    client, sb, shot, fal = studio
    shot.video_audio = False

    quoted = client.get("/api/render/quote").json()
    assert quoted["paid_beats"] == 1
    r = client.post("/api/assemble/render",
                    params={"accepted_cost": quoted["estimated_cost"]})
    _settle()

    assert fal.subscribed == 1, f"nothing was rendered: {r.text[:300]}"
    assert [a.estimated_cost for a in _video_attempts()] == [VEO_8S_SILENT]


def test_the_render_route_refuses_a_total_that_moved(studio):
    client, sb, shot, fal = studio
    shot.video_audio = True

    r = client.post("/api/assemble/render",
                    params={"accepted_cost": VEO_8S_SILENT})
    _settle()

    assert fal.subscribed == 0, (
        "the audio toggle doubled the bill and the render ran on the old total")
    assert r.status_code == 409


# --- the price is the price of the request being made ---------------------------

@pytest.mark.parametrize("key", sorted(capabilities.VIDEO_CAPS))
def test_the_quote_prices_the_seconds_that_go_on_the_wire(key):
    """Not the beat's editorial length -- the length the model is asked for.

    A 3.34s beat on kling is BILLED for kling's 5s minimum, and a 20s beat on
    seedance is billed for its 15s ceiling. If the quote and the request could
    disagree about that, the number on the button would be for a call nobody
    makes.
    """
    import re
    for wanted in (2.0, 3.34, 5.0, 8.0, 20.0):
        q = paid_video.quote(key, wanted, generate_audio=False)
        args, _ = capabilities.video_arguments(key, wanted, generate_audio=False,
                                               cap_to_ceiling=True)
        on_the_wire = re.match(r"^(\d+)", str(args["duration"])).group(1)
        assert int(on_the_wire) == q.generate_seconds, (
            f"{key} at {wanted}s is quoted for {q.generate_seconds}s and asked "
            f"for {on_the_wire}s")
        assert q.price == pytest.approx(
            q.generate_seconds * capabilities.spec(key)["cost_per_second"], abs=1e-4)


@pytest.mark.parametrize("key", sorted(
    k for k, c in capabilities.VIDEO_CAPS.items() if not c.get("supports_generate_audio")))
def test_audio_is_not_a_price_on_a_model_that_is_never_told_about_it(key):
    """``video_arguments`` omits ``generate_audio`` for these models entirely.

    A caller asking for audio on kling cannot be billed for audio, because kling
    is never asked for any. Charging more here would be an overstatement invented
    by this repo, not a rate fal publishes.
    """
    args, _ = capabilities.video_arguments(key, 5.0, generate_audio=True)
    assert "generate_audio" not in args
    assert (capabilities.clip_price(key, 5.0, generate_audio=True)
            == capabilities.clip_price(key, 5.0, generate_audio=False))


# --- the compile path's quotes did not move -------------------------------------

def test_the_compile_paths_quotes_are_exactly_what_they_were():
    """``clip_price`` gained an argument. No existing answer may have changed.

    Every figure here is a literal, computed by hand from fal's published
    per-second rates -- 8 x 0.20 and 5 x 0.3024 -- rather than through
    ``clip_price`` with the new argument defaulted. An expectation derived from
    the function under test would agree with any default at all, including the
    wrong one.
    """
    assert capabilities.clip_price("veo_3_1", 8.0) == 1.60
    assert capabilities.clip_price("seedance_2_0", 5.0) == 1.512
    assert capabilities.clip_price("kling_2_master", 5.0) == 1.40

    # The compile quote comes through resolve(), which is what
    # director.paid_clip_price and the "COMPILE & SPEND" button both read.
    # director.generate_paid_clip sends generate_audio=False unconditionally, so
    # this must stay the silent rate even though veo now has an audio rate.
    assert (capabilities.resolve({"duration": 8.0}, prefer=["veo_3_1"])
            ["estimated_cost"] == 1.60)
    assert (capabilities.resolve({"duration": 5.0}, prefer=["seedance_2_0"])
            ["estimated_cost"] == 1.512)


def test_a_compiled_shot_is_quoted_at_the_silent_rate(tmp_path, monkeypatch):
    """End to end through the function the compile button actually calls.

    ``quote_shot`` = stills + clip. One identity-uncritical shot buys one still
    at $0.15, and its 8s veo clip is billed silent -- $1.75, the same figure as
    before this change.
    """
    from backend import director
    monkeypatch.setattr(config, "MANIFEST_PATH", tmp_path / "storyboard_manifest.json")

    ds = director.DirectorShot(id="s001.01", beat_id=BEAT, motion_type="ai_video",
                               prompt="p", backend="veo_3_1",
                               camera=Camera(move="static", duration=8.0))
    assert director.quote_shot(ds) == pytest.approx(VEO_8S_SILENT + STILL)
    assert director.paid_clip_price(ds) == VEO_8S_SILENT

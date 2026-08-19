"""Spike C — what each generation backend can actually do, as data.

Model selection used to be split between prose and guesswork. ``VIDEO_BACKENDS``
carried a human-readable ``note`` ("Duration must be 4s/6s/8s"), and the render
path applied one hardcoded ceiling to every model:

    dur_int = max(3, min(10, int(round(target_dur))))

Neither is usable by a router. The prose is unparseable without a regex that
fails silently, and the ceiling is wrong in both directions — it caps an 8s-max
model at 10s and lets a 5s-max model be asked for 10. The same class of gap let
``kling_2_5_turbo_pro``, a key in no registry, resolve by substring match onto
Kling 2.1 Standard and render there silently for months.

This module makes the constraints machine-readable and gives the Director a
resolver that answers one question: *given what this shot needs, which backend is
legal and cheapest, and how many seconds should we actually ask for?*

Two things it deliberately is not:

* **Not an LLM decision.** The planner describes intent — duration, whether a
  character moves, how complex the motion is, what references it needs. Which fal
  endpoint serves that is application logic, so models can change without
  re-prompting or re-training anything.
* **Not authoritative on price.** fal is the authority on billing. ``cost_per_second``
  is this repo's transcription of fal's PUBLISHED tariff for the tier each call
  actually requests, so a plan can be costed before it is run. Every row carries
  ``price_basis``, ``price_source`` and ``price_checked`` — a bare float is not
  auditable, and an unsourced one is how the table came to sit 3x under fal's real
  seedance rate while a quote was derived from it and called an upper bound.
  Published pricing moves; re-check the sources and correct the row.

  (An earlier version of this note said the rates were "env-overridable". They
  never were — they are literals in the table below. The claim is removed rather
  than implemented: one sourced number that someone must edit deliberately is
  safer for money than a number an environment variable can move silently.)

All fields are optional at read time (``spec()`` fills defaults), so existing
callers of the registries are unaffected.
"""

from __future__ import annotations

import os
import re

from . import assets

# --- capability table ----------------------------------------------------------
#
# Keyed to assets.VIDEO_BACKENDS. Anything absent here falls back to CONTINUOUS,
# which is the permissive default: better to attempt a generation and get a clear
# API error than to silently exclude a model because nobody filled in a row.

# `verified: True` means the durations were observed from a real generation.
# Everything else is inferred from documentation or assumed, and assumption is
# exactly how a 3s request came back as a 5s clip and failed a gestural shot.
# Correct an entry the moment a run contradicts it.
CONTINUOUS: dict = {
    "allowed_durations": None,     # None => any integer length in [min, max]
    "min_seconds": 3,
    "max_seconds": 10,
    # The price of a model nobody has priced. Set to the DEAREST rate in the
    # table below rather than to a middling guess: an unregistered key is exactly
    # the case where nothing is known, and the one direction that must not happen
    # is quoting a human less than the call turns out to cost. This is the only
    # figure here that is deliberately conservative rather than published, and it
    # is conservative only within what this table knows.
    "cost_per_second": 0.3024,
    "price_basis": "unknown model — priced at the dearest configured rate",
    "price_source": "",
    "price_checked": "",
    # Same argument, one dimension over: a model nobody has priced, asked for
    # audio, is quoted at the dearest configured WITH-AUDIO rate. Reachable only
    # if a future row declares supports_generate_audio and omits its own audio
    # rate -- test_fal_tariff refuses that -- so this is the backstop under the
    # test rather than the policy.
    "cost_per_second_audio": 0.40,
    "price_basis_audio": "unknown model — priced at the dearest configured "
                         "with-audio rate",
    "needs_start_image": True,
    "supports_reference_image": False,
    "supports_character_reference": False,
    "verified": False,
}

VIDEO_CAPS: dict[str, dict] = {
    "seedance_2_0": {
        # From fal's OpenAPI schema. Duration of the video in seconds. Supports 4 to 15 seconds, or auto to let the model decide based on the prompt.
        "allowed_durations": [4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0],
        "duration_values": ['auto', '4', '5', '6', '7', '8', '9', '10', '11', '12', '13', '14', '15'],
        "min_seconds": 4.0, "max_seconds": 15.0,
        "duration_wire_type": 'string',
        "duration_default": 'auto',
        "supports_generate_audio": True,
        # THE DEFAULT BACKEND, and the row that was 3x wrong: 0.10 against a
        # published 0.3024. A 5s seedance shot was quoted $0.60 after the old
        # floor against ~$1.51 of real billing.
        "cost_per_second": 0.3024,
        "price_basis": "standard tier, 720p (the model's default resolution). "
                       "The fast tier is 0.2419/s and 1080p is 0.682/s — this "
                       "path requests neither, and sends no resolution at all, "
                       "so it gets the 720p default.",
        "price_source": "https://fal.ai/models/bytedance/seedance-2.0/image-to-video",
        "price_checked": "2026-08-16",
        # Audio is free on this one, and that is a transcription, not an
        # assumption: fal's page says "audio generation is included at no extra
        # cost regardless of the generate_audio setting". Stated as its own
        # number anyway, because "the audio rate happens to equal the silent
        # rate" and "nobody filled the audio rate in" must not look the same to
        # a reader or to clip_price.
        "cost_per_second_audio": 0.3024,
        "price_basis_audio": "same as silent — fal includes audio generation at "
                             "no extra cost regardless of generate_audio",
        "price_checked_audio": "2026-08-18",
        "needs_start_image": True,
        "supports_reference_image": False,
        "supports_character_reference": False,
        "verified": True,
    },
    "veo_3_1": {
        # From fal's OpenAPI schema. The duration of the generated video.
        "allowed_durations": [4.0, 6.0, 8.0],
        "duration_values": ['4s', '6s', '8s'],
        "min_seconds": 4.0, "max_seconds": 8.0,
        "duration_wire_type": 'string',
        "duration_default": '8s',
        "supports_generate_audio": True,
        # THE ROW WHERE AUDIO IS THE PRICE. 0.20 is the silent rate and 0.40 is
        # the with-audio rate; on this model the toggle is a 2x difference, the
        # largest in the table.
        #
        # This comment used to say "director.generate_paid_clip sends
        # generate_audio=False unconditionally ... if audio is ever turned on
        # here, this doubles", and stop there. It was true of the compile path
        # and false of the two beat-level paths in main.py, which pass the
        # studio's audio toggle straight through to fal -- so the row's stated
        # basis and the request those paths actually made disagreed, silently,
        # at double. Both figures now live here as numbers, and clip_price picks
        # between them from the generate_audio of the request being priced.
        "cost_per_second": 0.20,
        "price_basis": "720p/1080p WITHOUT audio. With audio it is 0.40/s; 4K is "
                       "0.40 silent and 0.60 with audio.",
        "price_source": "https://fal.ai/models/fal-ai/veo3.1/image-to-video",
        "price_checked": "2026-08-16",
        "cost_per_second_audio": 0.40,
        "price_basis_audio": "720p/1080p WITH audio (this pipeline pins no "
                             "resolution, so it gets that default tier; 4K "
                             "would be 0.60/s with audio)",
        "price_checked_audio": "2026-08-18",
        "needs_start_image": True,
        "supports_reference_image": True,
        "supports_character_reference": False,
        "verified": True,
    },
    "kling_2_1_standard": {
        # From fal's OpenAPI schema. The duration of the generated video in seconds
        "allowed_durations": [5.0, 10.0],
        "duration_values": ['5', '10'],
        "min_seconds": 5.0, "max_seconds": 10.0,
        "duration_wire_type": 'string',
        "duration_default": '5',
        "supports_generate_audio": False,
        # fal states this as a base plus an increment: "$0.28 for 5s, $0.056 for
        # every additional second". A flat 0.056/s reproduces BOTH published
        # figures exactly (5s -> 0.28, 10s -> 0.56), and 5 and 10 are the only
        # lengths this model allows, so nothing is lost by storing it per-second.
        "cost_per_second": 0.056,
        "price_basis": "$0.28 for 5s, +$0.056/s thereafter; exact per-second at "
                       "both allowed lengths",
        "price_source": "https://fal.ai/models/fal-ai/kling-video/v2.1/standard/image-to-video",
        "price_checked": "2026-08-16",
        "needs_start_image": True,
        "supports_reference_image": False,
        "supports_character_reference": False,
        "verified": True,
    },
    "kling_2_master": {
        # From fal's OpenAPI schema. The duration of the generated video in seconds
        "allowed_durations": [5.0, 10.0],
        "duration_values": ['5', '10'],
        "min_seconds": 5.0, "max_seconds": 10.0,
        "duration_wire_type": 'string',
        "duration_default": '5',
        "supports_generate_audio": False,
        # The one row that was already right. Same base-plus-increment shape:
        # "$1.40 for 5s, $0.28 per additional second" -> 0.28/s at 5s and 10s.
        "cost_per_second": 0.28,
        "price_basis": "$1.40 for 5s, +$0.28/s thereafter; exact per-second at "
                       "both allowed lengths",
        "price_source": "https://fal.ai/models/fal-ai/kling-video/v2/master/image-to-video",
        "price_checked": "2026-08-16",
        "needs_start_image": True,
        "supports_reference_image": True,
        "supports_character_reference": True,
        "verified": True,
    },
    "wan_2_7": {
        # From fal's OpenAPI schema. Output video duration in seconds (2-15).
        "allowed_durations": [2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0],
        "duration_values": [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
        "min_seconds": 2.0, "max_seconds": 15.0,
        "duration_wire_type": 'integer',
        "duration_default": 5,
        "supports_generate_audio": False,
        "cost_per_second": 0.10,
        "price_basis": "720p, the resolution this pipeline renders at "
                       "(director.CANON_WIDTH/HEIGHT is 1280x720). 1080p is "
                       "0.15/s; this path sends no resolution, so a change to "
                       "fal's default would move the real bill and not this row.",
        "price_source": "https://fal.ai/models/fal-ai/wan/v2.7/image-to-video",
        "price_checked": "2026-08-16",
        "needs_start_image": True,
        "supports_reference_image": False,
        "supports_character_reference": False,
        "verified": True,
    },
    "luma_dream_machine": {
        # From fal's OpenAPI schema. The duration of the generated video
        "allowed_durations": [5.0, 9.0],
        "duration_values": ['5s', '9s'],
        "min_seconds": 5.0, "max_seconds": 9.0,
        "duration_wire_type": 'string',
        "duration_default": '5s',
        "supports_generate_audio": False,
        # Ray-2 is priced off a 540p base of $0.50 per 5s, doubling per tier:
        # 720p is $1.00 per 5s = 0.20/s, and the 9s option scales proportionally.
        "cost_per_second": 0.20,
        "price_basis": "720p ($1.00 per 5s, 2x the 540p base of $0.50). This path "
                       "sends no resolution; 540p would bill 0.10/s, so this row "
                       "is the dearer of the two plausible tiers.",
        "price_source": "https://fal.ai/models/fal-ai/luma-dream-machine/ray-2/image-to-video",
        "price_checked": "2026-08-16",
        "needs_start_image": True,
        "supports_reference_image": True,
        "supports_character_reference": False,
        "verified": True,
    },
}

# Local tiers. Free, unlimited length, and the reason this pipeline can afford a
# ten-minute episode at all.
LOCAL_COST_PER_SECOND = 0.0

# --- what one draft still costs ------------------------------------------------
#
# Same provenance discipline as the video rows above, and for the same reason: a
# bare float that moves real money is not auditable, and this one was wrong by
# 3.8x for as long as it existed.
#
# It sat at 0.15 -- transcribed from a published page for `nano2`, described in
# assets.py as Gemini 3 Pro Image. The account's own invoice bills the endpoint
# `nano2` ACTUALLY requests, `fal-ai/nano-banana`, at $0.0398 an image. That is
# not a page anyone had to re-read; it is fal's billing API answering for
# itself, and it is the first figure in this repo anchored to what was charged
# rather than to what was advertised.
#
# The direction matters and is stated rather than left to be inferred: an
# OVERSTATEMENT is the safe direction. Every quote a human consented to was
# higher than the real bill, so nobody was ever charged more than they agreed
# to. This is a correctness fix, not an exposure -- which is exactly the
# opposite of the seedance row, where the table was 3x UNDER and consent was
# taken for one amount while another was spent.
#
# Kept env-overridable, as it always was: fal's prices move, and a figure that
# can only be corrected by a deploy goes stale in silence.
IMAGE_PRICE: dict = {
    "endpoint": "fal-ai/nano-banana",
    "cost_per_image": 0.0398,
    "unit": "images",
    "price_basis": "billed rate for the endpoint `nano2` (DEFAULT_BACKEND) "
                   "actually calls -- assets.NANO2_ENDPOINT is "
                   "'fal-ai/nano-banana'. Derived from the account's own line "
                   "items: 22.0 images billed $0.8756 over 2026-08-15..19, "
                   "which is $0.0398/image exactly. Resolution is not a factor "
                   "in the line item; this path requests 2K "
                   "(assets.NANO2_RESOLUTION).",
    "price_source": "fal billing API: GET https://api.fal.ai/v1/models/usage "
                    "(admin key required; see backend/fal_usage.py)",
    "price_checked": "2026-08-18",
}

COST_PER_IMAGE = float(os.environ.get(
    "COST_PER_IMAGE", str(IMAGE_PRICE["cost_per_image"])))

# --- how many units fal actually bills, as OBSERVED --------------------------------
#
# The rate above is right. The QUANTITY was not, and that is a separate defect
# with a separate authority.
#
# `cost_per_second` is fal's published tariff, and every row is checked against
# it. Multiplying it by the duration we REQUEST assumes fal bills the duration we
# request. On the only endpoint where this pipeline has both numbers for a
# below-default request, it does not:
#
#     wan v2.7    requested duration=4    fal billed 6.0 units    (x2, identical)
#     kling 2.1   requested duration=5    fal billed 5   units    (x4, identical)
#
# So a 4s wan shot is quoted 4 x 0.10 = $0.40 and billed 6 x 0.10 = $0.60. A
# correct rate times a wrong quantity, understating by 50%. Kling on the same
# runs billed exactly what was asked, which is the point: this is PER ENDPOINT
# and cannot be assumed either way. A confirmation is worth recording for the
# same reason a discrepancy is.
#
# THE BILLING RULE IS NOT DISCOVERABLE BEFORE THE CALL. Checked, 2026-08-18:
#
# * `GET /v1/models/pricing` returns exactly endpoint_id, unit_price, unit,
#   currency. No minimum, no step, no rounding.
# * `GET /v1/models` (model metadata) returns display name, category, tags,
#   thumbnail. Nothing about billing.
# * The endpoint's own OpenAPI schema documents `duration` as
#   `enum [2..15], default 5, "Output video duration in seconds (2-15)"`. That is
#   what may be REQUESTED. It says nothing about what is BILLED -- and it is
#   where `allowed_durations` above was faithfully transcribed from, which is how
#   a schema that permits 4 became a quote that assumed 4.
# * `POST /v1/models/pricing/estimate` with unit_quantity=4 answers $0.40 --
#   fal's OWN cost estimator reproduces the same wrong figure, because it prices
#   the quantity you hand it and does not know the rule either. Its
#   `historical_api_price` mode answers $0, the account-level usage data being
#   behind the admin key that `/v1/models/usage` 403s on.
#
# So this table records OBSERVATIONS, not a rule. Two requests at one duration
# cannot distinguish a 6-unit floor from a 2-unit step from a 1.5x multiplier,
# and nothing here pretends otherwise: `min_units_observed` is the smallest
# billed quantity ever seen, used as a FLOOR under the quote. A floor can only
# ever over-quote, which is the safe direction, and it is not a bound above it --
# see the note under clip_price.
#
# `units_source` and `units_checked` carry the same provenance obligation as
# `price_source`/`price_checked`: a bare number here is not auditable. The source
# is a fal request id, so anyone can re-fetch the header and check it. Every
# paid Tier-C attempt now records its own `billable_units` (see
# backend.fal_billing), so production fills this table in from real billing
# rather than from anybody's reasoning about it.
BILLED_UNITS: dict[str, dict] = {
    "wan_2_7": {
        "min_units_observed": 6.0,
        "unit": "seconds",
        "observed_at_durations": [4.0],
        "observations": 2,
        "units_source": "x-fal-billable-units on fal requests "
                        "01a01871-5487-7162-bdb0-0cd41219c03e and "
                        "01a01833-50e1-7331-92cc-2681d6227e3d, both "
                        "duration=4, both billed 6.0",
        "units_checked": "2026-08-18",
        "rule_known": False,
    },
    "kling_2_1_standard": {
        "min_units_observed": 5.0,
        "unit": "seconds",
        "observed_at_durations": [5.0],
        "observations": 4,
        "units_source": "x-fal-billable-units on fal requests "
                        "01a01873-45d3-7ac0-a938-62fa337f7299, "
                        "01a0183e-b41a-77d0-998d-76c9f7920b38, "
                        "01a0183d-62c0-73e1-93ba-bd572fd9e58d and "
                        "01a01832-0a0e-7473-a194-238bd53c21b4, all "
                        "duration=5, all billed 5 -- the requested duration WAS "
                        "the billed quantity here",
        "units_checked": "2026-08-18",
        "rule_known": False,
    },
}


def billed_units(key: str, generate_seconds: float) -> tuple[float, str]:
    """How many units to expect to be billed, and why we believe that.

    Returns ``(units, basis)``. ``basis`` is prose for a human reading a job log
    or a quote; it is deliberately not a code, because the honest answer varies
    in kind and not just in value.

    Below the smallest quantity ever observed, the observation wins -- a request
    for 4s of wan is quoted at the 6 units fal has twice billed for exactly that
    request. Above it, we fall back to the requested duration, and say plainly
    that nothing has ever confirmed it.
    """
    seconds = float(generate_seconds)
    # Through spec() so an alias resolves the same way it does for the price. A
    # key that finds its rate but misses its observed units would quote the
    # published tariff while believing it had checked.
    row = BILLED_UNITS.get(spec(key)["key"])
    if not row:
        return seconds, ("no billed quantity has ever been observed for this "
                         "model; quoted at the requested duration")
    floor = float(row["min_units_observed"])
    at = ", ".join(f"{d:g}s" for d in row["observed_at_durations"])
    if seconds in [float(d) for d in row["observed_at_durations"]]:
        # This exact length HAS been billed, and what it billed is on the
        # record. Saying "never observed" here would understate what is known
        # as badly as the old code overstated it.
        return floor, (
            f"quoted at {floor:g} {row['unit']}, which is what fal billed for a "
            f"{seconds:g}s request across {row['observations']} observation(s) "
            f"of exactly this length")
    if seconds < floor:
        return floor, (
            f"quoted at the {floor:g} {row['unit']} floor: fal has never been "
            f"observed to bill this model less, across {row['observations']} "
            f"request(s) at {at}. Whether that is a floor, a step or something "
            f"else is NOT known — see BILLED_UNITS")
    return seconds, (
        f"quoted at the requested {seconds:g}s. fal's billing at this length "
        f"has never been observed ({row['observations']} observation(s), at "
        f"{at}), so this is the published tariff and not a measured quantity")


def tariff_price(key: str, units: float, *, generate_audio: bool = False) -> float:
    """fal's published rate times a quantity of billing units.

    Split out from :func:`clip_price` so the two questions stop sharing one
    function: this one is anchored to fal's published tariff by
    tests/test_fal_tariff.py, and knows nothing about how many units a request
    turns into. ``clip_price`` answers the quantity question first and then
    calls this.

    ``generate_audio`` belongs on THIS side of the split, not the other. It
    selects which published rate a unit is charged at -- veo_3_1 is 0.20/s silent
    and 0.40/s with audio -- and says nothing about how many units there will be.
    See :func:`rate_per_second`.
    """
    rate = rate_per_second(key, generate_audio=generate_audio)
    return round(float(units) * rate, 4)

# --- what one Tier-C generation costs, for the quote AND for the ledger ---------
#
# Two numbers used to answer this question and they disagreed. The quote came
# from ``cost_per_second`` above; the figure written to the generation ledger came
# from a flat ``PAID_CLIP_COST = 0.60`` living in director.py. On the first real
# end-to-end compile the human approved "COMPILE & SPEND $1.99" and the ledger
# then recorded $0.60 for each of the two paid shots, quoted at $0.40 and $0.39 --
# a ~50% understatement on the paid tier.
#
# An overstated quote costs a human an unnecessary hesitation. An understated one
# takes consent for one amount and spends another, which is the single thing Gate
# 1 exists to prevent. So the two figures are collapsed into this one function,
# and every caller on both sides of consent goes through it.
#
# WHAT THIS IS NOT: an upper bound. The first version of this function claimed to
# be one -- max(table, a flat $0.60 floor) -- on the reasoning that taking the
# larger of two figures must be safe. It is not, because max() is only
# conservative if one of its arguments is, and the table was 3x UNDER fal's real
# seedance rate at the time. The floor bounded nothing; it merely papered over
# short requests. The docstring six lines above the table said so already: fal is
# the authority, these are transcriptions. You cannot derive a bound from a source
# that declares itself non-authoritative, and calling the result a bound is worse
# than having no bound at all, because it invites callers to stop checking.
#
# WHAT IT IS: the published tariff for the tier this pipeline actually requests,
# multiplied by the seconds actually requested. It can be wrong in BOTH
# directions -- if fal moves a price, if a model's default resolution changes
# under a request that pins none (see the wan and luma rows), or if this table is
# simply out of date. tests/test_fal_tariff.py transcribes fal's published figures
# independently and fails when the two disagree; that test, not this comment, is
# what keeps the number honest.
#
# The one thing it does guarantee is the thing it was written for: the quote and
# the ledger are the same function of the same inputs, so consent cannot be taken
# for one number while another is recorded.


def rate_per_second(key: str, *, generate_audio: bool = False) -> float:
    """The per-second rate for the request as it will actually be sent.

    Audio is a price dimension on exactly the models that accept an audio flag,
    because ``video_arguments`` omits ``generate_audio`` entirely for the rest --
    a caller asking for audio on kling cannot be billed for audio, since kling is
    never told about it. So the silent rate is the right answer there, and that
    is a fact about this pipeline's request shape, not a rounding-down.

    For a model that DOES take the flag, the rate comes from its own transcribed
    ``cost_per_second_audio``. The fallback underneath is deliberately the dearer
    of (this model's silent rate, the dearest configured audio rate): a row that
    declares it supports audio and does not say what audio costs is the one case
    where nothing is known, and the direction that must not happen is quoting a
    human less than the call turns out to cost.
    """
    caps = spec(key)
    silent = float(caps["cost_per_second"])
    if not generate_audio or not caps.get("supports_generate_audio"):
        return silent
    stated = caps.get("cost_per_second_audio")
    if stated is not None:
        return float(stated)
    return max(silent, float(CONTINUOUS["cost_per_second_audio"]))


def clip_price(key: str, generate_seconds: float, *,
               generate_audio: bool = False) -> float:
    """Best-effort price of one Tier-C generation. Not a bound — see above.

    ``generate_seconds`` is the length actually requested of the model, not the
    editorial length of the shot -- a 3.34s shot billed as kling's 5s minimum is
    billed for five seconds. Callers get that number from
    :func:`legal_durations` / :func:`resolve` / :func:`clamp_duration`, never
    from ``Shot.duration``.

    The requested duration is NOT necessarily the billed quantity, which is a
    thing this function used to assume and fal does not honour: wan v2.7 bills 6
    units for a 4-second request. So the duration goes through
    :func:`billed_units` first, and only then through the published tariff. That
    substitution is the whole of the fix; the rate itself was always right.

    ``generate_audio`` picks WHICH published rate, and the two questions are
    orthogonal by construction: :func:`billed_units` answers how many units the
    request becomes, :func:`rate_per_second` answers what a unit of it costs.
    Audio changes only the second, and only on the models fal is actually sent an
    audio flag for.

    IT DEFAULTS TO FALSE, and the default is load-bearing rather than a
    convenience. Every caller that existed before this argument did --
    :func:`resolve`, and through it ``director.paid_clip_price`` and the quote on
    the compile button -- prices a request that sends ``generate_audio=False``
    unconditionally (``director.generate_paid_clip``). Defaulting the other way
    would move a number a human has already been quoted, in the one function
    where the quote and the ledger are supposed to be the same call. Adding the
    dimension must not change any existing answer; the only new answers are for
    the requests that actually carry audio.

    Still not a bound. Below the observed floor the figure is anchored to a real
    billed quantity; above it, it is the published tariff times a duration
    nothing has ever confirmed. :func:`clip_price_basis` says which you have, and
    a caller taking consent for this number should be prepared to say so.
    """
    units, _ = billed_units(key, generate_seconds)
    return tariff_price(key, units, generate_audio=generate_audio)


def clip_price_basis(key: str, generate_seconds: float) -> str:
    """Why :func:`clip_price` is the number it is, in one sentence, for a human.

    About the QUANTITY only. Which rate that quantity is multiplied by -- silent
    or with audio -- is not in doubt in the way the billed unit count is: it is
    published, transcribed and anchored, and the caller knows which one it asked
    for. What nobody knows is fal's billing rule, and that is what this says.
    """
    return billed_units(key, generate_seconds)[1]


def spec(key: str) -> dict:
    """Capabilities for a video backend, with permissive defaults.

    The key is resolved through ``assets.VIDEO_BACKEND_ALIASES`` HERE rather than
    at the call sites. main.py resolved the alias when building the endpoint URL
    and then passed the RAW string to video_arguments/clamp_duration, so the
    request went to the right model while the limits came from the permissive
    CONTINUOUS fallback -- min 3, max 10, no duration_values, no
    supports_generate_audio. A legacy manifest naming an aliased model could
    therefore still be sent a duration its endpoint rejects, which is precisely
    the failure this table exists to prevent.

    Resolving inside spec() means no caller can get it wrong.
    """
    resolved = key
    try:
        aliases = getattr(assets, "VIDEO_BACKEND_ALIASES", {}) or {}
        if key not in VIDEO_CAPS:
            resolved = aliases.get(key) or aliases.get(str(key).lower()) or key
    except Exception:  # noqa: BLE001 — a missing alias table must not break lookup
        resolved = key
    caps = dict(CONTINUOUS)
    caps.update(VIDEO_CAPS.get(resolved, {}))
    caps["key"] = resolved
    caps["requested_key"] = key
    caps["label"] = (assets.VIDEO_BACKENDS.get(key, {}) or {}).get("label", key)
    caps["supports_extend"] = bool((assets.VIDEO_BACKENDS.get(key, {}) or {}).get("supports_extend"))
    return caps


def legal_durations(key: str, seconds: float) -> tuple[int | None, str]:
    """Nearest legal generation length for ``seconds`` on ``key``.

    Returns ``(length, note)``. ``length`` is None when the request cannot be
    served at all — a 3s shot on a model with a 4s floor, say — which is a fact
    the caller needs, not something to paper over by silently generating 4s.
    """
    caps = spec(key)
    allowed = caps.get("allowed_durations")
    lo, hi = int(caps["min_seconds"]), int(caps["max_seconds"])

    if allowed:
        at_or_above = [d for d in allowed if d >= seconds - 0.01]
        if at_or_above:
            pick = min(at_or_above)
            note = "" if abs(pick - seconds) < 0.05 else f"rounded {seconds:.2f}s up to {pick}s"
            return pick, note
        return None, f"{seconds:.2f}s exceeds the longest {caps['label']} allows ({max(allowed)}s)"

    if seconds > hi:
        # Deliberately None rather than a clamp. Silently serving a 45s request
        # with a 5s clip is how a beat ends up as five seconds of motion and forty
        # seconds of frozen frame — the exact failure this whole layer exists to
        # prevent. The caller needs to know the request cannot be met so it can
        # split the shot or drop it to a free tier.
        return None, (f"{seconds:.2f}s exceeds what {caps['label']} can generate "
                      f"({hi}s maximum)")
    if seconds < lo:
        return lo, f"raised {seconds:.2f}s to the {caps['label']} minimum of {lo}s"
    return int(round(seconds)), ""



def duration_argument(key: str, seconds: float):
    """The value this endpoint wants for ``duration``, in its own spelling.

    The wire type differs per model and is not interchangeable: wan_2_7 declares
    `integer`, veo wants "4s", kling wants "5", seedance wants "5". Sending "3" as
    a string to wan_2_7's integer field was silently ignored, the model used its
    default of 5, and a 3.34s gestural shot came back 5.00s and could not be
    trimmed. Nothing about that is visible without the schema.
    """
    caps = spec(key)
    picked, _ = legal_durations(key, seconds)
    if picked is None:
        return None
    values = caps.get("duration_values") or []
    # Match on the leading number so "4s", "4" and 4 all resolve from one place.
    for v in values:
        m = re.match(r"^(\d+(?:\.\d+)?)", str(v))
        if m and abs(float(m.group(1)) - picked) < 0.01:
            return v
    return int(picked) if caps.get("duration_wire_type") == "integer" else str(int(picked))

def video_arguments(key: str, seconds: float, *, generate_audio: bool = True,
                    cap_to_ceiling: bool = False) -> tuple[dict, str]:
    """The duration/audio half of a fal video request, in this model's own spelling.

    Returns ``(arguments, note)``. The note is empty when the request was served
    exactly, and otherwise says what was changed -- callers log it rather than
    discovering the difference in the finished cut.

    Replaces a hand-written block that lived byte-for-byte at two call sites in
    main.py:

        dur_int = max(3, min(10, int(round(target_dur))))
        arguments = {"duration": str(dur_int), "generate_audio": gen_audio}
        if "veo" in endpoint:      arguments["duration"] = "4s"/"6s"/"8s"
        elif "seedance" not in endpoint:
            arguments.pop("duration"); arguments.pop("generate_audio")

    Four separate defects in nine lines. kling, wan and luma -- three of the six
    registered backends, all selectable from the studio dropdown -- were sent NO
    duration at all, so each rendered its own 5s default and the rest of the beat
    became a freeze-frame. seedance, the default, was sent "3" for any beat under
    3.5s, which is not in its enum (minimum 4) and 422s, losing the beat's clip
    entirely. Every model was capped at 10s though seedance and wan allow 15. And
    veo rounded a 5.0s beat DOWN to "4s" while ``legal_durations`` rounds it up to
    6 -- the same shot came out a different length depending on which path ran it.

    ``cap_to_ceiling`` is the difference between the two callers. Director coverage
    plans a shot to fit a slot, so a request beyond the model's reach means the
    plan is wrong and must be re-routed or split -- omitting the duration is right.
    The batch render has no such option: the beat gets one clip and freeze-frame
    padding for the remainder, so asking for the model's MAXIMUM is strictly better
    than omitting the field and letting it fall back to a 5s default. Without this
    a 20s beat on seedance generated 5s and froze for 15 rather than generating 15
    and freezing for 5.
    """
    caps = spec(key)
    out: dict = {}
    picked, note = legal_durations(key, seconds)
    if picked is None and cap_to_ceiling:
        ceiling = float(caps["max_seconds"])
        picked, note = legal_durations(key, ceiling)
        if picked is not None:
            note = (f"{seconds:.2f}s exceeds what {caps['label']} can generate; "
                    f"asked for its maximum {picked}s (the remainder freeze-frames)")
    if picked is not None:
        dur = duration_argument(key, float(picked))
        if dur is not None:
            out["duration"] = dur
    if caps.get("supports_generate_audio"):
        out["generate_audio"] = bool(generate_audio)
    return out, note


def clamp_duration(key: str, seconds: float) -> int:
    """Legal length, falling back to the model's own ceiling.

    Replaces the hardcoded ``max(3, min(10, ...))`` that treated every model the
    same regardless of its real limits.
    """
    picked, _ = legal_durations(key, seconds)
    return int(picked if picked is not None else spec(key)["max_seconds"])


def resolve(intent: dict, prefer: list[str] | None = None) -> dict:
    """Choose a legal backend for one Director Shot's intent.

    ``intent`` uses the planner's vocabulary, not model names:
        duration, gestural, character_motion, face_visibility,
        motion_complexity, needs_character_reference

    Returns a dict carrying the choice, the length to *generate*, the estimated
    cost, and every constraint that was applied — because a shot whose framing was
    changed by a technical limit must be able to say so. Six months on, nobody
    remembers whether a film is observational by choice or because a model was
    weak in March.
    """
    want = float(intent.get("duration") or 0)
    gestural = bool(intent.get("gestural"))
    needs_charref = bool(intent.get("needs_character_reference"))

    # Ranked on the same figure that is reported, which is only correct because
    # that figure is now the published tariff. It briefly was not: while
    # clip_price applied a flat floor, ranking on it would have collapsed every
    # cheap model into one tie and silently re-routed shots, so the rank was
    # carried separately. With the floor gone there is one number again, and one
    # number is the point of this whole module.
    candidates: list[dict] = []
    for key in (prefer or list(VIDEO_CAPS)):
        caps = spec(key)
        if needs_charref and not caps["supports_character_reference"]:
            continue
        picked, note = legal_durations(key, want)
        if picked is None:
            continue
        # Generating longer than the shot and trimming back is fine for ambient
        # motion and wrong for a gesture: a designed movement cut at 80% stops the
        # head mid-turn. See director.fit_clip, which refuses that trim.
        if gestural and picked > want + 0.05:
            continue
        candidates.append({
            "backend": key, "label": caps["label"], "generate_seconds": picked,
            # No generate_audio here, on purpose and not by omission: this is the
            # coverage-compile quote, and director.generate_paid_clip sends
            # generate_audio=False unconditionally. Priced for the request that
            # path actually makes. The beat-level paths honour the studio's audio
            # toggle and therefore pass their own flag (see backend/paid_video.py).
            "estimated_cost": clip_price(key, picked, generate_audio=False),
            "note": note,
        })

    if not candidates:
        return {
            "backend": None, "generate_seconds": None, "estimated_cost": 0.0,
            "constraints": ["no_legal_backend"],
            "reason": (f"No configured model can produce {want:.2f}s"
                       + (" without trimming a gesture" if gestural else "")),
        }

    ranked = sorted(candidates,
                    key=lambda c: (c["estimated_cost"], c["generate_seconds"]))
    best = ranked[0]
    constraints = []
    if best["note"]:
        constraints.append("duration_quantized")
    if best["generate_seconds"] > want + 0.05:
        constraints.append("will_trim_to_editorial_length")
    return {
        "backend": best["backend"], "label": best["label"],
        "generate_seconds": best["generate_seconds"],
        "estimated_cost": best["estimated_cost"],
        "constraints": constraints,
        "reason": best["note"] or f"{best['label']} at {best['generate_seconds']}s",
        "alternatives": ranked[1:4],
    }


def estimate_shot_cost(motion_type: str, seconds: float, takes: int = 1,
                       backend: str | None = None) -> float:
    """What one Director Shot is expected to cost, stills included."""
    cost = COST_PER_IMAGE * max(1, takes)
    if motion_type == "ai_video":
        r = resolve({"duration": seconds}, prefer=[backend] if backend else None)
        cost += float(r.get("estimated_cost") or 0.0)
    # Four decimals, not three. Every money figure in this repo now rounds to
    # 4dp, matching ``clip_price``. At the old $0.15 an image, 3dp was exact;
    # at the billed $0.0398 it is not, and a quote that rounds a still to
    # $0.040 is a systematic ~0.5% divergence from what fal bills -- which
    # ``backend.reconcile`` would then report as a real, sustained gap on
    # every image endpoint. Manufacturing the exact signal the reconciliation
    # exists to detect is worse than not rounding at all.
    return round(cost, 4)


def table() -> list[dict]:
    """The whole capability table, for the studio and for the contract doc."""
    return [spec(k) for k in assets.VIDEO_BACKENDS]

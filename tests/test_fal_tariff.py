"""What fal publishes, transcribed by hand, against what this repo charges for.

THE POINT OF THIS FILE IS THAT IT IS INDEPENDENT. Every expected figure below is
a literal copied from fal's own pricing page, with the URL and the date it was
read. Nothing here computes an expectation through ``clip_price``,
``cost_per_second`` or anything else in ``capabilities``, because that is exactly
the circularity that let the original defect through: the quote and the ledger
were made to agree with each other, both were checked against each other, and
neither was ever checked against the authority. Two figures agreeing is not
evidence when nothing anchors either one.

It is also why this file is not parametrised over ``VIDEO_CAPS``. Iterating the
table would test the table against itself. The rows are written out.

WHEN THIS FAILS it usually means fal moved a price, not that the code broke.
Re-read the ``source`` URL, correct ``VIDEO_CAPS``, and update the literal and
the date here in the same commit — that is the maintenance this test exists to
force. It cannot detect a price that moved and was never re-read; nothing offline
can. What it does guarantee is that the repo's number and the last transcription
of fal's number cannot drift apart silently.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(m, types.ModuleType(m))

from backend import capabilities  # noqa: E402

CHECKED = "2026-08-16"

# (backend, seconds, dollars, how fal states it, source)
#
# `dollars` is TRANSCRIBED, not derived. Where fal quotes a base plus an
# increment, both endpoints of the range are listed so the shape is covered and
# not just one point on it.
PUBLISHED_TARIFF = [
    (
        "seedance_2_0", 5.0, 1.512,
        "$0.3024/sec, standard tier, 720p default → 5 × 0.3024",
        "https://fal.ai/models/bytedance/seedance-2.0/image-to-video",
    ),
    (
        "seedance_2_0", 10.0, 3.024,
        "$0.3024/sec → 10 × 0.3024",
        "https://fal.ai/models/bytedance/seedance-2.0/image-to-video",
    ),
    (
        "veo_3_1", 8.0, 1.60,
        "$0.20/sec at 720p/1080p WITHOUT audio (this path sends "
        "generate_audio=False) → 8 × 0.20",
        "https://fal.ai/models/fal-ai/veo3.1/image-to-video",
    ),
    (
        "kling_2_1_standard", 5.0, 0.28,
        "'For 5s video your request will cost $0.28'",
        "https://fal.ai/models/fal-ai/kling-video/v2.1/standard/image-to-video",
    ),
    (
        "kling_2_1_standard", 10.0, 0.56,
        "$0.28 for 5s + 5 additional seconds at $0.056",
        "https://fal.ai/models/fal-ai/kling-video/v2.1/standard/image-to-video",
    ),
    (
        "kling_2_master", 5.0, 1.40,
        "'For 5s video your request will cost $1.40'",
        "https://fal.ai/models/fal-ai/kling-video/v2/master/image-to-video",
    ),
    (
        "kling_2_master", 10.0, 2.80,
        "$1.40 for 5s + 5 additional seconds at $0.28",
        "https://fal.ai/models/fal-ai/kling-video/v2/master/image-to-video",
    ),
    (
        "wan_2_7", 5.0, 0.50,
        "$0.10/sec at 720p → 5 × 0.10",
        "https://fal.ai/models/fal-ai/wan/v2.7/image-to-video",
    ),
    (
        "luma_dream_machine", 5.0, 1.00,
        "$0.50 per 5s at 540p, 720p is 2× → $1.00 per 5s",
        "https://fal.ai/models/fal-ai/luma-dream-machine/ray-2/image-to-video",
    ),
]


@pytest.mark.parametrize("key,seconds,dollars,how,source", PUBLISHED_TARIFF)
def test_the_price_this_repo_charges_for_is_the_price_fal_publishes(
        key, seconds, dollars, how, source):
    """The anchor. A quote is only honest if something outside it holds it down.

    ``dollars`` never passes through capabilities. If it did, this test would
    pass for any table at all — which is precisely what happened before it
    existed: a reviewer mutated seedance's price inside clip_price to bill one
    second regardless of duration and all twelve cost tests still passed.
    """
    charged = capabilities.clip_price(key, seconds)
    assert charged == pytest.approx(dollars, abs=0.005), (
        f"{key} at {seconds}s: this repo charges ${charged:.4f}, fal publishes "
        f"${dollars:.4f} ({how}). Re-read {source} and correct VIDEO_CAPS and "
        f"this row together.")


def test_the_defaults_are_not_a_price_anyone_can_be_quoted_quietly():
    """An unregistered model must not be cheap by accident.

    CONTINUOUS is what ``spec()`` falls back to for a key nobody has priced —
    the case where the least is known. It must not undercut anything the table
    does know, or the one model whose cost is a total mystery becomes the one
    the router picks.
    """
    fallback = capabilities.CONTINUOUS["cost_per_second"]
    dearest = max(c["cost_per_second"] for c in capabilities.VIDEO_CAPS.values())
    assert fallback >= dearest, (
        f"an unpriced model is quoted at ${fallback}/s while a known one costs "
        f"${dearest}/s — the router would prefer the mystery")


def test_every_priced_row_says_where_its_number_came_from():
    """A bare float is not auditable, and this table's floats move real money.

    The seedance row sat at 0.10 against a published 0.3024 with nothing on it
    to say who had checked, when, or against what — so nobody could tell a
    verified number from a guess, and a quote was derived from it and called an
    upper bound.
    """
    missing = []
    for key, caps in capabilities.VIDEO_CAPS.items():
        for field in ("price_basis", "price_source", "price_checked"):
            if not str(caps.get(field) or "").strip():
                missing.append(f"{key}.{field}")
    assert not missing, (
        f"these prices cannot be audited back to a source: {missing}")


def test_the_tariff_covers_every_model_that_can_be_billed():
    """A model in the registry and not in the table above is unanchored.

    Adding a backend without adding its published price is how the next
    understatement arrives, and it would arrive green.
    """
    covered = {row[0] for row in PUBLISHED_TARIFF}
    unanchored = sorted(set(capabilities.VIDEO_CAPS) - covered)
    assert not unanchored, (
        f"these backends can be billed and no published price is transcribed "
        f"for them: {unanchored}")


def test_a_price_is_not_a_bound_and_the_code_does_not_claim_it_is():
    """The finding this file was written for, kept from coming back as wording.

    ``max(table, floor)`` was called an upper bound. It was not: max() is only
    conservative if one argument is, and the table was 3× under fal's real
    seedance rate. A claim of safety that is not true is worse than no claim,
    because it stops callers checking.
    """
    src = (Path(__file__).resolve().parent.parent
           / "backend" / "capabilities.py").read_text(encoding="utf-8")
    doc = capabilities.clip_price.__doc__ or ""
    assert "upper bound" not in doc.lower(), (
        "clip_price describes itself as an upper bound again; nothing validates "
        "it against fal, so it cannot be one")
    assert "PAID_CLIP_FLOOR" not in src, (
        "the flat floor is back. It bounded nothing — it only masked short "
        "requests while the table underneath it was wrong")


# --- the draft still, anchored to the INVOICE rather than to a pricing page ------
#
# Every figure above is transcribed from a published page. This one is stronger
# evidence than any of them: it is what the account was actually billed, read
# back from fal's own line items.
#
#     fal-ai/nano-banana    22.0 images    $0.8756    (2026-08-15..19)
#
# The two literals below are that line, copied. The per-image rate is NOT
# computed from capabilities — dividing this repo's own number by itself is the
# circularity the top of this file exists to refuse. It is the quotient of two
# transcribed invoice figures, which is why it can contradict the code.
#
# It did. `COST_PER_IMAGE` defaulted to 0.15, priced from the model assets.py's
# docstring NAMED (`fal-ai/gemini-3-pro-image-preview`) rather than the endpoint
# `nano2` actually CALLS (`assets.NANO2_ENDPOINT` = `fal-ai/nano-banana`, and
# nano2 is `DEFAULT_BACKEND`). Every still in every quote was 3.8x over.
INVOICE_IMAGES = 22.0
INVOICE_IMAGE_DOLLARS = 0.8756
INVOICE_IMAGE_ENDPOINT = "fal-ai/nano-banana"
INVOICE_WINDOW = "2026-08-15..2026-08-19"
INVOICE_SOURCE = ("fal billing API: GET https://api.fal.ai/v1/models/usage "
                  "(admin key)")


def test_a_draft_still_is_charged_what_fal_billed_for_one():
    """The anchor for the image tier. Transcribed, never derived.

    Overstating is the safe direction — no human was ever charged more than a
    quote they consented to — which is why this is a correctness fix and not an
    exposure. It is still wrong, and a quote that is wrong by 3.8x sizes every
    episode against a budget that does not exist.
    """
    billed_per_image = INVOICE_IMAGE_DOLLARS / INVOICE_IMAGES
    assert capabilities.COST_PER_IMAGE == pytest.approx(billed_per_image, abs=1e-6), (
        f"this repo charges ${capabilities.COST_PER_IMAGE} per draft still; fal "
        f"billed {INVOICE_IMAGES} images at ${INVOICE_IMAGE_DOLLARS} over "
        f"{INVOICE_WINDOW}, which is ${billed_per_image:.4f} each. Re-read the "
        f"invoice ({INVOICE_SOURCE}) and correct capabilities.IMAGE_PRICE and "
        f"this row together.")


def test_the_endpoint_that_price_belongs_to_is_the_one_a_draft_still_calls():
    """The mapping the price depends on, checked rather than assumed.

    If `nano2` resolved somewhere else in some path, the invoice line above
    would be pricing a different model and the correction would be wrong. It
    does not: DEFAULT_BACKEND is `nano2`, the registry maps it to
    NANO2_ENDPOINT, and NANO2_ENDPOINT is what the invoice bills.
    """
    from backend import assets

    assert assets.NANO2_ENDPOINT == INVOICE_IMAGE_ENDPOINT, (
        f"draft stills now call {assets.NANO2_ENDPOINT!r}, not the "
        f"{INVOICE_IMAGE_ENDPOINT!r} the price above was read off. The rate is "
        f"no longer anchored to anything.")
    assert assets.DEFAULT_BACKEND == "nano2"
    assert assets.IMAGE_BACKENDS["nano2"]["endpoint"] == INVOICE_IMAGE_ENDPOINT
    assert capabilities.IMAGE_PRICE["endpoint"] == INVOICE_IMAGE_ENDPOINT


def test_the_image_price_says_where_its_number_came_from():
    """Same rule as the video rows: a bare float that moves money is not
    auditable, and this one sat unsourced at 3.8x the billed rate."""
    missing = [f for f in ("price_basis", "price_source", "price_checked")
               if not str(capabilities.IMAGE_PRICE.get(f) or "").strip()]
    assert not missing, (
        f"the draft-still price cannot be audited back to a source: {missing}")


def test_the_script_stage_quotes_stills_at_the_same_rate_as_the_director():
    """Two literals of one price is how a correction reaches one quote and not
    the other. `script.COST_PER_IMAGE` was its own
    `os.environ.get("COST_PER_IMAGE", "0.15")`, so fixing capabilities alone
    would have left every budget plan sizing stills 3.8x over."""
    from backend import script

    assert script.COST_PER_IMAGE == capabilities.COST_PER_IMAGE, (
        f"the script stage quotes stills at ${script.COST_PER_IMAGE} while the "
        f"Director charges ${capabilities.COST_PER_IMAGE}")

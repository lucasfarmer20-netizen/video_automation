"""Gate 1 only opens on an approval somebody actually gave. Contract §5.4.

The defect this file pins, reproduced exactly:

    s = Shot(scene_id='s001', motion_type=MotionType.AI_VIDEO,
             video_model='seedance_2_0')
    s.approved = 'no'
    Storyboard(title='T', storyboard_approved=True, shots=[s]).gate_cleared()
    #  ->  True

`gate_cleared()` read `s.approved` for its truth value, and every non-empty
string is truthy. The beat said no; Python said yes; the paid video API was
called. `storyboard_approved` had the same defect one line above, where
`if not "no"` is False and so never short-circuits.

WHY THIS FILE EXISTS SEPARATELY FROM test_manifest.py
-----------------------------------------------------
test_manifest.py covers the same predicate and covers it well -- every position
of every list length, so no fixed-prefix weakening survives. What it could not
catch is this defect, and the reason is worth stating because it is the mistake
being corrected: its approval fixture is `approved: bool = True`, and every case
it generates passes either `True` or `False`. A parametrization over one axis
proves the predicate handles that axis. It says nothing about values off it, and
the whole loose-value space -- `"no"`, `1`, `[]`, `{"by": "vesper"}` -- sat off
it. So the axis here is the VALUE, and it is deliberately wide.

HOW THE CASES ARE ORDERED, AND WHY IT MATTERS
---------------------------------------------
Every test below asserts the gate is SHUT first, before any fixture-integrity
assertion. A test whose first assertion is "the helper still builds a clearing
beat" would fail under a broken helper for a reason that has nothing to do with
Gate 1, and a reader chasing the failure would land in the wrong place. The
defect-proving assertion runs first so that a failure here means the gate moved.

And an assertion only protects something if it is REACHED.
`test_every_truthy_case_would_have_opened_the_old_gate` runs the pre-fix
predicate verbatim against the same case list: a value that does not clear THAT
gate never exercised the defect, and its case here would pass against the
original code while proving nothing. That test is the one that keeps the rest of
this file honest -- half of the loose-value space is falsy and was always safe by
accident, and without it a case list that drifted entirely falsy would still look
green.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import manifest as M  # noqa: E402
from backend.manifest import (  # noqa: E402
    MotionType,
    Shot,
    Storyboard,
    approval_is_explicit,
)


# --- the loose-value space ------------------------------------------------------
#
# (id, value, truthy). `truthy` is not decoration: it is asserted against
# `bool(value)` below, and it is what
# test_every_truthy_case_would_have_opened_the_old_gate uses to say which of
# these actually reproduce the defect rather than merely being safe already.
#
# Both halves are here on purpose. The truthy half is the defect. The falsy half
# was already shut -- by accident, not by design -- and it still has to be
# covered, because `from_dict` now has to turn it into a real `False` rather than
# leaving `0` or `[]` in the field for `to_dict` to write back out.
_LOOSE: list[tuple[str, object, bool]] = [
    # Truthy: every one of these opened Gate 1 on a paid beat before the fix.
    ("str-no", "no", True),
    ("str-false", "false", True),
    ("str-true", "true", True),
    ("str-pending", "pending", True),
    ("int-1", 1, True),
    ("float-1", 1.0, True),
    ("list-nonempty", ["vesper"], True),
    ("dict-nonempty", {"by": "vesper", "at": "2026-08-16"}, True),
    # Falsy: shut before the fix too, but for the wrong reason.
    ("str-empty", "", False),
    ("int-0", 0, False),
    ("none", None, False),
    ("list-empty", [], False),
    ("dict-empty", {}, False),
]

_LOOSE_IDS = [name for name, _, _ in _LOOSE]
_LOOSE_VALUES = [value for _, value, _ in _LOOSE]

# Positions and lengths, so a gate that checks only some of the paid beats is
# caught on the value axis too. Small on purpose -- test_manifest.py already
# closes the fixed-prefix class up to six with its own cross, and repeating that
# width against thirteen values would buy nothing but collection time.
_LXP = [(n, i) for n in (1, 2, 3) for i in range(n)]
_LXP_IDS = [f"{n}paid-break{i}" for n, i in _LXP]


def _paid(scene_id: str, *, approved: object = True,
          video_model: str | None = "seedance_2_0") -> Shot:
    """A Tier-C (paid) beat, gate-clearing unless told otherwise.

    ``approved`` is typed ``object`` rather than ``bool`` precisely because this
    file's job is the values that are not booleans."""
    s = Shot(scene_id=scene_id, motion_type=MotionType.AI_VIDEO,
             video_model=video_model)
    s.approved = approved
    return s


def _free(scene_id: str) -> Shot:
    """A local-tier beat. Costs nothing, so it may never hold the gate shut."""
    return Shot(scene_id=scene_id, motion_type=MotionType.PARALLAX)


def _paid_dict(scene_id: str, **over) -> dict:
    """The JSON shape of a gate-clearing paid beat, as a manifest carries it."""
    return {"scene_id": scene_id, "motion_type": "ai_video",
            "video_model": "seedance_2_0", "approved": True, **over}


def _siblings_are_ready(shots: list[Shot], broken_at: int) -> bool:
    """Every beat except ``broken_at`` clears the gate on its own.

    The mixed-list cases assert a SHUT gate, which a broken `_paid()` would also
    produce. This pins that exactly one beat is holding it shut."""
    rest = [s for i, s in enumerate(shots) if i != broken_at]
    return Storyboard(title="T", storyboard_approved=True,
                      shots=rest).gate_cleared() is True


def _old_gate(sb: Storyboard) -> bool:
    """The pre-fix predicate, verbatim. The reference the defect is measured against.

    Kept as a literal copy rather than imported, because the point is to compare
    against code that no longer exists."""
    if not sb.storyboard_approved:
        return False
    return all(
        s.approved and bool(s.video_model)
        for s in sb.shots
        if s.needs_paid_video()
    )


# --- the case list is itself load-bearing ----------------------------------------

def test_the_truthiness_labels_on_the_case_list_are_correct():
    """`truthy` is asserted, not asserted-by-comment. Mislabel one and the test
    below would silently stop demanding that it reproduces the defect."""
    for name, value, truthy in _LOOSE:
        assert bool(value) is truthy, name


def test_every_truthy_case_would_have_opened_the_old_gate():
    """Each truthy case genuinely reproduces the shipped defect on a paid beat.

    This is the reachability check for the whole file. Without it, a case list
    that drifted entirely falsy -- or a `_paid()` helper that stopped building a
    paid beat at all -- would leave every test below passing against the ORIGINAL
    predicate, proving nothing. Here that shows up as a failure on the exact case
    that stopped exercising the bug.

    It also states the asymmetry plainly: the falsy half was shut before the fix,
    so those cases pin normalisation, not the gate."""
    for name, value, truthy in _LOOSE:
        sb = Storyboard(title="T", storyboard_approved=True,
                        shots=[_paid("s001", approved=value)])
        assert _old_gate(sb) is truthy, name
    assert any(truthy for _, _, truthy in _LOOSE), (
        "no case in _LOOSE reproduces the defect; this file would be vacuous")


def test_the_paid_fixture_clears_the_gate_when_approval_is_a_real_boolean():
    """The floor under every 'gate is shut' assertion below: with `True`, this
    exact fixture opens Gate 1. Otherwise a shut gate proves nothing."""
    sb = Storyboard(title="T", storyboard_approved=True,
                    shots=[_paid("s001"), _paid("s002"), _free("s900")])
    assert sb.gate_cleared() is True


# --- approval_is_explicit ---------------------------------------------------------

@pytest.mark.parametrize("value", _LOOSE_VALUES, ids=_LOOSE_IDS)
def test_nothing_loose_counts_as_an_approval(value):
    assert approval_is_explicit(value) is False


def test_only_the_boolean_true_counts_as_an_approval():
    assert approval_is_explicit(True) is True
    assert approval_is_explicit(False) is False


# --- gate_cleared(): the last line of defence ------------------------------------

@pytest.mark.parametrize("value", _LOOSE_VALUES, ids=_LOOSE_IDS)
@pytest.mark.parametrize("n_paid,broken_at", _LXP, ids=_LXP_IDS)
def test_gate_shut_when_any_paid_beats_approval_is_not_a_boolean(
        value, n_paid, broken_at):
    """The defect itself, at every position of every length up to three.

    `/api/approve` (backend/main.py:4315) assigns `s.approved` on a live
    Storyboard, so a beat can reach this predicate without ever passing
    `from_dict`. That is why the check here is not redundant with the boundary
    one, and why this test builds its shots in memory rather than loading them."""
    paid = [_paid(f"s{i + 1:03d}") for i in range(n_paid)]
    paid[broken_at].approved = value
    sb = Storyboard(title="T", storyboard_approved=True,
                    shots=[*paid, _free("s900")])
    assert sb.gate_cleared() is False
    assert _siblings_are_ready(paid, broken_at)


@pytest.mark.parametrize("value", _LOOSE_VALUES, ids=_LOOSE_IDS)
def test_gate_shut_when_the_storyboard_approval_is_not_a_boolean(value):
    """The project-level term, which fails the same way one line earlier: with
    `"no"`, `if not self.storyboard_approved` is False, so the short-circuit
    never fires and a fully-ready paid beat carries the gate open."""
    sb = Storyboard(title="T", shots=[_paid("s001"), _free("s900")])
    sb.storyboard_approved = value
    assert sb.gate_cleared() is False
    assert Storyboard(title="T", storyboard_approved=True,
                      shots=list(sb.shots)).gate_cleared() is True


def test_a_loose_approval_does_not_hold_the_gate_shut_for_free_beats():
    """No over-correction. A local-tier beat costs nothing at the video API, so
    its approval field -- loose or not -- is not a term in Gate 1 and must not
    become one. Otherwise the fix would block episodes that spend no money."""
    free = _free("s001")
    free.approved = "no"
    sb = Storyboard(title="T", storyboard_approved=True, shots=[free])
    assert sb.gate_cleared() is True


# --- from_dict(): the boundary ----------------------------------------------------

@pytest.mark.parametrize("value", _LOOSE_VALUES, ids=_LOOSE_IDS)
def test_a_loose_approval_never_becomes_an_approved_shot(value):
    """Validate on read, so the bad value never reaches the object at all.

    Both storage paths land here: `load()` for local JSON and `load_project()`
    for Firestore both end in `from_dict`."""
    sb = Storyboard.from_dict(
        "p", {"title": "T", "storyboard_approved": True},
        [_paid_dict("s001", approved=value), _paid_dict("s002")])
    assert sb.gate_cleared() is False
    assert sb.shots[0].approved is False
    assert sb.shots[1].approved is True, "only the loose beat may be reset"


@pytest.mark.parametrize("value", _LOOSE_VALUES, ids=_LOOSE_IDS)
def test_a_loose_storyboard_approval_never_becomes_an_approved_project(value):
    sb = Storyboard.from_dict("p", {"title": "T", "storyboard_approved": value},
                              [_paid_dict("s001")])
    assert sb.gate_cleared() is False
    assert sb.storyboard_approved is False


@pytest.mark.parametrize("value", _LOOSE_VALUES, ids=_LOOSE_IDS)
def test_a_loose_approval_is_degraded_rather_than_raised(value):
    """It degrades, and it degrades quietly enough to keep the project readable.

    `get_current_project` (backend/main.py:280-294) answers a manifest that
    exists but will not parse with HTTP 500 rather than overwriting it -- which
    is the right refusal, and which means a `from_dict` that raised on a stray
    value would take the whole project offline until a human edited the file by
    hand. Refusing to open Gate 1 costs a click; this would cost the episode.

    That trade is only defensible because the degrade runs toward the safe side,
    which the two tests above are what actually pin."""
    sb = Storyboard.from_dict("p", {"title": "T", "storyboard_approved": value},
                              [_paid_dict("s001", approved=value)])
    assert isinstance(sb, Storyboard)
    assert [s.scene_id for s in sb.shots] == ["s001"], "the beat must survive"


def test_an_absent_approval_is_not_approved():
    """Absent is the oldest shape a manifest can have, and it is not a decision."""
    payload = {"scene_id": "s001", "motion_type": "ai_video",
               "video_model": "seedance_2_0"}
    assert "approved" not in payload
    sb = Storyboard.from_dict("p", {"title": "T", "storyboard_approved": True},
                              [payload])
    assert sb.gate_cleared() is False
    assert sb.shots[0].approved is False


def test_a_real_approval_still_opens_the_gate_through_from_dict():
    """The other direction of over-correction: a manifest a human genuinely
    approved must still clear, or the fix has broken every project."""
    sb = Storyboard.from_dict(
        "p", {"title": "T", "storyboard_approved": True},
        [_paid_dict("s001"), _paid_dict("s002"),
         {"scene_id": "s003", "motion_type": "parallax"}])
    assert sb.gate_cleared() is True
    assert sb.storyboard_approved is True
    assert [s.approved for s in sb.shots] == [True, True, False]


@pytest.mark.parametrize("value", _LOOSE_VALUES, ids=_LOOSE_IDS)
def test_the_loose_value_does_not_survive_into_what_gets_written_back(value):
    """The degrade is persisted, and that is the intended behaviour rather than
    an accident: the next save writes `false` over the unreadable value, so what
    the manifest says afterwards is what the gate believes.

    A `0` or `[]` left in the field would serialise straight back out and reach
    the next reader, and `json.dumps` would happily write it. This asserts the
    field is a real `bool` -- `is False`, not merely falsy -- so it round-trips
    as JSON `false`."""
    sb = Storyboard.from_dict("p", {"title": "T", "storyboard_approved": value},
                              [_paid_dict("s001", approved=value)])
    written = json.loads(json.dumps(sb.to_dict()))
    assert written["shots"][0]["approved"] is False
    assert written["storyboard_approved"] is False


# --- the note: a re-approval must be legible, not mysterious ----------------------

@pytest.mark.parametrize("value,truthy", [(v, t) for _, v, t in _LOOSE
                                          if v is not None],
                         ids=[n for n, v, _ in _LOOSE if v is not None])
def test_a_wrong_type_is_said_out_loud(value, truthy, capsys):
    """A beat that flips from approved to unapproved must say why.

    This is the whole answer to "the user will be asked to re-approve and will
    not know why". The note names the field, the value and the beat, so the
    re-approval is a five-second explanation rather than a bug report."""
    Storyboard.from_dict("p", {"title": "T", "storyboard_approved": value},
                         [_paid_dict("s001", approved=value)])
    out = capsys.readouterr().out
    assert "approved" in out and repr(value) in out, out
    assert "s001" in out, out


@pytest.mark.parametrize("value", [True, False, None], ids=["true", "false", "null"])
def test_a_boolean_or_a_null_is_not_worth_a_note(value, capsys):
    """`null` and absent both mean "no decision recorded", which is what the
    default already says -- and every other nested field in this loader coerces
    null to its empty form without comment. A note that fires on the idiom the
    loader accepts everywhere else is a note people learn to ignore, and this one
    has to still be readable on the day it means something."""
    Storyboard.from_dict("p", {"title": "T", "storyboard_approved": value},
                         [_paid_dict("s001", approved=value)])
    assert "not a boolean" not in capsys.readouterr().out


# --- through real files, the way a manifest actually arrives ---------------------

def test_a_hand_edited_manifest_on_disk_cannot_open_gate_1(tmp_path):
    """End to end, through `load()`: JSON on disk is the store of record in local
    development and the mirror everywhere else, and a hand edit is the one route
    by which a loose value can plausibly get into the field at all."""
    path = tmp_path / "storyboard_manifest.json"
    path.write_text(json.dumps({
        "title": "T", "storyboard_approved": True,
        "shots": [_paid_dict("s001", approved="no")],
    }, indent=2), encoding="utf-8")

    sb = M.load(path)
    assert sb.gate_cleared() is False
    assert sb.shots[0].approved is False

    M.save(sb, path)
    assert json.loads(path.read_text(encoding="utf-8"))["shots"][0]["approved"] is False
    assert M.load(path).gate_cleared() is False


# --- the sidebar, which reads the raw JSON and never passes from_dict ------------

@pytest.mark.parametrize("value,truthy", [(v, t) for _, v, t in _LOOSE],
                         ids=_LOOSE_IDS)
def test_the_sidebar_does_not_badge_a_project_gate_1_refuses_to_open(
        value, truthy, tmp_path, monkeypatch):
    """`_scan_projects` reads the manifest JSON directly, so `from_dict` never
    normalises it -- it is the one remaining place a loose approval is read raw.

    It used to coerce with `bool()`, which lit "Approved ✓" in the sidebar for a
    project Gate 1 now refuses to open. Contract §11.4: the UI must not claim a
    state the system is not in, and the two disagreeing is worse than either
    answer, because the badge is what a user checks before wondering why nothing
    will render.

    Parametrized over the whole space rather than the truthy half, so a
    regression to `bool()` is caught by the eight truthy cases while the falsy
    ones pin that nothing over-corrected."""
    from backend import main as MAIN

    proj = tmp_path / "bestiary" / "leshy"
    proj.mkdir(parents=True)
    mf = proj / "storyboard_manifest.json"
    mf.write_text(json.dumps({
        "title": "Leshy", "channel": "bestiary", "storyboard_approved": value,
        "shots": [_paid_dict("s001", approved=value)],
    }), encoding="utf-8")

    monkeypatch.setattr(MAIN, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(MAIN, "get_active_manifest_path", lambda: str(mf))

    listed = [p for p in MAIN._scan_projects() if p["name"] == "Leshy"]
    assert len(listed) == 1, listed
    assert listed[0]["storyboard_approved"] is False
    assert M.load(mf).gate_cleared() is False, (
        "the badge and the gate must not disagree")


@pytest.fixture
def _sidebar(tmp_path, monkeypatch):
    """A one-project workspace plus a stubbed durable store, for /api/projects."""
    from backend import main as MAIN

    proj = tmp_path / "bestiary" / "leshy"
    proj.mkdir(parents=True)
    mf = proj / "storyboard_manifest.json"
    mf.write_text(json.dumps({
        "title": "Leshy", "channel": "bestiary", "storyboard_approved": False,
        "shots": [_paid_dict("s001", approved=False)],
    }), encoding="utf-8")

    monkeypatch.setattr(MAIN, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(MAIN, "get_active_manifest_path", lambda: str(mf))
    monkeypatch.setattr(MAIN, "get_project_id_from_path", lambda rel: "leshy")

    def badge(doc_value) -> object:
        monkeypatch.setattr(
            MAIN.manifest, "list_projects",
            lambda channel=None: [{"id": "leshy", "title": "Leshy",
                                   "channel": "bestiary",
                                   "storyboard_approved": doc_value}])
        entries = [p for p in MAIN.get_projects()["projects"]
                   if p["name"] == "Leshy"]
        assert len(entries) == 1, entries
        return entries[0]["storyboard_approved"]

    return badge


@pytest.mark.parametrize("value", _LOOSE_VALUES, ids=_LOOSE_IDS)
def test_the_durable_store_cannot_restore_a_badge_the_scan_withheld(_sidebar, value):
    """`/api/projects` refines the scanned entry from the Firestore document, and
    that refinement is a second RAW read -- `manifest.list_projects` returns
    documents, not Storyboards, so `from_dict` never sees them either.

    This arm is the one that decides in practice. A document exists only once a
    project has been bootstrapped into Firestore, which every deployed project
    has been, so this line overwrites whatever `_scan_projects` worked out. Fix
    the scan and leave this and the fix is undone one line later, on exactly the
    projects that are running."""
    assert _sidebar(value) is False


def test_a_real_durable_approval_still_badges_the_project(_sidebar):
    """The refinement still does its job: Firestore is the source of truth for a
    bootstrapped project, and a genuine approval there must reach the sidebar
    even though the scanned manifest on disk says otherwise."""
    assert _sidebar(True) is True

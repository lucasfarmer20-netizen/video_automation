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
this file honest -- a third of the loose-value space is falsy and was always safe
by accident, and without it a case list that drifted entirely falsy would still
look green.

TOKENS CLOSE INSTANCES; THE OBJECT CLOSES THE CLASS
---------------------------------------------------
The first revision of this file made, one level up, the exact mistake it was
written to correct. It replaced a table of `True`/`False` with a table of
thirteen loose values -- and every truthy one of them read as a REFUSAL
(`"no"`, `"false"`, `"pending"`) or as a structure (`1`, `[]`, `{}`). Weaken any
of the four enforcement points to `value is True or value == "yes"` and all
thirteen still passed, with the whole suite green, while a paid Tier-C beat
carrying `approved: "yes"` cleared Gate 1.

The lesson is not "add 'yes'". A denial in the field is the SAFE input, because
anyone reading the manifest can see it is wrong. The dangerous input is the one
that reads as consent, because that is the one somebody writes a sentinel for --
and the token they pick is, by definition, one this list does not contain.

So there are two mechanisms below, and only the second is load-bearing:

  * `_LOOSE` now carries the affirmative tokens as well. That kills the named
    instances and documents the shape for a reader.
  * `_EqualsAnything` compares equal to everything, so ANY equality or
    membership sentinel accepts it -- `== "yes"`, `== "sanctioned"`,
    `in AFFIRMATIVE` -- whatever token was chosen, including one nobody
    listed. `is` is the single comparison it cannot fool, which is why
    `approval_is_explicit` is written with `is`.

The harness proves the difference rather than asserting it: one sentinel
mutation uses `"yes"` (in the table) and another uses a token that appears
NOWHERE in this file, and both must die.
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
    # Denials and undecided states. Truthy, so every one opened Gate 1 before
    # the fix -- this is the reported defect.
    ("str-no", "no", True),
    ("str-false", "false", True),
    ("str-pending", "pending", True),
    ("str-maybe", "maybe", True),
    # AFFIRMATIVE-LOOKING. A first revision of this table had none of these, and
    # the omission was the same mistake it was written to correct: it was heavy
    # on values that LOOK like refusals, because those are what the bug report
    # showed, and a check weakened to `value is True or value == "yes"` sailed
    # through all of them with the entire suite green.
    #
    # A denial is not the dangerous input. It is the SAFE one -- a reviewer
    # looking at `approved: "no"` knows something is wrong. The dangerous input
    # is the one that reads as consent to a human skimming the manifest and is
    # still a value nobody checked, because that is the one a sentinel gets
    # added for. Case and punctuation vary because a sentinel is usually
    # written to be forgiving.
    ("str-yes", "yes", True),
    ("str-YES", "YES", True),
    ("str-Y", "Y", True),
    ("str-y", "y", True),
    ("str-t", "t", True),
    ("str-on", "on", True),
    ("str-ok", "ok", True),
    ("str-true", "true", True),
    ("str-TRUE", "TRUE", True),
    ("str-True", "True", True),
    ("str-approved", "approved", True),
    ("str-one", "1", True),
    # Numbers and containers: what a loosely-typed client sends.
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


class _EqualsAnything:
    """Compares equal to every value there is, and is truthy.

    THIS is what closes the class; the token list above only closes instances.

    A list of affirmative strings kills `value is True or value == "yes"` and
    kills nothing else. Pick a token the list does not contain -- `"sanctioned"`,
    `"GRANTED"`, a UUID, whatever the next author reaches for -- and the
    weakened check passes every case again. Enumerating tokens is unwinnable:
    the space is every string, and the one that matters is by definition the one
    nobody listed.

    So instead of guessing the token, this refuses to be distinguished from it.
    ``__eq__`` returns True against anything, so ANY equality or membership
    sentinel -- ``== <token>``, ``in (<tokens>)``, ``in AFFIRMATIVE_SET`` --
    accepts this object whatever token was chosen, and the assertion that it
    must be rejected fails. ``is`` is the one comparison it cannot fool, which
    is exactly why ``approval_is_explicit`` uses ``is`` and why this object
    passes against the real implementation.

    ``__hash__`` is defined because ``__eq__`` alone would make it unhashable,
    and an unhashable value would raise TypeError inside a ``value in {...}``
    membership sentinel rather than being accepted by it -- the test would still
    fail, but for the wrong reason, and a reader chasing it would land on the
    container type instead of on the missing approval.
    """

    def __eq__(self, other) -> bool:
        return True

    def __ne__(self, other) -> bool:
        return False

    def __hash__(self) -> int:
        return hash(True)

    def __bool__(self) -> bool:
        return True

    def __repr__(self) -> str:
        return "<equal-to-everything>"


class _AffirmativeString(str):
    """A ``str`` that reads "yes" and is a genuine ``str`` subclass.

    Closes the sibling class: a check that decides by SHAPE rather than by
    value -- ``isinstance(value, str) and value.lower() not in ("no", "false")``,
    or any `str` fast-path added for a client that sends text. `_EqualsAnything`
    is not a str and would slip past that one; a plain `"yes"` in the table
    catches the common form, and this catches the form that also asks about the
    type. Rejected by the real implementation for the same single reason
    everything else is: it is not the ``True`` singleton.
    """

    def __new__(cls):
        return super().__new__(cls, "yes")


# Values that cannot round-trip through JSON, so they exercise the in-memory
# predicate and the loader but not the on-disk tests.
_ADVERSARIAL: list[tuple[str, object]] = [
    ("equals-anything", _EqualsAnything()),
    ("str-subclass-yes", _AffirmativeString()),
]

_ADV_IDS = [name for name, _ in _ADVERSARIAL]
_ADV_VALUES = [value for _, value in _ADVERSARIAL]

# Everything that must be refused, JSON-safe or not.
_ALL_REFUSED_IDS = _LOOSE_IDS + _ADV_IDS
_ALL_REFUSED_VALUES = _LOOSE_VALUES + _ADV_VALUES

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
    for name, value in _ADVERSARIAL:
        sb = Storyboard(title="T", storyboard_approved=True,
                        shots=[_paid("s001", approved=value)])
        assert _old_gate(sb) is True, name
    assert any(truthy for _, _, truthy in _LOOSE), (
        "no case in _LOOSE reproduces the defect; this file would be vacuous")


def test_the_affirmative_half_of_the_table_is_not_token_bound():
    """The token list must not be the only thing standing between the gate and a
    sentinel, and this is the assertion that says so out loud.

    `_EqualsAnything` is rejected for the same single reason `"yes"` is -- it is
    not the `True` singleton -- but it is rejected under EVERY choice of token,
    including the ones nobody listed. If this file ever loses that object and
    keeps only the strings, it is back to closing instances."""
    assert _EqualsAnything() == "yes"
    assert _EqualsAnything() == "sanctioned-by-vesper-2026"
    assert _EqualsAnything() in ("yes", "true", "on")
    assert bool(_EqualsAnything()) is True
    # ...and none of that is an approval.
    assert approval_is_explicit(_EqualsAnything()) is False


def test_the_paid_fixture_clears_the_gate_when_approval_is_a_real_boolean():
    """The floor under every 'gate is shut' assertion below: with `True`, this
    exact fixture opens Gate 1. Otherwise a shut gate proves nothing."""
    sb = Storyboard(title="T", storyboard_approved=True,
                    shots=[_paid("s001"), _paid("s002"), _free("s900")])
    assert sb.gate_cleared() is True


# --- approval_is_explicit ---------------------------------------------------------

@pytest.mark.parametrize("value", _ALL_REFUSED_VALUES, ids=_ALL_REFUSED_IDS)
def test_nothing_loose_counts_as_an_approval(value):
    assert approval_is_explicit(value) is False


def test_only_the_boolean_true_counts_as_an_approval():
    assert approval_is_explicit(True) is True
    assert approval_is_explicit(False) is False


# --- gate_cleared(): the last line of defence ------------------------------------

@pytest.mark.parametrize("value", _ALL_REFUSED_VALUES, ids=_ALL_REFUSED_IDS)
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


@pytest.mark.parametrize("value", _ALL_REFUSED_VALUES, ids=_ALL_REFUSED_IDS)
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

@pytest.mark.parametrize("value", _ALL_REFUSED_VALUES, ids=_ALL_REFUSED_IDS)
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


@pytest.mark.parametrize("value", _ALL_REFUSED_VALUES, ids=_ALL_REFUSED_IDS)
def test_a_loose_storyboard_approval_never_becomes_an_approved_project(value):
    sb = Storyboard.from_dict("p", {"title": "T", "storyboard_approved": value},
                              [_paid_dict("s001")])
    assert sb.gate_cleared() is False
    assert sb.storyboard_approved is False


@pytest.mark.parametrize("value", _ALL_REFUSED_VALUES, ids=_ALL_REFUSED_IDS)
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


# --- the two paid doors: same question, asked the same way ----------------------
#
# Neither of these is reachable today. Every writer stores a real boolean and
# from_dict normalises what it loads, so nothing can currently put a loose value
# in front of them -- these are LOW, and saying otherwise would overstate them.
#
# They are here because this PR's own argument applies to them more than to
# anything else it touches: a gate that infers its input is one edit from being
# bypassed, and these two are the functions money actually passes through.
# require_paid_gate exists at all because the check was attached to routes
# instead of to the spend and one route was missed; director's exists because
# coverage was a second door onto Tier C that walked past the first. Two doors
# onto the same money that disagree about what "approved" means is how the third
# gets missed too.

@pytest.mark.parametrize("value", _ALL_REFUSED_VALUES, ids=_ALL_REFUSED_IDS)
def test_the_paid_gate_refuses_an_approval_that_is_not_a_boolean(value):
    """`require_paid_gate` is the single helper every paid render route calls."""
    from fastapi import HTTPException
    from backend import main as MAIN

    sb = Storyboard(title="T", shots=[_paid("s001")])
    sb.storyboard_approved = value
    with pytest.raises(HTTPException) as exc:
        MAIN.require_paid_gate(sb, "render")
    assert exc.value.status_code == 400
    assert "Approve the storyboard first" in str(exc.value.detail)


def test_the_paid_gate_still_admits_a_real_approval():
    """No over-correction: the gate must open for a genuinely approved board, or
    every paid render is dead."""
    from backend import main as MAIN

    sb = Storyboard(title="T", storyboard_approved=True, shots=[_paid("s001")])
    assert MAIN.require_paid_gate(sb, "render") is None


@pytest.mark.parametrize("value", _ALL_REFUSED_VALUES, ids=_ALL_REFUSED_IDS)
def test_paid_coverage_refuses_an_approval_that_is_not_a_boolean(
        value, tmp_path, monkeypatch):
    """The director's coverage route, the second door onto the paid video tier."""
    from backend import director
    from backend.director import CoveragePlan, DirectorShot, PlanError
    from backend.manifest import Camera

    monkeypatch.setattr(director.config, "MANIFEST_PATH", tmp_path / "m.json")
    plan = CoveragePlan(
        beat_id="s003", beat_duration=27.0, status="locked",
        coverage=[DirectorShot(id="s003.01", beat_id="s003", prompt="a thing",
                               camera=Camera(duration=27.0),
                               motion_type="ai_video")])
    director.save_plan(plan)

    sb = Storyboard(id="p", title="T",
                    shots=[Shot(scene_id="s003", narration="x",
                                camera=Camera(duration=27.0))])
    sb.storyboard_approved = value

    with pytest.raises(PlanError, match="not approved"):
        director.compile_coverage(plan, sb, tmp_path / "render",
                                  log=lambda m: None)


@pytest.mark.parametrize("value", _ALL_REFUSED_VALUES, ids=_ALL_REFUSED_IDS)
def test_free_coverage_is_unaffected_by_the_strict_check(
        value, tmp_path, monkeypatch):
    """Free tiers stay open before the gate — parallax costs nothing, and drafts
    are explicitly a pre-gate activity. Tightening the approval test must not
    quietly close a door that was deliberately left open."""
    from backend import director
    from backend.director import CoveragePlan, DirectorShot
    from backend.manifest import Camera

    monkeypatch.setattr(director.config, "MANIFEST_PATH", tmp_path / "m.json")
    plan = CoveragePlan(
        beat_id="s003", beat_duration=27.0, status="locked",
        coverage=[DirectorShot(id="s003.01", beat_id="s003", prompt="a thing",
                               camera=Camera(duration=27.0),
                               motion_type="parallax")])
    director.save_plan(plan)

    sb = Storyboard(id="p", title="T",
                    shots=[Shot(scene_id="s003", narration="x",
                                camera=Camera(duration=27.0))])
    sb.storyboard_approved = value

    # Fails later for want of media, but must NOT fail on the approval check.
    with pytest.raises(Exception) as exc:
        director.compile_coverage(plan, sb, tmp_path / "render",
                                  log=lambda m: None)
    assert "not approved" not in str(exc.value)


@pytest.mark.parametrize("value", _ALL_REFUSED_VALUES, ids=_ALL_REFUSED_IDS)
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

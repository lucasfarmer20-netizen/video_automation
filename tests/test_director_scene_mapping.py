"""The studio's read model accounts for every field the server sends it.

`GET /api/director/scene` serialises `director.CoveragePlan` with `asdict`, and
`frontend/src/lib/directorApi.ts::fetchCoveragePlan` rebuilds it field by field
into the studio's flat plan shape. The envelope makes that normalisation
legitimate; the hand-written copy makes it lossy. It already lost one:
`warning_dispositions` — the durable record of which critic findings a human has
decided — was written by the backend, returned by this route, and never copied,
so every refetch re-raised findings that had been resolved. The write had been
made durable precisely to stop that happening, and the read threw it away.

The mapper now declares `PLAN_FIELDS_CARRIED` and `PLAN_FIELDS_DROPPED`, the same
discipline as `_MATERIAL_SHOT_FIELDS` / `_NON_MATERIAL_SHOT_FIELDS` in
`backend/director.py`: dropping a field is a decision on the record rather than
an omission. This is the guard that can see BOTH sides — the TypeScript cannot
know what the dataclass holds, and a frontend fixture of the server's keys is
just the same copy one layer further out. Adding a field to `CoveragePlan` fails
here until someone says what the studio does with it.

The mapper carrying a field is checked on the other side, in
`frontend/src/lib/directorApi.dispositions.test.ts`; that a human sees the result
is checked in `frontend/src/components/DirectorWorkspace.dispositions.test.tsx`.
"""

from __future__ import annotations

import dataclasses
import re
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

from backend import director  # noqa: E402

MAPPER = (
    Path(__file__).resolve().parent.parent
    / "frontend" / "src" / "lib" / "directorApi.ts"
)


def _declared(record: str) -> dict[str, str]:
    """The keys of one `export const <record> = {...}` object, in source order.

    Deliberately a source read and not an import: the fact under test is what
    the TypeScript says, and a Node round trip would only add a way for this to
    be skipped on a machine without one.
    """
    src = MAPPER.read_text(encoding="utf-8")
    start = src.index(f"export const {record}")
    body = src[src.index("{", start) + 1:]
    end = body.index("\n};")
    # Keys sit at one indent; the reason strings continue at two, so they cannot
    # be mistaken for keys.
    return dict.fromkeys(re.findall(r"^  (\w+):", body[:end], re.M), "")


def test_the_mapper_accounts_for_every_field_of_a_coverage_plan():
    served = {f.name for f in dataclasses.fields(director.CoveragePlan)}
    carried = set(_declared("PLAN_FIELDS_CARRIED"))
    dropped = set(_declared("PLAN_FIELDS_DROPPED"))

    unclassified = served - carried - dropped
    assert not unclassified, (
        f"GET /api/director/scene sends {sorted(unclassified)}, and "
        f"fetchCoveragePlan neither carries nor declares them dropped. A "
        f"hand-written mapper loses a field silently — that is how "
        f"warning_dispositions was lost. Carry it, or add it to "
        f"PLAN_FIELDS_DROPPED with the reason it is not needed."
    )


def test_the_mapper_declares_no_field_the_server_does_not_send():
    served = {f.name for f in dataclasses.fields(director.CoveragePlan)}
    declared = set(_declared("PLAN_FIELDS_CARRIED")) | set(_declared("PLAN_FIELDS_DROPPED"))

    phantom = declared - served
    assert not phantom, (
        f"fetchCoveragePlan declares {sorted(phantom)}, which CoveragePlan no "
        f"longer has. A stale declaration makes the accounting above pass while "
        f"describing a server that is gone."
    )


def test_the_decisions_a_human_records_are_carried_not_dropped():
    """The specific field, pinned by name.

    The two tests above are satisfied by *classifying* a field, and "dropped
    with a reason" is a legitimate classification for most of them. It is not a
    legitimate one for this: a screen that cannot see which findings were decided
    re-raises every one of them on every read, and asks a human to review their
    own completed review.
    """
    carried = _declared("PLAN_FIELDS_CARRIED")

    assert "warning_dispositions" in carried, (
        "warning_dispositions must reach the studio. Without it the Director "
        "shows resolved critic findings as outstanding after every refetch, and "
        "a scene switch is a refetch."
    )
    # And it is what the backend actually calls it, so the classification is
    # about a real field rather than a name that once matched.
    assert any(
        f.name == "warning_dispositions"
        for f in dataclasses.fields(director.CoveragePlan)
    )


def test_what_a_compile_produced_is_carried_not_dropped():
    """The second field the same mapper lost, and the second symptom.

    `compiled` is what `director.compile` writes when a beat clip exists:
    {beat_clip, runtime, shots, sub_clips}. `status` says a compile happened;
    only this says what came out of it. Dropped, the Director's account of a
    finished compile lived entirely in one-shot React state and vanished with
    the visit — while the assembly view, which reads the project directly,
    carried on showing it. Classifying it as "dropped with a reason" was true
    while nothing rendered it, and stopped being true the moment a human noticed
    the screen had forgotten.
    """
    carried = _declared("PLAN_FIELDS_CARRIED")

    assert "compiled" in carried, (
        "compiled must reach the studio. Without it the Director cannot say what "
        "a compile produced once the job's own message is gone, and a scene "
        "switch is enough to lose it."
    )
    assert any(
        f.name == "compiled" for f in dataclasses.fields(director.CoveragePlan)
    )
